# Usage — command catalog

Conventions shared by every command: `--help`; `-v/--verbose`, `-q/--quiet`, `--log-file FILE`; token from `PD_API_TOKEN` (or `--prompt` locally). Read commands: `-f table|csv|json`, `-o FILE`. Write commands: `--dry-run` and `-y/--yes` — **always review a `--dry-run` first**, then re-run with `-y`. Legacy `python pd_*.py` names accept identical arguments.

## Read / export commands

### pd-list-users
```bash
pd-list-users -f csv -o users.csv
pd-list-users --filter "smith" -f json
```

### pd-list-teams
```bash
pd-list-teams --filter platform
```

### pd-list-services
```bash
pd-list-services -f csv -o services.csv
pd-list-services --filter prod --team-id PXXXXXX -f json
```

### pd-list-schedules (v2)
```bash
pd-list-schedules --filter "SRE" -f csv -o schedules.csv
pd-list-schedules --name-filter "SRE" --team-id PXXXXXX   # --name-filter still works
```

### pd-list-escalation-policies
```bash
pd-list-escalation-policies -f csv -o escalation_policies.csv
pd-list-escalation-policies --filter platform --team-id PXXXXXX
```

### pd-list-webhooks
```bash
pd-list-webhooks -f csv -o webhooks.csv
pd-list-webhooks --service-id PXXXXXX --filter datadog
pd-list-webhooks --team-id PXXXXXX -f json
```

### pd-list-incidents
```bash
pd-list-incidents --since 2026-05-01T00:00:00Z --until 2026-06-01T00:00:00Z \
    --status triggered,acknowledged --team-id PXXXXXX -f csv -o incidents.csv
```
Unbounded exports warn and are capped at 10,000 records by the API — use `--since/--until` windows.

### pd-list-status-pages
```bash
pd-list-status-pages
pd-list-status-pages --posts PSTATUSPAGEID -f json
```

### pd-v3-schedules (Early Access — inventory only)
```bash
pd-v3-schedules
pd-v3-schedules --get PSCHEDID --include-users
```

### pd-export-ids
```bash
pd-export-ids -f csv -o inventory.csv
```

### pd-audit-export
```bash
pd-audit-export --since 2026-04-01T00:00:00Z --until 2026-05-01T00:00:00Z \
    --action update --root-resource-type services -f csv -o audit.csv
```

### pd-export-log-entries
```bash
pd-export-log-entries --since 2026-05-01T00:00:00Z --until 2026-06-01T00:00:00Z -f csv -o logs.csv
pd-export-log-entries --incident-id PT4KHLK -f json
```

### pd-export-change-events
```bash
pd-export-change-events --since 2026-05-01T00:00:00Z -f csv -o changes.csv
pd-export-change-events --service-id PIJ90N7 --since 2026-05-01T00:00:00Z
pd-export-change-events --incident-id PT4KHLK -f json
```

### pd-scim-user-audit (token needs SCIM scope)
```bash
pd-scim-user-audit expected_users.csv -o scim_report.txt
```
CSV columns: `email, displayName, active`. Validation is strict: a row without an email, an unrecognized `active` value (accepted: `true/false`, `yes/no`, `1/0`, `active/inactive`) or a duplicate email exits 2.

### pd-standards-report
```bash
pd-standards-report --resource-type technical_services --failing-only -f csv -o standards.csv
```

### pd-team-members
```bash
pd-team-members --team-id PXXXXXX -f csv      # or PD_TEAM_ID
```

### pd-eo-export
```bash
pd-eo-export -o event_orchestrations/         # commit the directory to git
```

### pd-validate (read-only consistency checks)
```bash
pd-validate                                         # all checks; exit 1 on any error finding
pd-validate --fail-on warning -f csv -o findings.csv
pd-validate --checks team_staffing,ep_coverage_gaps
pd-validate --config validate.json --plan-out fixes.json      # remediation plan for pd-reorg
pd-validate --servicenow --instance dallastx                  # + ServiceNow checks 9/10
```

| # | Check id | Default severity | Finds | Remediation plan |
|---|---|---|---|---|
| 1 | `user_not_on_owning_team` | warning | user on a team's schedule/EP but not on that team | `add_member` (single owning team) |
| 2 | `service_ep_team_mismatch` | warning | service EP with no/several teams; EP targeting other teams' schedules | `move_resource` EP (when its schedules agree on one team) |
| 3 | `ep_coverage_gaps` | error / warning | EP with no rules or empty rules; schedule with no users; coverage < 100% over `coverage_days` | — |
| 4 | `unreachable_responders` | error | on-call user missing, with no contact methods, or no high-urgency rule | — |
| 5 | `team_staffing` | warning | team with no members, no manager, or one member | — |
| 6 | `user_utilization` | info | user on no team; responder-capable role on no rotation | — |
| 7 | `orphaned_resources` | warning / info | schedule/EP/service with no team; unused schedule/EP | — |
| 8 | `naming_conventions` | info | names not matching `naming` regexes | — (see `pd-rename-resources`) |
| 9 | `servicenow_integration` | warning | service without a ServiceNow extension; with `--servicenow`, no group references it | — |
| 10 | `servicenow_drift` | warning | `pd-sn-sync` drift (needs `--servicenow`) | use `pd-sn-sync plan` |

Config (`--config`, JSON; every key optional):
```json
{
  "checks": {"user_utilization": {"enabled": false}, "naming_conventions": {"severity": "warning"}},
  "coverage_days": 14,
  "servicenow_extension_pattern": "servicenow",
  "naming": {"services": "^[A-Z]{2,5} - ", "teams": "^DAL "},
  "default_member_role": "responder"
}
```
The validator never writes. `--plan-out` produces a `pd-reorg` plan. Review it, then run `pd-reorg apply fixes.json --dry-run`.

## Write commands

### pd-eo-apply (diff-first)
```bash
pd-eo-apply -i event_orchestrations/                                   # diff only
pd-eo-apply -i event_orchestrations/ --apply -y --from-email you@org.com
```

### pd-patch-role
```bash
pd-patch-role --from-role user --to-role observer --dry-run
pd-patch-role --from-role user --to-role observer -y
```

### pd-rename-resources (idempotent)
```bash
pd-rename-resources --resource services --suffix " SVC" --dry-run
pd-rename-resources --resource schedules --prefix "[SRE] " --filter prod -y
pd-rename-resources --resource escalation_policies --list
```

### pd-update-team-roles
```bash
pd-update-team-roles --team-id PXXXXXX                       # interactive
pd-update-team-roles --set-role responder --dry-run          # bulk preview
pd-update-team-roles --set-role responder -y                 # bulk apply
```

### pd-remove-team-members (interactive offboarding)
```bash
pd-remove-team-members --team-id PXXXXXX --dry-run
pd-remove-team-members --team-id PXXXXXX
```
Walks each member through removal from schedule layers and escalation-policy targets **before** team removal. Fully paginated (no 25-member truncation).

### pd-service-urgency
```bash
pd-service-urgency --dry-run && pd-service-urgency -y
```

### pd-bulk-maintenance-window (idempotent)
```bash
pd-bulk-maintenance-window windows.csv --from-email you@org.com --dry-run
pd-bulk-maintenance-window windows.csv --from-email you@org.com -y
```
CSV: `service_id, start_time, end_time, description` (ISO 8601 **with timezone**; validated before any write). Rows whose identical window already exists are skipped, so re-runs are safe.

### pd-apply-tags
```bash
pd-apply-tags tags.csv --dry-run
pd-apply-tags tags.csv -y
```
CSV: `entity_type (users|teams|services|escalation_policies), entity_id, tag_label, action (add|remove)`. Grouped into one atomic `change_tags` call per entity.

### pd-bulk-extensions (idempotent)
```bash
pd-bulk-extensions --schema "Generic Webhook" --name "Datadog hook" \
    --endpoint-url https://example.com/hook --service-filter prod --dry-run
```
`--endpoint-url` must be `https://`. Services already carrying an identical extension are skipped.

### pd-alert-grouping
```bash
pd-alert-grouping --list
pd-alert-grouping --attach "Intelligent grouping" --services-csv services.csv --dry-run
pd-alert-grouping --get-json PZC4OM1 -o setting.json
pd-alert-grouping --create-json new_setting.json --dry-run
pd-alert-grouping --update-json setting.json -y
pd-alert-grouping --delete PZC4OM1 --dry-run
```

### pd-reorg (team reorganizations)
```bash
pd-reorg export -o teams.yaml [--team-ids PT1,PT2]
pd-reorg plan --moves moves.csv --teams-yaml teams.yaml [--prune] --plan-out reorg_plan.json
pd-reorg apply reorg_plan.json --dry-run
pd-reorg apply reorg_plan.json -y [--sn-plan-out sn_plan.json --instance dallastx]
pd-reorg apply fixes.json --dry-run        # remediation plan from pd-validate --plan-out
```
Conflicting inputs are refused (exit 2). Services follow their escalation policy and are verified, not moved. Formats: [reorg.md](reorg.md).

### pd-service-deps (idempotent)
```bash
pd-service-deps export -o deps.csv                  # re-appliable CSV of all dependencies
pd-service-deps apply deps.csv --dry-run
pd-service-deps apply deps.csv -y [--batch-size 100]
```
CSV: `action (associate|disassociate), supporting_kind, supporting_id, dependent_kind, dependent_id`. Kinds are `service` or `business_service`. Existing relationships of each dependent are read first, and no-ops are skipped. A relationship that is both associated and disassociated in the same file is rejected. PagerDuty limits a service to 2,000 dependencies.

### pd-schedule-overrides (idempotent)
```bash
pd-schedule-overrides holiday_coverage.csv --dry-run
pd-schedule-overrides holiday_coverage.csv -y
pd-schedule-overrides v3_coverage.csv --v3 -y          # shift-based (v3) schedules
```
CSV: `action (create|delete), schedule_id, user_id | user_email, start, end` (ISO 8601 **with timezone**).
- **Emails** are matched exactly, ignoring case. A row whose email matches zero or several users fails validation, and nothing is written.
- **Re-runs are safe:** identical existing overrides are skipped on create, and missing ones on delete.
- **v3 (`--v3`)** also needs `overridden_user_id | overridden_user_email`, the person being replaced, plus exactly one of `rotation_id` or `custom_shift_id`.

```csv
action,schedule_id,user_id,user_email,start,end
create,PSCHED1,,jane.doe@dallas.gov,2026-12-24T17:00:00-06:00,2026-12-26T08:00:00-06:00
```

### pd-subscriptions (idempotent)
```bash
pd-subscriptions list -o subs.csv                    # via business services (default)
pd-subscriptions list --by team -f json
pd-subscriptions apply subs.csv --dry-run
pd-subscriptions apply subs.csv -y
```
CSV: `action (subscribe|unsubscribe), subscriber_kind (user|team), subscriber_id, subscribable_kind (business_service|incident), subscribable_id`. PagerDuty supports notification subscriptions only to business services and incidents. Existing subscriptions are read first, and changes are grouped into one call per subscriber.

### pd-custom-fields (export → diff-first apply)
```bash
pd-custom-fields export -o custom_fields/            # commit the directory to git
pd-custom-fields apply -i custom_fields/             # diff only (no writes)
pd-custom-fields apply -i custom_fields/ --apply -y  # create/update
pd-custom-fields apply -i custom_fields/ --apply -y --prune   # also delete fields/options not in files
```
Covers service custom fields, incident types, and incident-type custom fields, including fixed-value options. The files are keyed by `name`, not ID, so they work across accounts.
- **Deletions need `--prune`,** because deleting a field deletes its data.
- **Incident types can't be deleted** through the API. Set `enabled: false` instead.
- **Some changes can't be made in place:** a change to `data_type`, `field_type` or `parent_type` is reported and exits 1.
- **Deprecated endpoints:** `/incidents/custom_fields` is not used.

### pd-cmdb-enrichment (PagerDuty **Early Access** — `--early-access` required)
```bash
pd-cmdb-enrichment list --early-access
pd-cmdb-enrichment tables-apply cmdb_tables.json --integration-id INTEG_ID --enable --dry-run --early-access
pd-cmdb-enrichment table-test --integration-id INTEG_ID --table-id TABLE_ID --early-access
pd-cmdb-enrichment schemas-apply schemas.json --dry-run --early-access
pd-cmdb-enrichment records-upload hosts.csv --schema-id SCHEMA_ID -y --early-access
pd-cmdb-enrichment associations assoc.csv --enrichment-id ENRICH_ID --dry-run --early-access
```
- `cmdb_tables.json`: `{"cmdb_tables": [{"display_name", "ci_table_name", "query_filter", "field_mappings": [{"servicenow_field", "event_field"}]}]}` (2–20 mappings). Tables are matched by `display_name`.
- `schemas.json`: `{"schemas": [{"name", "fields": [{"name", "type": "query"|"enriched"}]}]}` (2–25 fields, at least one `query`). Schemas are matched by `name`, and only missing ones are created. Fields can't change after creation, so a field difference on an existing schema is reported and exits 1.
- `records-upload`: CSV of at most 10 MB. It must contain every `query` column, and rows with matching query values update existing records. Processing is asynchronous.
- `assoc.csv`: `action (add|remove), type (service|event_orchestration), id`.
- **ServiceNow credentials are not managed by this tool.** Set them up once in the PagerDuty UI.

### pd-service-field-values (idempotent)
```bash
pd-service-field-values export -o field_values.csv            # one row per service
pd-service-field-values export --fields tier,owner_team -f json
pd-service-field-values apply field_values.csv --dry-run
pd-service-field-values apply field_values.csv -y
pd-service-field-values apply field_values.csv --clear-empty -y   # empty cells reset to null
```
The CSV is wide, with `service_id, service_name` followed by one column per field `name`. Export and apply use the same layout, so you can export, edit and apply.
- **Validation:** every cell is checked before any write. It must be a known field and service, match the field's data type (`true/false`, integer, a finite float, ISO 8601 with a timezone, `http(s)` URL, at most 200 characters), and, for fixed fields, be one of the allowed options.
- **Multi-value fields** hold up to 10 values separated by `|`. Inside a value, a literal `|` is written as `\|` and a literal `\` as `\\`, so an unedited export applies with no changes.
- **Only changes are written,** as one atomic `PUT` per service. A failed `PUT` applies none of that service's changes.
- **Empty cells** leave the value unchanged unless you pass `--clear-empty`.
- **Field definitions** are managed separately with `pd-custom-fields`.

### pd-event-rules-migrate (Event Rules to Event Orchestration)
```bash
pd-event-rules-migrate inventory -f csv -o migration.csv          # read-only
pd-event-rules-migrate inventory --fail-on-unmigrated -q           # cron: exit 1 while any remain
pd-event-rules-migrate convert --service-ids PSVC1,PSVC2 --dry-run
pd-event-rules-migrate convert --service-ids PSVC1,PSVC2 -y        # IRREVERSIBLE
pd-event-rules-migrate activate --service-ids PSVC1,PSVC2 -y
pd-event-rules-migrate deactivate --service-ids PSVC1 -y           # roll back evaluation
```
The migration has two steps, because PagerDuty separates them:
1. **`convert`** creates equivalent Event Orchestration rules and makes the service's Event Rules **read-only**. It **can't be undone**, and it doesn't change how events are processed.
2. **`activate`** switches new events to the Service Orchestration. `deactivate` reverses it at any time.

`inventory` reports each service's state: `needs_convert`, `needs_activate`, `migrated` or `no_event_rules`.

**Safety guards:**
- Every change needs `--service-ids` or an explicit `--all`, and is capped by `--max-services` (default 10).
- `activate` refuses services that still have Event Rules that haven't been converted, because those rules would silently stop applying.
- Nothing is changed if any service's state can't be read.

Suggested rollout:
1. Convert and activate one low-risk service.
2. Send a test event and check the result.
3. Repeat in small batches.

PagerDuty has announced Event Rules end-of-life.

### pd-incident-workflows
```bash
pd-incident-workflows export -o incident_workflows/            # commit the directory to git
pd-incident-workflows diff -i incident_workflows/ --fail-on-drift   # read-only; exit 1 on drift
pd-incident-workflows triggers-export -o trigger_links.csv
pd-incident-workflows triggers-apply trigger_links.csv --dry-run
pd-incident-workflows triggers-apply trigger_links.csv -y
```
- **`export` and `diff`:** each workflow is written with its steps and triggers. Fields the server changes on its own (`updated_at`, `self` and similar) are left out, so `diff` only reports real changes. Re-exporting removes this tool's own earlier files for workflows that were renamed or deleted, so the directory always matches PagerDuty. Other files are left alone. This tool doesn't write workflow definitions back; edit them in the PagerDuty UI.
- **Trigger CSV:** `action (attach|detach), trigger_id, service_id`. The extra `workflow_name` and `trigger_type` columns from `triggers-export` are informational.
- **Validation:** every row is checked before any write, and a row missing a `trigger_id` or `service_id` is an error rather than being skipped. Triggers subscribed to all services can't have per-service links and are rejected.
- **Re-runs are safe:** existing links are skipped, and a detach that returns 404 counts as done.

### pd-scim-user-sync (SCIM writes; **deletions can't be undone**)
```bash
pd-scim-user-sync plan expected_users.csv --plan-out scim_plan.json        # read-only
pd-scim-user-sync plan expected_users.csv --plan-out scim_plan.json --delete-orphans
pd-scim-user-sync apply scim_plan.json --dry-run
pd-scim-user-sync apply scim_plan.json -y [--max-deletes 10]
```
This is the write side of `pd-scim-user-audit`, and it uses the same IdP CSV. **In PagerDuty's SCIM API, setting `active` to false deletes the user.** The command guards that:
- **Display names:** drift is corrected with an update.
- **Who gets deleted:** only users the IdP marks inactive. Users missing from the CSV are reported only, unless you pass `--delete-orphans`. Use that flag only with a *complete* IdP export.
- **Blocked deletions:** a deletion is **blocked**, and never applied, for the account owner, for anyone still on a schedule (v2 layers or shift-based v3 schedules) or escalation policy, when v3 schedule membership can't be checked (fail closed), or when a SCIM user doesn't match exactly one PagerDuty user by email. SCIM IDs aren't PagerDuty IDs, so users are matched by email. Clear rotations first, for example with `pd-remove-team-members`, then re-plan.
- **Live re-check:** `apply` re-checks each item before writing and skips items that changed or became blocked, exiting 1.
- **Plan age and cap:** `apply` refuses plans older than 24 hours (`--max-plan-age-hours`), and caps deletions at `--max-deletes` (default 10).
- **Missing users:** users missing from PagerDuty are reported, not created; provisioning belongs to the IdP.
- **Token:** needs SCIM enabled with `users.write`.

### pd-business-services (export → diff-first apply)
```bash
pd-business-services export -o business_services.json        # commit to git
pd-business-services apply -i business_services.json         # diff only (no writes)
pd-business-services apply -i business_services.json --apply -y
pd-business-services apply -i business_services.json --apply -y --prune [--max-deletes 5]
```
- **The file** is keyed by business-service `name` and contains `description`, `point_of_contact` and `team_id`. `team_name` is informational and ignored on apply.
- **Validation:** before any write, it rejects duplicate names, an unknown `team_id`, unknown keys, and live names that more than one business service shares.
- **Deleting:** removing business services that aren't in the file needs `--prune` and is capped by `--max-deletes` (default 5), because deleting a business service stops new incidents for it.
- **Renaming:** a rename in the file means delete and create. Rename in the UI instead to keep history and dependencies.
- **Related commands:** dependencies are handled by `pd-service-deps` and subscribers by `pd-subscriptions`.

### pd-security-config (IP allow lists and session configurations)
```bash
pd-security-config export -o security_config.json             # commit to git for review
pd-security-config diff -i security_config.json --fail-on-drift   # cron: exit 1 on drift
pd-security-config apply-sessions -i security_config.json --dry-run
pd-security-config apply-sessions -i security_config.json -y
```
- **IP allow lists** are exported and diffed, but this tool **never writes them**. An enabled list restricts the whole account to its CIDRs, so a wrong entry would lock everyone out. Change them in the PagerDuty UI and let `diff` catch drift.
- **Session configurations** (`web` and `mobile`): `apply-sessions` writes only the types that changed. Writing one **immediately revokes all existing sessions of that type**, and the command warns before confirming.
- **Validation:** `absolute_session_ttl` must be 600–18,144,000 seconds and `idle_session_ttl` 60–15,552,000 seconds, and idle can't exceed absolute.
- **Token:** needs an Account Owner or Global Admin user token, or an account API key.

### pd-sn-sync (ServiceNow ID reconciliation — PagerDuty wins)
```bash
pd-sn-sync report --instance dallastx -f csv -o sn_report.csv      # read-only
pd-sn-sync report --instance dallastx --fail-on-drift -q            # cron: exit 1 on drift
pd-sn-sync plan --instance dallastx --plan-out sn_plan.json [--prune] [--team-ids PT1,PT2]
pd-sn-sync apply sn_plan.json --instance dallastx-test -y           # rehearsal → receipt
pd-sn-sync apply sn_plan.json --instance dallastx --dry-run         # production preview
pd-sn-sync apply sn_plan.json --instance dallastx -y                # needs receipt < 7 days
```
Writes only the `x_pd_integration_*` ID and error-message columns. Production writes require a rehearsal receipt for the identical plan and are capped by `--max-writes` (default 25). Full guide: [servicenow.md](servicenow.md); Okta setup: [servicenow-okta-setup.md](servicenow-okta-setup.md).
