"""Test-only stand-in for pd-validate (#80): the real command's parser, risk declaration and
Drift declaration (DRIFT_EXIT_CODE, DRIFT_SUMMARY), but a main() that reports canned
findings instead of reading PagerDuty.

UI_FIXTURE_FINDINGS ("info=0, warning=1, error=2") are the counts it reports; it logs them
on the "Findings: ..." line and exits as the real command does for --fail-on.
UI_FIXTURE_API_FAIL makes it fail the way an API error does: exit 1, no summary line.
"""

import os
import sys

from pagerduty_ops.commands.validate import (  # noqa: F401
    DRIFT_EXIT_CODE,
    DRIFT_SUMMARY,
    RISK,
    SEV_RANK,
    SEVERITIES,
    build_parser,
)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("UI_FIXTURE_API_FAIL"):
        sys.stderr.write("ERROR   pd_ops.validate: GET /users -> HTTP 503\n")
        return 1
    raw = os.environ.get("UI_FIXTURE_FINDINGS", "info=0, warning=0, error=0")
    counts = {k: int(v) for k, v in (part.strip().split("=") for part in raw.split(","))}
    sys.stderr.write(f"INFO    pd_ops.validate: {DRIFT_SUMMARY}"
                     f"{', '.join(f'{s}={counts.get(s, 0)}' for s in SEVERITIES)}\n")
    if args.fail_on == "never":
        return 0
    threshold = SEV_RANK[args.fail_on]
    found = any(counts.get(s, 0) and SEV_RANK[s] >= threshold for s in SEVERITIES)
    return DRIFT_EXIT_CODE if found else 0
