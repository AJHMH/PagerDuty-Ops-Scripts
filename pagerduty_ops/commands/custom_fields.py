"""Export and diff/apply service custom fields, incident types, and incident-type
custom fields (the Event Orchestration pattern: commit the JSON, review diffs).

Files (keyed by `name`, never IDs, so they port between accounts):
  <dir>/service_custom_fields.json   {"fields": [field, ...]}
  <dir>/incident_types.json          {"incident_types": [{..., "fields": [field, ...]}]}

field = {name, display_name, data_type, field_type, enabled, [default_value],
         [options: [value, ...]]}   (options only for *_fixed field types)

apply prints the planned changes; --apply -y writes them. Creates and updates are
always allowed; deleting fields/options not in the files needs --prune (deleting a
field deletes its data). Incident types cannot be deleted via the API — extra live
types are reported. data_type/field_type cannot change in place: reported, skipped.
The deprecated /incidents/custom_fields endpoints are not used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..api import PDApiError, paginate, request
from ..cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, add_logging_args, add_token_args, init
from ..log import get_logger
from ..risk import READ_ONLY, writes_with_apply

log = get_logger("custom_fields")

SERVICE_FILE = "service_custom_fields.json"
TYPES_FILE = "incident_types.json"
SERVICE_BASE = "services/custom_fields"
FIELD_KEYS = ("name", "display_name", "data_type", "field_type", "enabled", "default_value")
MUTABLE = ("display_name", "enabled", "default_value")
IMMUTABLE = ("data_type", "field_type")


RISK = {
    "export": READ_ONLY,
    "apply": writes_with_apply(irreversible_flags=("--prune",)),
}


def build_parser():
    p = argparse.ArgumentParser(description="Export / diff-apply custom fields & incident types.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write JSON files for git.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output-dir", default="custom_fields")
    a = sub.add_parser("apply", help="Diff (default) or apply JSON files.")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("-i", "--input-dir", default="custom_fields")
    a.add_argument("--apply", action="store_true", help="Write changes (requires -y).")
    a.add_argument("-y", "--yes", action="store_true", help="Acknowledge --apply.")
    a.add_argument("--prune", action="store_true",
                   help="Also delete live fields/options absent from the files.")
    return p


# ---------------- reading live state ----------------

def _type_base(type_name):
    return f"incidents/types/{type_name}/custom_fields"


def live_fields(token, base) -> list[dict]:
    fields = []
    for f in paginate(base, token, items_key="fields"):
        field = {k: f.get(k) for k in FIELD_KEYS if f.get(k) is not None}
        field["id"] = f.get("id")
        if str(f.get("field_type", "")).endswith("_fixed"):
            opts = request(f"{base}/{f['id']}/field_options", token).get("field_options") or []
            field["_options"] = {str((o.get("data") or {}).get("value")): o.get("id")
                                 for o in opts}
            field["options"] = sorted(field["_options"])
        fields.append(field)
    return sorted(fields, key=lambda x: x["name"])


def live_types(token) -> list[dict]:
    """All incident types (filter=all, so disabled ones are included), with the
    parent reference ({id, type}) resolved to the parent's name."""
    types = request("incidents/types", token, params={"filter": "all"}).get(
        "incident_types") or []
    name_by_id = {t.get("id"): t.get("name") for t in types if t.get("id")}
    out = []
    for t in sorted(types, key=lambda x: x.get("name") or ""):
        parent = t.get("parent") or t.get("parent_type")
        if isinstance(parent, dict):
            parent = parent.get("name") or name_by_id.get(parent.get("id")) or parent.get("id")
        out.append({"name": t.get("name"), "display_name": t.get("display_name"),
                    "enabled": t.get("enabled"), "parent_type": parent, "id": t.get("id")})
    return out


def _public(field):
    return {k: v for k, v in field.items() if not k.startswith("_") and k != "id"}


def export_main(args) -> int:
    token = init(args)
    os.makedirs(args.output_dir, exist_ok=True)
    svc = [_public(f) for f in live_fields(token, SERVICE_BASE)]
    types = []
    for t in live_types(token):
        t = {k: v for k, v in t.items() if k != "id"}
        t["fields"] = [_public(f) for f in live_fields(token, _type_base(t["name"]))]
        types.append(t)
    for name, payload in ((SERVICE_FILE, {"fields": svc}), (TYPES_FILE, {"incident_types": types})):
        path = os.path.join(args.output_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        log.info("Wrote %s", path)
    log.info("Exported %d service fields and %d incident types.", len(svc), len(types))
    return EXIT_OK


# ---------------- diff ----------------

def _read(path, key):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error: {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    items = doc.get(key)
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("name")
                                              for i in items):
        print(f"Error: {path}: '{key}' must be a list of objects with 'name'", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    names = [i["name"] for i in items]
    if len(names) != len(set(names)):
        print(f"Error: {path}: duplicate names", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return items


def diff_fields(base, desired, live, prune):
    """Return (changes, notes, errors). errors can never converge (exit 1)."""
    changes, problems, errors = [], [], []
    live_by = {f["name"]: f for f in live}
    for d in desired:
        cur = live_by.get(d["name"])
        if cur is None:
            body = {k: d[k] for k in FIELD_KEYS if k in d}
            if d.get("options"):
                body["field_options"] = [{"data": {"data_type": d.get("data_type", "string"),
                                                   "value": v}} for v in d["options"]]
            changes.append({"action": "create_field", "base": base, "name": d["name"],
                            "body": {"field": body}})
            continue
        bad = [k for k in IMMUTABLE if k in d and d[k] != cur.get(k)]
        if bad:
            errors.append(f"{base} {d['name']}: {', '.join(bad)} cannot change in place "
                            f"({', '.join(f'{cur.get(k)}→{d[k]}' for k in bad)})")
        upd = {k: d[k] for k in MUTABLE if k in d and d[k] != cur.get(k)}
        if upd:
            changes.append({"action": "update_field", "base": base, "name": d["name"],
                            "id": cur["id"], "body": {"field": upd}, "from": {
                                k: cur.get(k) for k in upd}})
        if "options" in d and "_options" in cur:
            want, have = set(map(str, d["options"])), set(cur["_options"])
            for v in sorted(want - have):
                changes.append({"action": "create_option", "base": base, "name": d["name"],
                                "id": cur["id"], "body": {"field_option": {"data": {
                                    "data_type": cur.get("data_type", "string"), "value": v}}}})
            for v in sorted(have - want):
                if prune:
                    changes.append({"action": "delete_option", "base": base, "name": d["name"],
                                    "id": cur["id"], "option_id": cur["_options"][v], "value": v})
                else:
                    problems.append(f"{base} {d['name']}: live option {v!r} not in file "
                                    "(kept; --prune deletes)")
    wanted = {d["name"] for d in desired}
    for name, cur in sorted(live_by.items()):
        if name not in wanted:
            if prune:
                changes.append({"action": "delete_field", "base": base, "name": name,
                                "id": cur["id"]})
            else:
                problems.append(f"{base} {name}: live field not in file (kept; --prune deletes)")
    return changes, problems, errors


def describe(c) -> str:
    a = c["action"]
    if a == "update_field":
        return f"~ {c['base']} {c['name']}: " + ", ".join(
            f"{k} {c['from'][k]!r}→{v!r}" for k, v in c["body"]["field"].items())
    if a in ("create_option", "delete_option"):
        val = c.get("value") or c["body"]["field_option"]["data"]["value"]
        return f"{'+' if a == 'create_option' else '-'} {c['base']} {c['name']} option {val!r}"
    if a == "create_type":
        return f"+ incident type {c['name']} (parent {c['body']['incident_type']['parent_type']})"
    if a == "update_type":
        return f"~ incident type {c['name']}: {c['body']['incident_type']}"
    return f"{'+' if a == 'create_field' else '-'} {c['base']} {c['name']}"


def execute(token, c):
    a = c["action"]
    if a == "create_field":
        request(c["base"], token, method="POST", data=c["body"])
    elif a == "update_field":
        request(f"{c['base']}/{c['id']}", token, method="PUT", data=c["body"])
    elif a == "delete_field":
        request(f"{c['base']}/{c['id']}", token, method="DELETE")
    elif a == "create_option":
        request(f"{c['base']}/{c['id']}/field_options", token, method="POST", data=c["body"])
    elif a == "delete_option":
        request(f"{c['base']}/{c['id']}/field_options/{c['option_id']}", token, method="DELETE")
    elif a == "create_type":
        request("incidents/types", token, method="POST", data=c["body"])
    elif a == "update_type":
        request(f"incidents/types/{c['name']}", token, method="PUT", data=c["body"])


def apply_main(args) -> int:
    token = init(args)
    if args.apply and not args.yes:
        print("Error: --apply requires -y/--yes.", file=sys.stderr)
        return EXIT_USAGE
    svc = _read(os.path.join(args.input_dir, SERVICE_FILE), "fields")
    types = _read(os.path.join(args.input_dir, TYPES_FILE), "incident_types")
    if svc is None and types is None:
        print(f"Error: neither {SERVICE_FILE} nor {TYPES_FILE} found in {args.input_dir}",
              file=sys.stderr)
        return EXIT_USAGE

    changes, problems, errors, pending_types = [], [], [], set()
    if svc is not None:
        c, p, e = diff_fields(SERVICE_BASE, svc, live_fields(token, SERVICE_BASE), args.prune)
        changes += c
        problems += p
        errors += e
    if types is not None:
        live_t = {t["name"]: t for t in live_types(token)}
        for t in types:
            cur = live_t.get(t["name"])
            if cur is None:
                if not t.get("parent_type") or not t.get("display_name"):
                    errors.append(f"incident type {t['name']}: new types need display_name "
                                    "and parent_type")
                    continue
                body = {k: t[k] for k in ("name", "display_name", "parent_type", "enabled")
                        if k in t}
                changes.append({"action": "create_type", "name": t["name"],
                                "body": {"incident_type": body}})
                pending_types.add(t["name"])
            else:
                upd = {k: t[k] for k in ("display_name", "enabled") if k in t
                       and t[k] != cur.get(k)}
                if upd:
                    changes.append({"action": "update_type", "name": t["name"],
                                    "body": {"incident_type": upd}})
                if t.get("parent_type") and t["parent_type"] != cur.get("parent_type"):
                    errors.append(f"incident type {t['name']}: parent_type cannot change")
            base = _type_base(t["name"])
            live = [] if t["name"] in pending_types else live_fields(token, base)
            c, p, e = diff_fields(base, t.get("fields") or [], live, args.prune)
            changes += c
            problems += p
            errors += e
        for name in sorted(set(live_t) - {t["name"] for t in types}):
            problems.append(f"incident type {name}: live but not in file (types cannot be "
                            "deleted via the API; disable it with enabled: false)")

    for line in map(describe, changes):
        print(line)
    for pr in problems:
        log.warning("%s", pr)
    for er in errors:
        log.error("%s", er)
    log.info("%d changes, %d notes, %d unresolvable.", len(changes), len(problems), len(errors))
    if not args.apply:
        if changes:
            log.info("Re-run with --apply -y to write these changes.")
        return EXIT_FAILURES if errors else EXIT_OK

    failed = 0
    for c in changes:
        try:
            execute(token, c)
            log.info("applied: %s", describe(c))
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("failed: %s: %s", describe(c), e)
            failed += 1
    return EXIT_FAILURES if failed or errors else EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"export": export_main, "apply": apply_main}[args.command](args)
