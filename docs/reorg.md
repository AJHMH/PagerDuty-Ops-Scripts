# Team reorganizations (`pd-reorg`)

Reorgs are planned in PagerDuty terms, reviewed as a plan file, then applied.

```bash
pd-reorg export -o teams.yaml                          # 1. start from reality
# edit teams.yaml and/or write moves.csv
pd-reorg plan --teams-yaml teams.yaml --moves moves.csv --plan-out reorg_plan.json
pd-reorg apply reorg_plan.json --dry-run               # 2. review
pd-reorg apply reorg_plan.json -y                      # 3. apply
pd-reorg apply reorg_plan.json -y --sn-plan-out sn_plan.json --instance dallastx
                                                        # optional: ServiceNow follow-up plan
```

Both inputs compile into **one** plan. If they disagree (for example, the CSV moves a user off
team A while the YAML lists them on team A), the plan is refused with exit code 2 and every
conflict is listed. No input wins silently. Planning compares against live PagerDuty and drops
no-ops, so a re-run plan contains only what is still left to do.

## Moves CSV

Columns: `action, kind, id, from_team, to_team, role` (blank cells are fine where unused).

| action | kind | uses | effect |
|---|---|---|---|
| `add-member` | `user` | `to_team`, `role` (default responder) | add to team, or set role |
| `remove-member` | `user` | `from_team` | remove from team |
| `move-member` | `user` | `from_team`, `to_team`, `role` (default: current role) | remove + add |
| `set-team-role` | `user` | `to_team`, `role` | change team role |
| `set-account-role` | `user` | `role` (`admin`, `user`, `limited_user`, `observer`, `read_only_user`, `read_only_limited_user`, `restricted_access`) | change account role (never the owner) |
| `move-resource` | `escalation_policy` \| `schedule` | `to_team`, optional `from_team` | change team ownership |

```csv
action,kind,id,from_team,to_team,role
move-member,user,PUSER01,PTEAMA,PTEAMB,
set-team-role,user,PUSER02,,PTEAMB,manager
set-account-role,user,PUSER03,,,observer
move-resource,escalation_policy,PEPOL01,PTEAMA,PTEAMB,
```

## Per-team YAML

`pd-reorg export` writes this format. Listing a team makes it authoritative **for the keys
you include**:

```yaml
teams:
  PTEAMB:
    name: Beta                     # informational
    members:
      PUSER01: responder           # shorthand: team role
      PUSER02: {team_role: manager, account_role: user, name: "Pat Doe"}
    escalation_policies: [PEPOL01]
    schedules: [PSCHED1]
    services: [PSVC001]            # verified, not written (see below)
```

- Members, escalation policies and schedules you list are added or updated.
- With `--prune`, members, escalation policies and schedules **of a listed team** that are not
  listed are removed. Prune applies only to the keys present for that team. Teams not in the
  file are never touched.
- Listing a resource under a new team *adds* that team's ownership. To move it, list the old
  team too and use `--prune`, or use a `move-resource` CSV row.
- An account role that differs between two team entries for the same user is a conflict.

### Services

PagerDuty derives a service's team from its escalation policy, so a service cannot be moved
directly. Services listed under a team are **checked**: the plan is refused unless the
service's escalation policy will belong to that team once the plan is applied. A
`move-resource` CSV row with kind `service` is rejected and tells you which escalation policy
to move instead.

## Plan ops and apply order

`add_member` → `set_team_role` → `set_account_role` → `move_resource` →
`remove_resource_team` → `remove_member`. Additions run before removals, so a partial failure
never leaves a rotation without its team.

- `remove_member` warns when the user is still on that team's schedules. Use
  `pd-remove-team-members` to take them off rotations; editing schedule layers stays out of
  `pd-reorg`'s scope.
- Removals that hit an already-removed object (HTTP 404) count as done, so apply is safe to
  re-run.
- Schedule ownership changes use `GET`/`PUT /schedules/{id}`, which keeps the schedule's layers.

`pd-validate --plan-out` writes the same plan format, so its remediations are applied with
`pd-reorg apply`.
