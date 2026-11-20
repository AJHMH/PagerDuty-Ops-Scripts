"""Reconcile PagerDuty IDs stored in ServiceNow against PagerDuty (PagerDuty wins).

Subcommands:
  report  read-only cross-reference of sys_user_group / sys_user PagerDuty IDs
  plan    write a reviewed plan file of ServiceNow field corrections
  apply   apply a plan to an instance (production requires a rehearsal receipt)

Derivation per group is anchored on the group's PagerDuty *service* ID:
  escalation policy = service.escalation_policy           (exact)
  team              = service's team, else the EP's team  (must be exactly one)
  schedule          = schedule target of the EP's first rule (exactly one)
  webhook           = v3 webhook subscription filtered to the service (exactly one)
Zero or several candidates is reported as ambiguous and never written. Group
membership differences are reported only — never written. See docs/servicenow.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import date

from ..api import PDApiError
from ..cli import (
    EXIT_FAILURES,
    EXIT_OK,
    EXIT_USAGE,
    add_format_args,
    add_logging_args,
    add_token_args,
    confirm,
    finish_bulk,
    init,
)
from ..log import configure_logging, get_logger
from ..output import render_rows, write_payload
from ..pdstate import PDState, ref_ids
from ..plan import load_plan, new_plan, plan_hash, write_plan
from ..receipts import receipts_dir, require_receipt, write_receipt
from ..servicenow import connect, load_field_map

log = get_logger("sn_sync")

GROUP_TABLE = "sys_user_group"
USER_TABLE = "sys_user"
MEMBER_TABLE = "sys_user_grmember"
MSG_PREFIX = "[pd-sn-sync"
MSG_MAX_LEN = 1000
IN_CHUNK = 100
DEFAULT_MAX_WRITES = 25

GROUP_ID_KEYS = ("group_service", "group_escalation", "group_team", "group_schedule",
                 "group_webhook")
LABELS = {"group_service": "service", "group_escalation": "escalation_policy",
          "group_team": "team", "group_schedule": "schedule", "group_webhook": "webhook"}
REPORT_FIELDS = ["table", "key", "sys_id", "field", "current", "expected", "status", "detail"]
NON_ISSUE_STATUSES = {"ok", "update"}


# ---------------- parser ----------------

def _sn_args(p):
    p.add_argument("--instance", help="ServiceNow instance name (or SN_INSTANCE).")
    p.add_argument("--auth-method", choices=["okta", "sn-oauth", "basic"],
                   help="Override SN_AUTH_METHOD (default okta).")
    p.add_argument("--field-map", help="JSON file overriding x_pd_integration column names "
                   "(or SN_FIELD_MAP).")


def _filter_args(p):
    p.add_argument("--team-ids", help="Comma-separated PD team IDs: only groups mapped to them.")
    p.add_argument("--service-ids", help="Comma-separated PD service IDs: only those groups.")


def build_parser():
    p = argparse.ArgumentParser(
        description="Reconcile PagerDuty IDs stored in ServiceNow (PagerDuty wins).")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("report", help="Read-only cross-reference report.")
    add_token_args(r)
    add_logging_args(r)
    add_format_args(r)
    _sn_args(r)
    _filter_args(r)
    r.add_argument("--all", action="store_true", help="Include rows that are already in sync.")
    r.add_argument("--fail-on-drift", action="store_true",
                   help="Exit 1 when any finding other than 'ok' exists (for cron alerting).")

    pl = sub.add_parser("plan", help="Write a plan file of ServiceNow corrections.")
    add_token_args(pl)
    add_logging_args(pl)
    _sn_args(pl)
    _filter_args(pl)
    pl.add_argument("--plan-out", required=True, help="Path of the plan JSON to write.")
    pl.add_argument("--prune", action="store_true",
                    help="Also clear IDs that point at deleted/wrong-type PagerDuty objects.")

    a = sub.add_parser("apply", help="Apply a plan file to a ServiceNow instance.")
    add_logging_args(a)
    _sn_args(a)
    a.add_argument("plan_file", help="Plan JSON produced by 'pd-sn-sync plan'.")
    a.add_argument("--dry-run", action="store_true", help="Preview every change; no writes.")
    a.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt.")
    a.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                   help=f"Refuse plans needing more record writes (default {DEFAULT_MAX_WRITES}).")
    a.add_argument("--pause-seconds", type=float, default=0.0,
                   help="Sleep between record writes (throttles business-rule side effects).")
    a.add_argument("--receipts-dir", help="Rehearsal receipts directory "
                   "(default .pd_ops_receipts or PD_OPS_RECEIPTS_DIR).")
    return p


# ---------------- ServiceNow loading ----------------

def _chunks(seq, n=IN_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_sn_state(sn, fields: dict) -> dict:
    """Mapped groups, their memberships, mapped users, and unmapped members."""
    id_cols = [fields[k] for k in GROUP_ID_KEYS]
    query = "^OR".join(f"{c}ISNOTEMPTY" for c in id_cols)
    log.info("Fetching ServiceNow groups with PagerDuty IDs...")
    groups = sn.get_records(GROUP_TABLE, query=query,
                            fields=["sys_id", "name", "active", *id_cols, fields["group_error"]])
    log.info("Found %d mapped groups.", len(groups))

    members: dict[str, set[str]] = defaultdict(set)
    for chunk in _chunks(g["sys_id"] for g in groups):
        for m in sn.get_records(MEMBER_TABLE, query=f"groupIN{','.join(chunk)}",
                                fields=["group", "user"]):
            if m.get("group") and m.get("user"):
                members[m["group"]].add(m["user"])

    pd_col = fields["user_pd_id"]
    log.info("Fetching ServiceNow users with PagerDuty IDs...")
    mapped_users = sn.get_records(USER_TABLE, query=f"{pd_col}ISNOTEMPTY",
                                  fields=["sys_id", "user_name", "name", "active", pd_col])
    users = {u["sys_id"]: u for u in mapped_users}
    missing = {uid for s in members.values() for uid in s} - set(users)
    for chunk in _chunks(sorted(missing)):
        for u in sn.get_records(USER_TABLE, query=f"sys_idIN{','.join(chunk)}",
                                fields=["sys_id", "user_name", "name", "active", pd_col]):
            users[u["sys_id"]] = u
    return {"groups": groups, "members": dict(members), "users": users}


# ---------------- analysis (pure: PDState-like + dicts in, rows/items out) ----------------

def _single(candidates):
    uniq = sorted(set(candidates))
    return uniq[0] if len(uniq) == 1 else None, uniq


def derive_expected(pd, service_id: str) -> dict:
    """{key: (expected_id_or_None, candidates, reason)} for one anchoring service."""
    svc = pd.services.get(service_id)
    out = {}
    ep_id = ((svc or {}).get("escalation_policy") or {}).get("id")
    ep = pd.escalation_policies.get(ep_id) if ep_id else None
    out["group_escalation"] = (ep_id, [ep_id] if ep_id else [],
                               "" if ep_id else "service has no escalation policy")

    team, cands = _single(ref_ids((svc or {}).get("teams")))
    reason = ""
    if team is None and not cands and ep:
        team, cands = _single(ref_ids(ep.get("teams")))
    if team is None:
        reason = (f"{len(cands)} candidate teams" if cands
                  else "neither the service nor its EP belongs to a team")
    out["group_team"] = (team, cands, reason)

    rules = (ep or {}).get("escalation_rules") or []
    first_targets = rules[0].get("targets", []) if rules else []
    sched, scands = _single(t["id"] for t in first_targets
                            if t.get("type") in ("schedule_reference", "schedule") and t.get("id"))
    out["group_schedule"] = (sched, scands, "" if sched else
                             f"EP first rule has {len(scands)} schedule targets")

    hooks = [w["id"] for w in pd.webhooks
             if (w.get("filter") or {}).get("type") == "service_reference"
             and (w.get("filter") or {}).get("id") == service_id and w.get("id")]
    hook, hcands = _single(hooks)
    out["group_webhook"] = (hook, hcands, "" if hook else
                            f"{len(hcands)} webhook subscriptions filtered to the service")
    return out


def _exists(pd, key: str, object_id: str) -> bool:
    coll = {"group_service": pd.services, "group_escalation": pd.escalation_policies,
            "group_team": pd.teams, "group_schedule": pd.schedules}.get(key)
    if key == "group_webhook":
        return object_id in pd.webhook_ids
    return object_id in coll


def _row(table, key, sys_id, field, current, expected, status, detail=""):
    return {"table": table, "key": key, "sys_id": sys_id, "field": field,
            "current": current or "", "expected": expected or "", "status": status,
            "detail": detail}


def _item(table, key_field, key, sys_id, field, value, expected, detail):
    return {"op": "sn_set_field", "table": table, "key_field": key_field, "key": key,
            "sys_id": sys_id, "field": field, "value": value, "expected": expected or "",
            "detail": detail}


def _stale_detail(pd, object_id):
    kind = pd.kind_of(object_id)
    return f"ID is a {kind}, not the expected type" if kind else "not found in PagerDuty"


def _strip_msg_date(msg: str) -> str:
    return msg.split("] ", 1)[1] if msg.startswith(MSG_PREFIX) and "] " in msg else msg


def split_message(msg: str) -> tuple[str, str]:
    """(foreign, ours): text written by anyone else (e.g. the PagerDuty app) and this
    tool's own `[pd-sn-sync ...]` segment, which always comes last."""
    idx = msg.find(MSG_PREFIX)
    if idx < 0:
        return msg.strip(), ""
    return msg[:idx].strip(), msg[idx:].strip()


def compose_message(foreign: str, ours: str) -> str:
    """Foreign text is kept verbatim; only our segment is truncated to fit."""
    if not ours:
        return foreign
    room = MSG_MAX_LEN - (len(foreign) + 1 if foreign else 0)
    ours = ours[:max(room, len(MSG_PREFIX) + 14)]
    return f"{foreign}\n{ours}" if foreign else ours


def analyze(pd, sn_state: dict, fields: dict, *, prune: bool = False, today: str | None = None,
            team_filter: set | None = None, service_filter: set | None = None):
    """Return (report_rows, plan_items). Pure apart from cached PDState lookups."""
    today = today or date.today().isoformat()
    rows: list[dict] = []
    items: list[dict] = []
    svc_col = fields["group_service"]
    by_service: dict[str, list[str]] = defaultdict(list)
    for g in sn_state["groups"]:
        if g.get(svc_col):
            by_service[g[svc_col]].append(g["sys_id"])

    for g in sn_state["groups"]:
        name, sys_id = g.get("name") or g["sys_id"], g["sys_id"]
        current = {k: (g.get(fields[k]) or "").strip() for k in GROUP_ID_KEYS}
        service_id = current["group_service"]
        expected = derive_expected(pd, service_id) if service_id else {}
        if team_filter is not None:
            teams = {current["group_team"], (expected.get("group_team") or (None,))[0]}
            if not teams & team_filter:
                continue
        if service_filter is not None and service_id not in service_filter:
            continue

        issues: list[str] = []
        group_items: list[dict] = []

        def add(key, cur, exp, status, detail="", value=None, _name=name, _sys=sys_id,
                _items=group_items, _issues=issues):
            rows.append(_row(GROUP_TABLE, _name, _sys, fields[key], cur, exp, status, detail))
            if value is not None:
                _items.append(_item(GROUP_TABLE, "name", _name, _sys, fields[key], value,
                                    cur, f"{LABELS[key]}: {status} {detail}".strip()))
            if status not in NON_ISSUE_STATUSES and not (status == "stale" and value == ""):
                _issues.append(f"{LABELS[key]} {status}: {detail or cur}")

        if not service_id:
            set_keys = [k for k in GROUP_ID_KEYS if current[k]]
            add("group_service", "", "", "unanchored",
                f"no service ID but {', '.join(LABELS[k] for k in set_keys)} set")
        elif len(by_service[service_id]) > 1:
            add("group_service", service_id, "", "duplicate_service",
                f"{len(by_service[service_id])} groups share this service")
        elif service_id not in pd.services:
            add("group_service", service_id, "", "stale", _stale_detail(pd, service_id),
                value="" if prune else None)
            for k in GROUP_ID_KEYS[1:]:
                if current[k] and not _exists(pd, k, current[k]):
                    add(k, current[k], "", "stale", _stale_detail(pd, current[k]),
                        value="" if prune else None)
        else:
            add("group_service", service_id, service_id, "ok")
            for k in GROUP_ID_KEYS[1:]:
                exp, cands, reason = expected[k]
                cur = current[k]
                if exp:
                    if cur == exp:
                        add(k, cur, exp, "ok")
                    else:
                        detail = (_stale_detail(pd, cur) if cur and not _exists(pd, k, cur)
                                  else "differs from PagerDuty" if cur else "missing")
                        add(k, cur, exp, "update", detail, value=exp)
                elif cur and cur in cands:
                    add(k, cur, "", "ok", f"current is one of {len(cands)} candidates")
                elif cur and not _exists(pd, k, cur):
                    add(k, cur, "", "stale", _stale_detail(pd, cur), value="" if prune else None)
                elif not cur and not cands:
                    add(k, "", "", "ok", reason)
                else:
                    add(k, cur, "", "ambiguous", reason)

        # Error-message field: only our own `[pd-sn-sync` segment is ever added, replaced
        # or cleared; text written by the PagerDuty app (or anyone else) is preserved.
        err_col = fields["group_error"]
        cur_msg = (g.get(err_col) or "").strip()
        foreign, ours = split_message(cur_msg)
        new_ours = f"{MSG_PREFIX} {today}] " + "; ".join(issues) if issues else ""
        if _strip_msg_date(ours) != _strip_msg_date(new_ours):
            group_items.append(_item(
                GROUP_TABLE, "name", name, sys_id, err_col, compose_message(foreign, new_ours),
                cur_msg, "record reconciliation issues" if issues
                else "clear resolved reconciliation message"))
        items.extend(group_items)

        # Membership: report only. Compare against the team PagerDuty says the group should
        # have (the derived one) when it is unique; fall back to a valid stored team.
        derived_team = (expected.get("group_team") or (None,))[0]
        team_id = derived_team or (
            current["group_team"] if current["group_team"] in pd.teams else None)
        if team_id:
            pd_members = set(pd.team_members(team_id))
            pd_col = fields["user_pd_id"]
            sn_members = set()
            for user_sys in sorted(sn_state["members"].get(sys_id, ())):
                u = sn_state["users"].get(user_sys) or {"sys_id": user_sys}
                pid = (u.get(pd_col) or "").strip()
                if pid:
                    sn_members.add(pid)
                else:
                    rows.append(_row(USER_TABLE, u.get("user_name") or user_sys, user_sys,
                                     pd_col, "", "", "unmapped_user",
                                     f"member of group {name!r} with no PagerDuty ID"))
            for pid in sorted(pd_members - sn_members):
                rows.append(_row(MEMBER_TABLE, name, sys_id, "user", "", pid,
                                 "membership_only_in_pd", f"PD team {team_id} member"))
            for pid in sorted(sn_members - pd_members):
                rows.append(_row(MEMBER_TABLE, name, sys_id, "user", pid, "",
                                 "membership_only_in_sn", f"not on PD team {team_id}"))

    if team_filter is None and service_filter is None:
        pd_col = fields["user_pd_id"]
        for u in sn_state["users"].values():
            pid = (u.get(pd_col) or "").strip()
            if not pid:
                continue
            key = u.get("user_name") or u["sys_id"]
            if pid in pd.users:
                rows.append(_row(USER_TABLE, key, u["sys_id"], pd_col, pid, pid, "ok"))
            else:
                rows.append(_row(USER_TABLE, key, u["sys_id"], pd_col, pid, "", "stale",
                                 _stale_detail(pd, pid)))
                if prune:
                    items.append(_item(USER_TABLE, "user_name", key, u["sys_id"], pd_col, "",
                                       pid, "user: stale ID cleared (--prune)"))
    return rows, items


# ---------------- subcommands ----------------

def _split(value):
    return {v.strip() for v in value.split(",") if v.strip()} if value else None


def _analyze_from_args(args):
    token = init(args)
    fields = load_field_map(args.field_map)
    sn = connect(args.instance, auth_method=args.auth_method)
    sn_state = load_sn_state(sn, fields)
    pd = PDState(token)
    return analyze(pd, sn_state, fields, prune=getattr(args, "prune", False),
                   team_filter=_split(args.team_ids), service_filter=_split(args.service_ids)), sn


def report_main(args) -> int:
    (rows, _items), _sn = _analyze_from_args(args)
    issues = [r for r in rows if r["status"] != "ok"]
    shown = rows if args.all else issues
    write_payload(render_rows(shown, REPORT_FIELDS, args.format), args.output)
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    log.info("Statuses: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return EXIT_FAILURES if (args.fail_on_drift and issues) else EXIT_OK


def plan_main(args) -> int:
    (rows, items), sn = _analyze_from_args(args)
    plan = new_plan("sn-sync", items, generated_by="pd-sn-sync plan",
                    options={"prune": args.prune, "source_instance": sn.instance.name,
                             "team_ids": sorted(_split(args.team_ids) or []),
                             "service_ids": sorted(_split(args.service_ids) or [])})
    sha = write_plan(plan, args.plan_out)
    records = len({(i["table"], i["key"]) for i in items})
    log.info("Wrote plan %s: %d field changes across %d records (sha256 %s).",
             args.plan_out, len(items), records, sha)
    unresolved = sum(1 for r in rows if r["status"] not in ("ok", "update"))
    if unresolved:
        log.warning("%d findings need human attention; see 'pd-sn-sync report'.", unresolved)
    return EXIT_OK


def _resolve_targets(sn, items) -> dict:
    """{(table, key): record} looked up by natural key on the *target* instance."""
    by_table: dict[tuple, set] = defaultdict(set)
    for i in items:
        by_table[(i["table"], i["key_field"])].add(i["key"])
    found: dict[tuple, list] = defaultdict(list)
    for (table, key_field), keys in by_table.items():
        cols = sorted({"sys_id", key_field, *(i["field"] for i in items if i["table"] == table)})
        # Encoded queries use ',' inside IN lists and '^' between terms.
        plain = sorted(k for k in keys if "," not in k and "^" not in k)
        with_comma = sorted(k for k in keys if "," in k and "^" not in k)
        for k in sorted(k for k in keys if "^" in k):
            log.error("%s %r: '^' in the key cannot be queried safely; skipping.", table, k)
        for chunk in _chunks(plain):
            for rec in sn.get_records(table, query=f"{key_field}IN{','.join(chunk)}",
                                      fields=cols):
                found[(table, rec.get(key_field))].append(rec)
        for k in with_comma:
            for rec in sn.get_records(table, query=f"{key_field}={k}", fields=cols):
                found[(table, rec.get(key_field))].append(rec)
    return found


def apply_main(args) -> int:
    configure_logging(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file)
    plan = load_plan(args.plan_file, expected_kind="sn-sync")
    sha = plan_hash(plan)
    fields = load_field_map(args.field_map)
    allowed = {fields[k] for k in fields}
    bad = [i for i in plan["items"] if i.get("op") != "sn_set_field"
           or i.get("table") not in (GROUP_TABLE, USER_TABLE) or i.get("field") not in allowed]
    if bad:
        print(f"Error: plan contains {len(bad)} items outside the allowed ops/tables/fields "
              "(only x_pd_integration ID and error-message fields may be written).",
              file=sys.stderr)
        return EXIT_USAGE
    if args.max_writes < 1:
        print("Error: --max-writes must be >= 1.", file=sys.stderr)
        return EXIT_USAGE

    sn = connect(args.instance, for_write=True, auth_method=args.auth_method)
    inst = sn.instance
    rdir = receipts_dir(args.receipts_dir)
    if inst.is_prod:
        require_receipt(rdir, sha=sha, rehearsal_instances=inst.rehearsal_instances)
    strict = inst.is_prod
    log.info("Plan %s (sha256 %s) -> %s [%s]", args.plan_file, sha[:12], inst.name,
             "production" if inst.is_prod else "rehearsal" if inst.is_rehearsal else "non-prod")

    targets = _resolve_targets(sn, plan["items"])
    writes: dict[tuple, dict] = {}
    skipped = failed = 0
    for i in plan["items"]:
        recs = targets.get((i["table"], i["key"]), [])
        if len(recs) != 1:
            log.error("%s %r: %d matching records on %s; skipping.", i["table"], i["key"],
                      len(recs), inst.name)
            failed += 1
            continue
        rec = recs[0]
        live = (rec.get(i["field"]) or "").strip()
        if live == i["value"]:
            skipped += 1  # already converged
            continue
        if live != (i.get("expected") or ""):
            msg = (f"{i['table']} {i['key']!r} {i['field']}: live value {live!r} differs from "
                   f"planned-from value {i.get('expected')!r}")
            if strict:
                log.error("%s; refusing on production (re-plan).", msg)
                failed += 1
                continue
            log.warning("%s; writing anyway on non-production instance.", msg)
        writes.setdefault((i["table"], rec["sys_id"], i["key"]), {})[i["field"]] = i["value"]

    log.info("%d record writes needed, %d items already in sync, %d unresolvable.",
             len(writes), skipped, failed)
    if len(writes) > args.max_writes:
        print(f"Error: plan needs {len(writes)} record writes, above --max-writes "
              f"{args.max_writes}. Split the plan (--team-ids/--service-ids) or raise the cap "
              "after reviewing business-rule impact.", file=sys.stderr)
        return EXIT_USAGE
    if writes and not confirm(f"Write {len(writes)} records on {inst.name}?",
                              assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK

    succeeded = 0
    for n, ((table, sys_id, key), values) in enumerate(sorted(writes.items())):
        if args.dry_run:
            print(f"[dry-run] {table} {key!r} ({sys_id}): {values}", file=sys.stderr)
            succeeded += 1
            continue
        if n and args.pause_seconds > 0:
            time.sleep(args.pause_seconds)
        try:
            sn.patch_record(table, sys_id, values)
            log.info("Updated %s %r: %s", table, key, sorted(values))
            succeeded += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s %r: write failed - %s", table, key, e)
            failed += 1

    if inst.is_rehearsal and not args.dry_run and failed == 0:
        write_receipt(rdir, sha=sha, kind="sn-sync", instance=inst.name,
                      succeeded=succeeded, skipped=skipped)
    return finish_bulk(succeeded, failed, dry_run=args.dry_run, label="records")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"report": report_main, "plan": plan_main, "apply": apply_main}[args.command](args)
