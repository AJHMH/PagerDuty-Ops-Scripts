"""Export and diff-first apply of business service definitions.

  export  write one JSON file keyed by business-service name (git-friendly, no IDs)
  apply   print the change set; --apply -y creates/updates; --prune also deletes
          business services absent from the file (capped by --max-deletes)

File format::

    {"business_services": [
        {"name": "Water Billing", "description": "...", "point_of_contact": "...",
         "team_id": "PTEAM01", "team_name": "Utilities (informational)"}]}

Dependencies are managed with pd-service-deps and subscribers with pd-subscriptions.
Names are the key, so renaming in the file means delete + create (use --prune); rename
in the UI instead to keep the business service's history and dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..api import PDApiError, paginate, request
from ..cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, add_logging_args, add_token_args, init
from ..log import get_logger
from ..risk import READ_ONLY, writes_with_apply

log = get_logger("business_services")

FIELDS = ("description", "point_of_contact", "team_id")
DEFAULT_MAX_DELETES = 5


RISK = {
    "export": READ_ONLY,
    "apply": writes_with_apply(irreversible_flags=("--prune",), cap_flags=("--max-deletes",)),
}


def build_parser():
    p = argparse.ArgumentParser(description="Export / diff-apply business service definitions.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write business services to a JSON file.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output", default="business_services.json")
    a = sub.add_parser("apply", help="Diff (default) or apply a business services file.")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("-i", "--input", default="business_services.json")
    a.add_argument("--apply", action="store_true", help="Write changes (requires -y).")
    a.add_argument("-y", "--yes", action="store_true", help="Acknowledge --apply.")
    a.add_argument("--prune", action="store_true",
                   help="Also delete business services absent from the file.")
    a.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES,
                   help=f"Refuse to delete more than this many (default {DEFAULT_MAX_DELETES}).")
    return p


def normalize(bs, team_names) -> dict:
    team_id = (bs.get("team") or {}).get("id") or ""
    return {"name": bs.get("name") or "", "description": bs.get("description") or "",
            "point_of_contact": bs.get("point_of_contact") or "", "team_id": team_id,
            "team_name": team_names.get(team_id, "")}


def load_live(token):
    teams = {t["id"]: t.get("name", "") for t in paginate("teams", token)}
    live = [dict(normalize(bs, teams), id=bs["id"]) for bs in paginate("business_services", token)]
    return live, teams


def export_main(args) -> int:
    token = init(args)
    live, _teams = load_live(token)
    doc = {"business_services": sorted(({k: v for k, v in b.items() if k != "id"} for b in live),
                                       key=lambda b: b["name"].lower())}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    log.info("Exported %d business services to %s", len(live), args.output)
    return EXIT_OK


def load_desired(path, teams) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get("business_services")
    except (OSError, ValueError, AttributeError) as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    errors, seen, out = [], set(), []
    if not isinstance(items, list):
        errors.append("'business_services' must be a list")
        items = []
    for n, b in enumerate(items):
        if not isinstance(b, dict) or not str(b.get("name") or "").strip():
            errors.append(f"entry {n}: 'name' is required")
            continue
        # Keep the name exactly as exported: live names are matched unstripped, so
        # stripping here would turn an untouched export into create + (with --prune) delete.
        name = str(b["name"])
        if name.lower() in seen:
            errors.append(f"{name!r}: duplicate name")
            continue
        seen.add(name.lower())
        team_id = str(b.get("team_id") or "").strip()
        if team_id and team_id not in teams:
            errors.append(f"{name!r}: unknown team_id {team_id}")
            continue
        unknown = set(b) - {"name", "team_name", *FIELDS}
        if unknown:
            errors.append(f"{name!r}: unknown keys {sorted(unknown)}")
            continue
        out.append({"name": name, "description": str(b.get("description") or ""),
                    "point_of_contact": str(b.get("point_of_contact") or ""),
                    "team_id": team_id})
    if errors:
        for e in errors:
            print(f"Error: {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return out


def _body(d) -> dict:
    body = {"name": d["name"], "description": d["description"],
            "point_of_contact": d["point_of_contact"]}
    body["team"] = {"id": d["team_id"], "type": "team_reference"} if d["team_id"] else None
    return {"business_service": body}


def plan_changes(desired, live, prune) -> tuple[list[dict], list[str]]:
    errors = []
    by_name: dict[str, list[dict]] = {}
    for b in live:
        by_name.setdefault(b["name"].lower(), []).append(b)
    changes = []
    for d in desired:
        matches = by_name.get(d["name"].lower(), [])
        if len(matches) > 1:
            errors.append(f"{d['name']!r}: {len(matches)} live business services share this "
                          "name; rename them in the UI first")
            continue
        if not matches:
            changes.append({"action": "create", "name": d["name"], "desired": d})
            continue
        cur = matches[0]
        diff = {k: (cur[k], d[k]) for k in FIELDS if cur[k] != d[k]}
        if diff:
            changes.append({"action": "update", "name": d["name"], "id": cur["id"],
                            "desired": d, "diff": diff})
    if prune:
        wanted = {d["name"].lower() for d in desired}
        for b in live:
            if b["name"].lower() not in wanted:
                changes.append({"action": "delete", "name": b["name"], "id": b["id"]})
    return changes, errors


def describe(c) -> str:
    if c["action"] == "create":
        return f"+ {c['name']}"
    if c["action"] == "delete":
        return f"- {c['name']} ({c['id']})"
    return f"~ {c['name']}: " + ", ".join(f"{k} {a!r}->{b!r}" for k, (a, b) in c["diff"].items())


def apply_main(args) -> int:
    token = init(args)
    if args.apply and not args.yes:
        print("Error: --apply requires -y/--yes.", file=sys.stderr)
        return EXIT_USAGE
    live, teams = load_live(token)
    desired = load_desired(args.input, teams)
    changes, errors = plan_changes(desired, live, args.prune)
    for e in errors:
        log.error("%s", e)
    if errors:
        return EXIT_USAGE
    for line in map(describe, changes):
        print(line)
    deletes = [c for c in changes if c["action"] == "delete"]
    log.info("%d changes (%d deletions).", len(changes), len(deletes))
    if len(deletes) > args.max_deletes:
        print(f"Error: {len(deletes)} deletions exceed --max-deletes {args.max_deletes}. "
              "Deleting a business service stops new incidents for it.", file=sys.stderr)
        return EXIT_USAGE
    if not args.apply:
        if changes:
            log.info("Re-run with --apply -y to write these changes.")
        return EXIT_OK
    failed = 0
    for c in changes:
        try:
            if c["action"] == "create":
                request("business_services", token, method="POST", data=_body(c["desired"]))
            elif c["action"] == "update":
                request(f"business_services/{c['id']}", token, method="PUT",
                        data=_body(c["desired"]))
            else:
                request(f"business_services/{c['id']}", token, method="DELETE")
            log.info("applied: %s", describe(c))
        except PDApiError as e:
            if e.is_auth_error:
                raise
            if c["action"] == "delete" and e.status_code == 404:
                continue
            log.error("failed: %s: %s", describe(c), e)
            failed += 1
    return EXIT_FAILURES if failed else EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"export": export_main, "apply": apply_main}[args.command](args)
