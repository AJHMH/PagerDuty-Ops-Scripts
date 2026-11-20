"""Which commands the UI can run, and how to launch each one (ADR 0004)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from pagerduty_ops.console_scripts import SCRIPTS
from pagerduty_ops.risk import BUILDER_FOR_ENTRY, Risk, declarations


@dataclass(frozen=True)
class CommandEntry:
    name: str         # console script name, e.g. "pd-list-users"
    module: str       # importable module path holding the entry function and RISK
    attr: str         # entry function, e.g. "main"
    builder: str      # parser builder whose risk declaration covers it


def engine_catalog() -> dict[str, CommandEntry]:
    return {
        name: CommandEntry(name, f"pagerduty_ops.commands.{module}", attr,
                           BUILDER_FOR_ENTRY[attr])
        for name, (module, attr) in SCRIPTS.items()
    }


def describe(entry: CommandEntry) -> dict:
    """Name, help description, and whether every mode of the command is read-only."""
    module = importlib.import_module(entry.module)
    declaration = declarations(module)[entry.builder]
    risks = [declaration] if isinstance(declaration, Risk) else list(declaration.values())
    return {
        "name": entry.name,
        "description": getattr(module, entry.builder)().description or "",
        "read_only": not any(r.writes for r in risks),
    }
