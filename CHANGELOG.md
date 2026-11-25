# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/) · Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- Package metadata declares the license as an SPDX expression (`license = "MIT"`, with
  `license-files`), replacing the `license = { file = ... }` table that setuptools deprecated
  (it stops building such projects after 2027-02-18). Building from source now needs
  setuptools 77 or later; pip fetches it automatically.

## [1.2.0] - 2026-11-25

There is no 1.1.0 release: a `v1.1.0` tag from April points at a commit whose version was
still 1.0.0, so the release workflow refused it.

### Added
- **Team web UI** (`pagerduty_ops_ui` service, `ui/` front end; install with
  `pip install -e ".[ui]"`). It is a thin layer over the CLI: each Run is the command's own
  process, and what may run comes from the engine (ADRs 0001–0016, `docs/ui.md`).
  - Okta sign-in with Viewer, Operator and Approver roles (#86).
  - Generated forms for every command, with file uploads and downloads (#87).
  - A live Run log (#94) and a searchable Run history (#95).
  - Dry runs of writing commands with a review of the diff (#88). An apply comes from a
    reviewed dry run, and is refused if a fresh dry run no longer matches it (#89).
  - One writing Run at a time, with timeouts and cancel (#92).
  - Raising a write cap needs a recorded reason (#96).
  - Approvals for Irreversible operations (#97), announced in Teams through a Workflows
    webhook (`PD_UI_TEAMS_WEBHOOK_URL`) (#106).
  - A permanent Ledger of Writing runs, and a 90-day Run purge (#98).
  - `PD_UI_MAX_PHASE` caps each server's rollout phase, read only by default (#100).
  - ServiceNow `pd-sn-sync` Runs, with one shared rehearsal receipts store so a production
    apply finds its rehearsal's receipt (#99, #101).
  - Scheduled read-only Runs in Central time, with Teams notifications when a scheduled
    `pd-validate` finds Drift (#107).
  - Runs are stored in SQL Server, with the schema managed by Alembic migrations
    (`pd-ui-migrate`) (#93). Reads that lose a SQL Server deadlock are retried (#105).
  - A Windows Server installation guide (`docs/install-windows.md`) (#103, #104).
- Every command declares its risk (`RISK`), and `pagerduty_ops.risk.assess()` says whether a
  command line writes, is a dry run, or is an Irreversible operation (#83).
- `pd-remove-team-members --user-ids`: a non-interactive mode for scripted use (#85).
- `pd-validate` declares what Drift looks like (`DRIFT_EXIT_CODE`, `DRIFT_SUMMARY`), for
  Scheduled runs (#107).
- `CONTEXT.md` glossary and architecture decision records in `docs/adr/` (#81).
- `pd-sn-sync` (report / plan / apply): reconciles PagerDuty IDs stored on ServiceNow
  `sys_user_group` and `sys_user` records, with PagerDuty as the source of truth. Groups are
  anchored on their service ID. Ambiguous matches are reported, never written. Stale IDs are
  cleared only with `--prune`. Membership differences are report-only.
- ServiceNow Table API client with pluggable auth (`okta` client credentials, `sn-oauth`,
  `basic`), a production guard (`SN_PROD_INSTANCES`, fails closed), and rehearsal receipts.
  A production apply requires the identical plan to have succeeded on a rehearsal instance
  within 7 days.
- `pd-validate`: read-only consistency checks (user/team ownership, EP/service team
  alignment, coverage gaps, unreachable responders, staffing, utilization, orphaned resources,
  naming, ServiceNow extension/mapping and ID drift). Severities are configurable, `--fail-on`
  sets the exit code, and `--plan-out` writes a remediation plan for `pd-reorg`.
- `pd-reorg` (export / plan / apply): team reorganizations from a moves CSV and/or an
  authoritative per-team YAML file. Both compile into one conflict-checked plan covering
  membership, team and account roles, and escalation-policy and schedule ownership.
  `--prune` is scoped to listed teams and keys. Service ownership is verified against EP
  ownership. Optionally writes a ServiceNow follow-up plan for the affected teams.
- `pd-service-deps` (export / apply): bulk technical and business service dependencies from
  CSV. It is idempotent (reads existing relationships first) and batches associate and
  disassociate calls.
- `pd-schedule-overrides`: bulk create and delete overrides from CSV for v2 schedules, or
  v3 shift-based schedules with `--v3`. Users can be looked up by exact email. It is
  idempotent and reports per-override rejections.
- `pd-subscriptions` (list / apply): bulk notification subscriptions for users and teams to
  business services and incidents. It is idempotent and groups changes per subscriber.
- `pd-custom-fields` (export / apply): git-friendly, name-keyed JSON for service custom
  fields, incident types, and incident-type custom fields, with a diff-first apply.
  Deletions require `--prune`. Changes that can't be made in place (`data_type`,
  `field_type`, `parent_type`) exit 1.
- `pd-cmdb-enrichment` (**Early Access**, gated by `--early-access`): idempotent apply of
  ServiceNow CMDB tables and enrichment schemas, CSV record upload, and event-enrichment
  associations. ServiceNow credentials are deliberately out of scope.
- `api.request` accepts `raw_body` (for non-JSON uploads) and sends a JSON body on DELETE
  when one is given.
- `docs/backlog.md`: the tabled incident-storm design, unexplored API-schema candidates,
  and live-verification follow-ups.
- `pd-service-field-values` (export / apply): bulk service custom field values from a
  wide, round-trippable CSV. Every cell is validated against the field's type and options
  before any write. It sends one atomic `PUT` per service with only the changed fields, and
  `--clear-empty` resets values.
- `pd-event-rules-migrate` (inventory / convert / activate / deactivate): migrates legacy
  service Event Rules to Event Orchestration in two explicit steps. Changes need explicit
  targets and are capped by `--max-services`. Activation is refused while Event Rules that
  haven't been converted remain. `inventory --fail-on-unmigrated` supports cron.
- `pd-incident-workflows` (export / diff / triggers-export / triggers-apply): git-friendly
  workflow exports with a read-only drift diff (`--fail-on-drift`), plus idempotent bulk
  attach and detach of workflow triggers to services from CSV.
- `pd-scim-user-sync` (plan / apply): the write side of `pd-scim-user-audit`. It corrects
  display-name drift and deletes users the IdP marks inactive (SCIM `active: false`
  **deletes** the user). Deletions are blocked for the owner, for anyone still on a v2 or
  v3 schedule or an escalation policy, and when v3 membership can't be checked. `apply` re-checks live, refuses plans older than 24 hours,
  and caps deletions (`--max-deletes`, default 10). Orphans are deleted only with
  `--delete-orphans`.
- `pd-business-services` (export / apply): name-keyed JSON for business service definitions
  (description, point of contact, owning team) with a diff-first apply. Deletions need
  `--prune` and are capped by `--max-deletes`.
- `pd-security-config` (export / diff / apply-sessions): IP allow lists and session
  configurations as code. Allow lists are exported and diffed but never written, because of
  the lockout risk. Session configurations are validated and written only when changed,
  with a warning that existing sessions are revoked. `diff --fail-on-drift` supports cron.
- `pd-templates` (export / apply): status-update templates as name-keyed JSON with a
  diff-first apply, so wording changes are reviewed in git. Deletions need `--prune` and
  are capped.
- Dependency: PyYAML.
- Shared plan-file format (`pagerduty_ops/plan.py`) and a cached account snapshot
  (`pagerduty_ops/pdstate.py`).

### Changed
- CI also runs the unit suite on Windows, the UI service tests on SQL Server, and the
  front-end build. Command output is byte-identical on Windows (#84, #93).

### Fixed
- Bulk CSV commands no longer silently skip incomplete rows. Previously, a row missing a
  required value (for example `detach,T1,`) was dropped, the valid rows were written, and
  the run exited 0. Now the whole file is rejected before any write (exit 2), with every
  offending line listed. Rows with more cells than the header are rejected the same way,
  and completely blank rows are still ignored. This affects `pd-apply-tags`,
  `pd-alert-grouping`, `pd-bulk-extensions`, `pd-bulk-maintenance-window`,
  `pd-cmdb-enrichment`, `pd-schedule-overrides`, `pd-service-deps`,
  `pd-service-field-values` and `pd-subscriptions`. `pd-apply-tags` also rejects unknown
  `entity_type` and `action` values instead of warning and skipping them.
  `load_csv_rows(skip_if_missing=)` is renamed to `require_values=`.
- `pd-scim-user-audit` reads its CSV strictly. Previously, a row without an email was
  skipped and any unrecognized `active` value (for example the typo `ture`) counted as
  inactive. Both are now validation errors, as are duplicate emails.

## [1.0.0] - 2026-06-11

### Added
- `pagerduty_ops` package: shared HTTP client with automatic retries and 429
  `Retry-After` backoff, typed `PDApiError`, structured stderr logging
  (`-v/-q/--log-file`), table/csv/json renderers, and a documented exit-code
  contract (0 ok / 1 failures / 2 usage / 3 auth / 130 interrupt).
- Console commands for every script (`pd-list-users`, `pd-patch-role`, …) via
  `pip install -e .`.
- Idempotency: maintenance-window creation skips identical existing windows;
  extension attachment skips services already carrying the same extension.
- Input validation: ISO 8601 timestamps (timezone required), https-only
  endpoint URLs, role enums, CSV column checks — all before any write.
- Test suite: 60+ unit tests (mocked HTTP via `responses`) and opt-in
  read-only integration tests gated on `PD_TEST_TOKEN`.
- Real CI (`ci.yml`): ruff, pytest on 3.10/3.12 with coverage, pip-audit,
  gitleaks full-history scan. Tag-driven `release.yml`.
- Docs: architecture, per-command usage, secrets-management guide (including
  token-scope table), operational runbook.

### Changed
- All `pd_*.py` scripts are now thin shims over the package; arguments and
  behavior preserved.
- All diagnostics moved to stderr — piped CSV/JSON output can no longer be
  corrupted by progress or error text.
- Bulk commands now exit 1 on partial failure (previously always 0) and abort
  immediately on auth errors.
- Team member operations are fully paginated (previously silently truncated
  at the API default of 25 — `pd_remove_team_members` could remove a user
  from a team while leaving them on an unseen schedule).
- API error logging redacted to PagerDuty's error envelope (no response-body
  PII in logs).
- `-t/--token` deprecated (warns); use `PD_API_TOKEN`.

### Removed
- Duplicated API client in `pd_export_ids.py` (and its 4 unused imports).
- Demo CI workflow (replaced by real gates; stub retained as deprecated).
- `prettytable` dependency (standardized on `tabulate`).

### Deprecated
- `pd_common` module (re-exports preserved, removal no earlier than v2.0.0).
- `pd_get_teams_user_role.py` (use `pd-team-members` or the PagerDuty MCP
  server's `list_team_members`).
