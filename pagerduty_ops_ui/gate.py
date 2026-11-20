"""What the UI refuses before a Run starts. Risk comes only from the engine (ADR 0010).

Refused with 422 (no Run is created):
- arguments the command's own parser rejects, and --help (not a Run);
- secrets or prompts on the command line (-t/--token, --prompt) and any argument
  containing a configured secret value (it would be stored with the Run);
- paths outside the Run's own directory (absolute, drive-qualified, UNC or with "..").
Refused with 403: commands that write — Apply arrives in Phase 2 (ADR 0015).
"""

from __future__ import annotations

import contextlib
import importlib
import io
import ntpath
import posixpath
import re
from pathlib import Path

from fastapi import HTTPException

from pagerduty_ops.risk import Assessment, assess

from .catalog import CommandEntry

FORBIDDEN_FLAGS = ("--token", "--prompt")
DRIVE = re.compile(r"^[A-Za-z]:")


def _refuse(message: str):
    raise HTTPException(422, message)


def _escapes_run_dir(value: str) -> bool:
    if posixpath.isabs(value) or ntpath.isabs(value) or DRIVE.match(value):
        return True
    return ".." in re.split(r"[\\/]", value)


def _forbidden(flag: str) -> bool:
    """--token/--prompt, any unique prefix argparse would expand to them, and -t[value]."""
    if flag.startswith("--"):
        return len(flag) >= 3 and any(f.startswith(flag) for f in FORBIDDEN_FLAGS)
    return flag.startswith("-t")


def _values(arg: str) -> list[str]:
    """The parts of one argument that may be a path: the argument itself, the value of
    --long=value, and the value attached to a short option (-oFILE)."""
    if arg.startswith("--"):
        return [arg.partition("=")[2]]
    if arg.startswith("-") and len(arg) > 2:
        return [arg[2:]]
    return [arg]


def check_argv(argv: list[str], secrets) -> None:
    for arg in argv:
        flag = arg.partition("=")[0] if arg.startswith("--") else arg[:2]
        if arg.startswith("-") and _forbidden(flag):
            _refuse(f"{flag} is not allowed: Runs use the service's credentials.")
        if any(s and len(s) >= 4 and s in arg for s in secrets):
            _refuse("An argument contains a secret value; secrets are never arguments.")
        for candidate in _values(arg):
            if candidate and _escapes_run_dir(candidate):
                _refuse(f"{candidate!r} points outside the Run's own directory.")


def assess_run(entry: CommandEntry, argv: list[str], cwd: Path) -> Assessment:
    module = importlib.import_module(entry.module)
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return assess(module, argv, entry.builder, cwd=str(cwd))
    except ValueError as e:
        _refuse(str(e))
    except SystemExit:
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        _refuse(lines[-1] if lines else "The command rejected these arguments.")
    raise AssertionError("unreachable")


def gate(entry: CommandEntry, argv: list[str], cwd: Path, secrets) -> Assessment:
    check_argv(argv, secrets)
    verdict = assess_run(entry, argv, cwd)
    if verdict.writes:
        raise HTTPException(403, f"{entry.name} writes to PagerDuty or ServiceNow with these "
                            "arguments; the UI runs only read-only commands until Apply "
                            "arrives in Phase 2.")
    return verdict
