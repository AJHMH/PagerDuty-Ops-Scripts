# Each command declares its risk in the engine, not in the UI

argparse describes a command's flags but not whether it writes or which flags make it an
irreversible operation. Each command module therefore declares that next to `build_parser()`
(for example `writes=True, irreversible_when=["--prune"]`, or `irreversible=True` for SCIM
delete and Event Rules convert), and `tests/unit/test_commands.py` fails any module that does
not. The UI reads the declaration to decide roles, dry-run gating and Approval; it keeps no
risk table of its own, so a new command cannot reach the UI without stating its risk (ADR 0001).
