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
CSV columns: `email, displayName, active`.

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
