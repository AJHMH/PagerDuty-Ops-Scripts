"""Test-only writing command with a --dry-run switch (no network). Its preview uses both of
the engine's styles: diff lines on stdout and "[dry-run] ..." lines on stderr."""

import argparse
import json
import os
import sys
import time

from pagerduty_ops.cli import add_logging_args, add_token_args
from pagerduty_ops.risk import writes_with_dry_run

RISK = writes_with_dry_run(irreversible_flags=("--purge",))


def build_parser():
    p = argparse.ArgumentParser(description="UI test fixture that writes.")
    add_token_args(p)
    add_logging_args(p)
    p.add_argument("--dry-run", action="store_true", help="Preview; no writes.")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--note", default="", help="A context line for the log (not part of the diff).")
    p.add_argument("--extra-delete", action="store_true", help="Preview one more deletion.")
    p.add_argument("--only-unrecognised", action="store_true",
                   help="Preview only lines in a style the review can't classify.")
    p.add_argument("--payload", help="Preview a POST whose JSON payload follows the header "
                                     "(like pd-alert-grouping).")
    p.add_argument("--windows", type=int, default=0,
                   help="Preview this many '[dry-run] line N: would create ...' lines "
                        "(like pd-bulk-maintenance-window).")
    p.add_argument("--fail", action="store_true", help="Exit 1 before previewing anything.")
    p.add_argument("--purge", action="store_true", help="Delete what isn't listed (irreversible).")
    p.add_argument("-i", "--input", help="A CSV whose first line appears in the preview.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    first_input_line = ""
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            first_input_line = f.readline().strip()
    if not args.dry_run:
        # Like the engine's confirm(): an unattended write needs -y.
        if not args.yes:
            sys.stderr.write("Refusing to write without -y.\n")
            return 2
        time.sleep(float(os.environ.get("UI_FIXTURE_APPLY_SLEEP", "0")))  # a slow apply
        exit_code = int(os.environ.get("UI_FIXTURE_APPLY_EXIT", "0"))
        if exit_code:
            sys.stderr.write(f"Error: apply failed with exit {exit_code}\n")
            return exit_code
        sys.stdout.write("APPLIED\n")
        return 0
    time.sleep(float(os.environ.get("UI_FIXTURE_DRY_SLEEP", "0")))  # a slow dry run
    if first_input_line:
        sys.stdout.write(f"~ input: {first_input_line}\n")
    if os.environ.get("UI_FIXTURE_DRIFT"):  # live state changed since the reviewed dry run
        sys.stdout.write("+ drifted-service\n")
    if args.fail:
        sys.stderr.write("Error: could not reach PagerDuty\n")
        return 1
    if args.payload is not None:
        sys.stderr.write("[dry-run] would POST /alert_grouping_settings with:\n")
        sys.stderr.write(json.dumps({"alert_grouping_setting": {"name": args.payload}},
                                    indent=2) + "\n")
        return 0
    if args.windows:
        for n in range(args.windows):
            sys.stderr.write(f"[dry-run] line {n + 2}: would create window for P{n} "
                             "2026-12-01T00:00:00Z -> 2026-12-01T02:00:00Z\n")
        return 0
    if args.only_unrecognised:  # like pd-sn-sync: "[dry-run] <table> <key>: <values>"
        sys.stderr.write("[dry-run] sys_user 'ann' (S1): {'pd_id': 'PU1'}\n")
        return 0
    if args.note:
        sys.stderr.write(f"2026-11-20 21:00:00 INFO {args.note}\n")
    sys.stdout.write("+ new-service\n~ api-service: name 'a'->'b'\n- old-service (P1)\n")
    sys.stderr.write("[dry-run] would DELETE /teams/PT1/members/PU1 for Ann\n")
    if args.extra_delete:
        sys.stderr.write("[dry-run] would remove PU2 from schedule 'Primary'\n")
    sys.stderr.write("[dry-run] previewing removal of Ann\n")
    return 0
