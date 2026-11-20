# Backlog

Work that was discussed and deliberately deferred. Each entry records the agreed design so it
can be picked up without re-deciding it.

## Tabled: incident storm cleanup (`pd-incidents`)

Status: **tabled by the maintainer** (2026-11-20). Agreed design so far:

- **Purpose:** clean up incident storms (not routine mass-resolve of stale incidents).
- **Selection:** `--service` (repeatable), `--since 2h` (required), `--status` (default
  `triggered,acknowledged`), optional `--title-regex`. Or a CSV of incident IDs.
- **`storm-plan`:** groups matched incidents by service plus a normalized title and writes a
  plan with one merge target per group (the **oldest** incident). Review, then `storm-apply`.
- **Actions:** `merge` (default), `resolve`, `ack`, `reassign --to EP/user`, `set-priority`.
- **API:** `PUT /incidents` (bulk manage, max 250 per call, own rate limit),
  `PUT /incidents/{id}/merge`. `PD_FROM_EMAIL` is required.
- **Safety:** always shows the matched set; `--max-incidents` defaults to 500 and higher values
  must be explicit; merges must stay within one service unless `--allow-cross-service`. Uses
  the same plan → review → apply flow and `--dry-run` / `-y` contract as everything else.

## Candidates from the PagerDuty API schema (not yet discussed)

| Area | Endpoints | Bulk value |
|---|---|---|
| Incident Workflows | `/incident_workflows`, triggers | Export/diff-apply workflows and bulk-attach triggers to services. |
| Automation Actions | `/automation_actions/actions`, runners, service/team associations | Bulk-associate actions with services and teams. |
| Business services | `/business_services` | Export/apply definitions (dependencies are already in `pd-service-deps`). |
| Team escalation-policy ownership audit | `/teams/{id}/escalation_policies` | Covered by `pd-reorg`; a standalone report may still help. |
| IP allow lists, session configurations | `/ip_allow_lists`, `/session_configurations` | Config-as-code with diff-first apply for security review. |
| Templates (status update / notification) | `/templates` | Export/apply for consistent messaging. |
| Slack / MS Teams / Jira integration specs | separate specs under `reference/` | Bulk channel or project mappings per service. |
| SCIM writes | `reference/scim` (`PATCH /Users`) | Deactivate users flagged by `pd-scim-user-audit` (a write counterpart). |

## Follow-ups from this round

- **First live runs:** run `pd-validate` (read-only), then `pd-sn-sync report` against
  `dallastx-dev`. Then do `--dry-run` passes of each write command against a sandbox scope.
- **Okta inbound OIDC for ServiceNow:** complete the setup with the Okta and ServiceNow admins
  ([servicenow-okta-setup.md](servicenow-okta-setup.md)).
- **PagerDuty-app business rules:** check whether they fire when `x_pd_integration_*` fields
  are updated, and tune the default `--max-writes` for `pd-sn-sync`.
- **Schedule team ownership:** confirm on one schedule that `PUT /schedules/{id}` with `teams`
  behaves as expected (`pd-reorg` relies on it; the OpenAPI schema marks it read-only).
