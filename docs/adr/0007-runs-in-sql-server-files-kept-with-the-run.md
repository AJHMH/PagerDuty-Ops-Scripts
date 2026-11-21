# Runs are stored in SQL Server; each Run keeps its input and output files

Run records (who requested and approved, inputs, dry run or apply, exit code, timestamps) live
in a City-managed SQL Server database, so backups and the records-retention period are handled
by the DBAs rather than by the admin team. A Run's uploaded inputs and generated outputs
(CSV/YAML/JSON, exports, plans, logs) are stored with the Run on the service's disk or file
share, with only their paths and SHA-256 hashes in the database, and can be downloaded from
the Run. Rehearsal receipts live in one shared directory on the service, so a plan rehearsed by
one admin can be applied to production by another.

## Considered Options

- SQLite on the VM — rejected: the admin team would own backups of the audit trail, and it
  rules out a second service instance.
- Admins keep files on their own machines — rejected: a Run without its exact input and output
  files is not auditable, and per-admin receipts would block two-person production applies.

## Consequences

- The schema is managed by Alembic migrations, applied by `pd-ui-migrate` on each deploy with an account that may change the schema. The service only checks the schema at startup and refuses to start on any other, so its own account needs only read and write on the tables (#70).
- CI runs the service's test suite on SQL Server as well as SQLite, so a difference between them is caught before deployment (#70).
