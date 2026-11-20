# The UI is a thin layer over the CLI engine

We are adding a UI for the admin team with full parity with the CLI, but the `pagerduty_ops`
commands remain the only place business logic and write-safety rules live: the UI drives the
same code and adds none of its own. Cron jobs, CI and the documented exit codes depend on the
CLI, and a single engine means the dry-run, caps, plans and rehearsal receipts cannot drift
between two implementations.

## Considered Options

- UI becomes the primary interface and the CLI is trimmed — rejected: breaks scheduled jobs.
- UI reimplements what it needs — rejected: duplicates the safety rules, which is how they erode.
