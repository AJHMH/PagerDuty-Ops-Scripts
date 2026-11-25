"""What the UI refuses before a Run starts. Risk comes only from the engine (ADR 0010).

Refused with 422 (no Run is created):
- arguments the command's own parser rejects, and --help (not a Run);
- secrets or prompts on the command line (-t/--token, --prompt) and any argument
  containing a configured secret value (it would be stored with the Run);
- options the service manages itself (--log-file, --receipts-dir);
- paths outside the Run's own directory (absolute, drive-qualified, UNC or with "..").
Options and values are judged as the command's own parser reads them, so argparse's prefixes
(--tok), attached values (-tX, -oPATH) and short-option clusters (-qoPATH) can't slip past.
Commands that write run only as dry runs (#72): the gate adds --dry-run where that is the
command's switch, refuses an explicit --apply, and re-checks with the engine that the final
argv really is a dry run. Only Operators and Approvers may dry-run a command that writes
(403). A new Run never applies: an apply is only ever of a reviewed dry run (`gate_apply`, ADR
0003), and whether the service allows it at all is its phase (`phase.py`, ADR 0016).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import ntpath
import posixpath
import re
from pathlib import Path

from fastapi import HTTPException

from pagerduty_ops.risk import APPLY_FLAG, DRY_RUN_FLAG, Assessment, assess

from .catalog import CommandEntry
from .roles import Role

# Options whose use is refused, whatever spelling argparse accepted for them (prefixes such as
# --tok, attached values such as -tVALUE, clusters such as -qtVALUE).
FORBIDDEN_OPTIONS = frozenset({"-t", "--token", "--prompt"})
# Options the service manages itself, and why each is refused:
# - every Run's log is already captured, and a timestamped log file in the Run directory would
#   be an output whose hash never matches a fresh dry run;
# - rehearsal receipts live in the service's one shared directory (ADR 0007). A receipts
#   directory inside the Run's own directory would let an uploaded file pose as a receipt.
SERVICE_MANAGED_OPTIONS = {
    "--log-file": "the service keeps every Run's log itself",
    "--receipts-dir": "rehearsal receipts live in the service's shared receipts directory",
}
DRIVE = re.compile(r"^[A-Za-z]:")


def _refuse(message: str):
    raise HTTPException(422, message)


def _escapes_run_dir(value: str) -> bool:
    if posixpath.isabs(value) or ntpath.isabs(value) or DRIVE.match(value):
        return True
    return ".." in re.split(r"[\\/]", value)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _actions(parser: argparse.ArgumentParser, namespace: argparse.Namespace):
    """The parser's actions plus those of the subcommand that was chosen."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            chosen = getattr(namespace, action.dest, None)
            if chosen in action.choices:
                yield from _actions(action.choices[chosen], namespace)
        else:
            yield action


def _parse(entry: CommandEntry, argv: list[str]):
    """Parse argv with the command's own parser, exactly as the Run will."""
    module = importlib.import_module(entry.module)
    parser = getattr(module, entry.builder)()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return module, parser, parser.parse_args(argv)
    except SystemExit:
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        _refuse(lines[-1] if lines else "The command rejected these arguments.")
    raise AssertionError("unreachable")


def check_arguments(entry: CommandEntry, argv: list[str], secrets):
    """Judge the arguments as the command's parser understands them, not as text: argparse
    accepts prefixes, attached values and clustered short options."""
    if "-h" in argv or "--help" in argv:
        _refuse("--help is not a Run.")
    for arg in argv:
        if any(s and len(s) >= 4 and s in arg for s in secrets):
            _refuse("An argument contains a secret value; secrets are never arguments.")
    module, parser, namespace = _parse(entry, argv)
    for action in _actions(parser, namespace):
        used = getattr(namespace, action.dest, action.default) != action.default
        if used and FORBIDDEN_OPTIONS & set(action.option_strings):
            _refuse(f"{'/'.join(action.option_strings)} is not allowed: Runs use the "
                    "service's credentials.")
        managed = SERVICE_MANAGED_OPTIONS.keys() & set(action.option_strings)
        if used and managed:
            _refuse(f"{'/'.join(action.option_strings)} is not allowed: "
                    f"{SERVICE_MANAGED_OPTIONS[min(managed)]}.")
        for value in _strings(getattr(namespace, action.dest, None)):
            if _escapes_run_dir(value):
                _refuse(f"{value!r} points outside the Run's own directory.")
    return module


def assess_run(module, entry: CommandEntry, argv: list[str], cwd: Path) -> Assessment:
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return assess(module, argv, entry.builder, cwd=str(cwd))


def gate(entry: CommandEntry, argv: list[str], cwd: Path, secrets,
         role: Role) -> tuple[list[str], Assessment]:
    """The argv the Run will use and the engine's verdict on it, or an HTTP refusal."""
    module = check_arguments(entry, argv, secrets)
    verdict = assess_run(module, entry, argv, cwd)
    if not verdict.writes:
        return argv, verdict
    if role < Role.OPERATOR:
        raise HTTPException(403, f"{entry.name} writes to PagerDuty or ServiceNow; only "
                                 "Operators and Approvers can dry-run it.")
    if not verdict.dry_run and verdict.write_switch == DRY_RUN_FLAG:
        argv = [*argv, DRY_RUN_FLAG]
        check_arguments(entry, argv, secrets)
        verdict = assess_run(module, entry, argv, cwd)
    if not verdict.dry_run:
        raise HTTPException(403, f"These arguments would apply {entry.name}'s changes. A Run "
                                 "of a command that writes starts as a dry run; apply it from "
                                 "its Run page once you've reviewed it.")
    return argv, verdict


def gate_apply(entry: CommandEntry, neutral_argv: list[str], cwd: Path,
               secrets) -> tuple[list[str], list[str], Assessment]:
    """For applying a reviewed dry run: the argv of the fresh dry run, the argv of the apply,
    and the engine's verdict on the apply. -y is added here, after every check (ADR 0003).
    Whether the apply is irreversible, and so needs an Approval (#77), is `verdict.irreversible`.
    """
    module = check_arguments(entry, neutral_argv, secrets)
    verdict = assess_run(module, entry, neutral_argv, cwd)
    if not verdict.writes:
        raise HTTPException(409, f"{entry.name} with these arguments doesn't write.")
    if verdict.write_switch == APPLY_FLAG:
        dry_argv, apply_argv = neutral_argv, [*neutral_argv, APPLY_FLAG, "-y"]
    else:
        dry_argv, apply_argv = [*neutral_argv, DRY_RUN_FLAG], [*neutral_argv, "-y"]
    check_arguments(entry, apply_argv, secrets)
    verdict = assess_run(module, entry, apply_argv, cwd)
    if not verdict.writes or verdict.dry_run:
        raise HTTPException(409, "The engine doesn't recognise this as an apply.")
    return dry_argv, apply_argv, verdict
