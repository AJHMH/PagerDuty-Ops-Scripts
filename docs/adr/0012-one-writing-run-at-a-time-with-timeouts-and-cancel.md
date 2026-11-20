# One writing Run at a time, with timeouts and cancellation

Runs that write are queued so only one executes at a time across the account; reports and dry
runs run freely. Interleaved reorgs or syncs would make both Runs' diffs untrustworthy, and at
team scale the queue costs almost nothing. Every Run has a timeout (2 hours by default,
overridable per command in its declaration, ADR 0010) and can be cancelled by its requester or
any Approver. A cancelled or timed-out apply is recorded as Stopped, and the UI offers a fresh
dry run to show what remains — safe because applies are idempotent.
