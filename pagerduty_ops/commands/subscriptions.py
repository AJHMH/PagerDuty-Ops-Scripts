"""Bulk notification subscriptions for users and teams.

  list   export current subscriptions (by business service, user, or team)
  apply  subscribe / unsubscribe from a CSV (idempotent)

CSV columns: action, subscriber_kind, subscriber_id, subscribable_kind, subscribable_id
- action: subscribe | unsubscribe
- subscriber_kind: user | team
- subscribable_kind: business_service | incident   (PagerDuty supports only these)
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from ..api import PDApiError, paginate, request
from ..bulkops import load_csv_rows
from ..cli import (
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

log = get_logger("subscriptions")

ACTIONS = ("subscribe", "unsubscribe")
SUBSCRIBERS = ("user", "team")
SUBSCRIBABLES = ("business_service", "incident")
COLUMNS = ["action", "subscriber_kind", "subscriber_id", "subscribable_kind", "subscribable_id"]
BATCH = 50


RISK = {
    "list": READ_ONLY,
    "apply": writes_with_dry_run(),
}


def build_parser():
    p = argparse.ArgumentParser(description="Bulk PagerDuty notification subscriptions.")
    sub = p.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="Export current subscriptions (re-appliable CSV).")
    add_token_args(ls)
    add_logging_args(ls)
    add_format_args(ls, formats=("csv", "json", "table"), default="csv")
    ls.add_argument("--by", choices=["business_service", "user", "team"],
                    default="business_service",
                    help="Enumerate via business services (default), users, or teams.")
    a = sub.add_parser("apply", help="Subscribe/unsubscribe from a CSV.")
    add_token_args(a)
    add_logging_args(a)
    add_write_guards(a)
    a.add_argument("csv_file", help="CSV: " + ", ".join(COLUMNS))
    return p


def _base(kind, oid):
    return f"{'users' if kind == 'user' else 'teams'}/{oid}/notification_subscriptions"


def subscriptions_of(token, kind, oid) -> set[tuple]:
    out = set()
    for entry in paginate(_base(kind, oid), token, items_key="subscriptions"):
        s = entry.get("subscription", entry)
        if s.get("subscribable_id"):
            out.add((kind, oid, s.get("subscribable_type"), s["subscribable_id"]))
    return out


def list_main(args) -> int:
    token = init(args)
    found: set[tuple] = set()
    if args.by == "business_service":
        for bs in paginate("business_services", token):
            for s in paginate(f"business_services/{bs['id']}/subscribers", token,
                              items_key="subscribers"):
                if s.get("subscriber_id"):
                    found.add((s.get("subscriber_type"), s["subscriber_id"], "business_service",
                               bs["id"]))
    else:
        for obj in paginate(f"{args.by}s", token):
            try:
                found |= subscriptions_of(token, args.by, obj["id"])
            except PDApiError as e:
                if e.is_auth_error:
                    raise
                log.error("%s %s: %s", args.by, obj["id"], e)
    rows = [dict(zip(COLUMNS, ("subscribe", *f), strict=True)) for f in sorted(found)]
    write_payload(render_rows(rows, COLUMNS, args.format), args.output)
    log.info("Exported %d subscriptions.", len(rows))
    return EXIT_OK


def load_rows(path) -> list[tuple]:
    rows = load_csv_rows(path, set(COLUMNS), require_values=("subscriber_id", "subscribable_id"))
    out, bad = [], []
    for r in rows:
        vals = (r["action"].lower(), r["subscriber_kind"].lower(), r["subscriber_id"],
                r["subscribable_kind"].lower(), r["subscribable_id"])
        if vals[0] not in ACTIONS or vals[1] not in SUBSCRIBERS or vals[3] not in SUBSCRIBABLES:
            bad.append(f"line {r['_line']}: action {ACTIONS}, subscriber_kind {SUBSCRIBERS}, "
                       f"subscribable_kind {SUBSCRIBABLES}")
            continue
        out.append(vals)
    if bad:
        for b in bad:
            print(f"Error: {b}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return out


def rejected_count(action: str, body: dict, batch_size: int) -> int:
    """Items PagerDuty refused inside an HTTP 200 batch response.

    subscribe: per-item `result` (success | duplicate | unauthorized);
    unsubscribe: `unauthorized_count` (non-existent ones are already in the desired state).
    """
    body = body or {}
    if action == "subscribe":
        results = body.get("subscriptions") or []
        return min(batch_size, sum(1 for r in results if r.get("result") == "unauthorized"))
    return min(batch_size, int(body.get("unauthorized_count") or 0))


def apply_main(args) -> int:
    token = init(args)
    rows = load_rows(args.csv_file)
    wanted = {a: {r[1:] for r in rows if r[0] == a} for a in ACTIONS}
    if wanted["subscribe"] & wanted["unsubscribe"]:
        print("Error: some subscriptions are both subscribed and unsubscribed.", file=sys.stderr)
        return EXIT_USAGE
    existing: set[tuple] = set()
    for kind, oid in sorted({(r[1], r[2]) for r in rows}):
        try:
            existing |= subscriptions_of(token, kind, oid)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            print(f"Error: cannot read subscriptions of {kind} {oid}: {e}", file=sys.stderr)
            return EXIT_USAGE
    todo = {"subscribe": wanted["subscribe"] - existing,
            "unsubscribe": wanted["unsubscribe"] & existing}
    total = sum(len(v) for v in todo.values())
    log.info("%d to subscribe, %d to unsubscribe, %d already in desired state.",
             len(todo["subscribe"]), len(todo["unsubscribe"]), len(rows) - total)
    if not total:
        return EXIT_OK
    if not confirm(f"Apply {total} subscription changes?", assume_yes=args.yes,
                   dry_run=args.dry_run):
        return EXIT_OK

    succeeded = failed = 0
    for action, subs in todo.items():
        grouped = defaultdict(list)
        for kind, oid, s_kind, s_id in sorted(subs):
            grouped[(kind, oid)].append({"subscribable_type": s_kind, "subscribable_id": s_id})
        for (kind, oid), subscribables in grouped.items():
            for i in range(0, len(subscribables), BATCH):
                batch = subscribables[i:i + BATCH]
                if args.dry_run:
                    print(f"[dry-run] {action} {kind} {oid}: "
                          f"{[b['subscribable_id'] for b in batch]}", file=sys.stderr)
                    succeeded += len(batch)
                    continue
                path = _base(kind, oid) + ("/unsubscribe" if action == "unsubscribe" else "")
                try:
                    body = request(path, token, method="POST", data={"subscribables": batch})
                    rejected = rejected_count(action, body, len(batch))
                    if rejected:
                        log.error("%s %s %s: %d of %d rejected by PagerDuty (unauthorized)",
                                  action, kind, oid, rejected, len(batch))
                    log.info("%s %s %s: %d", action, kind, oid, len(batch) - rejected)
                    succeeded += len(batch) - rejected
                    failed += rejected
                except PDApiError as e:
                    if e.is_auth_error:
                        raise
                    log.error("%s %s %s failed: %s", action, kind, oid, e)
                    failed += len(batch)
    return finish_bulk(succeeded, failed, dry_run=args.dry_run, label="subscriptions")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"list": list_main, "apply": apply_main}[args.command](args)
