"""Account consistency validator — read-only.

Runs configurable checks across teams, users, services, escalation policies and
schedules (plus ServiceNow cross-references with --servicenow), reports findings
with severities, exits non-zero at or above --fail-on, and can write a
remediation plan (kind "pd-reorg") for `pd-reorg apply`. It never writes.

Checks (ids usable in --checks and the config file):
  user_not_on_owning_team   1  user on a team's schedule/EP but not a member of that team
  service_ep_team_mismatch  2  service EP has no/several teams, or differs from its schedules'
  ep_coverage_gaps          3  empty EP rules, empty schedules, on-call gaps
  unreachable_responders    4  on-call users missing, without contact methods / high-urgency rules
  team_staffing             5  teams with no manager or a single member
  user_utilization          6  users on no team; responder-capable users on no rotation
  orphaned_resources        7  unowned or unreferenced schedules/EPs/services
  naming_conventions        8  names not matching configured regexes
  servicenow_integration    9  services lacking the ServiceNow extension / group mapping
  servicenow_drift         10  ServiceNow ID drift (pd-sn-sync analysis)
"""

from __future__ import annotations

import json
import re
import sys

from ..cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, init, standard_parser
from ..log import get_logger
from ..output import render_rows, write_payload
from ..pdstate import PDState, ref_ids
from ..plan import new_plan, write_plan
from ..risk import READ_ONLY

log = get_logger("validate")

SEVERITIES = ("info", "warning", "error")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CHECKS = {
    "user_not_on_owning_team": "warning",
    "service_ep_team_mismatch": "warning",
    "ep_coverage_gaps": "error",
    "unreachable_responders": "error",
    "team_staffing": "warning",
    "user_utilization": "info",
    "orphaned_resources": "warning",
    "naming_conventions": "info",
    "servicenow_integration": "warning",
    "servicenow_drift": "warning",
}
SN_CHECKS = {"servicenow_drift"}
NON_RESPONDER_ROLES = {"observer", "read_only_user", "read_only_limited_user",
                       "restricted_access"}
NAMING_RESOURCES = ("teams", "services", "escalation_policies", "schedules")
FIELDS = ["check", "severity", "resource_type", "resource_id", "resource_name", "message",
          "remediation"]
DEFAULT_CONFIG = {
    "checks": {},                       # {check_id: {"enabled": bool, "severity": str}}
    "coverage_days": 14,
    "servicenow_extension_pattern": "servicenow",
    "naming": {},                       # {resource: regex}
    "default_member_role": "responder",
}


RISK = READ_ONLY


def build_parser():
    p = standard_parser("Validate PagerDuty account consistency (read-only).",
                        formats=("table", "csv", "json"))
    p.add_argument("--config", help="JSON config (enable/disable checks, severities, naming).")
    p.add_argument("--checks", help="Comma-separated check ids to run (default: all enabled).")
    p.add_argument("--fail-on", choices=[*SEVERITIES, "never"], default="error",
                   help="Exit 1 if any finding is at or above this severity (default error).")
    p.add_argument("--plan-out", help="Write a remediation plan for 'pd-reorg apply'.")
    p.add_argument("--servicenow", action="store_true",
                   help="Also run ServiceNow checks (needs SN_* configuration).")
    p.add_argument("--instance", help="ServiceNow instance (with --servicenow).")
    p.add_argument("--auth-method", choices=["okta", "sn-oauth", "basic"])
    p.add_argument("--field-map", help="ServiceNow column-name override JSON.")
    return p


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not path:
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error: cannot read config {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    unknown = set(user) - set(DEFAULT_CONFIG)
    bad_checks = set(user.get("checks", {})) - set(CHECKS)
    bad_naming = set(user.get("naming", {})) - set(NAMING_RESOURCES)
    problems = []
    if unknown:
        problems.append(f"unknown keys {sorted(unknown)}")
    if bad_checks:
        problems.append(f"unknown checks {sorted(bad_checks)}")
    if bad_naming:
        problems.append(f"naming supports only {list(NAMING_RESOURCES)}")
    for cid, c in user.get("checks", {}).items():
        if c.get("severity") and c["severity"] not in SEVERITIES:
            problems.append(f"{cid}: severity must be one of {SEVERITIES}")
    for res, rx in user.get("naming", {}).items():
        try:
            re.compile(rx)
        except re.error as e:
            problems.append(f"naming.{res}: invalid regex ({e})")
    if problems:
        print(f"Error: {path}: {'; '.join(problems)}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    cfg.update({k: v for k, v in user.items() if k != "checks"})
    cfg["checks"] = user.get("checks", {})
    return cfg


class Validator:
    def __init__(self, pd: PDState, cfg: dict):
        self.pd = pd
        self.cfg = cfg
        self.findings: list[dict] = []
        self.plan_items: list[dict] = []
        self._plan_keys: set = set()
        self._current = ""

    # -------- helpers --------
    def finding(self, severity, rtype, rid, name, message, remediation=""):
        override = (self.cfg["checks"].get(self._current) or {}).get("severity")
        self.findings.append({
            "check": self._current, "severity": override or severity, "resource_type": rtype,
            "resource_id": rid, "resource_name": name or "", "message": message,
            "remediation": remediation,
        })

    def plan(self, item: dict):
        key = json.dumps({k: v for k, v in item.items() if k != "detail"}, sort_keys=True)
        if key not in self._plan_keys:
            self._plan_keys.add(key)
            self.plan_items.append(item)

    def _name(self, coll, oid):
        return (coll.get(oid) or {}).get("name") or (coll.get(oid) or {}).get("summary") or ""

    def _schedule_user_ids(self, schedule) -> list[str]:
        return ref_ids(schedule.get("users"))

    def _ep_targets(self, ep, kind):
        types = {"user": ("user_reference", "user"), "schedule": ("schedule_reference",
                                                                    "schedule")}[kind]
        return [t["id"] for r in ep.get("escalation_rules") or []
                for t in r.get("targets") or [] if t.get("type") in types and t.get("id")]

    # -------- checks --------
    def user_not_on_owning_team(self):
        pd = self.pd
        role = self.cfg["default_member_role"]
        owned = [("schedule", s, ref_ids(s.get("teams")), self._schedule_user_ids(s))
                 for s in pd.schedules.values()]
        owned += [("escalation_policy", e, ref_ids(e.get("teams")), self._ep_targets(e, "user"))
                  for e in pd.escalation_policies.values()]
        for rtype, res, team_ids, user_ids in owned:
            if not team_ids:
                continue
            for uid in sorted(set(user_ids)):
                if pd.user_team_ids.get(uid, set()) & set(team_ids):
                    continue
                uname = self._name(pd.users, uid) or uid
                if len(team_ids) == 1:
                    tid = team_ids[0]
                    self.plan({"op": "add_member", "team_id": tid, "user_id": uid, "role": role,
                               "detail": f"{uname} is on {rtype} {res.get('name')!r}"})
                    fix = f"add to team {self._name(pd.teams, tid) or tid} as {role} (plan)"
                else:
                    fix = "add to one of the owning teams, or remove from the rotation"
                self.finding("warning", rtype, res["id"], res.get("name"),
                             f"user {uname} ({uid}) is not a member of any owning team "
                             f"({', '.join(team_ids)})", fix)

    def service_ep_team_mismatch(self):
        pd = self.pd
        for svc in pd.services.values():
            ep_id = (svc.get("escalation_policy") or {}).get("id")
            ep = pd.escalation_policies.get(ep_id)
            if not ep:
                self.finding("error", "service", svc["id"], svc.get("name"),
                             f"escalation policy {ep_id or '(none)'} not found")
                continue
            ep_teams = ref_ids(ep.get("teams"))
            if len(ep_teams) != 1:
                # Only plan a move when EVERY referenced schedule exists and is owned by
                # exactly one team, and they all agree; anything unknown stays a finding.
                sched_ids = self._ep_targets(ep, "schedule")
                owners = [ref_ids((pd.schedules.get(sid) or {}).get("teams"))
                          if sid in pd.schedules else [] for sid in sched_ids]
                sched_teams = {o[0] for o in owners if len(o) == 1}
                all_known = bool(sched_ids) and all(len(o) == 1 for o in owners)
                fix = ""
                if not ep_teams and all_known and len(sched_teams) == 1:
                    (tid,) = sched_teams
                    self.plan({"op": "move_resource", "kind": "escalation_policy",
                               "id": ep_id, "from_team": "", "to_team": tid,
                               "detail": f"EP of service {svc.get('name')!r}; its schedules "
                                         f"belong to {tid}"})
                    fix = f"assign EP to team {self._name(pd.teams, tid) or tid} (plan)"
                self.finding("warning", "service", svc["id"], svc.get("name"),
                             f"escalation policy {ep.get('name')!r} belongs to "
                             f"{len(ep_teams)} teams", fix)
                continue
            for sid in self._ep_targets(ep, "schedule"):
                s_teams = ref_ids((pd.schedules.get(sid) or {}).get("teams"))
                if s_teams and ep_teams[0] not in s_teams:
                    self.finding("warning", "escalation_policy", ep_id, ep.get("name"),
                                 f"targets schedule {self._name(pd.schedules, sid) or sid} "
                                 f"owned by other team(s) {', '.join(s_teams)}")

    def ep_coverage_gaps(self):
        pd = self.pd
        days = int(self.cfg["coverage_days"])
        used_schedules = set()
        for ep in pd.escalation_policies.values():
            rules = ep.get("escalation_rules") or []
            if not rules:
                self.finding("error", "escalation_policy", ep["id"], ep.get("name"),
                             "has no escalation rules")
            for n, rule in enumerate(rules, start=1):
                if not rule.get("targets"):
                    self.finding("error", "escalation_policy", ep["id"], ep.get("name"),
                                 f"rule {n} has no targets")
            used_schedules.update(self._ep_targets(ep, "schedule"))
        for sid in sorted(used_schedules):
            sched = pd.schedules.get(sid)
            if not sched:
                continue
            if not self._schedule_user_ids(sched):
                self.finding("error", "schedule", sid, sched.get("name"), "schedule has no users")
                continue
            detail = pd.schedule_detail(sid, days=days)
            if detail is None:
                self.finding("error", "schedule", sid, sched.get("name"),
                             "on-call coverage could not be retrieved (API error); "
                             "coverage was not checked")
                continue
            pct = (detail.get("final_schedule") or {}).get("rendered_coverage_percentage")
            if pct is not None and float(pct) < 100:
                self.finding("warning", "schedule", sid, sched.get("name"),
                             f"on-call coverage {float(pct):.1f}% over the next {days} days")

    def unreachable_responders(self):
        pd = self.pd
        refs: dict[str, list[str]] = {}
        for s in pd.schedules.values():
            for uid in self._schedule_user_ids(s):
                refs.setdefault(uid, []).append(f"schedule {s.get('name')}")
        for e in pd.escalation_policies.values():
            for uid in self._ep_targets(e, "user"):
                refs.setdefault(uid, []).append(f"EP {e.get('name')}")
        for uid, where in sorted(refs.items()):
            user = pd.users.get(uid)
            used = "; ".join(sorted(set(where))[:3])
            if not user:
                self.finding("error", "user", uid, "", f"referenced user not found ({used})")
                continue
            name = user.get("name")
            if not user.get("contact_methods"):
                self.finding("error", "user", uid, name, f"no contact methods ({used})")
            rules = user.get("notification_rules") or []
            if not any(r.get("urgency") in ("high", "any") for r in rules):
                self.finding("error", "user", uid, name,
                             f"no high-urgency notification rule ({used})")

    def team_staffing(self):
        pd = self.pd
        for tid, team in sorted(pd.teams.items()):
            members = pd.team_members(tid)
            if not members:
                self.finding("warning", "team", tid, team.get("name"), "team has no members")
                continue
            if "manager" not in members.values():
                self.finding("warning", "team", tid, team.get("name"), "team has no manager")
            if len(members) == 1:
                self.finding("warning", "team", tid, team.get("name"),
                             "single member (single point of failure)")

    def user_utilization(self):
        pd = self.pd
        on_rotation = {uid for s in pd.schedules.values() for uid in self._schedule_user_ids(s)}
        on_rotation |= {uid for e in pd.escalation_policies.values()
                        for uid in self._ep_targets(e, "user")}
        for uid, user in sorted(pd.users.items()):
            if not pd.user_team_ids.get(uid):
                self.finding("info", "user", uid, user.get("name"), "user belongs to no team")
            role = user.get("role") or ""
            if role and role not in NON_RESPONDER_ROLES and role != "owner" \
                    and uid not in on_rotation:
                self.finding("info", "user", uid, user.get("name"),
                             f"role {role!r} but on no schedule or escalation policy "
                             "(license may be unneeded)")

    def orphaned_resources(self):
        pd = self.pd
        ep_schedules = {sid for e in pd.escalation_policies.values()
                        for sid in self._ep_targets(e, "schedule")}
        used_eps = {(s.get("escalation_policy") or {}).get("id") for s in pd.services.values()}
        for rtype, coll in (("schedule", pd.schedules), ("escalation_policy",
                            pd.escalation_policies), ("service", pd.services)):
            for oid, obj in sorted(coll.items()):
                if not ref_ids(obj.get("teams")):
                    self.finding("warning", rtype, oid, obj.get("name"), "owned by no team")
                if rtype == "schedule" and oid not in ep_schedules:
                    self.finding("info", rtype, oid, obj.get("name"),
                                 "not used by any escalation policy")
                if rtype == "escalation_policy" and oid not in used_eps:
                    self.finding("info", rtype, oid, obj.get("name"), "not used by any service")

    def naming_conventions(self):
        rules = self.cfg.get("naming") or {}
        if not rules:
            log.info("naming_conventions: no regexes configured; skipped.")
        colls = {"teams": self.pd.teams, "services": self.pd.services,
                 "escalation_policies": self.pd.escalation_policies,
                 "schedules": self.pd.schedules}
        for res, rx in rules.items():
            pattern = re.compile(rx)
            for oid, obj in sorted(colls[res].items()):
                if not pattern.search(obj.get("name") or ""):
                    self.finding("info", res.rstrip("s"), oid, obj.get("name"),
                                 f"name does not match {rx!r}",
                                 "pd-rename-resources can add a prefix/suffix")

    def servicenow_integration(self, sn_groups=None, fields=None):
        pattern = re.compile(self.cfg["servicenow_extension_pattern"], re.IGNORECASE)
        with_ext = set()
        for ext in self.pd.extensions:
            schema = ext.get("extension_schema") or {}
            label = " ".join(str(schema.get(k) or "") for k in ("summary", "label", "key"))
            if pattern.search(label) or pattern.search(ext.get("name") or ""):
                with_ext.update(ref_ids(ext.get("extension_objects")))
        mapped = None
        if sn_groups is not None:
            mapped = {(g.get(fields["group_service"]) or "").strip() for g in sn_groups}
        for sid, svc in sorted(self.pd.services.items()):
            if sid not in with_ext:
                self.finding("warning", "service", sid, svc.get("name"),
                             "no ServiceNow extension attached",
                             "pd-bulk-extensions can attach it")
            if mapped is not None and sid not in mapped:
                self.finding("warning", "service", sid, svc.get("name"),
                             "no ServiceNow assignment group references this service")

    def servicenow_drift(self, sn_state=None, fields=None):
        from .sn_sync import analyze

        rows, _items = analyze(self.pd, sn_state, fields)
        for r in rows:
            if r["status"] == "ok":
                continue
            sev = "warning" if r["status"] in ("update", "stale", "duplicate_service") else "info"
            self.finding(sev, r["table"], r["sys_id"], r["key"],
                         f"{r['field']}: {r['status']} {r['detail']}".strip(),
                         "pd-sn-sync plan / apply" if r["status"] in ("update", "stale") else "")

    # -------- driver --------
    def run(self, check_ids, sn_state=None, fields=None):
        for cid in check_ids:
            self._current = cid
            log.info("Running %s...", cid)
            if cid == "servicenow_integration":
                self.servicenow_integration(sn_state["groups"] if sn_state else None, fields)
            elif cid == "servicenow_drift":
                self.servicenow_drift(sn_state, fields)
            else:
                getattr(self, cid)()
        return self.findings


def select_checks(cfg, cli_checks, servicenow: bool) -> list[str]:
    if cli_checks:
        chosen = [c.strip() for c in cli_checks.split(",") if c.strip()]
        unknown = set(chosen) - set(CHECKS)
        if unknown:
            print(f"Error: unknown checks {sorted(unknown)}; valid: {list(CHECKS)}",
                  file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
    else:
        chosen = [c for c in CHECKS if (cfg["checks"].get(c) or {}).get("enabled", True)]
    if not servicenow:
        skipped = [c for c in chosen if c in SN_CHECKS]
        if skipped:
            log.info("Skipping %s (pass --servicenow to enable).", ", ".join(skipped))
        chosen = [c for c in chosen if c not in SN_CHECKS]
    return chosen


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = init(args)
    cfg = load_config(args.config)
    checks = select_checks(cfg, args.checks, args.servicenow)

    sn_state = fields = None
    if args.servicenow:
        from ..servicenow import connect, load_field_map
        from .sn_sync import load_sn_state

        fields = load_field_map(args.field_map)
        sn_state = load_sn_state(connect(args.instance, auth_method=args.auth_method), fields)

    validator = Validator(PDState(token), cfg)
    findings = validator.run(checks, sn_state, fields)
    findings.sort(key=lambda f: (-SEV_RANK[f["severity"]], f["check"], f["resource_type"],
                                 f["resource_name"]))
    write_payload(render_rows(findings, FIELDS, args.format), args.output)

    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    log.info("Findings: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    if args.plan_out:
        plan = new_plan("pd-reorg", validator.plan_items, generated_by="pd-validate",
                        options={"checks": checks})
        sha = write_plan(plan, args.plan_out)
        log.info("Wrote remediation plan %s (%d items, sha256 %s). Review, then "
                 "'pd-reorg apply %s --dry-run'.", args.plan_out, len(validator.plan_items),
                 sha[:12], args.plan_out)
    if args.fail_on == "never":
        return EXIT_OK
    threshold = SEV_RANK[args.fail_on]
    return EXIT_FAILURES if any(SEV_RANK[f["severity"]] >= threshold for f in findings) \
        else EXIT_OK
