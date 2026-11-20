"""A test-only command the UI service runs as a real subprocess (Seam B).

It behaves like an engine command: a RISK declaration, build_parser() and main(argv), run
through pagerduty_ops.cli.run so stdout/stderr and exit codes are the real ones.
"""

import argparse
import os
import sys
import time

from pagerduty_ops.risk import READ_ONLY

RISK = READ_ONLY


def build_parser():
    p = argparse.ArgumentParser(description="UI test fixture.")
    p.add_argument("--print", dest="text", default="", help="Write this to stdout.")
    p.add_argument("--log", default="", help="Write this to stderr.")
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--print-env", help="Write the value of this environment variable.")
    p.add_argument("-r", "--report", help="A file argument (ignored).")
    p.add_argument("--sleep", type=float, default=0, help="Sleep this many seconds first.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.sleep:
        time.sleep(args.sleep)
    if args.text:
        sys.stdout.write(args.text + "\n")
    if args.print_env:
        sys.stdout.write(os.environ.get(args.print_env, "<unset>") + "\n")
    if args.log:
        sys.stderr.write(args.log + "\n")
    return args.exit_code

