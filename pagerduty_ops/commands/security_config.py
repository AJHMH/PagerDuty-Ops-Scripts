"""Account security configuration as code: IP allow lists and session configurations.

  export          write both to one JSON file for review in git
  diff            read-only: compare the file with live settings (--fail-on-drift for cron)
  apply-sessions  write session configurations (web / mobile) from the file

IP allow lists are exported and diffed but deliberately NEVER written by this tool: an
enabled list restricts the whole account to the listed CIDRs, so one wrong entry locks
everyone out — including whoever is running the tool. Change them in the PagerDuty UI and
let `diff` catch drift from the reviewed file.

Writing a session configuration immediately REVOKES all existing sessions of that type
(per the API), so apply-sessions only writes types that changed and asks first.
Requires an Account Owner / Global Admin user token or an account API key.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys

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

log = get_logger("security_config")

SESSION_TYPES = ("web", "mobile")
ABSOLUTE_TTL = (600, 18144000)      # seconds, per the API's write schema
IDLE_TTL = (60, 15552000)


def build_parser():
    p = argparse.ArgumentParser(
        description="IP allow lists and session configurations: export, diff, apply sessions.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write live settings to a JSON file.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output", default="security_config.json")
    d = sub.add_parser("diff", help="Compare the file with live settings (read-only).")
    add_token_args(d)
    add_logging_args(d)
    d.add_argument("-i", "--input", default="security_config.json")
    d.add_argument("--fail-on-drift", action="store_true", help="Exit 1 if anything differs.")
    a = sub.add_parser("apply-sessions",
                       help="Write session configurations (revokes existing sessions).")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("-i", "--input", default="security_config.json")
    a.add_argument("--dry-run", action="store_true", help="Preview; no writes.")
    a.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt.")
    return p


# ---------------- live state ----------------

def _normalize_allow_list(a) -> dict:
    return {"state": a.get("state"),
            # API returns {"cidr": ...} objects; exported files hold plain strings.
            "cidr_entries": sorted((e.get("cidr") or "") if isinstance(e, dict) else str(e)
                                   for e in a.get("cidr_entries") or []),
            "targets": sorted(({"type": t.get("type"), "value": t.get("value")}
                               for t in a.get("targets") or []),
                              key=lambda t: (str(t["type"]), str(t["value"])))}


def live_allow_lists(token) -> list[dict]:
    body = request("ip_allow_lists", token)
    return [_normalize_allow_list(a) for a in body.get("ip_allow_lists") or []]


def live_sessions(token) -> list[dict]:
    try:
        body = request("session_configurations", token)
    except PDApiError as e:
        if e.status_code == 404:  # no configurations exist yet
            return []
        raise
    out = [{"type": c.get("type"), "absolute_session_ttl": c.get("absolute_session_ttl"),
            "idle_session_ttl": c.get("idle_session_ttl")}
           for c in body.get("session_configurations") or []]
    return sorted(out, key=lambda c: str(c["type"]))


def snapshot(token) -> dict:
    return {"ip_allow_lists": live_allow_lists(token), "session_configurations":
            live_sessions(token)}


# ---------------- file ----------------

def load_file(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    errors = []
    if not isinstance(doc, dict) or set(doc) - {"ip_allow_lists", "session_configurations"}:
        errors.append("top level must contain only 'ip_allow_lists' and "
                      "'session_configurations'")
        doc = {}
    sessions = doc.get("session_configurations") or []
    seen = set()
    for n, c in enumerate(sessions if isinstance(sessions, list) else [None]):
        if not isinstance(c, dict):
            errors.append(f"session_configurations[{n}] must be an object")
            continue
        typ = c.get("type")
        if typ not in SESSION_TYPES or typ in seen:
            errors.append(f"session_configurations[{n}]: type must be a unique one of "
                          f"{SESSION_TYPES}")
            continue
        seen.add(typ)
        abs_ttl, idle = c.get("absolute_session_ttl"), c.get("idle_session_ttl")
        if not isinstance(abs_ttl, int) or not ABSOLUTE_TTL[0] <= abs_ttl <= ABSOLUTE_TTL[1]:
            errors.append(f"{typ}: absolute_session_ttl must be an integer in {ABSOLUTE_TTL}")
        if not isinstance(idle, int) or not IDLE_TTL[0] <= idle <= IDLE_TTL[1]:
            errors.append(f"{typ}: idle_session_ttl must be an integer in {IDLE_TTL}")
        elif isinstance(abs_ttl, int) and idle > abs_ttl:
            errors.append(f"{typ}: idle_session_ttl cannot exceed absolute_session_ttl")
    if errors:
        for e in errors:
            print(f"Error: {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    doc["ip_allow_lists"] = [_normalize_allow_list(a) for a in doc.get("ip_allow_lists") or []]
    doc["session_configurations"] = sorted(
        ({"type": c["type"], "absolute_session_ttl": c["absolute_session_ttl"],
          "idle_session_ttl": c["idle_session_ttl"]} for c in sessions),
        key=lambda c: c["type"])
    return doc


def _lines(obj):
    return json.dumps(obj, indent=2, sort_keys=True).splitlines()


# ---------------- subcommands ----------------

def export_main(args) -> int:
    token = init(args)
    snap = snapshot(token)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
        f.write("\n")
    log.info("Exported %d IP allow lists and %d session configurations to %s",
             len(snap["ip_allow_lists"]), len(snap["session_configurations"]), args.output)
    return EXIT_OK


def diff_main(args) -> int:
    token = init(args)
    desired, live = load_file(args.input), snapshot(token)
    drift = 0
    for key in ("ip_allow_lists", "session_configurations"):
        lines = list(difflib.unified_diff(_lines(desired[key]), _lines(live[key]),
                                          fromfile=f"{key} (file)", tofile=f"{key} (live)",
                                          lineterm=""))
        if lines:
            drift += 1
            print("\n".join(lines) + "\n")
        else:
            log.info("%s: in sync", key)
    if drift and desired["ip_allow_lists"] != live["ip_allow_lists"]:
        log.warning("IP allow list changes are not applied by this tool; make them in the "
                    "PagerDuty UI (a wrong entry can lock the account out).")
    return EXIT_FAILURES if args.fail_on_drift and drift else EXIT_OK


def apply_sessions_main(args) -> int:
    token = init(args)
    desired = {c["type"]: c for c in load_file(args.input)["session_configurations"]}
    live = {c["type"]: c for c in live_sessions(token)}
    todo = [c for t, c in sorted(desired.items())
            if live.get(t) is None or
            (live[t]["absolute_session_ttl"], live[t]["idle_session_ttl"]) !=
            (c["absolute_session_ttl"], c["idle_session_ttl"])]
    log.info("%d session configuration types to update.", len(todo))
    if not todo:
        return EXIT_OK
    for c in todo:
        cur = live.get(c["type"]) or {}
        print(f"~ {c['type']}: absolute {cur.get('absolute_session_ttl')} -> "
              f"{c['absolute_session_ttl']}, idle {cur.get('idle_session_ttl')} -> "
              f"{c['idle_session_ttl']}", file=sys.stderr)
    log.warning("Applying immediately REVOKES all existing %s sessions (everyone using those "
                "is signed out).", " and ".join(c["type"] for c in todo))
    if not confirm(f"Update {len(todo)} session configuration(s) and revoke those sessions?",
                   assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK
    ok = failed = 0
    for c in todo:
        if args.dry_run:
            print(f"[dry-run] PUT session_configurations?type={c['type']}", file=sys.stderr)
            ok += 1
            continue
        try:
            request("session_configurations", token, method="PUT", params={"type": c["type"]},
                    data={"session_configuration": {
                        "absolute_session_ttl": c["absolute_session_ttl"],
                        "idle_session_ttl": c["idle_session_ttl"]}})
            log.info("updated %s session configuration", c["type"])
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s session configuration failed: %s", c["type"], e)
            failed += 1
    return finish_bulk(ok, failed, dry_run=args.dry_run, label="session configurations")


HANDLERS = {"export": export_main, "diff": diff_main, "apply-sessions": apply_sessions_main}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)
