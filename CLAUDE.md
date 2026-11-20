# CLAUDE.md

Python CLI toolkit for bulk PagerDuty administration (no web server, database, or Docker).
All logic lives in the `pagerduty_ops` package; root `pd_*.py` files are thin compatibility
shims. Details live in `docs/` — this file is the short list of rules.

## Commands

| Task | Command |
|---|---|
| Install (dev) | `pip install -e ".[dev]"` |
| Unit tests (mocked HTTP, no token) | `python -m pytest tests/unit` |
| Lint | `ruff check .` |
| Integration tests (opt-in, read-only) | `PD_TEST_TOKEN=... python -m pytest tests/integration` |
| Demo output (no token) | `python demo_sample_output.py` |

CI (`.github/workflows/ci.yml`) runs ruff, pytest on 3.10/3.12, pip-audit, and a gitleaks
history scan. Run the unit suite and ruff before every commit.

## Layout

- `pagerduty_ops/api.py` — the only PagerDuty HTTP client (`request`, `paginate`,
  `paginate_cursor`, `fetch_all`, `PDApiError`). Never call `requests` directly from a command.
- `pagerduty_ops/cli.py` — `standard_parser`, `init`, `confirm`, `finish_bulk`, `run`, exit codes.
- `pagerduty_ops/bulkops.py` — CSV loading/validation, ISO 8601 parsing, idempotent renames.
- `pagerduty_ops/commands/<name>.py` — one module per command, exposing `build_parser()` and
  `main(argv=None) -> int`.
- `pd_common.py` — deprecated re-export surface; do not add to it.

## Adding a command (checklist)

1. `pagerduty_ops/commands/<name>.py` with `build_parser()` + `main()`.
2. Root shim `pd_<name>.py` (copy an existing shim).
3. Entry in `pagerduty_ops/console_scripts.py` and `[project.scripts]` in `pyproject.toml`.
4. Add the module to `COMMAND_MODULES` in `tests/unit/test_commands.py`, plus behavior tests
   using `responses` (never live HTTP in unit tests).
5. Row in the README command table and a section in `docs/usage.md`.

## Write-safety contract (every mutating command, no exceptions)

- `--dry-run` previews every change and performs zero writes; `-y/--yes` skips the prompt.
- Writes go through `cli.confirm()`, which refuses on a non-interactive stdin without `-y`.
- Validate all input (CSV columns, enums, ISO 8601 with timezone, https URLs) **before** the
  first write; bad input exits 2.
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
- Secrets come from the environment (`PD_API_TOKEN`, etc.) or a secret store — never CLI
  arguments, never logged, never committed. Log only the redacted error envelope.
- Ruff line length 100; match surrounding style.

## Branching and pull requests

- One branch per feature (`feature/<topic>`), one PR per branch into `main`.
- Branch from up-to-date `main`; if a feature depends on an unmerged one, stack it on that
  branch and say so in the PR description.
