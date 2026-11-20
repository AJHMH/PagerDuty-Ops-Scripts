#!/usr/bin/env python3
"""Shim — logic lives in pagerduty_ops.commands.cmdb_enrichment."""

from pagerduty_ops.cli import run
from pagerduty_ops.commands.cmdb_enrichment import main

if __name__ == "__main__":
    run(main)
