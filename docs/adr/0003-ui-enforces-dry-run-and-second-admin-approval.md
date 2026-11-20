# The UI enforces dry runs, and a second admin approves irreversible operations

In the UI, Apply is only possible for a dry run the same admin reviewed with identical inputs,
existing caps (`--max-deletes`, `--max-writes`) cannot be raised without a recorded override,
and an irreversible operation also needs an Approval from a different admin. The CLI only
*encourages* reviewing a dry run; a team UI with an Apply button for every command would
otherwise make skipping it a single click.

## Consequences

The UI still passes `-y` to the commands it runs, because the engine's `confirm()` refuses a
non-interactive stdin without it; the UI's own review and approval gates are what replace the
interactive prompt.
