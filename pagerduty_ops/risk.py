"""Risk declarations: what each command can change, and when that change is irreversible.

Every command module declares its risk next to its parser (ADR 0010), so a front end such as
the team web UI can gate roles, dry runs and Approvals without keeping a risk table of its
own. A declaration is either one ``Risk`` (a command without subcommands) or a mapping of
subcommand name to ``Risk``. The attribute name follows the parser builder:

    build_parser         -> RISK
    build_export_parser  -> EXPORT_RISK
    build_apply_parser   -> APPLY_RISK

``assess()`` turns a declaration plus a concrete argv into a verdict for one Run.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from types import ModuleType

BUILDER_ATTRS = {
    "build_parser": "RISK",
    "build_export_parser": "EXPORT_RISK",
    "build_apply_parser": "APPLY_RISK",
}

DRY_RUN_FLAG = "--dry-run"  # writes unless --dry-run is given
APPLY_FLAG = "--apply"      # previews (diff) unless --apply is given


@dataclass(frozen=True)
class PlanItemMatch:
    """Plan items with this ``op`` (and these exact field values) make an apply irreversible.

    Matching the items themselves, not the Plan's recorded options, is what the apply will
    actually do: an edited ``options`` block cannot hide a deletion.
    """

    op: str
    fields: tuple[tuple[str, object], ...] = ()
    describe: str = ""

    def matches(self, item: dict) -> bool:
        return item.get("op") == self.op and all(item.get(k) == v for k, v in self.fields)

    def reason(self, count: int) -> str:
        suffix = f" {self.describe}" if self.describe else ""
        return f"plan has {count} {self.op} item(s){suffix}"


@dataclass(frozen=True)
class Risk:
    """What one command (or subcommand) can do.

    writes: it can change PagerDuty or ServiceNow.
    write_switch: how a dry run is chosen — ``DRY_RUN_FLAG`` or ``APPLY_FLAG``.
    irreversible: every write is an Irreversible operation (e.g. Event Rules conversion).
    irreversible_flags: flags that make a write irreversible (e.g. ``--prune``).
    irreversible_plan_items: Plan items whose presence makes the apply irreversible. Judged
        from the items, never the Plan's recorded options, which are not hashed.
    irreversible_on_sn_production: an apply to a ServiceNow production instance is
        irreversible.
    cap_flags: write caps that must not be raised without a recorded override.
    timeout_minutes: Run timeout when the 2-hour default is not enough.
    """

    writes: bool = False
    write_switch: str | None = None
    irreversible: bool = False
    irreversible_flags: tuple[str, ...] = ()
    irreversible_plan_items: tuple[PlanItemMatch, ...] = ()
    irreversible_on_sn_production: bool = False
    cap_flags: tuple[str, ...] = ()
    timeout_minutes: int | None = None

    def __post_init__(self):
        if self.writes and self.write_switch not in (DRY_RUN_FLAG, APPLY_FLAG):
            raise ValueError("a writing command must declare its write_switch")
        if not self.writes and (self.write_switch or self.irreversible
                                or self.irreversible_flags or self.irreversible_plan_items
                                or self.irreversible_on_sn_production or self.cap_flags):
            raise ValueError("a read-only command cannot declare write settings")


READ_ONLY = Risk()


CLEARS_SN_FIELD = PlanItemMatch("sn_set_field", (("value", ""),), "clearing a value")
DELETES_SCIM_USER = PlanItemMatch("delete_user")


def writes_with_dry_run(**kw) -> Risk:
    return Risk(writes=True, write_switch=DRY_RUN_FLAG, **kw)


def writes_with_apply(**kw) -> Risk:
    return Risk(writes=True, write_switch=APPLY_FLAG, **kw)


def declarations(module: ModuleType) -> dict[str, Risk | dict[str, Risk]]:
    """Map each parser builder the module has to its declaration.

    Raises ``LookupError`` naming the first builder without one.
    """
    found = {}
    for builder, attr in BUILDER_ATTRS.items():
        if not hasattr(module, builder):
            continue
        if not hasattr(module, attr):
            raise LookupError(f"{module.__name__} has {builder}() but no {attr} declaration")
        found[builder] = getattr(module, attr)
    return found


@dataclass(frozen=True)
class Assessment:
    """The verdict for one Run of a command with a concrete argv."""

    writes: bool              # the command/subcommand can write
    dry_run: bool             # this argv previews only
    irreversible: bool        # applying this argv is an Irreversible operation
    reasons: tuple[str, ...]  # why it is irreversible (empty otherwise)
    cap_flags: tuple[str, ...]
    timeout_minutes: int | None


def _subcommand(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return getattr(args, action.dest)
    return None


def _flag_set(parser: argparse.ArgumentParser, args: argparse.Namespace, flag: str) -> bool:
    for action in _all_actions(parser, args):
        if flag in action.option_strings:
            value = getattr(args, action.dest, None)
            return bool(value) and value != action.default
    return False


def _all_actions(parser, args):
    yield from parser._actions
    sub = _subcommand(parser, args)
    if sub is not None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                yield from action.choices[sub]._actions


def resolve(declaration: Risk | dict[str, Risk], subcommand: str | None) -> Risk:
    if isinstance(declaration, Risk):
        return declaration
    if subcommand not in declaration:
        raise LookupError(f"no risk declared for subcommand {subcommand!r}")
    return declaration[subcommand]


def assess(module: ModuleType, argv: list[str], builder: str = "build_parser", *,
           cwd: str | None = None) -> Assessment:
    """Assess one Run: does it write, is it a dry run, is it irreversible, and why.

    ``cwd`` is the directory the Run will execute in; relative file arguments resolve
    against it. Invalid arguments raise ``SystemExit(2)`` (argparse); ``-h/--help`` raises
    ``ValueError`` because it is not a Run.

    Fails closed: when the Plan file or the ServiceNow instance configuration cannot be
    read, the Run is treated as irreversible.
    """
    if "-h" in argv or "--help" in argv:
        raise ValueError("--help is not a Run")
    parser = getattr(module, builder)()
    args = parser.parse_args(argv)
    risk = resolve(declarations(module)[builder], _subcommand(parser, args))
    if not risk.writes:
        return Assessment(False, False, False, (), (), risk.timeout_minutes)

    if risk.write_switch == DRY_RUN_FLAG:
        dry_run = _flag_set(parser, args, DRY_RUN_FLAG)
    else:
        dry_run = not _flag_set(parser, args, APPLY_FLAG)

    reasons = []
    if risk.irreversible:
        reasons.append("always irreversible")
    reasons += [f"{flag} given" for flag in risk.irreversible_flags
                if _flag_set(parser, args, flag)]
    if risk.irreversible_plan_items:
        reasons += _plan_reasons(_resolve_path(getattr(args, "plan_file", None), cwd), risk)
    if risk.irreversible_on_sn_production:
        reasons += _sn_production_reasons(getattr(args, "instance", None))
    return Assessment(True, dry_run, bool(reasons), tuple(reasons), risk.cap_flags,
                      risk.timeout_minutes)


def _resolve_path(path: str | None, cwd: str | None) -> str | None:
    if path is None or cwd is None or os.path.isabs(path):
        return path
    return os.path.join(cwd, path)


def _plan_reasons(plan_file: str | None, risk: Risk) -> list[str]:
    try:
        with open(plan_file, encoding="utf-8") as fh:
            plan = json.load(fh)
        items = [i for i in plan.get("items") or [] if isinstance(i, dict)]
    except (OSError, TypeError, ValueError, AttributeError):
        return ["plan file unreadable (fail closed)"]
    reasons = []
    for match in risk.irreversible_plan_items:
        count = sum(1 for item in items if match.matches(item))
        if count:
            reasons.append(match.reason(count))
    return reasons


def _sn_production_reasons(instance: str | None) -> list[str]:
    from .config import ConfigError
    from .servicenow import get_instance_config

    try:
        cfg = get_instance_config(instance, for_write=True)
    except ConfigError:
        return ["ServiceNow instance not resolvable as non-production (fail closed)"]
    return [f"ServiceNow production instance {cfg.name}"] if cfg.is_prod else []
