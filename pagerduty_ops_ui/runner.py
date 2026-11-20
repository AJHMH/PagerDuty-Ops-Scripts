"""Run one command as its own process (ADR 0004).

The child is the command's own entry point run through pagerduty_ops.cli.run, exactly as
its console script would, so exit codes and stdout/stderr are the engine's. Secrets reach
it only through the environment and are redacted from everything captured.
"""

from __future__ import annotations

import os
import subprocess
import sys
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


def outcome_for(exit_code: int) -> str:
    return OUTCOMES.get(exit_code, "failed")


def child_env(extra_env: dict[str, str], secret_env: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    env.update(extra_env)
    env.update(secret_env)
    return env


def redact(text: str, secrets) -> str:
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, REDACTED)
    return text


def run_command(entry: CommandEntry, argv: list[str], *, cwd: Path,
                extra_env: dict[str, str], secret_env: dict[str, str],
                timeout: float | None = None) -> Result:
    """Run to completion, or kill the process at `timeout` seconds (outcome "stopped")."""
    code = LAUNCHER.format(name=entry.name, module=entry.module, attr=entry.attr)
    secrets = secret_env.values()
    try:
        done = subprocess.run(  # noqa: S603 - fixed interpreter, argv list, no shell
            [sys.executable, "-c", code, *argv],
            cwd=cwd, env=child_env(extra_env, secret_env), stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return Result(
            exit_code=None, outcome="stopped",
            stdout=redact((e.stdout or b"").decode("utf-8", "replace"), secrets),
            stderr=redact((e.stderr or b"").decode("utf-8", "replace"), secrets)
            + f"\n[stopped: no result after {timeout:g} seconds]\n",
        )
    return Result(
        exit_code=done.returncode,
        outcome=outcome_for(done.returncode),
        stdout=redact(done.stdout.decode("utf-8", "replace"), secrets),
        stderr=redact(done.stderr.decode("utf-8", "replace"), secrets),
    )
