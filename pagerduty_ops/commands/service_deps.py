"""Bulk service dependencies: export to CSV, apply associate/disassociate from CSV.

CSV columns: action, supporting_kind, supporting_id, dependent_kind, dependent_id
- action: associate | disassociate
- *_kind: service (technical) | business_service

apply reads existing relationships of every dependent first and skips no-ops
(idempotent), then batches POST /service_dependencies/{associate,disassociate}.
"""

from __future__ import annotations

import argparse
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

log = get_logger("service_deps")

KINDS = ("service", "business_service")
ACTIONS = ("associate", "disassociate")
COLUMNS = ["action", "supporting_kind", "supporting_id", "dependent_kind", "dependent_id"]
DEFAULT_BATCH = 100


RISK = {
    "export": READ_ONLY,
    "apply": writes_with_dry_run(),
}


def build_parser():
    p = argparse.ArgumentParser(description="Export or bulk-apply PagerDuty service dependencies.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Export all dependencies (re-appliable CSV).")
    add_token_args(e)
    add_logging_args(e)
    add_format_args(e, formats=("csv", "json", "table"), default="csv")
    a = sub.add_parser("apply", help="Associate/disassociate from a CSV.")
    add_token_args(a)
    add_logging_args(a)
    add_write_guards(a)
    a.add_argument("csv_file", help="CSV: " + ", ".join(COLUMNS))
    a.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                   help=f"Relationships per API call (default {DEFAULT_BATCH}).")
    return p


def norm_kind(type_value: str) -> str:
    return "business_service" if "business" in (type_value or "") else "service"


def _endpoint(kind, oid):
    seg = "business_services" if kind == "business_service" else "technical_services"
    return f"service_dependencies/{seg}/{oid}"


def relationships_of(token, kind, oid) -> set[tuple]:
    body = request(_endpoint(kind, oid), token)
    out = set()
    for rel in body.get("relationships") or []:
        sup, dep = rel.get("supporting_service") or {}, rel.get("dependent_service") or {}
        if sup.get("id") and dep.get("id"):
            out.add((norm_kind(sup.get("type")), sup["id"], norm_kind(dep.get("type")), dep["id"]))
    return out


def export_main(args) -> int:
    token = init(args)
    seen: set[tuple] = set()
    failed = 0
    for kind, resource in (("service", "services"), ("business_service", "business_services")):
        for obj in paginate(resource, token):
            try:
                seen |= relationships_of(token, kind, obj["id"])
            except PDApiError as e:
                if e.is_auth_error:
                    raise
                log.error("%s %s: %s", kind, obj["id"], e)
                failed += 1
    if failed:
        # Never publish a partial export that automation could mistake for a full backup.
        log.error("Export aborted: dependencies of %d services could not be read; no output "
                  "written.", failed)
        return EXIT_FAILURES
    rows = [dict(zip(COLUMNS, ("associate", *rel), strict=True)) for rel in sorted(seen)]
    write_payload(render_rows(rows, COLUMNS, args.format), args.output)
    log.info("Exported %d dependencies.", len(rows))
    return EXIT_OK


def load_rows(path) -> list[tuple]:
    rows = load_csv_rows(path, set(COLUMNS), require_values=("supporting_id", "dependent_id"))
    out, bad = [], []
    for r in rows:
        action = r["action"].lower()
        sk, dk = r["supporting_kind"].lower() or "service", r["dependent_kind"].lower() or "service"
        if action not in ACTIONS or sk not in KINDS or dk not in KINDS:
            bad.append(f"line {r['_line']}: action must be {ACTIONS}, kinds {KINDS}")
            continue
        if (sk, r["supporting_id"]) == (dk, r["dependent_id"]):
            bad.append(f"line {r['_line']}: a service cannot depend on itself")
            continue
        out.append((action, sk, r["supporting_id"], dk, r["dependent_id"]))
    if bad:
        for b in bad:
            print(f"Error: {b}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return out


def _rel_body(rel):
    sk, sid, dk, did = rel
    return {"supporting_service": {"id": sid, "type": sk},
            "dependent_service": {"id": did, "type": dk}}


def apply_main(args) -> int:
    token = init(args)
    if args.batch_size < 1:
        print("Error: --batch-size must be >= 1.", file=sys.stderr)
        return EXIT_USAGE
    rows = load_rows(args.csv_file)
    wanted = {a: {r[1:] for r in rows if r[0] == a} for a in ACTIONS}
    clash = wanted["associate"] & wanted["disassociate"]
    if clash:
        print(f"Error: {len(clash)} relationships are both associated and disassociated.",
              file=sys.stderr)
        return EXIT_USAGE

    existing: set[tuple] = set()
    for kind, oid in sorted({(r[3], r[4]) for r in rows}):
        try:
            existing |= relationships_of(token, kind, oid)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            print(f"Error: cannot read dependencies of {kind} {oid}: {e}", file=sys.stderr)
            return EXIT_USAGE
    # Disassociate first: a replacement must free capacity (2,000 per service) before adding.
    todo = {"disassociate": sorted(wanted["disassociate"] & existing),
            "associate": sorted(wanted["associate"] - existing)}
    skipped = len(rows) - sum(len(v) for v in todo.values())
    log.info("%d to associate, %d to disassociate, %d already in desired state.",
             len(todo["associate"]), len(todo["disassociate"]), skipped)
    total = sum(len(v) for v in todo.values())
    if not total:
        return EXIT_OK
    if not confirm(f"Apply {total} dependency changes?", assume_yes=args.yes,
                   dry_run=args.dry_run):
        return EXIT_OK

    succeeded = failed = 0
    for action, rels in todo.items():
        for i in range(0, len(rels), args.batch_size):
            batch = rels[i:i + args.batch_size]
            if args.dry_run:
                for rel in batch:
                    print(f"[dry-run] {action} {rel[0]} {rel[1]} -> supports {rel[2]} {rel[3]}",
                          file=sys.stderr)
                succeeded += len(batch)
                continue
            try:
                request(f"service_dependencies/{action}", token, method="POST",
                        data={"relationships": [_rel_body(r) for r in batch]})
                log.info("%s: %d relationships", action, len(batch))
                succeeded += len(batch)
            except PDApiError as e:
                if e.is_auth_error:
                    raise
                log.error("%s batch of %d failed: %s", action, len(batch), e)
                failed += len(batch)
    return finish_bulk(succeeded, failed, dry_run=args.dry_run, label="dependencies")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"export": export_main, "apply": apply_main}[args.command](args)
