"""Remove users from a PagerDuty team, interactively or from a list of user IDs.

Interactive (no --user-ids): for each member, walk through removal from schedule
layers, escalation policy rule targets, and finally the team itself.

Non-interactive (--user-ids, for cron, scripts and the UI; ADR 0009): the named users
are removed from the team, and with --from-schedules / --from-escalation-policies also
from the team's schedule layers and escalation rules. Everything is validated before
the first write, --dry-run previews, -y skips the prompt, and re-runs skip what is
already done. A user stays on the team if removing them from a schedule or policy
failed, so the re-run can finish the job. Fully paginated (the previous
version silently truncated members/schedules/policies at the API default of
25 — dangerous, since a user could be removed from the team while still on an
unseen schedule). --dry-run previews every API call.
"""

from __future__ import annotations

import sys

from ..api import PDApiError, paginate, request
from ..cli import EXIT_USAGE, confirm, finish_bulk, init, standard_parser
from ..config import get_team_id
from ..log import get_logger
from ..output import render_rows
from ..risk import writes_with_dry_run

log = get_logger("remove_team_members")


RISK = writes_with_dry_run()


def build_parser():
    p = standard_parser("Remove users from a PagerDuty team (interactive, or --user-ids).")
    p.add_argument("--team-id", help="PagerDuty team ID (or set PD_TEAM_ID).")
    p.add_argument("--user-ids",
                   help="Comma-separated PagerDuty user IDs to remove, without prompting.")
    p.add_argument("--from-schedules", action="store_true",
                   help="With --user-ids: also remove them from the team's schedule layers.")
    p.add_argument("--from-escalation-policies", action="store_true",
                   help="With --user-ids: also remove them from the team's escalation rules.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be done without modifying anything.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="With --user-ids: skip the confirmation prompt.")
    return p


def get_schedule(token, schedule_id) -> dict:
    return request(f"schedules/{schedule_id}", token).get("schedule", {})


def get_policy(token, policy_id) -> dict:
    return request(f"escalation_policies/{policy_id}", token).get("escalation_policy", {})


def remove_user_from_schedule(token, schedule_id, user_id, user_name, dry_run=False) -> bool:
    schedule = get_schedule(token, schedule_id)
    schedule_name = schedule.get("summary", "Unknown Schedule")
    modified = False
    for idx, layer in enumerate(schedule.get("schedule_layers") or []):
        users = layer.get("users")
        if users and any(u.get("id") == user_id for u in users):
            layer["users"] = [u for u in users if u.get("id") != user_id]
            modified = True
            log.info("Removing %s from layer %d in schedule %r", user_name, idx + 1, schedule_name)
    if not modified:
        log.info("%s not found in any layer of schedule %r", user_name, schedule_name)
        return False
    if dry_run:
        print(f"[dry-run] would update schedule {schedule_name!r} to remove {user_name}",
              file=sys.stderr)
        return True
    try:
        request(f"schedules/{schedule_id}", token, method="PUT", data={
            "schedule": {
                "name": schedule.get("name"),
                "schedule_layers": schedule.get("schedule_layers", []),
            }
        })
        log.info("Removed %s from schedule %r", user_name, schedule_name)
        return True
    except PDApiError as e:
        if e.is_auth_error:
            raise
        log.error("Failed to update schedule %r: %s", schedule_name, e)
        return False


def remove_user_from_policy(token, policy_id, user_id, user_name, *, policy=None,
                            dry_run=False) -> bool:
    policy = policy or get_policy(token, policy_id)
    policy_name = policy.get("summary", "Unknown Policy")
    modified = False
    for idx, rule in enumerate(policy.get("escalation_rules") or []):
        targets = rule.get("targets")
        if not targets:
            continue
        kept = [t for t in targets if not (t.get("type") == "user" and t.get("id") == user_id)]
        if len(kept) < len(targets):
            rule["targets"] = kept
            modified = True
            log.info("Removing %s from rule %d in policy %r", user_name, idx + 1, policy_name)
    if not modified:
        log.info("%s not found in any rule of policy %r", user_name, policy_name)
        return False
    if dry_run:
        print(f"[dry-run] would update policy {policy_name!r} to remove {user_name}",
              file=sys.stderr)
        return True
    try:
        request(f"escalation_policies/{policy_id}", token, method="PUT", data={
            "escalation_policy": {
                "name": policy.get("name"),
                "escalation_rules": policy.get("escalation_rules", []),
            }
        })
        log.info("Removed %s from policy %r", user_name, policy_name)
        return True
    except PDApiError as e:
        if e.is_auth_error:
            raise
        log.error("Failed to update policy %r: %s", policy_name, e)
        return False


def build_assignment_maps(token, team_id):
    """user_id -> [schedule refs], user_id -> [policy refs]; fully paginated."""
    schedules = list(paginate("schedules", token, params={"team_ids[]": [team_id]}))
    policies = list(paginate("escalation_policies", token, params={"team_ids[]": [team_id]}))

    user_schedules: dict = {}
    for schedule in schedules:
        sid = schedule.get("id")
        sname = schedule.get("summary", "Unknown")
        for user in paginate(f"schedules/{sid}/users", token, items_key="users"):
            user_schedules.setdefault(user.get("id"), []).append({"id": sid, "name": sname})

    user_policies: dict = {}
    policy_details: dict = {}
    for policy in policies:
        pid = policy.get("id")
        pname = policy.get("summary", "Unknown")
        detail = get_policy(token, pid)
        policy_details[pid] = detail
        for rule in detail.get("escalation_rules", []):
            for target in rule.get("targets", []):
                if target.get("type") == "user":
                    bucket = user_policies.setdefault(target.get("id"), [])
                    if not any(p["id"] == pid for p in bucket):
                        bucket.append({"id": pid, "name": pname})
    return user_schedules, user_policies, policy_details


# ---------------- non-interactive (--user-ids) ----------------

def _usage(message: str) -> int:
    log.error("%s", message)
    return EXIT_USAGE


def _parse_user_ids(raw: str) -> list[str]:
    ids = []
    for part in raw.split(","):
        uid = part.strip()
        if uid and uid not in ids:
            ids.append(uid)
    return ids


def _layer_user_id(entry: dict) -> str | None:
    """Schedule layer users are ``{"user": {...}}``; tolerate a bare reference too."""
    return (entry.get("user") or entry).get("id")


def _plan_schedules(token, team_id, user_ids, errors) -> list[dict]:
    """Schedules (full detail) where a listed user sits on a layer, with layers rewritten."""
    planned = []
    for ref in paginate("schedules", token, params={"team_ids[]": [team_id]}):
        schedule = get_schedule(token, ref["id"])
        hit = set()
        for layer in schedule.get("schedule_layers") or []:
            users = layer.get("users") or []
            kept = [u for u in users if _layer_user_id(u) not in user_ids]
            if len(kept) == len(users):
                continue
            hit |= {_layer_user_id(u) for u in users} & set(user_ids)
            if not kept:
                errors.append(f"schedule {schedule.get('summary')!r} layer "
                              f"{layer.get('name')!r} would have no users left; "
                              f"add someone else to it first")
            layer["users"] = kept
        if hit:
            planned.append({"schedule": schedule, "users": sorted(hit)})
    return planned


def _plan_policies(token, team_id, user_ids, errors) -> list[dict]:
    """Escalation policies (full detail) where a listed user is a rule target, rewritten."""
    planned = []
    for ref in paginate("escalation_policies", token, params={"team_ids[]": [team_id]}):
        policy = get_policy(token, ref["id"])
        hit = set()
        for idx, rule in enumerate(policy.get("escalation_rules") or []):
            targets = rule.get("targets") or []
            kept = [t for t in targets
                    if not (t.get("type", "").startswith("user") and t.get("id") in user_ids)]
            if len(kept) == len(targets):
                continue
            hit |= {t.get("id") for t in targets} & set(user_ids)
            if not kept:
                errors.append(f"escalation policy {policy.get('summary')!r} rule {idx + 1} "
                              f"would have no targets left; add another target first")
            rule["targets"] = kept
        if hit:
            planned.append({"policy": policy, "users": sorted(hit)})
    return planned


def remove_listed_users(args, token, team_id) -> int:
    user_ids = _parse_user_ids(args.user_ids)
    if not user_ids:
        return _usage("--user-ids is empty; name at least one user ID.")

    names, errors = {}, []
    for uid in user_ids:
        try:
            names[uid] = request(f"users/{uid}", token).get("user", {}).get("name") or uid
        except PDApiError as e:
            if e.is_auth_error:
                raise
            if e.status_code != 404:
                raise
            errors.append(f"user {uid} does not exist")
    members = {m.get("user", {}).get("id")
               for m in paginate(f"teams/{team_id}/members", token, items_key="members")}
    schedules = (_plan_schedules(token, team_id, user_ids, errors)
                 if args.from_schedules else [])
    policies = (_plan_policies(token, team_id, user_ids, errors)
                if args.from_escalation_policies else [])
    if errors:
        for e in errors:
            log.error("%s", e)
        return _usage(f"{len(errors)} problem(s) found; nothing was changed.")

    leaving = [uid for uid in user_ids if uid in members]
    for uid in user_ids:
        if uid not in members:
            log.info("%s (%s) is not on team %s; skipping the team removal.",
                     names[uid], uid, team_id)
    changes = len(schedules) + len(policies) + len(leaving)
    if not changes:
        log.info("Nothing to do: every listed user is already removed.")
        return 0
    for s in schedules:
        print(f"{'[dry-run] would update' if args.dry_run else 'Will update'} schedule "
              f"{s['schedule'].get('summary')!r} to remove "
              f"{', '.join(names[u] for u in s['users'])}", file=sys.stderr)
    for p in policies:
        print(f"{'[dry-run] would update' if args.dry_run else 'Will update'} policy "
              f"{p['policy'].get('summary')!r} to remove "
              f"{', '.join(names[u] for u in p['users'])}", file=sys.stderr)
    for uid in leaving:
        print(f"{'[dry-run] would' if args.dry_run else 'Will'} DELETE "
              f"/teams/{team_id}/members/{uid} for {names[uid]}", file=sys.stderr)
    if args.dry_run:
        return finish_bulk(changes, 0, dry_run=True, label="changes")
    if not confirm(f"Apply {changes} change(s) to team {team_id}?", assume_yes=args.yes):
        return 0

    ok = failed = 0
    blocked: set[str] = set()  # users whose schedule/policy removal failed stay on the team
    for s in schedules:
        schedule = s["schedule"]
        try:
            request(f"schedules/{schedule['id']}", token, method="PUT", data={"schedule": {
                "name": schedule.get("name"),
                "schedule_layers": schedule.get("schedule_layers", [])}})
            log.info("Updated schedule %r.", schedule.get("summary"))
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("Failed to update schedule %r: %s", schedule.get("summary"), e)
            failed += 1
            blocked.update(s["users"])
    for p in policies:
        policy = p["policy"]
        try:
            request(f"escalation_policies/{policy['id']}", token, method="PUT",
                    data={"escalation_policy": {
                        "name": policy.get("name"),
                        "escalation_rules": policy.get("escalation_rules", [])}})
            log.info("Updated escalation policy %r.", policy.get("summary"))
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("Failed to update escalation policy %r: %s", policy.get("summary"), e)
            failed += 1
            blocked.update(p["users"])
    for uid in leaving:
        if uid in blocked:
            log.error("Kept %s on team %s: a schedule or policy update failed; re-run to finish.",
                      names[uid], team_id)
            failed += 1
            continue
        try:
            request(f"teams/{team_id}/members/{uid}", token, method="DELETE")
            log.info("Removed %s from team %s.", names[uid], team_id)
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("Failed to remove %s from team %s: %s", names[uid], team_id, e)
            failed += 1
    return finish_bulk(ok, failed, dry_run=False, label="changes")


# ---------------- interactive ----------------

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = init(args)
    team_id = get_team_id(args.team_id)
    dry_run = args.dry_run

    if args.user_ids is not None:
        return remove_listed_users(args, token, team_id)
    if args.from_schedules or args.from_escalation_policies or args.yes:
        return _usage("--from-schedules, --from-escalation-policies and -y need --user-ids.")

    if not sys.stdin.isatty() and not dry_run:
        log.error("This command is interactive and needs a TTY (or use --dry-run).")
        return 2
    if dry_run:
        log.info("[dry-run] No schedules, policies, or memberships will be modified.")

    members = list(paginate(f"teams/{team_id}/members", token, items_key="members"))
    user_schedules, user_policies, policy_details = build_assignment_maps(token, team_id)

    rows = []
    for idx, member in enumerate(members):
        user = member.get("user", {})
        uid = user.get("id", "")
        rows.append({
            "#": idx,
            "user_id": uid,
            "name": user.get("summary", ""),
            "on_schedule": ", ".join(s["name"] for s in user_schedules.get(uid, [])) or "No",
            "in_policy": ", ".join(p["name"] for p in user_policies.get(uid, [])) or "No",
        })
    print("\n=== Team Members and Their Assignments ===", file=sys.stderr)
    print(render_rows(rows, ["#", "user_id", "name", "on_schedule", "in_policy"], "table"),
          file=sys.stderr)

    failures = 0
    for member in members:
        user = member.get("user", {})
        uid = user.get("id", "")
        name = user.get("summary", "")
        print(f"\n--- Processing {name} ---", file=sys.stderr)

        if sys.stdin.isatty():
            if input(f"Remove {name} from team? (y/N): ").strip().lower() != "y":
                print(f"Skipped {name}.", file=sys.stderr)
                continue
        else:  # dry-run on non-TTY: preview everyone
            print(f"[dry-run] previewing removal of {name}", file=sys.stderr)

        for schedule in user_schedules.get(uid, []):
            if not sys.stdin.isatty() or input(
                f"Remove {name} from schedule '{schedule['name']}'? (y/N): "
            ).strip().lower() == "y":
                remove_user_from_schedule(token, schedule["id"], uid, name, dry_run=dry_run)

        for policy in user_policies.get(uid, []):
            if not sys.stdin.isatty() or input(
                f"Remove {name} from escalation policy '{policy['name']}'? (y/N): "
            ).strip().lower() == "y":
                remove_user_from_policy(token, policy["id"], uid, name,
                                        policy=policy_details.get(policy["id"]),
                                        dry_run=dry_run)

        if dry_run:
            print(f"[dry-run] would DELETE /teams/{team_id}/members/{uid} for {name}",
                  file=sys.stderr)
            continue
        try:
            request(f"teams/{team_id}/members/{uid}", token, method="DELETE")
            log.info("Removed %s from team.", name)
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("Failed to remove %s from team: %s", name, e)
            failures += 1

    log.info("Done. All requested user removals processed (%d failures).", failures)
    return 1 if failures else 0
