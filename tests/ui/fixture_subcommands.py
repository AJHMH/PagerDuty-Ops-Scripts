"""Test-only command with a read-only and a writing subcommand (no network)."""

import argparse
import sys

from pagerduty_ops.risk import READ_ONLY, writes_with_dry_run

RISK = {"report": READ_ONLY, "apply": writes_with_dry_run()}


def build_parser():
    p = argparse.ArgumentParser(description="UI test fixture with subcommands.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("report")
    a = sub.add_parser("apply")
    a.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.write(f"{args.command}\n")
    return 0
