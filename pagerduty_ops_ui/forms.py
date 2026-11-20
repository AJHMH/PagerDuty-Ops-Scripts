"""Forms generated from each command's own argparse parser (ADR 0004).

A form is data the front end renders: one field per argument the UI offers, with its kind,
flags, whether it is required, its choices, default and help, whether it repeats, and which
mutually exclusive group it belongs to. Commands with subcommands get one section each,
marked with whether that subcommand writes (from the engine's risk declaration, ADR 0010).

argparse has no notion of "this argument is a file", so FILE_ROLES names the file
arguments; tests fail if a path-like argument is missing from it or if any argument has a
shape this module can't render, so a new command can't quietly get a broken form.
"""

from __future__ import annotations

import argparse
import importlib

from pagerduty_ops.risk import Risk, declarations

from .catalog import CommandEntry

FILE_ROLES = {
    # uploaded by the admin, stored with the Run
    "csv_file": "input-file", "expected_csv": "input-file", "field_map": "input-file",
    "config": "input-file", "create_json": "input-file", "update_json": "input-file",
    "input": "input-file", "moves": "input-file", "plan_file": "input-file",
    "schemas_json": "input-file", "services_csv": "input-file", "tables_json": "input-file",
    "teams_yaml": "input-file",
    "input_dir": "input-dir",
    # written by the command inside the Run's directory, stored with the Run
    "output": "output-file", "plan_out": "output-file", "sn_plan_out": "output-file",
    "output_dir": "output-dir",
    # not offered: the service manages these itself
    "log_file": "hidden", "receipts_dir": "hidden",
}

# Arguments whose names look like paths but are not files (reviewed; see test_forms).
NOT_FILES = frozenset({"get_json"})  # pd-alert-grouping --get-json takes a setting ID

# Never offered in a form: credentials and prompts are refused outright, -y/--dry-run/--apply
# are set by the UI's own review and Approval gates (ADRs 0003, 0011).
HIDDEN_DESTS = frozenset({"help", "token", "prompt", "yes", "dry_run", "apply"})

FLAG_ACTIONS = (argparse._StoreTrueAction, argparse._StoreFalseAction,
                argparse._StoreConstAction)
VALUE_ACTIONS = (argparse._StoreAction, argparse._AppendAction)


class UnsupportedArgument(Exception):
    """An argument whose shape the form generator can't render."""


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _help(action: argparse.Action, prog: str) -> str:
    text = action.help or ""
    if text == argparse.SUPPRESS:
        return ""
    try:
        return text % {"default": action.default, "prog": prog, "dest": action.dest}
    except (KeyError, TypeError, ValueError):
        return text


def _kind(action: argparse.Action) -> str:
    role = FILE_ROLES.get(action.dest)
    if isinstance(action, FLAG_ACTIONS):
        return "flag"
    if isinstance(action, argparse._CountAction):
        return "count"
    if not isinstance(action, VALUE_ACTIONS):
        raise UnsupportedArgument(f"{action.dest}: unsupported action {type(action).__name__}")
    if action.choices is not None:
        return "choice"
    if action.type in (int, float):
        return "number"
    if action.type is not None:
        raise UnsupportedArgument(f"{action.dest}: unsupported type {action.type!r}")
    return role or "text"


def _field(action: argparse.Action, groups: dict[int, int], prog: str) -> dict | None:
    if action.dest in HIDDEN_DESTS or FILE_ROLES.get(action.dest) == "hidden":
        return None
    kind = _kind(action)
    multiple = isinstance(action, argparse._AppendAction) or action.nargs in ("*", "+")
    if action.nargs not in (None, "*", "+", "?", 0) and not isinstance(action, FLAG_ACTIONS):
        raise UnsupportedArgument(f"{action.dest}: unsupported nargs {action.nargs!r}")
    return {
        "dest": action.dest,
        "flags": list(action.option_strings),
        "positional": not action.option_strings,
        "kind": kind,
        "required": bool(action.required) or (not action.option_strings
                                               and action.nargs not in ("?", "*")),
        "default": None if action.default is argparse.SUPPRESS else _json_safe(action.default),
        "choices": [_json_safe(c) for c in action.choices] if action.choices else None,
        "help": _help(action, prog),
        "metavar": action.metavar if isinstance(action.metavar, str) else None,
        "multiple": multiple,
        "group": groups.get(id(action)),
    }


def _section(parser: argparse.ArgumentParser) -> tuple[list[dict], argparse._SubParsersAction]:
    groups = {id(a): i for i, g in enumerate(parser._mutually_exclusive_groups)
              for a in g._group_actions}
    fields, subparsers = [], None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers = action
            continue
        field = _field(action, groups, parser.prog)
        if field is not None:
            fields.append(field)
    return fields, subparsers


def form_for(entry: CommandEntry) -> dict:
    module = importlib.import_module(entry.module)
    parser = getattr(module, entry.builder)()
    declaration = declarations(module)[entry.builder]
    fields, subparsers = _section(parser)
    form = {"name": entry.name, "description": parser.description or "",
            "fields": fields, "subcommands": None,
            "writes": declaration.writes if isinstance(declaration, Risk) else None}
    if subparsers is not None:
        helps = {a.dest: a.help for a in subparsers._choices_actions}
        form["subcommands"] = []
        for name, sub in subparsers.choices.items():
            sub_fields, nested = _section(sub)
            if nested is not None:
                raise UnsupportedArgument(f"{entry.name} {name}: nested subcommands")
            form["subcommands"].append({
                "name": name, "help": helps.get(name) or sub.description or "",
                "fields": sub_fields, "writes": declaration[name].writes})
    return form


def all_fields(form: dict) -> list[dict]:
    fields = list(form["fields"])
    for sub in form["subcommands"] or []:
        fields += sub["fields"]
    return fields


class BadValues(Exception):
    """Form values that don't match the form (unknown field, wrong type)."""


def _flag_name(field: dict) -> str:
    return next((f for f in field["flags"] if f.startswith("--")), field["flags"][0])


def _as_list(field: dict, value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not field["multiple"] and len(values) > 1:
        raise BadValues(f"{field['dest']} takes one value")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise BadValues(f"{field['dest']} must be text or a number")
        out.append(str(v))
    return out


def _emit(field: dict, value, uploads: dict[str, list[str]]) -> list[str]:
    kind = field["kind"]
    if kind in ("input-file", "input-dir"):
        paths = uploads.get(field["dest"])
        if value not in (None, "", []):
            raise BadValues(f"{field['dest']} is a file: upload it instead")
        if not paths:
            return []
        if kind == "input-file" and len(paths) != 1:
            raise BadValues(f"{field['dest']} takes exactly one file")
        value = paths[0] if kind == "input-file" else paths[0].rsplit("/", 1)[0]
    if value in (None, "", [], False):
        return []
    if kind == "flag":
        if value is not True:
            raise BadValues(f"{field['dest']} is a yes/no flag")
        return [_flag_name(field)]
    if kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BadValues(f"{field['dest']} must be a whole number")
        return [_flag_name(field)] * value
    items = _as_list(field, value)
    if field["positional"]:
        return items
    argv = []
    for item in items:
        argv += [_flag_name(field), item]
    return argv


def build_argv(form: dict, subcommand: str | None, values: dict,
               uploads: dict[str, list[str]]) -> list[str]:
    """argv for a Run, from form values and the relative paths of uploaded files.

    Arguments are emitted in the parser's own order with long flag names; anything not on
    the form (including hidden fields such as credentials) is refused.
    """
    sections = [form["fields"]]
    if form["subcommands"] is not None:
        sub = next((s for s in form["subcommands"] if s["name"] == subcommand), None)
        if sub is None:
            raise BadValues(f"choose one of: {', '.join(s['name'] for s in form['subcommands'])}")
        sections.append(sub["fields"])
    elif subcommand:
        raise BadValues(f"{form['name']} has no subcommands")
    known = {f["dest"]: f for section in sections for f in section}
    unknown = (set(values) | set(uploads)) - set(known)
    if unknown:
        raise BadValues(f"not on this form: {', '.join(sorted(unknown))}")
    not_files = [d for d in uploads if known[d]["kind"] not in ("input-file", "input-dir")]
    if not_files:
        raise BadValues(f"not a file field: {', '.join(sorted(not_files))}")
    argv = [arg for f in sections[0] for arg in _emit(f, values.get(f["dest"]), uploads)]
    if len(sections) > 1:
        argv.append(subcommand)
        argv += [arg for f in sections[1] for arg in _emit(f, values.get(f["dest"]), uploads)]
    return argv
