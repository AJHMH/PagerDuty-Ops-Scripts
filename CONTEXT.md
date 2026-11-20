# PagerDuty Ops

Bulk administration of the City's PagerDuty account and the PagerDuty IDs mirrored in
ServiceNow, run by the PagerDuty/ServiceNow admin team.

## Language

### Changing the account

**Plan**:
A reviewable file listing the exact changes a command intends to make; applying it is a separate step.
_Avoid_: Change set, job file

**Dry run**:
Performing every step of an apply except the writes, so the result can be reviewed first.
_Avoid_: Preview, simulation

**Diff-first apply**:
Applying a desired-state file by first showing how it differs from live, and writing only on explicit confirmation.
_Avoid_: Sync, push

**Prune**:
Deleting live objects that are absent from a desired-state file; never done without being asked for explicitly.
_Avoid_: Clean up, purge

**Drift**:
Any difference between a reviewed desired-state file and what is live.
_Avoid_: Mismatch, discrepancy

### ServiceNow

**Rehearsal instance**:
The non-production ServiceNow instance where a plan must succeed before it may be applied to production.
_Avoid_: Test instance, sandbox

**Rehearsal receipt**:
Proof that a specific plan succeeded on a rehearsal instance recently; production refuses a plan without one.
_Avoid_: Approval, sign-off

**Anchor service**:
The PagerDuty service a ServiceNow group points at, from which that group's other PagerDuty IDs are derived.
_Avoid_: Primary service

**Irreversible operation**:
A change that cannot be undone once applied: SCIM user deletion, Event Rules conversion, any prune, and any ServiceNow production apply.
_Avoid_: Destructive change, dangerous action

### Running the tools

**Run**:
One execution of a command: who ran it, its inputs, whether it was a dry run or an apply, the plan or diff it produced, its output and its outcome.
_Avoid_: Job, task, execution

**Approval**:
A second admin's recorded consent to apply a reviewed dry run of an irreversible operation; the admin who requested the run cannot approve it.
_Avoid_: Sign-off, rehearsal receipt (a receipt proves a rehearsal, not a person's consent)

**Writing run**:
A Run of a command that can change PagerDuty or ServiceNow and is not a dry run; only one executes at a time.
_Avoid_: Write job, mutation

**Stopped**:
The outcome of an apply that was cancelled or timed out, which may have been partly applied.
_Avoid_: Aborted, killed, failed (failed means the command itself reported failures)

**Ledger**:
The permanent record of who requested and approved each Writing run, kept after the Run itself is purged.
_Avoid_: Audit log (that is PagerDuty's own), history

**Scheduled run**:
A read-only Run started on a schedule by the UI rather than by an admin.
_Avoid_: Cron job, recurring task

### People

**Admin**:
A member of the PagerDuty/ServiceNow admin team who operates these tools.
_Avoid_: User (ambiguous with PagerDuty users)

**Viewer**:
An admin who may read reports and the Run history but run nothing that writes.
_Avoid_: Read-only user

**Operator**:
An admin who may also dry-run and apply changes that can be undone.
_Avoid_: Editor, power user

**Approver**:
An Operator who may also give an Approval for another admin's irreversible operation.
_Avoid_: Reviewer, supervisor
