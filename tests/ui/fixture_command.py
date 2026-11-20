"""A test-only command the UI service runs as a real subprocess (Seam B).

It behaves like an engine command: a RISK declaration, build_parser() and main(argv), run
through pagerduty_ops.cli.run so stdout/stderr and exit codes are the real ones.
"""

import argparse
import os
import sys
import time

from pagerduty_ops.cli import add_logging_args, add_token_args
from pagerduty_ops.risk import READ_ONLY

RISK = READ_ONLY


def build_parser():
    p = argparse.ArgumentParser(description="UI test fixture.")
    add_token_args(p)    # the engine's real -t/--token and --prompt
    add_logging_args(p)  # -v/-q/--log-file, so short options can be clustered
    p.add_argument("--print", dest="text", default="", help="Write this to stdout.")
    p.add_argument("--log", default="", help="Write this to stderr.")
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--print-env", help="Write the value of this environment variable.")
    p.add_argument("-r", "--report", help="A file argument (ignored).")
    p.add_argument("-o", "--output", help="Write a small report to this file.")
    p.add_argument("-i", "--input", help="Echo this file's contents to stdout.")
    p.add_argument("--input-dir", help="List the files in this directory on stdout.")
    p.add_argument("--status", action="append", help="Repeatable; echoed on stdout.")
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
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            sys.stdout.write(f.read())
    if args.input_dir:
        sys.stdout.write(",".join(sorted(os.listdir(args.input_dir))) + "\n")
    if args.status:
        sys.stdout.write("status=" + "|".join(args.status) + "\n")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("report\n")
    if args.log:
        sys.stderr.write(args.log + "\n")
    return args.exit_code

