"""Write side of pd-scim-user-audit: plan -> review -> apply SCIM user corrections.

  plan   compare PagerDuty SCIM users with the IdP CSV (email, displayName, active) and
         write a reviewable plan file (kind "scim-sync")
  apply  apply a plan with --dry-run / -y, re-checking every item live first

IMPORTANT: in PagerDuty's SCIM API, setting `active` to false DELETES the user (204).
That cannot be undone, so deletions are guarded:
- only users the IdP CSV marks inactive are planned for deletion; users missing from the
  CSV ("orphans") are reported, and planned only with --delete-orphans
- a deletion is BLOCKED (never applied) for the account owner, for users on any schedule
  or escalation policy (clear rotations first, e.g. pd-remove-team-members), and when the
  SCIM user cannot be matched by email to exactly one PagerDuty user
- apply re-checks the same conditions live, refuses plans older than --max-plan-age-hours
  (default 24), and caps deletions with --max-deletes (default 10)
Users missing from PagerDuty are reported only; provisioning belongs to the IdP.
SCIM user IDs are not PagerDuty user IDs, so users are matched by email.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..api import PDApiError, request
from ..cli import (
    EXIT_FAILURES,
    EXIT_OK,
    EXIT_USAGE,
    add_logging_args,
    add_token_args,
    confirm,
    finish_bulk,
    init,
)
from ..log import get_logger
from ..pdstate import PDState, ref_ids
from ..plan import load_plan, new_plan, plan_hash, write_plan
from .scim_user_audit import (
    SCIM_HEADERS,
    fetch_scim_users,
    load_expected,
    normalize_pd_user,
)

log = get_logger("scim_user_sync")

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
PATCH_HEADERS = {**SCIM_HEADERS, "Content-Type": "application/scim+json"}
DEFAULT_MAX_DELETES = 10
DEFAULT_MAX_PLAN_AGE_HOURS = 24


def build_parser():
    p = argparse.ArgumentParser(description="Plan/apply SCIM user corrections from an IdP CSV.")
    sub = p.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan", help="Write a scim-sync plan (read-only).")
    add_token_args(pl)
    add_logging_args(pl)
    pl.add_argument("expected_csv", help="IdP CSV: email, displayName, active")
    pl.add_argument("--plan-out", required=True, help="Plan JSON to write.")
    pl.add_argument("--delete-orphans", action="store_true",
                    help="Also plan deletion of PagerDuty users absent from the CSV. Use only "
                         "with a COMPLETE IdP export.")
    a = sub.add_parser("apply", help="Apply a scim-sync plan.")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("plan_file")
    a.add_argument("--dry-run", action="store_true", help="Preview every change; no writes.")
    a.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt.")
    a.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES,
                   help=f"Refuse plans deleting more users (default {DEFAULT_MAX_DELETES}).")
    a.add_argument("--max-plan-age-hours", type=float, default=DEFAULT_MAX_PLAN_AGE_HOURS,
                   help=f"Refuse older plans (default {DEFAULT_MAX_PLAN_AGE_HOURS}).")
    return p


# ---------------- safety checks ----------------

def rest_user_index(pd: PDState) -> dict[str, list[str]]:
    by_email: dict[str, list[str]] = defaultdict(list)
    for uid, u in pd.users.items():
        if u.get("email"):
            by_email[u["email"].strip().lower()].append(uid)
    return by_email


def on_call_references(pd: PDState, rest_id: str) -> list[str]:
    refs = [f"schedule {s.get('name') or sid}" for sid, s in pd.schedules.items()
            if rest_id in ref_ids(s.get("users"))]
    for eid, e in pd.escalation_policies.items():
        for rule in e.get("escalation_rules") or []:
            if any(t.get("id") == rest_id and t.get("type") in ("user_reference", "user")
                   for t in rule.get("targets") or []):
                refs.append(f"escalation policy {e.get('name') or eid}")
                break
    return refs


def deletion_blockers(pd: PDState, by_email, email: str) -> tuple[str | None, list[str]]:
    """(rest_user_id, reasons) — any reason blocks the deletion."""
    matches = by_email.get(email, [])
    if len(matches) != 1:
        return None, [f"matches {len(matches)} PagerDuty users by email"]
    rest_id = matches[0]
    reasons = []
    if (pd.users.get(rest_id) or {}).get("role") == "owner":
        reasons.append("is the account owner")
    refs = on_call_references(pd, rest_id)
    if refs:
        reasons.append("still on " + ", ".join(sorted(refs)[:5])
                       + (" ..." if len(refs) > 5 else ""))
    return rest_id, reasons


# ---------------- plan ----------------

def build_items(scim_users, expected, pd: PDState, delete_orphans: bool) -> list[dict]:
    by_email = rest_user_index(pd)
    items = []
    for u in sorted(scim_users, key=lambda x: x["email"]):
        email = u["email"]
        if not email:
            continue
        exp = expected.get(email)
        if exp is None:
            if not delete_orphans:
                log.info("orphan (report only; --delete-orphans to plan): %s", email)
                continue
            why = "not in IdP CSV (--delete-orphans)"
        elif exp["active"] or not u["active"]:
            if exp["active"] and u["active"] and exp["displayName"] and \
                    exp["displayName"] != u["displayName"]:
                items.append({"op": "set_display_name", "scim_id": u["id"], "email": email,
                              "value": exp["displayName"], "expected": u["displayName"],
                              "detail": f"{u['displayName']!r} -> {exp['displayName']!r}"})
            continue
        else:
            why = "IdP marks inactive"
        rest_id, blockers = deletion_blockers(pd, by_email, email)
        item = {"op": "delete_user", "scim_id": u["id"], "email": email,
                "rest_user_id": rest_id or "", "detail": why}
        if blockers:
            item = {**item, "op": "blocked_delete", "detail": f"{why}; " + "; ".join(blockers)}
        items.append(item)
    return items


def plan_main(args) -> int:
    token = init(args)
    expected = load_expected(args.expected_csv)
    scim_users = [normalize_pd_user(u) for u in fetch_scim_users(token)]
    in_pd = {u["email"] for u in scim_users}
    for email in sorted(set(expected) - in_pd):
        log.info("missing from PagerDuty (report only; provision via the IdP): %s", email)
    items = build_items(scim_users, expected, PDState(token), args.delete_orphans)
    plan = new_plan("scim-sync", items, generated_by="pd-scim-user-sync plan",
                    options={"delete_orphans": args.delete_orphans,
                             "expected_csv": args.expected_csv})
    sha = write_plan(plan, args.plan_out)
    counts = defaultdict(int)
    for i in items:
        counts[i["op"]] += 1
    log.info("Wrote %s (sha256 %s): %s", args.plan_out, sha[:12],
             ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes")
    for i in items:
        print(f"{i['op']:<17} {i['email']}: {i['detail']}", file=sys.stderr)
    if counts.get("blocked_delete"):
        log.warning("%d deletions are blocked and will not be applied; resolve the reasons "
                    "shown, then re-plan.", counts["blocked_delete"])
    return EXIT_OK


# ---------------- apply ----------------

def _plan_age_ok(plan, max_hours) -> bool:
    try:
        made = datetime.fromisoformat(str(plan.get("generated_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - made <= timedelta(hours=max_hours)


def _scim_patch(token, scim_id, operations):
    request(f"scim/v2/Users/{scim_id}", token, method="PATCH",
            data={"schemas": [PATCH_SCHEMA], "Operations": operations},
            extra_headers=PATCH_HEADERS)


def apply_main(args) -> int:
    token = init(args)
    plan = load_plan(args.plan_file, expected_kind="scim-sync")
    if args.max_deletes < 0:
        print("Error: --max-deletes must be >= 0.", file=sys.stderr)
        return EXIT_USAGE
    if not _plan_age_ok(plan, args.max_plan_age_hours):
        print(f"Error: plan is older than {args.max_plan_age_hours} hours (or undated); "
              "re-run 'plan' so deletions reflect current data.", file=sys.stderr)
        return EXIT_USAGE
    items = [i for i in plan["items"] if i.get("op") in ("set_display_name", "delete_user")]
    bad = [i for i in plan["items"]
           if i.get("op") not in ("set_display_name", "delete_user", "blocked_delete")
           or not i.get("scim_id") or not i.get("email")]
    if bad:
        print(f"Error: plan contains {len(bad)} malformed items.", file=sys.stderr)
        return EXIT_USAGE
    deletes = [i for i in items if i["op"] == "delete_user"]
    log.info("Plan %s (sha256 %s): %d display-name updates, %d deletions, %d blocked.",
             args.plan_file, plan_hash(plan)[:12], len(items) - len(deletes), len(deletes),
             sum(1 for i in plan["items"] if i["op"] == "blocked_delete"))
    if len(deletes) > args.max_deletes:
        print(f"Error: plan deletes {len(deletes)} users, above --max-deletes "
              f"{args.max_deletes}. Deletion is irreversible; split the CSV or raise the cap.",
              file=sys.stderr)
        return EXIT_USAGE
    if not items:
        return EXIT_OK

    # Re-check everything live: the plan may be hours old.
    live = {u["id"]: u for u in (normalize_pd_user(x) for x in fetch_scim_users(token))}
    pd = PDState(token)
    by_email = rest_user_index(pd)
    todo, failed = [], 0
    for i in items:
        cur = live.get(i["scim_id"])
        if cur is None or cur["email"] != i["email"]:
            if i["op"] == "delete_user" and cur is None:
                log.info("%s: already gone; skipping.", i["email"])
            else:
                log.error("%s: SCIM user changed since planning; re-plan.", i["email"])
                failed += 1
            continue
        if i["op"] == "set_display_name":
            if cur["displayName"] == i["value"]:
                continue
            if cur["displayName"] != i.get("expected", ""):
                log.error("%s: display name changed since planning; re-plan.", i["email"])
                failed += 1
                continue
        else:
            _rest_id, blockers = deletion_blockers(pd, by_email, i["email"])
            if blockers:
                log.error("%s: deletion now blocked (%s); skipping.", i["email"],
                          "; ".join(blockers))
                failed += 1
                continue
        todo.append(i)

    n_del = sum(1 for i in todo if i["op"] == "delete_user")
    if n_del:
        log.warning("%d users will be DELETED from PagerDuty. This cannot be undone.", n_del)
    if todo and not confirm(f"Apply {len(todo)} SCIM changes ({n_del} irreversible deletions)?",
                            assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK
    ok = 0
    for i in todo:
        if args.dry_run:
            verb = "DELETE user" if i["op"] == "delete_user" else "set displayName"
            print(f"[dry-run] {verb} {i['email']}: {i['detail']}", file=sys.stderr)
            ok += 1
            continue
        try:
            if i["op"] == "delete_user":
                _scim_patch(token, i["scim_id"], [{"op": "replace", "value": {"active": False}}])
                log.info("deleted %s (%s)", i["email"], i["detail"])
            else:
                _scim_patch(token, i["scim_id"],
                            [{"op": "replace", "path": "displayName", "value": i["value"]}])
                log.info("updated displayName of %s: %s", i["email"], i["detail"])
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            if i["op"] == "delete_user" and e.status_code == 404:
                ok += 1  # already deleted
                continue
            log.error("%s %s failed: %s", i["op"], i["email"], e)
            failed += 1
    code = finish_bulk(ok, failed, dry_run=args.dry_run, label="SCIM changes")
    return EXIT_FAILURES if failed else code


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"plan": plan_main, "apply": apply_main}[args.command](args)
