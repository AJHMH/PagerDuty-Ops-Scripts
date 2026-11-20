# Each command declares its risk in the engine, not in the UI

argparse describes a command's flags but not whether it writes or which flags make it an
irreversible operation. Each command module therefore declares that next to `build_parser()`
(for example `writes=True, irreversible_when=["--prune"]`, or `irreversible=True` for SCIM
delete and Event Rules convert), and `tests/unit/test_commands.py` fails any module that does
not. The UI reads the declaration to decide roles, dry-run gating and Approval; it keeps no
risk table of its own, so a new command cannot reach the UI without stating its risk (ADR 0001).

## Consequences

- Plan-based applies are judged from the Plan's items (SCIM `delete_user`, ServiceNow
  values being cleared), never its recorded options: options are not part of the plan hash,
  so an edited options block must not be able to hide a deletion.
- `pd-reorg` applies, including Plans compiled with `--prune`, are **not** irreversible.
  Prune removals are indistinguishable from move removals inside a Plan, so an
  option-based check could be bypassed, and team membership and resource-team removals can
  be re-added. Reorgs still require a reviewed dry run; they do not need an Approval.
- Single-object deletes (`pd-alert-grouping --delete`, deleting schedule overrides) stay
  reversible: the dry run is enforced and the object can be recreated by hand. The
  Irreversible operation list in `CONTEXT.md` is the whole list.
- `pd-remove-team-members` runs unattended only in its `--user-ids` mode (#65); the UI runs it
  only that way, never the interactive mode.
