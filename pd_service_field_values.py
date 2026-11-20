#!/usr/bin/env python3
"""Shim — logic lives in pagerduty_ops.commands.service_field_values."""

from pagerduty_ops.cli import run
from pagerduty_ops.commands.service_field_values import main

if __name__ == "__main__":
    run(main)
