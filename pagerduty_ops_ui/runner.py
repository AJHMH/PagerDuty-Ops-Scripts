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
from collections.abc import Callable
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

LAUNCHER_MARK = b"from pagerduty_ops.cli import run"  # how a Run's process is recognised

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


def as_data(raw: bytes, secrets) -> str:
    """stdout is data: decode and redact, but keep it byte-exact otherwise."""
    return redact(raw.decode("utf-8", "replace"), secrets)


def redact(text: str, secrets) -> str:
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, REDACTED)
    return text


POLL_SECONDS = 0.2
LOG_FLUSH_SECONDS = 0.5  # how often a running Run's log is handed on (#68)
DRAIN_SECONDS = 10  # after a kill, how long to wait for the last of the output


class LiveLog:
    """The Run's log as it arrives: complete lines, each normalised (CRLF to LF) and redacted
    on its own, so a secret can't slip out split across two writes. `on_log` gets the whole
    log so far, at most every LOG_FLUSH_SECONDS (#68)."""

    def __init__(self, secrets, on_log: Callable[[str], None] | None):
        self.secrets = list(secrets)
        self.on_log = on_log
        self.lines: list[str] = []
        self.sent = 0
        self.last_sent = 0.0
        self.lock = threading.Lock()

    def add(self, raw_line: bytes) -> None:
        line = redact(raw_line.decode("utf-8", "replace").replace("\r\n", "\n"), self.secrets)
        with self.lock:
            self.lines.append(line)

    def text(self) -> str:
        with self.lock:
            return "".join(self.lines)

    def flush(self, force: bool = False) -> None:
        """Hand the log so far to `on_log` if there is something new and it is time."""
        if self.on_log is None:
            return
        with self.lock:
            count = len(self.lines)
            due = force or time.monotonic() - self.last_sent >= LOG_FLUSH_SECONDS
            if count == self.sent or not due:
                return
            text = "".join(self.lines)
            self.sent, self.last_sent = count, time.monotonic()
        self.on_log(text)


def run_command(entry: CommandEntry, argv: list[str], *, cwd: Path,
                extra_env: dict[str, str], secret_env: dict[str, str],
                timeout: float | None = None,
                cancel: threading.Event | None = None,
                on_start: Callable[[int], None] | None = None,
                on_log: Callable[[str], None] | None = None) -> Result:
    """Run to completion, or kill the whole process tree at `timeout` seconds or when
    `cancel` is set; either way the outcome is "stopped" (ADR 0012). `on_start` gets the
    process ID once the process exists; `on_log` gets the redacted log so far while the
    command runs (#68)."""
    if cancel is not None and cancel.is_set():  # never start a process that is cancelled
        return Result(exit_code=None, outcome="stopped", stdout="",
                      stderr="[stopped: cancelled]\n", stop_reason="cancelled")
    code = LAUNCHER.format(name=entry.name, module=entry.module, attr=entry.attr)
    secrets = secret_env.values()
    log = LiveLog(secrets, on_log)
    stdout_chunks: list[bytes] = []
    tree = ProcessTree(
        [sys.executable, "-c", code, *argv],
        cwd=cwd, env=child_env(extra_env, secret_env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    process = tree.process
    readers = [
        threading.Thread(target=lambda: stdout_chunks.append(process.stdout.read()),
                         daemon=True),
        threading.Thread(target=lambda: [log.add(line) for line in
                                         iter(process.stderr.readline, b"")], daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    stopped = None
    try:
        if on_start is not None:
            on_start(process.pid)
        while True:
            try:
                process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    stopped = "cancelled"
                elif deadline is not None and time.monotonic() >= deadline:
                    stopped = f"no result after {timeout:g} seconds"
                if stopped:
                    break
                log.flush()
    finally:
        # Also on success or an error here (e.g. the service shutting down): a Run must
        # not leave processes behind, and its pipes then reach their end.
        tree.kill()
        for reader in readers:
            reader.join(timeout=DRAIN_SECONDS)
        tree.close()
        process.stdout.close()
        process.stderr.close()
    if stopped:
        process.wait(timeout=DRAIN_SECONDS)
    stdout = as_data(b"".join(stdout_chunks), secrets)
    if stopped:
        return Result(exit_code=None, outcome="stopped", stdout=stdout,
                      stderr=log.text() + f"\n[stopped: {stopped}]\n", stop_reason=stopped)
    return Result(exit_code=process.returncode, outcome=outcome_for(process.returncode),
                  stdout=stdout, stderr=log.text())


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _PROCESS_SET_QUOTA_AND_TERMINATE = 0x0100 | 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                  wintypes.LPVOID, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _check(ok) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    class ProcessTree:
        """The Run's process in a Windows Job Object: every process it starts joins the
        job, even after its parent exits, and the whole job is killed together."""

        def __init__(self, args, **popen_kwargs):
            self.job = _kernel32.CreateJobObjectW(None, None)
            _check(self.job)
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(_kernel32.SetInformationJobObject(
                self.job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits), ctypes.sizeof(limits)))
            self.process = subprocess.Popen(args, **popen_kwargs)  # noqa: S603
            # The interpreter takes far longer to start than this takes to run, so the
            # command can't start a process before it is in the job.
            handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA_AND_TERMINATE, False,
                                           self.process.pid)
            try:
                _check(handle)
                _check(_kernel32.AssignProcessToJobObject(self.job, handle))
            except OSError:
                self.process.kill()
                self.close()
                raise
            finally:
                if handle:
                    _kernel32.CloseHandle(handle)

        def kill(self) -> None:
            if self.job:
                _kernel32.TerminateJobObject(self.job, 1)

        def close(self) -> None:
            if self.job:
                _kernel32.CloseHandle(self.job)  # kill-on-close: nothing outlives it
                self.job = None

    def kill_leftover(pid: int) -> None:
        """Nothing to do: when the service process dies, Windows closes its handle to each
        Run's Job Object, which kills the job (kill-on-close)."""
else:
    class ProcessTree:
        """The Run's process leads its own process group: the command and every process
        it starts, which are killed together."""

        def __init__(self, args, **popen_kwargs):
            self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter, argv list
                args, start_new_session=True, **popen_kwargs)

        def kill(self) -> None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        def close(self) -> None:
            pass

    def kill_leftover(pid: int) -> None:
        """After a crash, kill the process group a Run left behind, if `pid` is still one
        of this service's Runs. A process ID can be reused, so the process must be running
        the Run launcher (read from /proc; where there is no /proc, nothing is killed)."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            return
        if LAUNCHER_MARK not in cmdline or os.getpgid(pid) != pid:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
