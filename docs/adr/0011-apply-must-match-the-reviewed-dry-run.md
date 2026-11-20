# Apply re-runs the dry run and is blocked if it differs from the one reviewed

Live state can change between the reviewed dry run and Apply, especially while an Approval is
pending. Immediately before an apply, the UI runs a fresh dry run with identical inputs and
compares its output with the reviewed one; any difference blocks the apply and the new dry run
must be reviewed again (and, for an irreversible operation, approved again). Approvals expire
24 hours after they are given. Approvers learn of pending requests through a Microsoft Teams
channel webhook and a "Waiting for approval" queue in the UI.

Relying on the engine's idempotence alone would be safe for re-runs but would let an admin
approve one diff while a different one is written.
