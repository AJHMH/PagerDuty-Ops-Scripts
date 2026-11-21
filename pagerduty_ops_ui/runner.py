"""Run one command as its own process (ADR 0004).

The child is the command's own entry point run through pagerduty_ops.cli.run, exactly as
its console script would, so exit codes and stdout/stderr are the engine's. Secrets reach
it only through the environment and are redacted from everything captured.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .catalog import CommandEntry

OUTCOMES = {0: "succeeded", 1: "failed", 2: "usage-error", 3: "auth-error", 130: "stopped"}

# Only these variables of the service's own environment reach a Run.
PASSTHROUGH_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                   "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL")

LAUNCHER = ("import sys\n"
            "from importlib import import_module\n"
            "from pagerduty_ops.cli import run\n"
            "sys.argv[0] = {name!r}\n"
            "run(getattr(import_module({module!r}), {attr!r}))\n")

REDACTED = "[redacted]"


@dataclass
class Result:
    exit_code: int | None
    outcome: str
    stdout: str
    stderr: str
    stop_reason: str | None = None  # why a "stopped" Run was killed


def outcome_for(exit_code: int) -> str:
    return OUTCOMES.get(exit_code, "failed")


def child_env(extra_env: dict[str, str], secret_env: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    env.update(extra_env)
    env.update(secret_env)
    return env


def as_log(raw: bytes, secrets) -> str:
    """stderr is display text: decode, normalise CRLF (Windows) to LF, and redact."""
    return redact(raw.decode("utf-8", "replace").replace("\r\n", "\n"), secrets)


def as_data(raw: bytes, secrets) -> str:
    """stdout is data: decode and redact, but keep it byte-exact otherwise."""
    return redact(raw.decode("utf-8", "replace"), secrets)


def redact(text: str, secrets) -> str:
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, REDACTED)
    return text


POLL_SECONDS = 0.2
DRAIN_SECONDS = 10  # after a kill, how long to wait for the last of the output


def run_command(entry: CommandEntry, argv: list[str], *, cwd: Path,
                extra_env: dict[str, str], secret_env: dict[str, str],
                timeout: float | None = None,
                cancel: threading.Event | None = None) -> Result:
    """Run to completion, or kill the whole process tree at `timeout` seconds or when
    `cancel` is set; either way the outcome is "stopped" (ADR 0012)."""
    code = LAUNCHER.format(name=entry.name, module=entry.module, attr=entry.attr)
    secrets = secret_env.values()
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter, argv list, no shell
        [sys.executable, "-c", code, *argv],
        cwd=cwd, env=child_env(extra_env, secret_env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NEW_PROCESS_GROUP,
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    stopped = None
    while True:
        try:
            stdout, stderr = process.communicate(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                stopped = "cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                stopped = f"no result after {timeout:g} seconds"
            if stopped:
                kill_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=DRAIN_SECONDS)
                except subprocess.TimeoutExpired:  # something still holds the pipes open
                    stdout, stderr = b"", b""
                break
        except BaseException:  # e.g. the service is shutting down: leave nothing behind
            kill_tree(process)
            raise
    kill_tree(process)  # a finished command must not leave background processes behind
    if stopped:
        return Result(exit_code=None, outcome="stopped", stdout=as_data(stdout, secrets),
                      stderr=as_log(stderr, secrets) + f"\n[stopped: {stopped}]\n",
                      stop_reason=stopped)
    return Result(
        exit_code=process.returncode,
        outcome=outcome_for(process.returncode),
        stdout=as_data(stdout, secrets),
        stderr=as_log(stderr, secrets),
    )


if os.name == "nt":
    NEW_PROCESS_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def kill_tree(process: subprocess.Popen) -> None:
        """Kill the Run's process and every process it started (taskkill /T)."""
        if process.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S603,S607
                           capture_output=True, check=False)
            process.kill()
else:
    NEW_PROCESS_GROUP = {"start_new_session": True}  # the Run leads its own process group

    def kill_tree(process: subprocess.Popen) -> None:
        """Kill the Run's process group: the command and every process it started."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
