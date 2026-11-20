"""Bulk schedule overrides from CSV (create / delete), v2 by default, v3 with --v3.

CSV columns:
  action          create | delete
  schedule_id     PagerDuty schedule ID
  user_id | user_email     the overriding (on-call) user — give exactly one
  start, end      ISO 8601 with timezone
  --v3 only: overridden_user_id | overridden_user_email (who is replaced),
             optional rotation_id or custom_shift_id

Emails are matched exactly (case-insensitive) against PagerDuty users; a row whose
email matches zero or several users fails validation before any write.
Idempotent: creates skip identical existing overrides; deletes skip missing ones.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

from ..api import PDApiError, paginate, request
from ..bulkops import load_csv_rows, parse_iso8601
from ..cli import EXIT_OK, EXIT_USAGE, confirm, finish_bulk, init, standard_parser
from ..log import get_logger

log = get_logger("schedule_overrides")

ACTIONS = ("create", "delete")
REQUIRED = {"action", "schedule_id", "start", "end"}


def build_parser():
    p = standard_parser("Bulk create/delete PagerDuty schedule overrides from a CSV.",
                        write_guards=True)
    p.add_argument("csv_file", help="CSV: action, schedule_id, user_id|user_email, start, end")
    p.add_argument("--v3", action="store_true",
                   help="Use the v3 (shift-based) schedules API; needs overridden_user_* column.")
    return p


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class UserResolver:
    def __init__(self, token):
        self.token = token
        self._by_email = None

    def by_email(self, email: str):
        if self._by_email is None:
            self._by_email = defaultdict(list)
            for u in paginate("users", self.token):
                if u.get("email"):
                    self._by_email[u["email"].strip().lower()].append(u["id"])
        return self._by_email.get(email.strip().lower(), [])


def _resolve_user(row, prefix, resolver, errors):
    uid, email = row.get(f"{prefix}user_id", ""), row.get(f"{prefix}user_email", "")
    label = f"line {row['_line']}"
    if bool(uid) == bool(email):
        errors.append(f"{label}: give exactly one of {prefix}user_id / {prefix}user_email")
        return None
    if uid:
        return uid
    matches = resolver.by_email(email)
    if len(matches) != 1:
        errors.append(f"{label}: {prefix}user_email {email!r} matches {len(matches)} users")
        return None
    return matches[0]


def load_rows(path, token, v3: bool) -> list[dict]:
    rows = load_csv_rows(path, REQUIRED, skip_if_missing=("schedule_id",))
    resolver, errors, out = UserResolver(token), [], []
    for r in rows:
        action = r["action"].lower()
        if action not in ACTIONS:
            errors.append(f"line {r['_line']}: action must be one of {ACTIONS}")
            continue
        start = parse_iso8601(r["start"], field="start", line=r["_line"])
        end = parse_iso8601(r["end"], field="end", line=r["_line"])
        if end <= start:
            errors.append(f"line {r['_line']}: end must be after start")
            continue
        user = _resolve_user(r, "", resolver, errors)
        replaced = _resolve_user(r, "overridden_", resolver, errors) if v3 else None
        if v3 and r.get("rotation_id") and r.get("custom_shift_id"):
            errors.append(f"line {r['_line']}: rotation_id and custom_shift_id are exclusive")
        out.append({"line": r["_line"], "action": action, "schedule_id": r["schedule_id"],
                    "user_id": user, "overridden_user_id": replaced, "start": start, "end": end,
                    "rotation_id": r.get("rotation_id", ""),
                    "custom_shift_id": r.get("custom_shift_id", "")})
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return out


def _parse(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def existing_overrides(token, schedule_id, since, until, v3) -> list[dict]:
    """Normalized [{id, user_id, start, end}] in the window."""
    params = {"since": _iso(since), "until": _iso(until)}
    if v3:
        body = request(f"v3/schedules/{schedule_id}/overrides", token, params=params)
        return [{"id": o.get("id"), "user_id": (o.get("overriding_member") or {}).get("user_id"),
                 "start": _parse(o["start_time"]), "end": _parse(o["end_time"])}
                for o in body.get("overrides") or [] if o.get("start_time")]
    body = request(f"schedules/{schedule_id}/overrides", token, params=params)
    return [{"id": o.get("id"), "user_id": (o.get("user") or {}).get("id"),
             "start": _parse(o["start"]), "end": _parse(o["end"])}
            for o in body.get("overrides") or [] if o.get("start")]


def _match(existing, row):
    return [o for o in existing if o["user_id"] == row["user_id"]
            and o["start"] == row["start"] and o["end"] == row["end"]]


def _create_body(rows, v3):
    if not v3:
        return {"overrides": [{"start": _iso(r["start"]), "end": _iso(r["end"]),
                               "user": {"id": r["user_id"], "type": "user_reference"}}
                              for r in rows]}
    out = []
    for r in rows:
        o = {"type": "override_shift", "start_time": _iso(r["start"]), "end_time": _iso(r["end"]),
             "overridden_member": {"type": "user_member", "user_id": r["overridden_user_id"]},
             "overriding_member": {"type": "user_member", "user_id": r["user_id"]}}
        for k in ("rotation_id", "custom_shift_id"):
            if r[k]:
                o[k] = r[k]
        out.append(o)
    return {"overrides": out}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = init(args)
    rows = load_rows(args.csv_file, token, args.v3)
    if not rows:
        log.info("No usable rows in CSV.")
        return EXIT_OK

    by_schedule = defaultdict(list)
    for r in rows:
        by_schedule[r["schedule_id"]].append(r)
    creates, deletes, skipped = defaultdict(list), [], 0
    for sid, srows in sorted(by_schedule.items()):
        since = min(r["start"] for r in srows)
        until = max(r["end"] for r in srows)
        try:
            existing = existing_overrides(token, sid, since, until, args.v3)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            print(f"Error: cannot read overrides for schedule {sid}: {e}", file=sys.stderr)
            return EXIT_USAGE
        for r in srows:
            found = _match(existing, r)
            if r["action"] == "create" and not found:
                creates[sid].append(r)
            elif r["action"] == "delete" and found:
                deletes.extend((sid, o["id"], r) for o in found)
            else:
                skipped += 1
    total = sum(len(v) for v in creates.values()) + len(deletes)
    log.info("%d overrides to create, %d to delete, %d already in desired state.",
             total - len(deletes), len(deletes), skipped)
    if not total:
        return EXIT_OK
    if not confirm(f"Apply {total} override changes?", assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK

    prefix = "v3/schedules" if args.v3 else "schedules"
    succeeded = failed = 0
    for sid, srows in sorted(creates.items()):
        if args.dry_run:
            for r in srows:
                print(f"[dry-run] create override on {sid}: {r['user_id']} "
                      f"{_iso(r['start'])} → {_iso(r['end'])}", file=sys.stderr)
            succeeded += len(srows)
            continue
        try:
            result = request(f"{prefix}/{sid}/overrides", token, method="POST",
                             data=_create_body(srows, args.v3))
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("schedule %s: create failed for %d overrides: %s", sid, len(srows), e)
            failed += len(srows)
            continue
        statuses = result if isinstance(result, list) else []
        bad = [s for s in statuses if int(s.get("status", 201)) >= 400]
        for s in bad:
            log.error("schedule %s: override rejected: %s", sid, "; ".join(s.get("errors") or []))
        failed += len(bad)
        succeeded += len(srows) - len(bad)
    for sid, oid, r in deletes:
        if args.dry_run:
            print(f"[dry-run] delete override {oid} on {sid} (line {r['line']})", file=sys.stderr)
            succeeded += 1
            continue
        try:
            request(f"{prefix}/{sid}/overrides/{oid}", token, method="DELETE")
            succeeded += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            if e.status_code == 404:
                succeeded += 1
                continue
            log.error("schedule %s: delete %s failed: %s", sid, oid, e)
            failed += 1
    return finish_bulk(succeeded, failed, dry_run=args.dry_run, label="overrides")
