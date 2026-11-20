"""Test-only writing command with a --dry-run switch (no network). Its preview uses both of
the engine's styles: diff lines on stdout and "[dry-run] ..." lines on stderr."""

import argparse
import sys

from pagerduty_ops.cli import add_logging_args, add_token_args
from pagerduty_ops.risk import writes_with_dry_run

RISK = writes_with_dry_run()


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
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dry_run:
        sys.stdout.write("APPLIED\n")  # the UI must never get here in Phase 1
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
