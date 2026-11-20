# ServiceNow ↔ PagerDuty ID sync (`pd-sn-sync`)

The PagerDuty ServiceNow app (scope `x_pd_integration`) stores PagerDuty IDs on ServiceNow
records. `pd-sn-sync` checks those IDs against PagerDuty and corrects ServiceNow.
**PagerDuty is the source of truth.**

## What it reads and writes

| Table | Column (default) | Holds | Written? |
|---|---|---|---|
| `sys_user` | `x_pd_integration_pagerduty_id` | PD user ID | only cleared, with `--prune` |
| `sys_user_group` | `x_pd_integration_pagerduty_service` | PD service ID (the **anchor**) | yes |
| `sys_user_group` | `x_pd_integration_pagerduty_escalation` | PD escalation policy ID | yes |
| `sys_user_group` | `x_pd_integration_pagerduty_team` | PD team ID | yes |
| `sys_user_group` | `x_pd_integration_pagerduty_schedule` | PD schedule ID | yes |
| `sys_user_group` | `x_pd_integration_pagerduty_webhook` | PD v3 webhook subscription ID | yes |
| `sys_user_group` | `x_pd_integration_pd_error_message` | reconciliation message | yes |
| `sys_user_grmember` | — | group membership | **never** (report only) |

Column names can be overridden with a JSON file (`--field-map` or `SN_FIELD_MAP`) containing
any subset of: `user_pd_id`, `group_service`, `group_escalation`, `group_team`,
`group_schedule`, `group_webhook`, `group_error`.

## How the correct IDs are worked out

Each group is anchored on its **service** ID:

| Field | Expected value |
|---|---|
| escalation policy | `service.escalation_policy` (always exact) |
| team | the service's team; if it has none, the EP's team. Must be exactly one. |
| schedule | the schedule target of the EP's **first** rule. Must be exactly one. |
| webhook | the v3 webhook subscription filtered to that service. Must be exactly one. |

Statuses in the report:

| Status | Meaning | Written? |
|---|---|---|
| `ok` | matches PagerDuty (or nothing to expect) | — |
| `update` | ServiceNow differs from (or is missing) the derived value | yes |
| `ambiguous` | zero or several candidates; current value is not one of them | no — message only |
| `stale` | ID points at a deleted object or the wrong type | cleared only with `--prune` |
| `unanchored` | group has PD IDs but no service ID | no — message only |
| `duplicate_service` | two or more groups share one service ID | no — message only |
| `unmapped_user` | member of a mapped group with no PD user ID | no (no email matching) |
| `membership_only_in_pd` / `membership_only_in_sn` | team vs group membership differs | no |

Unresolved issues are summarised in the group's error-message column with a
`[pd-sn-sync YYYY-MM-DD]` prefix. The tool clears only messages carrying that prefix, so
messages written by the PagerDuty app itself are never removed.

## Workflow: plan → rehearse → production

```bash
# 1. Read-only report (any instance)
pd-sn-sync report --instance dallastx -f csv -o sn_report.csv

# 2. Build a plan from production data
pd-sn-sync plan --instance dallastx --plan-out sn_plan.json          # add --prune to clear stale IDs

# 3. Rehearse the *same file* on the rehearsal instance (writes a receipt on success)
pd-sn-sync apply sn_plan.json --instance dallastx-test --dry-run
pd-sn-sync apply sn_plan.json --instance dallastx-test -y

# 4. Apply to production (refused without a matching receipt < 7 days old)
pd-sn-sync apply sn_plan.json --instance dallastx --dry-run
pd-sn-sync apply sn_plan.json --instance dallastx -y
```

- Records are matched by **natural key** on the target instance (group `name`, user
  `user_name`), so a plan built from production can be rehearsed on a clone.
- The plan hash ignores `sys_id` and the observed `expected` value, and covers everything the
  plan *does*. Editing the plan after rehearsal invalidates the receipt.
- On production, an item whose live value no longer equals the value it was planned from is a
  conflict: it is refused, the run exits 1, and you should re-plan. On other instances the
  drift is logged and the write proceeds.
- `--max-writes` (default 25) refuses plans needing more record writes than that. Narrow the
  plan with `--team-ids` / `--service-ids`, or raise the cap once you understand the
  business-rule impact. `--pause-seconds` spaces out writes.
- Dev instances (`dallastx-dev`) accept ad-hoc runs. Their receipts never unlock production.

### Business rules

Updating `x_pd_integration` fields on `sys_user_group` may fire the PagerDuty app's business
rules, which can call PagerDuty back. The Table API cannot suppress them. That is why every
production write requires a rehearsal and is capped by `--max-writes`. Review rehearsal side
effects in `dallastx-test` (PagerDuty audit log, `pd-audit-export`) before applying to
production.

## Configuration

| Variable | Purpose |
|---|---|
| `SN_INSTANCE` | default instance (`--instance` overrides), e.g. `dallastx-dev` |
| `SN_PROD_INSTANCES` | comma list of production instances, e.g. `dallastx`. **Required for any write** (fails closed). |
| `SN_REHEARSAL_INSTANCES` | comma list whose successful runs produce receipts, e.g. `dallastx-test` |
| `SN_RECEIPT_MAX_AGE_DAYS` | receipt lifetime (default 7) |
| `PD_OPS_RECEIPTS_DIR` | receipts directory (default `.pd_ops_receipts`, git-ignored) |
| `SN_AUTH_METHOD` | `okta` (default), `sn-oauth`, or `basic` |
| `OKTA_DOMAIN`, `OKTA_AUTH_SERVER_ID`, `OKTA_CLIENT_ID`, `OKTA_CLIENT_SECRET`, `OKTA_SCOPE` | Okta client-credentials (see [servicenow-okta-setup.md](servicenow-okta-setup.md)) |
| `SN_CLIENT_ID`, `SN_CLIENT_SECRET` | ServiceNow OAuth client credentials (`sn-oauth`) |
| `SN_USER`, `SN_PASSWORD` | integration user (`basic`) |
| `SN_FIELD_MAP` | path of a JSON column-name override file |

Secrets are read only from the environment or `.env` (git-ignored), never from CLI
arguments, and never logged. Token and error bodies are not logged.

### Integration user permissions

The ServiceNow account that the token maps to needs:

- read on `sys_user`, `sys_user_group`, and `sys_user_grmember`
- field-level write on the `x_pd_integration_*` columns listed above, on `sys_user_group`
  and on `sys_user`. Cross-scope write access to the `x_pd_integration` scope may require a
  cross-scope privilege record.

It does **not** need `admin`. A 401/403 response exits with code 3.
