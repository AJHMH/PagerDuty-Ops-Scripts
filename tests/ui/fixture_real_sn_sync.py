"""Test-only stand-in for pd-sn-sync (#77, #79): the real command's parser and risk
declaration, so the engine's assess() judges exactly what the real command would get, and the
engine's own Rehearsal receipt steps (pagerduty_ops.receipts, the instance configuration and
the Plan hash), but a main() that prints a canned diff instead of calling ServiceNow.

Like the real `apply`: a production instance needs a matching receipt, even for a dry run, and
a clean rehearsal apply (not a dry run) writes one. The receipts store comes from
--receipts-dir or PD_OPS_RECEIPTS_DIR, as in the engine."""

import json
import sys

from pagerduty_ops.commands.sn_sync import RISK, build_parser  # noqa: F401
from pagerduty_ops.plan import plan_hash
from pagerduty_ops.receipts import receipts_dir, require_receipt, write_receipt
from pagerduty_ops.servicenow import ConfigError, get_instance_config

PREVIEWS = "dry"  # "dry": previews with --dry-run; "apply": previews unless --apply


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    applying = (not getattr(args, "dry_run", False)) if PREVIEWS == "dry" else args.apply
    if args.command == "apply":
        with open(args.plan_file, encoding="utf-8") as f:
            sha = plan_hash(json.load(f))
        try:
            inst = get_instance_config(args.instance, for_write=True)
        except ConfigError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        rdir = receipts_dir(args.receipts_dir)
        if inst.is_prod:
            require_receipt(rdir, sha=sha, rehearsal_instances=inst.rehearsal_instances)
    if applying:
        sys.stdout.write("APPLIED\n")
        if args.command == "apply" and inst.is_rehearsal:
            write_receipt(rdir, sha=sha, kind="sn-sync", instance=inst.name,
                          succeeded=1, skipped=0)
    else:
        sys.stdout.write("- removed-thing (P1)\n")
    return 0
