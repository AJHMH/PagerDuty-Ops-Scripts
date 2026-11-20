# Runs and their files are kept for 90 days; a permanent ledger keeps who changed what

The City's retention period for these Runs is 90 days; after that a Run's database record,
full diff and stored files are purged automatically. Because PagerDuty's audit log attributes
every UI change to the service credential (ADR 0006), purging everything would make "who did
this?" unanswerable after 90 days. So each Writing run also leaves a permanent ledger row —
Run ID, command, requester, approver, timestamps and outcome, with no inputs, diffs or output —
which is never purged. Records Management is to confirm the ledger fits City retention rules;
if 90 days must apply to all Run data, the ledger goes too and the gap is accepted.
