"""ServiceNow CMDB enrichment administration (PagerDuty EARLY ACCESS APIs).

Every subcommand requires --early-access: these endpoints "may change at any
time" and need PagerDuty to enable them on the account.

  list            integrations (+ CMDB tables), enrichment schemas, event enrichments
  tables-apply    create/update CMDB tables on an integration from JSON (idempotent)
  table-test      run a CMDB table test query
  schemas-apply   create enrichment schemas from JSON (idempotent, by name; fields
                  of existing schemas cannot change — differences exit 1)
  records-upload  upload a CSV of enrichment records into a schema (≤ 10 MB)
  associations    add/remove event-enrichment associations from CSV (idempotent)

ServiceNow *credentials* are intentionally NOT managed here: configure them once in
the PagerDuty UI so ServiceNow secrets never pass through this tool.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

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

log = get_logger("cmdb_enrichment")

SN_BASE = "enrichment/integrations/servicenow"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TABLE_KEYS = ("display_name", "ci_table_name", "query_filter", "field_mappings")
ASSOC_TYPES = {"service": "service_reference",
               "event_orchestration": "event_orchestration_reference"}
SCHEMA_FIELD_TYPES = ("query", "enriched")


def _common(p, write=False):
    add_token_args(p)
    add_logging_args(p)
    p.add_argument("--early-access", action="store_true",
                   help="Required: acknowledge these are PagerDuty Early Access APIs.")
    if write:
        add_write_guards(p)


RISK = {
    "list": READ_ONLY,
    "table-test": READ_ONLY,
    "tables-apply": writes_with_dry_run(),
    "schemas-apply": writes_with_dry_run(),
    "records-upload": writes_with_dry_run(),
    "associations": writes_with_dry_run(),
}


def build_parser():
    p = argparse.ArgumentParser(
        description="ServiceNow CMDB enrichment administration (EARLY ACCESS).")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list", help="List integrations, schemas, and event enrichments.")
    _common(ls)
    add_format_args(ls, formats=("table", "json"))

    ta = sub.add_parser("tables-apply", help="Create/update CMDB tables from JSON.")
    _common(ta, write=True)
    ta.add_argument("tables_json", help='{"cmdb_tables": [{display_name, ci_table_name, '
                    'query_filter, field_mappings}]}')
    ta.add_argument("--integration-id", required=True)
    ta.add_argument("--enable", action="store_true",
                    help="Enable sync for tables that were created or changed.")

    tt = sub.add_parser("table-test", help="Test a CMDB table query.")
    _common(tt)
    tt.add_argument("--integration-id", required=True)
    tt.add_argument("--table-id", required=True)

    sa = sub.add_parser("schemas-apply", help="Create/update enrichment schemas from JSON.")
    _common(sa, write=True)
    sa.add_argument("schemas_json", help='{"schemas": [{name, fields: [{name, type}]}]}')

    ru = sub.add_parser("records-upload", help="Upload enrichment records (CSV) to a schema.")
    _common(ru, write=True)
    ru.add_argument("csv_file")
    ru.add_argument("--schema-id", required=True)

    asc = sub.add_parser("associations", help="Add/remove event enrichment associations.")
    _common(asc, write=True)
    asc.add_argument("csv_file", help="CSV: action (add|remove), type (service|"
                     "event_orchestration), id")
    asc.add_argument("--enrichment-id", required=True)
    return p


def _require_ea(args):
    if not args.early_access:
        print("Error: CMDB enrichment uses PagerDuty Early Access APIs that may change at any "
              "time. Re-run with --early-access to acknowledge.", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _load_json(path, key) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get(key)
    except (OSError, ValueError, AttributeError) as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        print(f"Error: {path}: '{key}' must be a list of objects", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return items


# ---------------- list / test ----------------

def list_main(args) -> int:
    token = init(args)
    integrations = request(SN_BASE, token).get("integrations") or []
    schemas = request("enrichment/schemas", token).get("schemas") or []
    enrichments = request("enrichment/event_enrichments", token).get("event_enrichments") or []
    if args.format == "json":
        payload = json.dumps({"integrations": integrations, "schemas": schemas,
                              "event_enrichments": enrichments}, indent=2) + "\n"
        write_payload(payload, args.output)
        return EXIT_OK
    rows = [{"kind": "integration", "id": i.get("id"), "name": i.get("name"),
             "detail": f"{len(i.get('cmdb_tables') or [])} CMDB tables"} for i in integrations]
    rows += [{"kind": "cmdb_table", "id": t.get("id"), "name": t.get("display_name"),
              "detail": f"{t.get('ci_table_name')} (integration {i.get('id')})"}
             for i in integrations for t in i.get("cmdb_tables") or []]
    rows += [{"kind": "schema", "id": s.get("id"), "name": s.get("name"),
              "detail": f"{len(s.get('fields') or [])} fields"} for s in schemas]
    rows += [{"kind": "event_enrichment", "id": e.get("id"), "name": e.get("name"),
              "detail": ""} for e in enrichments]
    write_payload(render_rows(rows, ["kind", "id", "name", "detail"], "table"), args.output)
    return EXIT_OK


def table_test_main(args) -> int:
    token = init(args)
    body = request(f"{SN_BASE}/{args.integration_id}/tables/{args.table_id}/test", token)
    sys.stdout.write(json.dumps(body, indent=2) + "\n")
    return EXIT_OK


# ---------------- tables ----------------

def _table_body(t):
    return {k: t[k] for k in TABLE_KEYS if k in t}


def _mapping_key(mappings):
    """Writable mapping properties only, in order (responses add e.g. a `type` field)."""
    return [(m.get("servicenow_field"), m.get("event_field")) for m in mappings or []]


def _table_differs(desired: dict, live: dict) -> bool:
    for k in ("display_name", "ci_table_name", "query_filter"):
        if k in desired and (desired.get(k) or None) != (live.get(k) or None):
            return True
    return "field_mappings" in desired and \
        _mapping_key(desired["field_mappings"]) != _mapping_key(live.get("field_mappings"))


def validate_tables(tables) -> list[str]:
    problems, seen = [], set()
    for n, t in enumerate(tables):
        name = t.get("display_name")
        if not name or not t.get("ci_table_name"):
            problems.append(f"table {n}: display_name and ci_table_name are required")
        if name in seen:
            problems.append(f"table {name!r}: duplicate display_name")
        seen.add(name)
        fm = t.get("field_mappings") or []
        if not 2 <= len(fm) <= 20:
            problems.append(f"table {name!r}: field_mappings needs 2-20 entries")
        if any(not (m.get("servicenow_field") and m.get("event_field")) for m in fm):
            problems.append(f"table {name!r}: each mapping needs servicenow_field and "
                            "event_field")
    return problems


def tables_apply_main(args) -> int:
    token = init(args)
    desired = _load_json(args.tables_json, "cmdb_tables")
    problems = validate_tables(desired)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return EXIT_USAGE
    integ = request(f"{SN_BASE}/{args.integration_id}", token)
    integ = integ.get("integration", integ)
    live = {t.get("display_name"): t for t in integ.get("cmdb_tables") or []}
    todo = []
    for t in desired:
        cur = live.get(t["display_name"])
        if cur is None:
            todo.append(("create", None, t))
        elif _table_differs(t, cur):
            todo.append(("update", cur.get("id"), t))
    log.info("%d tables to create, %d to update, %d unchanged.",
             sum(1 for a, *_ in todo if a == "create"),
             sum(1 for a, *_ in todo if a == "update"), len(desired) - len(todo))
    if not todo or not confirm(f"Apply {len(todo)} CMDB table changes?", assume_yes=args.yes,
                               dry_run=args.dry_run):
        return EXIT_OK
    ok = failed = 0
    for action, table_id, t in todo:
        if args.dry_run:
            print(f"[dry-run] {action} CMDB table {t['display_name']!r} "
                  f"({t['ci_table_name']}){' + enable' if args.enable else ''}", file=sys.stderr)
            ok += 1
            continue
        try:
            if action == "create":
                res = request(f"{SN_BASE}/{args.integration_id}/tables", token, method="POST",
                              data={"cmdb_table": _table_body(t)})
                table_id = (res.get("cmdb_table") or res).get("id")
            else:
                request(f"{SN_BASE}/{args.integration_id}/tables/{table_id}", token,
                        method="PUT", data={"cmdb_table": _table_body(t)})
            if args.enable and table_id:
                request(f"{SN_BASE}/{args.integration_id}/tables/{table_id}/enable", token,
                        method="POST")
            log.info("%sd CMDB table %r", action, t["display_name"])
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s CMDB table %r failed: %s", action, t["display_name"], e)
            failed += 1
    return finish_bulk(ok, failed, dry_run=args.dry_run, label="CMDB tables")


# ---------------- schemas ----------------

def _norm_fields(fields):
    return sorted((f.get("name"), f.get("type")) for f in fields or [])


def validate_schemas(schemas) -> list[str]:
    problems, seen = [], set()
    for n, sc in enumerate(schemas):
        name = sc.get("name")
        if not name or len(name) > 50:
            problems.append(f"schema {n}: name is required (≤ 50 chars)")
        if name in seen:
            problems.append(f"schema {name!r}: duplicate name")
        seen.add(name)
        fields = sc.get("fields") or []
        if not 2 <= len(fields) <= 25:
            problems.append(f"schema {name!r}: fields needs 2-25 entries")
        if any(f.get("type") not in SCHEMA_FIELD_TYPES or not f.get("name") for f in fields):
            problems.append(f"schema {name!r}: each field needs name and type in "
                            f"{SCHEMA_FIELD_TYPES}")
        if fields and not any(f.get("type") == "query" for f in fields):
            problems.append(f"schema {name!r}: at least one 'query' field is required")
    return problems


def schemas_apply_main(args) -> int:
    token = init(args)
    desired = _load_json(args.schemas_json, "schemas")
    problems = validate_schemas(desired)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return EXIT_USAGE
    live = {sc.get("name"): sc for sc in request("enrichment/schemas", token).get("schemas")
            or []}
    todo, unsupported = [], []
    for sc in desired:
        cur = live.get(sc["name"])
        if cur is None:
            todo.append(("create", None, sc))
        elif _norm_fields(cur.get("fields")) != _norm_fields(sc["fields"]):
            # The API only allows renaming; fields are fixed at creation.
            unsupported.append(sc["name"])
            log.error("schema %r: fields differ from the live schema, but schema fields "
                      "cannot change after creation (create a new schema instead)", sc["name"])
    log.info("%d schemas to create, %d unchanged, %d unsupported field changes.", len(todo),
             len(desired) - len(todo) - len(unsupported), len(unsupported))
    if not todo:
        return EXIT_FAILURES if unsupported else EXIT_OK
    if not confirm(f"Create {len(todo)} schemas?", assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_FAILURES if unsupported else EXIT_OK
    ok = failed = 0
    for action, _sid, sc in todo:
        body = {"name": sc["name"], "fields": [{"name": f["name"], "type": f["type"]}
                                               for f in sc["fields"]]}
        if args.dry_run:
            print(f"[dry-run] {action} schema {sc['name']!r}: {_norm_fields(sc['fields'])}",
                  file=sys.stderr)
            ok += 1
            continue
        try:
            request("enrichment/schemas", token, method="POST",
                    data={**body, "integration_type": "CSV"})
            log.info("created schema %r", sc["name"])
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("create schema %r failed: %s", sc["name"], e)
            failed += 1
    return finish_bulk(ok, failed + len(unsupported), dry_run=args.dry_run, label="schemas")


# ---------------- records ----------------

def records_upload_main(args) -> int:
    token = init(args)
    try:
        size = os.path.getsize(args.csv_file)
        with open(args.csv_file, encoding="utf-8", newline="") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"Error: cannot read {args.csv_file}: {e}", file=sys.stderr)
        return EXIT_USAGE
    if size > MAX_UPLOAD_BYTES:
        print(f"Error: {args.csv_file} is {size} bytes; the API limit is 10 MB. Split it.",
              file=sys.stderr)
        return EXIT_USAGE
    header = next(csv.reader(io.StringIO(content)), [])
    rows = max(0, sum(1 for _ in csv.reader(io.StringIO(content))) - 1)
    sc = request(f"enrichment/schemas/{args.schema_id}", token)
    sc = sc.get("schema", sc)
    fields = {f.get("name"): f.get("type") for f in sc.get("fields") or []}
    missing_query = [n for n, t in fields.items() if t == "query" and n not in header]
    if missing_query:
        print(f"Error: CSV lacks query column(s) {missing_query}; records are keyed by them.",
              file=sys.stderr)
        return EXIT_USAGE
    extra = [h for h in header if h not in fields]
    missing = [n for n in fields if n not in header]
    if extra:
        log.warning("Columns not in schema (ignored by PagerDuty): %s", extra)
    if missing:
        log.warning("Schema fields missing from CSV (uploaded as empty): %s", missing)
    log.info("%d records for schema %r (rows matching existing query values update them).",
             rows, sc.get("name") or args.schema_id)
    if not confirm(f"Upload {rows} records?", assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK
    if args.dry_run:
        print(f"[dry-run] would upload {rows} records ({size} bytes)", file=sys.stderr)
        return EXIT_OK
    request(f"enrichment/schemas/{args.schema_id}/records", token, method="POST",
            params={"filename": os.path.basename(args.csv_file)},
            extra_headers={"Content-Type": "text/csv"}, raw_body=content.encode("utf-8"))
    log.info("Upload accepted for asynchronous processing.")
    return EXIT_OK


# ---------------- associations ----------------

def associations_main(args) -> int:
    token = init(args)
    rows = load_csv_rows(args.csv_file, {"action", "type", "id"}, require_values=("id",))
    want = {"add": set(), "remove": set()}
    bad = []
    for r in rows:
        action, typ = r["action"].lower(), r["type"].lower()
        if action not in want or typ not in ASSOC_TYPES:
            bad.append(f"line {r['_line']}: action add|remove, type {sorted(ASSOC_TYPES)}")
            continue
        want[action].add((ASSOC_TYPES[typ], r["id"]))
    if bad or (want["add"] & want["remove"]):
        for b in bad or ["some associations are both added and removed"]:
            print(f"Error: {b}", file=sys.stderr)
        return EXIT_USAGE
    base = f"enrichment/event_enrichments/{args.enrichment_id}/associations"
    live = {(a.get("type"), a.get("id"))
            for a in paginate(base, token, items_key="associations")}
    todo = {"add": sorted(want["add"] - live), "remove": sorted(want["remove"] & live)}
    total = len(todo["add"]) + len(todo["remove"])
    log.info("%d to add, %d to remove, %d already in desired state.", len(todo["add"]),
             len(todo["remove"]), len(rows) - total)
    if not total or not confirm(f"Apply {total} association changes?", assume_yes=args.yes,
                                dry_run=args.dry_run):
        return EXIT_OK
    ok = failed = 0
    for action, items in todo.items():
        if not items:
            continue
        if args.dry_run:
            print(f"[dry-run] {action} {items}", file=sys.stderr)
            ok += len(items)
            continue
        try:
            body = request(base, token, method="POST" if action == "add" else "DELETE",
                           data={"associations": [{"type": t, "id": i} for t, i in items]})
            # HTTP 207 = partial success: per-item statuses in body["associations"].
            bad = [r for r in (body or {}).get("associations") or []
                   if int(r.get("status") or 0) >= 400]
            for r in bad:
                a = r.get("association") or {}
                log.error("%s %s %s failed: %s", action, a.get("type"), a.get("id"),
                          "; ".join(r.get("errors") or []) or r.get("status"))
            failed += len(bad)
            ok += len(items) - len(bad)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s associations failed: %s", action, e)
            failed += len(items)
    return finish_bulk(ok, failed, dry_run=args.dry_run, label="associations")


HANDLERS = {"list": list_main, "tables-apply": tables_apply_main, "table-test": table_test_main,
            "schemas-apply": schemas_apply_main, "records-upload": records_upload_main,
            "associations": associations_main}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _require_ea(args)
    return HANDLERS[args.command](args)
