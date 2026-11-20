#!/usr/bin/env python3
"""Shim — logic lives in pagerduty_ops.commands.business_services."""

from pagerduty_ops.cli import run
from pagerduty_ops.commands.business_services import main

if __name__ == "__main__":
    run(main)
