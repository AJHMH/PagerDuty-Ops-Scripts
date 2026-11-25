# Runs and their files are kept for 90 days; a permanent ledger keeps who changed what

The City's retention period for these Runs is 90 days; after that a Run's database record,
full diff and stored files are purged automatically. Because PagerDuty's audit log attributes
every UI change to the service credential (ADR 0006), purging everything would make "who did
this?" unanswerable after 90 days. So each Writing run also leaves a permanent ledger row —
Run ID, command, requester, approver, timestamps and outcome, with no inputs, diffs or output —
which is never purged. Records Management is to confirm the ledger fits City retention rules;
if 90 days must apply to all Run data, the ledger goes too and the gap is accepted.

## Consequences

- The Ledger is a separate table written in the same transaction that finishes each apply, so the purge, which deletes only Runs and their files, can't take it with them (#76).
- Until Records Management confirms the Ledger, it can be switched off with `PD_UI_LEDGER=off`; the purge then removes Ledger rows older than 90 days too.
- The purge runs inside the service, at startup and daily, so the Windows host needs no scheduled task for it.
