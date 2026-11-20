"""Test-only diff-first command: previews unless --apply is given (no network)."""

import argparse
import sys

from pagerduty_ops.risk import writes_with_apply

RISK = writes_with_apply()


def build_parser():
    p = argparse.ArgumentParser(description="UI test fixture that diffs, then applies.")
    p.add_argument("--apply", action="store_true", help="Write changes.")
    p.add_argument("-y", "--yes", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and not args.yes:
        sys.stderr.write("--apply requires -y.\n")
        return 2
    sys.stdout.write("APPLIED\n" if args.apply else "+ new-template\n")
    return 0
