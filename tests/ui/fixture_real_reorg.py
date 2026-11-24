"""Test-only stand-in for pd-reorg (#77): the real command's parser and risk
declaration, so the engine's assess() judges exactly what the real command would
get, but a main() that prints a canned diff instead of calling PagerDuty."""

import sys

from pagerduty_ops.commands.reorg import RISK, build_parser  # noqa: F401

PREVIEWS = "dry"  # "dry": previews with --dry-run; "apply": previews unless --apply


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    applying = (not getattr(args, "dry_run", False)) if PREVIEWS == "dry" else args.apply
    if applying:
        sys.stdout.write("APPLIED\n")
    else:
        sys.stdout.write("- removed-thing (P1)\n")
    return 0
