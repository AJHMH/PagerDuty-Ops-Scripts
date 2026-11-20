#!/usr/bin/env python3
"""Shim — logic lives in pagerduty_ops.commands.sn_sync (see docs/servicenow.md)."""

from pagerduty_ops.cli import run
from pagerduty_ops.commands.sn_sync import main

if __name__ == "__main__":
    run(main)
