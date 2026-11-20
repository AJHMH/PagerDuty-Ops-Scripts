"""Export and diff-first apply of status-update templates.

  export  write templates to one JSON file keyed by name (git-friendly, no IDs)
  apply   print the change set; --apply -y creates/updates; --prune also deletes
          templates absent from the file (capped by --max-deletes)

File format::

    {"templates": [
        {"name": "Major outage", "description": "...", "template_type": "status_update",
         "templated_fields": {"email_subject": "...", "email_body": "<p>...</p>",
                              "message": "..."}}]}

`status_update` is the only template type PagerDuty supports. Names are the key and are
kept exactly as exported, so an untouched export always re-applies as a no-op.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..api import PDApiError, paginate, request
from ..cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, add_logging_args, add_token_args, init
from ..log import get_logger

log = get_logger("templates")

TEMPLATE_TYPES = ("status_update",)
TEMPLATED_FIELDS = ("email_subject", "email_body", "message")
DEFAULT_MAX_DELETES = 5


def build_parser():
    p = argparse.ArgumentParser(description="Export / diff-apply status-update templates.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Write templates to a JSON file.")
    add_token_args(e)
    add_logging_args(e)
    e.add_argument("-o", "--output", default="templates.json")
    a = sub.add_parser("apply", help="Diff (default) or apply a templates file.")
    add_token_args(a)
    add_logging_args(a)
    a.add_argument("-i", "--input", default="templates.json")
    a.add_argument("--apply", action="store_true", help="Write changes (requires -y).")
    a.add_argument("-y", "--yes", action="store_true", help="Acknowledge --apply.")
    a.add_argument("--prune", action="store_true",
                   help="Also delete templates absent from the file.")
    a.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES,
                   help=f"Refuse to delete more than this many (default {DEFAULT_MAX_DELETES}).")
    return p


def normalize(t) -> dict:
    fields = t.get("templated_fields") or {}
    return {"name": t.get("name") or "", "description": t.get("description") or "",
            "template_type": t.get("template_type") or "status_update",
            "templated_fields": {k: fields.get(k) or "" for k in TEMPLATED_FIELDS}}


def load_live(token) -> list[dict]:
    """Full live templates. The list endpoint may return metadata only (PagerDuty's own
    example omits `templated_fields`), so each template is fetched individually; an
    unreadable template raises rather than being treated as empty content."""
    out = []
    for t in paginate("templates", token):
        full = request(f"templates/{t['id']}", token).get("template") or t
        out.append(dict(normalize(full), id=t["id"]))
    return out


def export_main(args) -> int:
    token = init(args)
    live = load_live(token)
    doc = {"templates": sorted(({k: v for k, v in t.items() if k != "id"} for t in live),
                               key=lambda t: t["name"].lower())}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    log.info("Exported %d templates to %s", len(live), args.output)
    return EXIT_OK


def load_desired(path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get("templates")
    except (OSError, ValueError, AttributeError) as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    errors, seen, out = [], set(), []
    if not isinstance(items, list):
        errors.append("'templates' must be a list")
        items = []
    for n, t in enumerate(items):
        if not isinstance(t, dict) or not str(t.get("name") or "").strip():
            errors.append(f"entry {n}: 'name' is required")
            continue
        name = str(t["name"])  # verbatim: live names are matched unstripped
        if name.lower() in seen:
            errors.append(f"{name!r}: duplicate name")
            continue
        seen.add(name.lower())
        unknown = set(t) - {"name", "description", "template_type", "templated_fields"}
        fields = t.get("templated_fields") or {}
        if unknown:
            errors.append(f"{name!r}: unknown keys {sorted(unknown)}")
            continue
        if (t.get("template_type") or "status_update") not in TEMPLATE_TYPES:
            errors.append(f"{name!r}: template_type must be one of {TEMPLATE_TYPES}")
            continue
        if not isinstance(fields, dict) or set(fields) - set(TEMPLATED_FIELDS):
            errors.append(f"{name!r}: templated_fields may only contain {TEMPLATED_FIELDS}")
            continue
        if not any(fields.get(k) for k in TEMPLATED_FIELDS):
            errors.append(f"{name!r}: at least one of {TEMPLATED_FIELDS} must be non-empty")
            continue
        out.append(normalize(t))
    if errors:
        for e in errors:
            print(f"Error: {path}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return out


def plan_changes(desired, live, prune) -> tuple[list[dict], list[str]]:
    errors, by_name = [], {}
    for t in live:
        by_name.setdefault(t["name"].lower(), []).append(t)
    changes = []
    for d in desired:
        matches = by_name.get(d["name"].lower(), [])
        if len(matches) > 1:
            errors.append(f"{d['name']!r}: {len(matches)} live templates share this name; "
                          "rename them in the UI first")
            continue
        if not matches:
            changes.append({"action": "create", "name": d["name"], "desired": d})
            continue
        cur = matches[0]
        diff = [k for k in ("description", "template_type") if cur[k] != d[k]]
        diff += [f"templated_fields.{k}" for k in TEMPLATED_FIELDS
                 if cur["templated_fields"][k] != d["templated_fields"][k]]
        if diff:
            changes.append({"action": "update", "name": d["name"], "id": cur["id"],
                            "desired": d, "fields": diff})
    if prune:
        wanted = {d["name"].lower() for d in desired}
        changes += [{"action": "delete", "name": t["name"], "id": t["id"]}
                    for t in live if t["name"].lower() not in wanted]
    return changes, errors


def describe(c) -> str:
    if c["action"] == "create":
        return f"+ {c['name']}"
    if c["action"] == "delete":
        return f"- {c['name']} ({c['id']})"
    return f"~ {c['name']}: {', '.join(c['fields'])}"


def _body(d) -> dict:
    return {"template": {"name": d["name"], "description": d["description"],
                         "template_type": d["template_type"],
                         "templated_fields": {k: d["templated_fields"][k] or None
                                              for k in TEMPLATED_FIELDS}}}


def apply_main(args) -> int:
    token = init(args)
    if args.apply and not args.yes:
        print("Error: --apply requires -y/--yes.", file=sys.stderr)
        return EXIT_USAGE
    desired = load_desired(args.input)
    changes, errors = plan_changes(desired, load_live(token), args.prune)
    for e in errors:
        log.error("%s", e)
    if errors:
        return EXIT_USAGE
    for line in map(describe, changes):
        print(line)
    deletes = sum(1 for c in changes if c["action"] == "delete")
    log.info("%d changes (%d deletions).", len(changes), deletes)
    if deletes > args.max_deletes:
        print(f"Error: {deletes} deletions exceed --max-deletes {args.max_deletes}.",
              file=sys.stderr)
        return EXIT_USAGE
    if not args.apply:
        if changes:
            log.info("Re-run with --apply -y to write these changes.")
        return EXIT_OK
    failed = 0
    for c in changes:
        try:
            if c["action"] == "create":
                request("templates", token, method="POST", data=_body(c["desired"]))
            elif c["action"] == "update":
                request(f"templates/{c['id']}", token, method="PUT", data=_body(c["desired"]))
            else:
                request(f"templates/{c['id']}", token, method="DELETE")
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
