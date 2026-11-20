"""Migrate legacy service Event Rules to Event Orchestration, in two explicit steps.

  inventory   read-only: event rule count, conversion status and EO active status per service
  convert     POST /services/{id}/rules/convert — creates equivalent Event Orchestration
              rules and makes the service's Event Rules READ-ONLY. This cannot be undone.
  activate    PUT /event_orchestrations/services/{id}/active {active: true} — new events are
              evaluated by the Service Orchestration instead of the Event Rules.
  deactivate  the reverse of activate (reversible at any time).

Converting does not change how events are processed; activating does. Activation is
refused for services that still have unconverted Event Rules, because those rules would
silently stop applying. PagerDuty has announced Event Rules end-of-life.
"""

from __future__ import annotations

import argparse
import sys

from ..api import PDApiError, paginate, request
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
from ..risk import READ_ONLY, writes_with_dry_run

log = get_logger("event_rules_migrate")

FIELDS = ["service_id", "service_name", "event_rules", "converted", "eo_active", "state"]
DEFAULT_MAX_SERVICES = 10


def _targets(p, *, allow_all_help: str):
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--service-ids", help="Comma-separated service IDs.")
    g.add_argument("--all", action="store_true", help=allow_all_help)
    p.add_argument("--max-services", type=int, default=DEFAULT_MAX_SERVICES,
                   help=f"Refuse to change more services than this "
                        f"(default {DEFAULT_MAX_SERVICES}).")


RISK = {
    "inventory": READ_ONLY,
    "convert": writes_with_dry_run(irreversible=True, cap_flags=("--max-services",)),
    "activate": writes_with_dry_run(cap_flags=("--max-services",)),
    "deactivate": writes_with_dry_run(cap_flags=("--max-services",)),
}


def build_parser():
    p = argparse.ArgumentParser(
        description="Migrate service Event Rules to Event Orchestration (convert, activate).")
    sub = p.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="Read-only migration status per service.")
    add_token_args(inv)
    add_logging_args(inv)
    add_format_args(inv)
    inv.add_argument("--fail-on-unmigrated", action="store_true",
                     help="Exit 1 if any service still evaluates events with Event Rules.")

    for name, help_text, all_help in (
        ("convert", "Convert Event Rules to EO rules (IRREVERSIBLE: rules become read-only).",
         "Every service that has unconverted Event Rules."),
        ("activate", "Evaluate new events with the Service Orchestration.",
         "Every service that is safe to activate (converted, or no Event Rules)."),
        ("deactivate", "Evaluate new events with the (read-only) Event Rules again.",
         "Every service whose Service Orchestration is active."),
    ):
        s = sub.add_parser(name, help=help_text)
        add_token_args(s)
        add_logging_args(s)
        add_write_guards(s)
        _targets(s, allow_all_help=all_help)
    return p


# ---------------- state ----------------

def service_state(token, service) -> dict:
    sid = service["id"]
    rules = request(f"services/{sid}/rules", token,
                    params={"include[]": ["migrated_metadata"], "limit": 1, "total": "true"})
    count = rules.get("total")
    if count is None:
        count = len(rules.get("rules") or [])
    converted = rules.get("migrated_status") == "completed"
    active = bool(request(f"event_orchestrations/services/{sid}/active", token).get("active"))
    if count == 0:
        state = "no_event_rules"
    elif not converted:
        state = "needs_convert"
    elif not active:
        state = "needs_activate"
    else:
        state = "migrated"
    return {"service_id": sid, "service_name": service.get("name", ""), "event_rules": count,
            "converted": converted, "eo_active": active, "state": state}


def load_states(token, service_ids=None) -> tuple[list[dict], int]:
    wanted = set(service_ids or [])
    states, failed = [], 0
    for svc in paginate("services", token):
        if wanted and svc["id"] not in wanted:
            continue
        try:
            states.append(service_state(token, svc))
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("service %s: %s", svc["id"], e)
            failed += 1
    missing = wanted - {s["service_id"] for s in states}
    if missing and not failed:
        print(f"Error: unknown service IDs {sorted(missing)}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return states, failed


# ---------------- subcommands ----------------

def inventory_main(args) -> int:
    token = init(args)
    states, failed = load_states(token)
    write_payload(render_rows(states, FIELDS, args.format), args.output)
    counts: dict[str, int] = {}
    for s in states:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    log.info("States: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
    if failed:
        log.error("%d services could not be read; inventory is incomplete.", failed)
        return EXIT_FAILURES
    pending = counts.get("needs_convert", 0) + counts.get("needs_activate", 0)
    return EXIT_FAILURES if args.fail_on_unmigrated and pending else EXIT_OK


def _select(args, states):
    """(targets, refusals) for the subcommand; refusals are human-readable reasons."""
    explicit = not args.all
    targets, refusals = [], []
    for s in states:
        label = f"{s['service_name'] or s['service_id']} ({s['service_id']})"
        if args.command == "convert":
            if s["state"] == "needs_convert":
                targets.append(s)
            elif explicit and s["event_rules"] and s["converted"]:
                log.info("%s: already converted; skipping.", label)
            elif explicit and not s["event_rules"]:
                log.info("%s: has no Event Rules; nothing to convert.", label)
        elif args.command == "activate":
            if s["eo_active"]:
                if explicit:
                    log.info("%s: already active; skipping.", label)
            elif s["state"] == "needs_convert":
                if explicit:
                    refusals.append(f"{label}: has {s['event_rules']} unconverted Event Rules; "
                                    "run 'convert' first or they would stop applying")
            else:
                targets.append(s)
        elif args.command == "deactivate":
            if s["eo_active"]:
                targets.append(s)
            elif explicit:
                log.info("%s: already inactive; skipping.", label)
    return targets, refusals


def change_main(args) -> int:
    token = init(args)
    if args.max_services < 1:
        print("Error: --max-services must be >= 1.", file=sys.stderr)
        return EXIT_USAGE
    ids = None
    if not args.all:
        # An empty/blank --service-ids (e.g. an unset shell variable) must never widen the
        # selection to every service: unfiltered selection requires an explicit --all.
        ids = [v.strip() for v in (args.service_ids or "").split(",") if v.strip()]
        if not ids:
            print("Error: --service-ids is empty; pass at least one service ID, or --all.",
                  file=sys.stderr)
            return EXIT_USAGE
    states, failed = load_states(token, ids)
    if failed:
        print(f"Error: {failed} services could not be read; refusing to change anything.",
              file=sys.stderr)
        return EXIT_USAGE
    targets, refusals = _select(args, states)
    for r in refusals:
        print(f"Error: {r}", file=sys.stderr)
    if refusals:
        return EXIT_USAGE
    log.info("%d services to %s.", len(targets), args.command)
    if not targets:
        return EXIT_OK
    if len(targets) > args.max_services:
        print(f"Error: {len(targets)} services selected, above --max-services "
              f"{args.max_services}. Narrow with --service-ids or raise the cap.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.command == "convert":
        log.warning("convert is IRREVERSIBLE: each service's Event Rules become read-only. "
                    "Event processing does not change until you run 'activate'.")
    if not confirm(f"{args.command.capitalize()} {len(targets)} services?",
                   assume_yes=args.yes, dry_run=args.dry_run):
        return EXIT_OK

    succeeded = failures = 0
    for s in targets:
        label = f"{s['service_name'] or s['service_id']} ({s['service_id']})"
        if args.dry_run:
            print(f"[dry-run] {args.command} {label}"
                  + (f" ({s['event_rules']} Event Rules)" if args.command == "convert" else ""),
                  file=sys.stderr)
            succeeded += 1
            continue
        try:
            if args.command == "convert":
                result = request(f"services/{s['service_id']}/rules/convert", token,
                                 method="POST")
                log.info("%s: converted (%s) -> %s", label, result.get("convert_status"),
                         result.get("converted_to"))
            else:
                active = args.command == "activate"
                request(f"event_orchestrations/services/{s['service_id']}/active", token,
                        method="PUT", data={"active": active})
                log.info("%s: Service Orchestration %s", label,
                         "active" if active else "inactive")
            succeeded += 1
        except PDApiError as e:
            if e.is_auth_error:
                raise
            log.error("%s: %s failed: %s", label, args.command, e)
            failures += 1
    return finish_bulk(succeeded, failures, dry_run=args.dry_run, label="services")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inventory":
        return inventory_main(args)
    return change_main(args)
