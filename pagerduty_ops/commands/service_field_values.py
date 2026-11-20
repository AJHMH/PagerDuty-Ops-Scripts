"""Bulk service custom field *values*: export to a wide CSV, edit, apply.

CSV layout (export and apply use the same one):
  service_id, service_name, <field_name>, <field_name>, ...
- service_name is informational and ignored on apply.
- multi_value fields hold values joined with "|" (at most 10); a literal "|" or "\\"
  inside a value is escaped as "\\|" / "\\\\" so export -> apply round-trips exactly.
- An empty cell leaves the field unchanged; with --clear-empty it resets it (null).

Field definitions are managed with `pd-custom-fields`; this command only sets values.
apply validates every cell (known field and service, data type, fixed-field options)
before the first write, diffs against current values, and sends one atomic PUT per
service containing only the fields that change.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from urllib.parse import urlparse

from ..api import PDApiError, paginate, request
from ..bulkops import load_csv_rows
from ..cli import (
    EXIT_FAILURES,
    EXIT_OK,
    EXIT_USAGE,
    add_format_args,
    add_logging_args,
    add_token_args,
    add_write_guards,
    confirm,
    finish_bulk,
    init,
)
from ..log import get_logger
from ..output import render_rows, write_payload
from ..risk import READ_ONLY, writes_with_dry_run

log = get_logger("service_field_values")

FIELDS_BASE = "services/custom_fields"
MULTI_SEP = "|"
MAX_MULTI = 10
MAX_STRING = 200
FIXED_COLUMNS = ("service_id", "service_name")
TRUE = {"true", "yes", "y", "1"}
FALSE = {"false", "no", "n", "0"}


RISK = {
    "export": READ_ONLY,
    "apply": writes_with_dry_run(),
}


def build_parser():
    p = argparse.ArgumentParser(description="Export / bulk-apply service custom field values.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write current values as a wide CSV (or JSON).")
    add_token_args(e)
    add_logging_args(e)
    add_format_args(e, formats=("csv", "json", "table"), default="csv")
    e.add_argument("--service-ids", help="Comma-separated service IDs (default: all).")
    e.add_argument("--fields", help="Comma-separated field names (default: all).")
    a = sub.add_parser("apply", help="Apply values from a wide CSV (idempotent).")
    add_token_args(a)
    add_logging_args(a)
    add_write_guards(a)
    a.add_argument("csv_file", help="CSV: service_id, [service_name], <field_name>...")
    a.add_argument("--clear-empty", action="store_true",
                   help="Treat empty cells as 'reset to null' instead of 'leave unchanged'.")
    return p


def _split(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


# ---------------- definitions & values ----------------

def field_definitions(token) -> dict:
    """{name: {id, data_type, field_type, options:set|None}}."""
    out = {}
    for f in paginate(FIELDS_BASE, token, params={"include[]": ["field_options"]},
                      items_key="fields"):
        options = None
        if str(f.get("field_type", "")).endswith("_fixed"):
            options = {str((o.get("data") or {}).get("value"))
                       for o in f.get("field_options") or []}
        out[f["name"]] = {"id": f.get("id"), "data_type": f.get("data_type", "string"),
                          "field_type": f.get("field_type", "single_value"),
                          "options": options}
    return out


def current_values(token, service_id) -> dict:
    body = request(f"services/{service_id}/custom_fields/values", token)
    return {f.get("name"): f.get("value") for f in body.get("custom_fields") or []
            if f.get("name")}


def is_multi(defn) -> bool:
    return defn["field_type"].startswith("multi_value")


def join_multi(values) -> str:
    """Join multi-values with "|", escaping a literal backslash or "|" in any value."""
    return MULTI_SEP.join(v.replace("\\", "\\\\").replace(MULTI_SEP, "\\" + MULTI_SEP)
                          for v in values)


def split_multi(text: str) -> list[str]:
    """Inverse of join_multi: split on unescaped "|" and unescape each part."""
    parts, cur, chars = [], [], iter(text)
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, None)
            if nxt not in ("\\", MULTI_SEP):
                raise ValueError("'\\' must be followed by '|' or '\\' in multi-value cells")
            cur.append(nxt)
        elif ch == MULTI_SEP:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def format_value(value, defn) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return join_multi(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------- export ----------------

def export_main(args) -> int:
    token = init(args)
    defs = field_definitions(token)
    names = _split(args.fields) or sorted(defs)
    unknown = [n for n in names if n not in defs]
    if unknown:
        print(f"Error: unknown fields {unknown}; known: {sorted(defs)}", file=sys.stderr)
        return EXIT_USAGE
    wanted = set(_split(args.service_ids) or [])
    rows, raw, failed = [], [], 0
    for svc in paginate("services", token):
        if wanted and svc["id"] not in wanted:
            continue
        try:
            values = current_values(token, svc["id"])
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("service %s: %s", svc["id"], e)
            failed += 1
            continue
        row = {"service_id": svc["id"], "service_name": svc.get("name", "")}
        row.update({n: format_value(values.get(n), defs[n]) for n in names})
        rows.append(row)
        raw.append({"service_id": svc["id"], "service_name": svc.get("name", ""),
                    "values": {n: values.get(n) for n in names}})
    if failed:
        log.error("Export aborted: values of %d services could not be read; no output written.",
                  failed)
        return EXIT_FAILURES
    write_payload(render_rows(rows, [*FIXED_COLUMNS, *names], args.format, raw=raw),
                  args.output, fmt=args.format)
    log.info("Exported %d services x %d fields.", len(rows), len(names))
    return EXIT_OK


# ---------------- parsing & validation ----------------

def _parse_scalar(text: str, defn: dict):
    dt = defn["data_type"]
    if dt == "boolean":
        low = text.lower()
        if low in TRUE:
            return True
        if low in FALSE:
            return False
        raise ValueError("expected true/false")
    if dt == "integer":
        return int(text)
    if dt == "float":
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("float must be finite (not nan, inf, or out of range)")
        return number
    if dt == "datetime":
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("datetime must include a timezone")
        return text
    if dt == "url":
        u = urlparse(text)
        if u.scheme not in ("http", "https") or not u.netloc or len(text) > MAX_STRING:
            raise ValueError("expected an http(s) URL of at most 200 characters")
        return text
    if len(text) > MAX_STRING:
        raise ValueError(f"longer than {MAX_STRING} characters")
    return text


def parse_cell(text: str, defn: dict):
    """CSV cell -> API value. Raises ValueError with a readable reason."""
    if is_multi(defn):
        parts = [p.strip() for p in split_multi(text) if p.strip()]
        if len(parts) > MAX_MULTI:
            raise ValueError(f"at most {MAX_MULTI} values")
        if len(set(parts)) != len(parts):
            raise ValueError("duplicate values")
        values = [_parse_scalar(p, defn) for p in parts]
    else:
        values = [_parse_scalar(text, defn)]
    if defn["options"] is not None:
        bad = [v for v in values if str(v) not in defn["options"]]
        if bad:
            raise ValueError(f"{bad} not in allowed options {sorted(defn['options'])}")
    return values if is_multi(defn) else values[0]


def same_value(a, b, defn) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if is_multi(defn):
        return sorted(map(str, a or [])) == sorted(map(str, b or []))
    if defn["data_type"] == "float":
        return float(a) == float(b)
    if defn["data_type"] == "datetime":
        def parse(v):
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return parse(a) == parse(b)
    return a == b


def load_desired(path, defs, known_services, clear_empty) -> dict:
    """{service_id: {field_name: value}} after validating every cell (exit 2 on errors)."""
    rows = load_csv_rows(path, {"service_id"}, require_values=("service_id",))
    columns = [c for c in (rows[0].keys() if rows else []) if c not in (*FIXED_COLUMNS, "_line")]
    errors = [f"unknown field column {c!r}" for c in columns if c not in defs]
    desired, seen = {}, set()
    for r in rows:
        sid = r["service_id"]
        where = f"line {r['_line']}"
        if sid in seen:
            errors.append(f"{where}: duplicate service_id {sid}")
            continue
        seen.add(sid)
        if sid not in known_services:
            errors.append(f"{where}: unknown service {sid}")
            continue
        changes = {}
        for c in columns:
            if c not in defs:
                continue
            text = r.get(c, "")
            if text == "":
                if clear_empty:
                    changes[c] = None
                continue
            try:
                changes[c] = parse_cell(text, defs[c])
            except ValueError as e:
                errors.append(f"{where} ({sid}) {c}={text!r}: {e}")
        desired[sid] = changes
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return desired


# ---------------- apply ----------------

def apply_main(args) -> int:
    token = init(args)
    defs = field_definitions(token)
    known = {s["id"]: s.get("name", "") for s in paginate("services", token)}
    desired = load_desired(args.csv_file, defs, known, args.clear_empty)

    plan = {}
    for sid, wanted in sorted(desired.items()):
        if not wanted:
            continue
        try:
            current = current_values(token, sid)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            print(f"Error: cannot read values of service {sid}: {e}", file=sys.stderr)
            return EXIT_USAGE
        diff = {n: v for n, v in wanted.items()
                if not same_value(current.get(n), v, defs[n])}
        if diff:
            plan[sid] = (diff, current)
    fields_changed = sum(len(d) for d, _c in plan.values())
    log.info("%d services need %d field changes; %d already match.", len(plan),
             fields_changed, len(desired) - len(plan))
    if not plan:
        return EXIT_OK
    if not confirm(f"Update custom field values on {len(plan)} services?", assume_yes=args.yes,
                   dry_run=args.dry_run):
        return EXIT_OK

    succeeded = failed = 0
    for sid, (diff, current) in plan.items():
        label = f"{known.get(sid) or sid} ({sid})"
        if args.dry_run:
            for n, v in sorted(diff.items()):
                print(f"[dry-run] {label} {n}: {current.get(n)!r} -> {v!r}", file=sys.stderr)
            succeeded += 1
            continue
        body = {"custom_fields": [{"name": n, "value": v} for n, v in sorted(diff.items())]}
        try:
            request(f"services/{sid}/custom_fields/values", token, method="PUT", data=body)
            log.info("%s: updated %s", label, ", ".join(sorted(diff)))
            succeeded += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s: update failed (no fields applied): %s", label, e)
            failed += 1
    return finish_bulk(succeeded, failed, dry_run=args.dry_run, label="services")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"export": export_main, "apply": apply_main}[args.command](args)
