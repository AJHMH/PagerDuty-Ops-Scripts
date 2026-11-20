# The UI rolls out in phases, read-only first

- **Phase 0 — engine only, one PR each:** a `windows-latest` CI job (ADR 0005), command risk
  declarations (ADR 0010), and the non-interactive `pd-remove-team-members` (ADR 0009).
- **Phase 1 — read-only service:** Okta sign-in, Run history, and every report and dry run;
  no Apply. The team and City security review the service before it can write anything.
- **Phase 2 — Apply for changes that can be undone,** with the writing-run queue, timeouts
  and cancellation (ADRs 0011, 0012).
- **Phase 3 — irreversible operations** with Approvals, including ServiceNow production
  applies; then read-only Scheduled runs (ADR 0014).

Each phase is usable on its own, so the UI earns trust before it gains the power to write.
