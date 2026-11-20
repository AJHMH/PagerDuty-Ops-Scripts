#!/usr/bin/env python3
"""Shim — logic lives in pagerduty_ops.commands.event_rules_migrate."""

from pagerduty_ops.cli import run
from pagerduty_ops.commands.event_rules_migrate import main

if __name__ == "__main__":
    run(main)
