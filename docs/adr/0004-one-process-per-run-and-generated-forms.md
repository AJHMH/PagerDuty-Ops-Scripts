# Each Run is a separate CLI process, and forms are generated from argparse

The UI service (FastAPI, with a React/TypeScript front end) executes every Run by starting
the command's console script as its own process, capturing stdout, stderr and the exit code,
and builds each command's form from that command's argparse parser rather than from
hand-written screens. Separate processes give exact behavioural parity with cron and the
terminal (ADR 0001), isolate one Run's crash, logging and HTTP session from the next, and make
timeouts and cancellation safe; generated forms keep parity across ~40 commands without a
second definition of every flag to drift.

## Considered Options

- Calling `main(argv)` in a background worker — rejected: process-wide state (logging,
  session, `.env`) leaks between Runs and a hung Run is hard to stop.
- Server-rendered Python with HTMX — viable and simpler to operate; rejected because plan
  diffs, approvals and live Run logs are the core of the UI and need a richer front end.
- Streamlit — rejected: a poor fit for Okta roles, approvals and an audit-grade Run history.
