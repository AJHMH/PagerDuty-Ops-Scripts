# CLAUDE.md

Python CLI toolkit for bulk PagerDuty administration. All logic lives in the `pagerduty_ops`
package; root `pd_*.py` files are thin compatibility shims. A team web UI
(`pagerduty_ops_ui`, `ui/`) is being built on top of the CLI as a thin layer: it runs commands,
it never holds their logic (ADRs 0001–0015, `docs/ui.md`). Details live in `docs/` — this
file is the short list of rules.

## Commands

| Task | Command |
|---|---|
| Install (dev) | `pip install -e ".[dev]"` |
| Install (with the UI service) | `pip install -e ".[dev,ui]"` |
| Unit + UI service tests (mocked HTTP, no token) | `python -m pytest` |
| Front end typecheck + build | `npm --prefix ui ci && npm --prefix ui run build` |
| Lint | `ruff check .` |
| Integration tests (opt-in, read-only) | `PD_TEST_TOKEN=... python -m pytest tests/integration` |
| Demo output (no token) | `python demo_sample_output.py` |

CI (`.github/workflows/ci.yml`) runs ruff, pytest (unit + UI service) on 3.10/3.12 (Linux) and
3.12 (Windows), the front-end build, pip-audit, and a gitleaks history scan. Run the unit suite and ruff before every commit.

## Layout

- `pagerduty_ops/api.py` — the only PagerDuty HTTP client (`request`, `paginate`,
  `paginate_cursor`, `fetch_all`, `PDApiError`). Never call `requests` directly from a command.
- `pagerduty_ops/cli.py` — `standard_parser`, `init`, `confirm`, `finish_bulk`, `run`, exit codes.
- `pagerduty_ops/bulkops.py` — CSV loading/validation, ISO 8601 parsing, idempotent renames.
- `pagerduty_ops/commands/<name>.py` — one module per command, exposing `build_parser()` and
  `main(argv=None) -> int`.
- `pagerduty_ops/risk.py` — `Risk` declarations and `assess()`: whether a Run writes, is a dry
  run, or is an Irreversible operation (ADR 0010).
- `pagerduty_ops/console_scripts.py` — `SCRIPTS`: console script → (module, entry function).
- `pagerduty_ops_ui/` — the UI service (FastAPI); `ui/` — its React/TypeScript front end.
  The service never imports command logic: Runs are subprocesses, risk comes from `risk.py`.
- `pd_common.py` — deprecated re-export surface; do not add to it.

## Adding a command (checklist)

1. `pagerduty_ops/commands/<name>.py` with `build_parser()` + `main()`, and a `RISK`
   declaration (`READ_ONLY`, or `writes_with_dry_run`/`writes_with_apply` with any irreversible
   flags, plan options and caps; one per subcommand). The unit suite fails without it.
2. Root shim `pd_<name>.py` (copy an existing shim).
3. Entry in `SCRIPTS` and a function in `pagerduty_ops/console_scripts.py`, and in
   `[project.scripts]` in `pyproject.toml` (a test keeps them in sync).
4. Add the module to `COMMAND_MODULES` in `tests/unit/test_commands.py`, plus behavior tests
   using `responses` (never live HTTP in unit tests).
5. Row in the README command table and a section in `docs/usage.md`.

## Write-safety contract (every mutating command, no exceptions)

- `--dry-run` previews every change and performs zero writes; `-y/--yes` skips the prompt.
- Writes go through `cli.confirm()`, which refuses on a non-interactive stdin without `-y`.
- Validate all input (CSV columns, enums, ISO 8601 with timezone, https URLs) **before** the
  first write; bad input exits 2. Never silently drop a CSV row: use
  `load_csv_rows(require_values=...)`, which rejects incomplete rows (blank rows are ignored).
- Idempotent: read current state first and skip no-ops, so re-runs after partial failure are safe.
- Re-raise `PDApiError` when `is_auth_error` (abort the run); count other failures and finish
  with `finish_bulk()` so partial failure exits 1.
- Config-heavy resources use export → diff-first apply (see `event_orchestration.py`); deletes
  only behind an explicit `--prune`.
- Early Access endpoints only behind an explicit `--early-access` flag, labelled in help/README.

## ServiceNow writes (`pd-sn-sync`, `pagerduty_ops/servicenow.py`)

- PagerDuty is the source of truth; only the `x_pd_integration_*` ID columns and the error
  message column may be written. Group membership is never written.
- Ambiguous derivations are reported, never written; clearing stale IDs needs `--prune`.
- Writes go plan → rehearse on `SN_REHEARSAL_INSTANCES` → production. Production requires a
  matching receipt (`pagerduty_ops/receipts.py`); never add a bypass flag.
- No live ServiceNow or PagerDuty calls in unit tests. Mock both with `responses`.

## Exit codes (contract for cron/CI)

`0` ok (incl. dry-run, nothing to do) · `1` operation failures · `2` usage/config ·
`3` auth (401/403) · `130` interrupted.

## Conventions

- stdout is data only; all diagnostics go to stderr via `pagerduty_ops.log.get_logger`.
  `cli.run()` makes stdout UTF-8 with no newline translation, so output is byte-identical on
  Windows; entry points must go through it.
- Secrets come from the environment (`PD_API_TOKEN`, etc.) or a secret store — never CLI
  arguments, never logged, never committed. Log only the redacted error envelope.
- Ruff line length 100; match surrounding style.

## Branching and pull requests

- One branch per feature (`feature/<topic>`), one PR per branch into `main`.
- Branch from up-to-date `main`; if a feature depends on an unmerged one, stack it on that
  branch and say so in the PR description.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues on AJHMH/PagerDuty-Ops-Scripts. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
