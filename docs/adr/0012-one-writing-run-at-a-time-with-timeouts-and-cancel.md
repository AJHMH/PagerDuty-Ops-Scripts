# One writing Run at a time, with timeouts and cancellation

Runs that write are queued so only one executes at a time across the account; reports and dry
runs run freely. Interleaved reorgs or syncs would make both Runs' diffs untrustworthy, and at
team scale the queue costs almost nothing. Every Run has a timeout (2 hours by default,
overridable per command in its declaration, ADR 0010) and can be cancelled by its requester or
any Approver. A cancelled or timed-out apply is recorded as Stopped, and the UI offers a fresh
dry run to show what remains — safe because applies are idempotent.

## Consequences

- One writer thread in the service keeps applies apart, so the service runs as **one process**. ADR 0007 chose SQL Server partly to allow a second service instance; a second instance would need a database lock on the writing slot first (#74).
- The queue is the Run store's queued rows, so it survives a restart. At startup, whatever was executing is Stopped, because a restart cut it off; queued applies resume in order. An apply stopped by a clean shutdown before it began writing (during its fresh dry run) goes back to the queue.
- Each Run has its own timeout, so an apply's fresh dry run and the apply itself each get one.
