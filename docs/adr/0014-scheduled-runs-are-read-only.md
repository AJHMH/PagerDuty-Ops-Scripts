# Scheduled Runs are read-only and come in phase 2

In phase 2 the UI can schedule read-only commands (for example a nightly `pd-validate` drift
report) in place of cron, so drift reports land in the same Run history as the fixes and the
Teams channel is told when drift appears. Scheduled writes are out of scope: a write nobody
reviewed or approved would defeat ADRs 0003 and 0011.
