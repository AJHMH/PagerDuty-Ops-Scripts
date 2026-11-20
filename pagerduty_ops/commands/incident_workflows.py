"""Incident Workflows: export to git, diff against live, and bulk trigger-service links.

  export           one JSON file per workflow (definition + steps + its triggers)
  diff             read-only: compare exported files with live workflows (unified diff)
  triggers-export  CSV of trigger <-> service associations (re-appliable)
  triggers-apply   attach / detach triggers to services from CSV (idempotent)

Workflow definitions are not written back by this tool: the update API's step format
cannot be verified against live data here, so edits stay in the PagerDuty UI and `diff`
shows drift from what is in git. Triggers subscribed to *all services* cannot have
per-service links and are reported instead of changed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

from ..api import PDApiError, paginate, paginate_cursor, request
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

log = get_logger("incident_workflows")

TRIGGER_COLUMNS = ["action", "trigger_id", "service_id", "workflow_name", "trigger_type"]
ACTIONS = ("attach", "detach")
# Server-managed fields that change without anyone editing the workflow.
VOLATILE_KEYS = {"self", "html_url", "created_at", "updated_at", "summary"}


def build_parser():
    p = argparse.ArgumentParser(description="Incident Workflows: export, diff, bulk triggers.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write one JSON file per workflow.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output-dir", default="incident_workflows")
    d = sub.add_parser("diff", help="Compare exported files with live workflows (read-only).")
    add_token_args(d)
    add_logging_args(d)
    d.add_argument("-i", "--input-dir", default="incident_workflows")
    d.add_argument("--fail-on-drift", action="store_true",
                   help="Exit 1 if any workflow differs from its file (or is missing).")
    te = sub.add_parser("triggers-export", help="CSV of trigger-service associations.")
    add_token_args(te)
    add_logging_args(te)
    add_format_args(te, formats=("csv", "json", "table"), default="csv")
    ta = sub.add_parser("triggers-apply", help="Attach/detach triggers to services from CSV.")
    add_token_args(ta)
    add_logging_args(ta)
    add_write_guards(ta)
    ta.add_argument("csv_file", help="CSV: action (attach|detach), trigger_id, service_id")
    return p


# ---------------- reading ----------------

def list_workflows(token) -> list[dict]:
    return list(paginate("incident_workflows", token, params={"include[]": ["steps", "team"]},
                         items_key="incident_workflows"))


def list_triggers(token) -> list[dict]:
    return list(paginate_cursor("incident_workflows/triggers", token, items_key="triggers"))


def strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


def _trigger_summary(t) -> dict:
    return strip_volatile({
        "id": t.get("id"), "trigger_type": t.get("trigger_type"),
        "condition": t.get("condition"), "is_disabled": t.get("is_disabled"),
        "is_subscribed_to_all_services": t.get("is_subscribed_to_all_services"),
        "services": sorted(s.get("id") for s in t.get("services") or [] if s.get("id")),
        "incident_types": t.get("incident_types"),
    })


def document(workflow, triggers) -> dict:
    """Stable, server-noise-free representation used for both export and diff."""
    wid = workflow.get("id")
    mine = [_trigger_summary(t) for t in triggers if (t.get("workflow") or {}).get("id") == wid]
    return {"workflow": strip_volatile(workflow),
            "triggers": sorted(mine, key=lambda t: t.get("id") or "")}


def slugify(name) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name or "").strip("-").lower() or "unnamed"


# ---------------- export / diff ----------------

def export_main(args) -> int:
    token = init(args)
    workflows, triggers = list_workflows(token), list_triggers(token)
    os.makedirs(args.output_dir, exist_ok=True)
    for wf in workflows:
        path = os.path.join(args.output_dir, f"{wf['id']}__{slugify(wf.get('name'))}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(document(wf, triggers), f, indent=2, sort_keys=True)
            f.write("\n")
        log.info("Wrote %s", path)
    log.info("Exported %d workflows (%d triggers) to %s/", len(workflows), len(triggers),
             args.output_dir)
    return EXIT_OK


def _lines(obj) -> list[str]:
    return json.dumps(obj, indent=2, sort_keys=True).splitlines()


def diff_main(args) -> int:
    token = init(args)
    if not os.path.isdir(args.input_dir):
        print(f"Error: not a directory: {args.input_dir}", file=sys.stderr)
        return EXIT_USAGE
    files = {}
    for name in sorted(os.listdir(args.input_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(args.input_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            files[doc["workflow"]["id"]] = (name, doc)
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"Error: {path}: not a workflow export ({e})", file=sys.stderr)
            return EXIT_USAGE
    workflows, triggers = list_workflows(token), list_triggers(token)
    live = {wf["id"]: document(wf, triggers) for wf in workflows}
    drift = 0
    for wid, (name, doc) in files.items():
        if wid not in live:
            log.warning("%s: workflow %s no longer exists in PagerDuty", name, wid)
            drift += 1
            continue
        lines = list(difflib.unified_diff(_lines(doc), _lines(live[wid]),
                                          fromfile=f"{name} (git)", tofile=f"{wid} (live)",
                                          lineterm=""))
        if lines:
            drift += 1
            print("\n".join(lines) + "\n")
        else:
            log.info("%s: in sync", name)
    for wid in sorted(set(live) - set(files)):
        log.warning("workflow %s (%s) exists in PagerDuty but has no file", wid,
                    live[wid]["workflow"].get("name"))
        drift += 1
    log.info("%d of %d workflows differ.", drift, len(set(live) | set(files)))
    return EXIT_FAILURES if args.fail_on_drift and drift else EXIT_OK


# ---------------- triggers ----------------

def triggers_export_main(args) -> int:
    token = init(args)
    rows, skipped = [], 0
    for t in list_triggers(token):
        if t.get("is_subscribed_to_all_services"):
            skipped += 1
            continue
        for s in t.get("services") or []:
            rows.append({"action": "attach", "trigger_id": t["id"], "service_id": s.get("id"),
                         "workflow_name": (t.get("workflow") or {}).get("name", ""),
                         "trigger_type": t.get("trigger_type", "")})
    rows.sort(key=lambda r: (r["workflow_name"], r["trigger_id"], r["service_id"]))
    write_payload(render_rows(rows, TRIGGER_COLUMNS, args.format), args.output)
    log.info("Exported %d trigger-service links (%d all-services triggers omitted).",
             len(rows), skipped)
    return EXIT_OK


def triggers_apply_main(args) -> int:
    token = init(args)
    rows = load_csv_rows(args.csv_file, {"action", "trigger_id", "service_id"},
                         skip_if_missing=("trigger_id", "service_id"))
    triggers = {t["id"]: t for t in list_triggers(token)}
    services = {s["id"] for s in paginate("services", token)}
    errors, wanted = [], {a: set() for a in ACTIONS}
    for r in rows:
        where = f"line {r['_line']}"
        action, tid, sid = r["action"].lower(), r["trigger_id"], r["service_id"]
        if action not in ACTIONS:
            errors.append(f"{where}: action must be attach or detach")
        elif tid not in triggers:
            errors.append(f"{where}: unknown trigger {tid}")
        elif sid not in services:
            errors.append(f"{where}: unknown service {sid}")
        elif triggers[tid].get("is_subscribed_to_all_services"):
            errors.append(f"{where}: trigger {tid} applies to all services; per-service "
                          "links cannot be changed")
        else:
            wanted[action].add((tid, sid))
    if wanted["attach"] & wanted["detach"]:
        errors.append("some trigger-service links are both attached and detached")
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        return EXIT_USAGE

    linked = {(tid, s.get("id")) for tid, t in triggers.items() for s in t.get("services") or []}
    todo = [("attach", *pair) for pair in sorted(wanted["attach"] - linked)]
    todo += [("detach", *pair) for pair in sorted(wanted["detach"] & linked)]
    log.info("%d to attach, %d to detach, %d already in desired state.",
             sum(1 for t in todo if t[0] == "attach"), sum(1 for t in todo if t[0] == "detach"),
             len(rows) - len(todo))
    if not todo or not confirm(f"Apply {len(todo)} trigger-service changes?",
                               assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK

    ok = failed = 0
    for action, tid, sid in todo:
        wf_name = (triggers[tid].get("workflow") or {}).get("name") or tid
        if args.dry_run:
            print(f"[dry-run] {action} trigger {tid} ({wf_name}) <-> service {sid}",
                  file=sys.stderr)
            ok += 1
            continue
        try:
            if action == "attach":
                request(f"incident_workflows/triggers/{tid}/services", token, method="POST",
                        data={"service": {"id": sid}})
            else:
                request(f"incident_workflows/triggers/{tid}/services/{sid}", token,
                        method="DELETE")
            log.info("%sed trigger %s (%s) <-> service %s", action, tid, wf_name, sid)
            ok += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            if action == "detach" and e.status_code == 404:
                ok += 1  # already detached
                continue
            log.error("%s trigger %s <-> service %s failed: %s", action, tid, sid, e)
            failed += 1
    return finish_bulk(ok, failed, dry_run=args.dry_run, label="trigger links")


HANDLERS = {"export": export_main, "diff": diff_main, "triggers-export": triggers_export_main,
            "triggers-apply": triggers_apply_main}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)
