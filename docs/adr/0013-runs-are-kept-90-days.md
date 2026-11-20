# Runs and their files are kept for 90 days

The City's retention period for these Runs is 90 days; after that a Run's database record and
its stored files are purged automatically. Because PagerDuty's audit log attributes every UI
change to the service credential (ADR 0006), who requested and approved a change older than
90 days is no longer recoverable.
