"""Team reorganizations: export → plan → apply.

Inputs (either or both; they compile into ONE plan and must not conflict):
  --moves CSV       one change per row: action, kind, id, from_team, to_team, role
  --teams-yaml FILE authoritative per-team desired state (see docs/reorg.md)

Plan ops (also produced by `pd-validate --plan-out`):
  add_member, set_team_role, remove_member, set_account_role,
  move_resource (escalation_policy | schedule), remove_resource_team

Services cannot be moved directly: PagerDuty derives a service's team from its
escalation policy. Services listed in YAML are verified against the planned EP
ownership and the plan is refused if they would not end up on the team.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import yaml

from ..api import PDApiError, request
from ..bulkops import load_csv_rows
from ..cli import (
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
from ..risk import READ_ONLY, writes_with_dry_run

log = get_logger("reorg")

TEAM_ROLES = ("observer", "responder", "manager")
ACCOUNT_ROLES = ("admin", "limited_user", "observer", "read_only_user",
                 "read_only_limited_user", "restricted_access", "user")
CSV_ACTIONS = ("add-member", "remove-member", "move-member", "set-team-role",
               "set-account-role", "move-resource")
RESOURCE_KINDS = ("escalation_policy", "schedule")
OP_ORDER = {"add_member": 0, "set_team_role": 1, "set_account_role": 2, "move_resource": 3,
            "remove_resource_team": 4, "remove_member": 5}
YAML_TEAM_KEYS = {"name", "members", "escalation_policies", "schedules", "services"}


RISK = {
    "export": READ_ONLY,
    "plan": READ_ONLY,
    "apply": writes_with_dry_run(),
}


def build_parser():
    p = argparse.ArgumentParser(description="Plan and apply PagerDuty team reorganizations.")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", help="Write current team state as YAML.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output", help="YAML file (default stdout).")
    e.add_argument("--team-ids", help="Comma-separated team IDs (default all).")

    pl = sub.add_parser("plan", help="Compile moves CSV and/or teams YAML into a plan.")
    add_token_args(pl)
    add_logging_args(pl)
    pl.add_argument("--moves", help="Moves CSV: action, kind, id, from_team, to_team, role.")
    pl.add_argument("--teams-yaml", help="Authoritative per-team YAML.")
    pl.add_argument("--prune", action="store_true",
                    help="For teams in the YAML, remove members/resources not listed.")
    pl.add_argument("--plan-out", required=True, help="Plan JSON to write.")

    a = sub.add_parser("apply", help="Apply a pd-reorg plan.")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("plan_file")
    a.add_argument("--dry-run", action="store_true", help="Preview every change; no writes.")
    a.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt.")
    a.add_argument("--sn-plan-out", help="After applying, write a pd-sn-sync plan for the "
                   "ServiceNow groups of the affected teams (needs SN_* configuration).")
    a.add_argument("--instance", help="ServiceNow instance for --sn-plan-out.")
    return p


def _split(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


# ---------------- export ----------------

def export_state(pd: PDState, team_ids=None) -> dict:
    teams = {}
    for tid in sorted(team_ids or pd.teams):
        if tid not in pd.teams:
            print(f"Error: unknown team {tid}", file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
        members = {}
        for uid, role in sorted(pd.team_members(tid).items()):
            u = pd.users.get(uid) or {}
            members[uid] = {"team_role": role, "account_role": u.get("role", ""),
                            "name": u.get("name", "")}
        teams[tid] = {
            "name": pd.teams[tid].get("name", ""),
            "members": members,
            "escalation_policies": sorted(i for i, e in pd.escalation_policies.items()
                                          if tid in ref_ids(e.get("teams"))),
            "schedules": sorted(i for i, s in pd.schedules.items()
                                if tid in ref_ids(s.get("teams"))),
            "services": sorted(i for i, s in pd.services.items()
                               if tid in ref_ids(s.get("teams"))),
        }
    return {"teams": teams}


def export_main(args) -> int:
    pd = PDState(init(args))
    text = yaml.safe_dump(export_state(pd, _split(args.team_ids)), sort_keys=False,
                          allow_unicode=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        log.info("Wrote %s", args.output)
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ---------------- intents ----------------

class Intents:
    """Desired changes from all sources, with conflict detection."""

    def __init__(self):
        self.membership: dict[tuple, tuple] = {}   # (team, user) -> (role|None, source)
        self.account_role: dict[str, tuple] = {}   # user -> (role, source)
        self.resource: dict[tuple, tuple] = {}     # (kind, id, team) -> (True|False, source)
        self.moves: list[dict] = []                # explicit move-resource rows
        self.service_asserts: list[tuple] = []     # (service_id, team, source)
        self.conflicts: list[str] = []

    def _set(self, store, key, value, source, what):
        if key in store and store[key][0] != value:
            self.conflicts.append(f"{what}: {store[key][1]} wants {store[key][0]!r}, "
                                  f"{source} wants {value!r}")
            return
        store.setdefault(key, (value, source))

    def member(self, team, user, role, source):
        self._set(self.membership, (team, user), role, source, f"team {team} / user {user}")

    def acct(self, user, role, source):
        self._set(self.account_role, user, role, source, f"account role of {user}")

    def own(self, kind, rid, team, owned, source):
        self._set(self.resource, (kind, rid, team), owned, source, f"{kind} {rid} on {team}")


def _err(errors, msg):
    errors.append(msg)


def load_moves(path, intents: Intents, pd: PDState, errors: list):
    rows = load_csv_rows(path, {"action", "kind", "id", "from_team", "to_team", "role"})
    for r in rows:
        src = f"{path}:{r['_line']}"
        action, kind, oid = r["action"].lower(), r["kind"].lower(), r["id"]
        frm, to, role = r["from_team"], r["to_team"], r["role"].lower()
        if action not in CSV_ACTIONS:
            _err(errors, f"{src}: unknown action {action!r}")
            continue
        if not oid:
            _err(errors, f"{src}: id is required")
            continue
        for t in (frm, to):
            if t and t not in pd.teams:
                _err(errors, f"{src}: unknown team {t}")
        if action == "move-resource":
            if kind == "service":
                _err(errors, f"{src}: services follow their escalation policy; move EP "
                     f"{((pd.services.get(oid) or {}).get('escalation_policy') or {}).get('id')} "
                     "instead")
                continue
            if kind not in RESOURCE_KINDS or not to:
                _err(errors, f"{src}: move-resource needs kind in {RESOURCE_KINDS} and to_team")
                continue
            coll = pd.escalation_policies if kind == "escalation_policy" else pd.schedules
            if oid not in coll:
                _err(errors, f"{src}: unknown {kind} {oid}")
                continue
            intents.own(kind, oid, to, True, src)
            if frm:
                intents.own(kind, oid, frm, False, src)
            continue
        if kind not in ("user", ""):
            _err(errors, f"{src}: {action} needs kind 'user'")
            continue
        if oid not in pd.users:
            _err(errors, f"{src}: unknown user {oid}")
            continue
        if action == "set-account-role":
            if role not in ACCOUNT_ROLES:
                _err(errors, f"{src}: account role must be one of {ACCOUNT_ROLES}")
                continue
            intents.acct(oid, role, src)
            continue
        if role and role not in TEAM_ROLES:
            _err(errors, f"{src}: team role must be one of {TEAM_ROLES}")
            continue
        if action == "remove-member":
            if not frm:
                _err(errors, f"{src}: remove-member needs from_team")
                continue
            intents.member(frm, oid, None, src)
        elif action in ("add-member", "set-team-role"):
            if not to:
                _err(errors, f"{src}: {action} needs to_team")
                continue
            intents.member(to, oid, role or "responder", src)
        elif action == "move-member":
            if not (frm and to):
                _err(errors, f"{src}: move-member needs from_team and to_team")
                continue
            current = pd.team_members(frm).get(oid)
            intents.member(frm, oid, None, src)
            intents.member(to, oid, role or current or "responder", src)


def load_teams_yaml(path, intents: Intents, pd: PDState, errors: list, prune: bool):
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    teams = doc.get("teams") if isinstance(doc, dict) else None
    if not isinstance(teams, dict):
        print(f"Error: {path}: top-level 'teams' mapping is required", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    for tid, spec in teams.items():
        src = f"{path}:teams.{tid}"
        if tid not in pd.teams:
            _err(errors, f"{src}: unknown team")
            continue
        spec = spec or {}
        bad = set(spec) - YAML_TEAM_KEYS
        if bad:
            _err(errors, f"{src}: unknown keys {sorted(bad)}")
            continue
        if "members" in spec:
            listed = set()
            for uid, m in (spec.get("members") or {}).items():
                m = {"team_role": m} if isinstance(m, str) else (m or {})
                if uid not in pd.users:
                    _err(errors, f"{src}.members.{uid}: unknown user")
                    continue
                role = (m.get("team_role") or "responder").lower()
                if role not in TEAM_ROLES:
                    _err(errors, f"{src}.members.{uid}: team_role must be one of {TEAM_ROLES}")
                    continue
                listed.add(uid)
                intents.member(tid, uid, role, src)
                if m.get("account_role"):
                    acct = str(m["account_role"]).lower()
                    current_acct = (pd.users.get(uid) or {}).get("role")
                    if acct == "owner" and current_acct == "owner":
                        pass  # unchanged owner (e.g. from pd-reorg export): no-op
                    elif acct not in ACCOUNT_ROLES:
                        _err(errors, f"{src}.members.{uid}: account_role must be one of "
                             f"{ACCOUNT_ROLES}")
                    else:
                        intents.acct(uid, acct, src)
            if prune:
                for uid in pd.team_members(tid):
                    if uid not in listed:
                        intents.member(tid, uid, None, f"{src} (--prune)")
        for key, kind, coll in (("escalation_policies", "escalation_policy",
                                 pd.escalation_policies), ("schedules", "schedule",
                                                           pd.schedules)):
            if key not in spec:
                continue
            listed = set(spec.get(key) or [])
            for rid in sorted(listed):
                if rid not in coll:
                    _err(errors, f"{src}.{key}: unknown {kind} {rid}")
                    continue
                intents.own(kind, rid, tid, True, src)
            if prune:
                for rid, obj in coll.items():
                    if tid in ref_ids(obj.get("teams")) and rid not in listed:
                        intents.own(kind, rid, tid, False, f"{src} (--prune)")
        for sid in spec.get("services") or []:
            if sid not in pd.services:
                _err(errors, f"{src}.services: unknown service {sid}")
                continue
            intents.service_asserts.append((sid, tid, src))


# ---------------- compile ----------------

def compile_plan(pd: PDState, intents: Intents, errors: list) -> list[dict]:
    items: list[dict] = []
    for (tid, uid), (role, src) in sorted(intents.membership.items()):
        current = pd.team_members(tid).get(uid)
        uname = (pd.users.get(uid) or {}).get("name", uid)
        if role is None:
            if current is None:
                continue
            on = [s.get("name") for s in pd.schedules.values()
                  if tid in ref_ids(s.get("teams")) and uid in ref_ids(s.get("users"))]
            warn = f"; still on team schedule(s): {', '.join(on)}" if on else ""
            if warn:
                log.warning("%s is still on %s schedules: %s (use pd-remove-team-members to "
                            "clear rotations)", uname, tid, ", ".join(on))
            items.append({"op": "remove_member", "team_id": tid, "user_id": uid,
                          "detail": f"{uname} from {tid} ({src}){warn}"})
        elif current is None:
            items.append({"op": "add_member", "team_id": tid, "user_id": uid, "role": role,
                          "detail": f"{uname} to {tid} as {role} ({src})"})
        elif current != role:
            items.append({"op": "set_team_role", "team_id": tid, "user_id": uid, "role": role,
                          "detail": f"{uname} on {tid}: {current} -> {role} ({src})"})
    for uid, (role, src) in sorted(intents.account_role.items()):
        current = (pd.users.get(uid) or {}).get("role")
        if current == "owner":
            _err(errors, f"{src}: refusing to change the account owner's role")
        elif current != role:
            items.append({"op": "set_account_role", "user_id": uid, "role": role,
                          "detail": f"{(pd.users.get(uid) or {}).get('name', uid)}: "
                                    f"{current} -> {role} ({src})"})

    final_teams: dict[tuple, set] = {}
    by_resource: dict[tuple, dict] = defaultdict(dict)
    for (kind, rid, tid), (owned, src) in intents.resource.items():
        by_resource[(kind, rid)][tid] = (owned, src)
    for (kind, rid), wanted in sorted(by_resource.items()):
        coll = pd.escalation_policies if kind == "escalation_policy" else pd.schedules
        current = set(ref_ids(coll[rid].get("teams")))
        adds = sorted(t for t, (o, _s) in wanted.items() if o and t not in current)
        removes = sorted(t for t, (o, _s) in wanted.items() if not o and t in current)
        name = coll[rid].get("name", rid)
        srcs = sorted({s for _o, s in wanted.values()})
        for i, to in enumerate(adds):
            frm = removes[i] if i < len(removes) and len(adds) == len(removes) else ""
            items.append({"op": "move_resource", "kind": kind, "id": rid, "from_team": frm,
                          "to_team": to, "detail": f"{kind} {name!r} -> {to}"
                          + (f" (from {frm})" if frm else "") + f" ({', '.join(srcs)})"})
        paired = set(removes[:len(adds)]) if len(adds) == len(removes) else set()
        for t in removes:
            if t not in paired:
                items.append({"op": "remove_resource_team", "kind": kind, "id": rid,
                              "team_id": t, "detail": f"{kind} {name!r} off {t} "
                                                      f"({', '.join(srcs)})"})
        final_teams[(kind, rid)] = (current | set(adds)) - set(removes)

    for sid, tid, src in intents.service_asserts:
        ep_id = (pd.services[sid].get("escalation_policy") or {}).get("id")
        ep_teams = final_teams.get(("escalation_policy", ep_id),
                                   set(ref_ids((pd.escalation_policies.get(ep_id) or {})
                                               .get("teams"))))
        if tid not in ep_teams:
            _err(errors, f"{src}: service {sid} follows escalation policy {ep_id}, which would "
                 f"not belong to {tid}; list {ep_id} under escalation_policies")
    items.sort(key=lambda i: OP_ORDER[i["op"]])
    return items


def plan_main(args) -> int:
    if not (args.moves or args.teams_yaml):
        print("Error: pass --moves and/or --teams-yaml.", file=sys.stderr)
        return EXIT_USAGE
    if args.prune and not args.teams_yaml:
        print("Error: --prune applies only to --teams-yaml.", file=sys.stderr)
        return EXIT_USAGE
    pd = PDState(init(args))
    intents, errors = Intents(), []
    if args.moves:
        load_moves(args.moves, intents, pd, errors)
    if args.teams_yaml:
        load_teams_yaml(args.teams_yaml, intents, pd, errors, args.prune)
    items = compile_plan(pd, intents, errors) if not (errors or intents.conflicts) else []
    if intents.conflicts or errors:
        for c in intents.conflicts:
            print(f"Conflict: {c}", file=sys.stderr)
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        print(f"Plan refused: {len(intents.conflicts)} conflicts, {len(errors)} errors.",
              file=sys.stderr)
        return EXIT_USAGE
    plan = new_plan("pd-reorg", items, generated_by="pd-reorg plan",
                    options={"moves": args.moves, "teams_yaml": args.teams_yaml,
                             "prune": args.prune})
    sha = write_plan(plan, args.plan_out)
    counts = defaultdict(int)
    for i in items:
        counts[i["op"]] += 1
    log.info("Wrote %s (sha256 %s): %s", args.plan_out, sha[:12],
             ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes")
    for i in items:
        print(f"{i['op']:<22} {i.get('detail', '')}", file=sys.stderr)
    return EXIT_OK


# ---------------- apply ----------------

def _set_schedule_teams(token, sid, add=(), remove=()):
    sched = request(f"schedules/{sid}", token).get("schedule") or {}
    teams = [t for t in ref_ids(sched.get("teams")) if t not in set(remove)]
    teams += [t for t in add if t not in teams]
    body = {k: sched[k] for k in ("name", "time_zone", "description", "schedule_layers")
            if k in sched}
    body.update({"type": "schedule", "teams": [{"id": t, "type": "team_reference"}
                                              for t in teams]})
    request(f"schedules/{sid}", token, method="PUT", data={"schedule": body})


def _delete_ok_if_gone(endpoint, token):
    try:
        request(endpoint, token, method="DELETE")
    except PDApiError as e:
        if e.status_code != 404:
            raise


def apply_item(token, item):
    op = item["op"]
    if op in ("add_member", "set_team_role"):
        request(f"teams/{item['team_id']}/users/{item['user_id']}", token, method="PUT",
                data={"role": item["role"]})
    elif op == "remove_member":
        _delete_ok_if_gone(f"teams/{item['team_id']}/users/{item['user_id']}", token)
    elif op == "set_account_role":
        request(f"users/{item['user_id']}", token, method="PUT",
                data={"user": {"type": "user", "role": item["role"]}})
    elif op == "move_resource" and item["kind"] == "escalation_policy":
        request(f"teams/{item['to_team']}/escalation_policies/{item['id']}", token, method="PUT")
        if item.get("from_team"):
            _delete_ok_if_gone(f"teams/{item['from_team']}/escalation_policies/{item['id']}",
                               token)
    elif op == "move_resource" and item["kind"] == "schedule":
        _set_schedule_teams(token, item["id"], add=[item["to_team"]],
                            remove=[item["from_team"]] if item.get("from_team") else [])
    elif op == "remove_resource_team" and item["kind"] == "escalation_policy":
        _delete_ok_if_gone(f"teams/{item['team_id']}/escalation_policies/{item['id']}", token)
    elif op == "remove_resource_team" and item["kind"] == "schedule":
        _set_schedule_teams(token, item["id"], remove=[item["team_id"]])
    else:
        raise ValueError(f"unsupported op {op!r}")


REQUIRED = {"add_member": ("team_id", "user_id", "role"), "set_team_role": ("team_id",
            "user_id", "role"), "remove_member": ("team_id", "user_id"),
            "set_account_role": ("user_id", "role"), "move_resource": ("kind", "id", "to_team"),
            "remove_resource_team": ("kind", "id", "team_id")}


def validate_items(items) -> list[str]:
    problems = []
    for n, i in enumerate(items):
        op = i.get("op")
        if op not in REQUIRED:
            problems.append(f"item {n}: unknown op {op!r}")
            continue
        missing = [k for k in REQUIRED[op] if not i.get(k)]
        if missing:
            problems.append(f"item {n} ({op}): missing {missing}")
        if op in ("add_member", "set_team_role") and i.get("role") not in TEAM_ROLES:
            problems.append(f"item {n}: bad team role {i.get('role')!r}")
        if op == "set_account_role" and i.get("role") not in ACCOUNT_ROLES:
            problems.append(f"item {n}: bad account role {i.get('role')!r}")
        if op in ("move_resource", "remove_resource_team") and i.get("kind") not in \
                RESOURCE_KINDS:
            problems.append(f"item {n}: bad kind {i.get('kind')!r}")
    return problems


def affected_teams(items) -> set:
    out = set()
    for i in items:
        out.update(t for t in (i.get("team_id"), i.get("to_team"), i.get("from_team")) if t)
    return out


def apply_main(args) -> int:
    token = init(args)
    plan = load_plan(args.plan_file, expected_kind="pd-reorg")
    items = sorted(plan["items"], key=lambda i: OP_ORDER.get(i.get("op"), 99))
    problems = validate_items(items)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return EXIT_USAGE
    log.info("Plan %s (sha256 %s): %d items.", args.plan_file, plan_hash(plan)[:12], len(items))
    if not items:
        return EXIT_OK
    if not confirm(f"Apply {len(items)} reorg changes?", assume_yes=args.yes,
                   dry_run=args.dry_run):
        return EXIT_OK
    succeeded = failed = 0
    add_failed_users: set[str] = set()
    for item in items:
        if args.dry_run:
            print(f"[dry-run] {item['op']}: {item.get('detail', '')}", file=sys.stderr)
            succeeded += 1
            continue
        if item["op"] == "remove_member" and item["user_id"] in add_failed_users:
            # A failed add for this user (e.g. the destination half of a move) must not be
            # followed by the removal, or the user would end up on neither team.
            log.error("skipping remove_member (%s): an earlier add/role change for this user "
                      "failed; re-run after fixing it", item.get("detail", ""))
            failed += 1
            continue
        try:
            apply_item(token, item)
            log.info("%s: %s", item["op"], item.get("detail", ""))
            succeeded += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s failed (%s): %s", item["op"], item.get("detail", ""), e)
            if item["op"] in ("add_member", "set_team_role"):
                add_failed_users.add(item["user_id"])
            failed += 1
    code = finish_bulk(succeeded, failed, dry_run=args.dry_run, label="changes")
    if args.sn_plan_out and not args.dry_run:
        from ..servicenow import connect, load_field_map
        from .sn_sync import analyze, load_sn_state

        fields = load_field_map()
        sn = connect(args.instance)
        teams = affected_teams(items)
        _rows, sn_items = analyze(PDState(token), load_sn_state(sn, fields), fields,
                                  team_filter=teams)
        sha = write_plan(new_plan("sn-sync", sn_items, generated_by="pd-reorg apply",
                                  options={"source_instance": sn.instance.name,
                                           "team_ids": sorted(teams)}), args.sn_plan_out)
        log.info("Wrote ServiceNow follow-up plan %s (%d changes, sha256 %s). Rehearse it with "
                 "'pd-sn-sync apply'.", args.sn_plan_out, len(sn_items), sha[:12])
    return code


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"export": export_main, "plan": plan_main, "apply": apply_main}[args.command](args)
