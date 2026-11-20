# Contributing

Thank you for helping improve **PagerDuty-Ops-Scripts**. This document describes how we work together and what to expect when you contribute. The short list of repository rules lives in [CLAUDE.md](CLAUDE.md); this guide expands on it.

## Code of conduct

Everyone participating in issues, pull requests, and discussions is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Python 3.10+.** Install the package with its dev tools, then install the pre-commit hooks (ruff and gitleaks):

  ```bash
  pip install -e ".[dev]"
  pre-commit install
  ```

- Read the [README](README.md) for the command list and environment variables (`PD_API_TOKEN`, `PD_TEAM_ID`, `PD_FROM_EMAIL`, and the `SN_*` / `OKTA_*` settings for ServiceNow).
- Many commands perform **bulk writes** against PagerDuty or ServiceNow. Always run `--dry-run` first, against a sandbox account or a narrow scope.

## How to contribute

1. **Issues first (optional but helpful).** For larger changes or new commands, opening an issue lets maintainers agree on scope before you invest time.
2. **One branch per feature.** Branch from up-to-date `main` with a name such as `feature/<topic>` or `fix/<topic>`. If your change depends on another unmerged branch, stack it on that branch and say so in the PR description.
3. **One pull request per branch, into `main`.** Use the [pull request template](.github/pull_request_template.md) as a checklist.

## Code layout

- All logic lives in the `pagerduty_ops` package. The root `pd_*.py` files are thin compatibility shims.
- `pagerduty_ops/api.py` is the only PagerDuty HTTP client (`request`, `paginate`, `paginate_cursor`, `fetch_all`, `PDApiError`). Never call `requests` directly from a command.
- `pagerduty_ops/cli.py` provides the shared parser, confirmation guard, bulk summary, and exit codes. `pagerduty_ops/bulkops.py` handles CSV loading and ISO 8601 parsing.
- `pd_common.py` is a deprecated re-export layer. Do not add to it.

See [docs/architecture.md](docs/architecture.md) for details.

## Adding a command

1. Create `pagerduty_ops/commands/<name>.py` exposing `build_parser()` and `main(argv=None) -> int`, plus a `RISK` declaration next to the parser: `READ_ONLY` for commands that never write, otherwise `writes_with_dry_run(...)` or `writes_with_apply(...)` naming any flags or Plan options that make it irreversible and any write caps. Commands with subcommands declare one per subcommand. See `pagerduty_ops/risk.py` and ADR 0010; the unit suite fails without it.
2. Add a root shim `pd_<name>.py` (copy an existing one).
3. Register it in `pagerduty_ops/console_scripts.py` and under `[project.scripts]` in `pyproject.toml`.
4. Add the module to `COMMAND_MODULES` in `tests/unit/test_commands.py`, plus behavior tests.
5. Add a row to the README command table and a section in [docs/usage.md](docs/usage.md).

## Write-safety rules (every command that changes data)

- `--dry-run` previews every change and writes nothing. `-y/--yes` skips the prompt, and writes are refused on a non-interactive stdin without it.
- Validate all input (CSV columns, enums, ISO 8601 timestamps with a timezone, `https://` URLs) **before** the first write. Bad input exits 2. Never silently skip a CSV row: `load_csv_rows(require_values=...)` rejects incomplete rows and only ignores completely blank ones.
- Be idempotent: read the current state first and skip changes that are already in place, so a re-run after a partial failure is safe.
- Abort the run on authentication errors. Count other failures and finish with `finish_bulk()` so a partial failure exits 1.
- Configuration-heavy resources use export, then a diff-first apply. Deletions happen only behind an explicit `--prune`.
- Early Access APIs are only used behind an explicit `--early-access` flag, and are labelled as such in help text and the README.

### ServiceNow writes

- PagerDuty is the source of truth. Only the `x_pd_integration_*` ID columns and the error-message column may be written; group membership is never written.
- Ambiguous matches are reported, never written. Clearing stale IDs requires `--prune`.
- Writes go plan, then rehearsal on an instance in `SN_REHEARSAL_INSTANCES`, then production. Production requires a matching rehearsal receipt. Never add a way to bypass it.

See [docs/servicenow.md](docs/servicenow.md).

## Coding conventions

- stdout is for data only. Send all diagnostics to stderr through `pagerduty_ops.log.get_logger`.
- **Never commit secrets.** Tokens and passwords come from the environment or a secret store, never from command-line arguments, and are never logged. Don't add `.env` files with real credentials or customer-specific identifiers.
- Match the surrounding style. Ruff enforces a line length of 100.
- **Dependencies:** add new packages to `[project.dependencies]` in `pyproject.toml` (the source of truth) and to `requirements.txt`, with a version floor, and mention them in the PR.

## Exit codes

Scripts and CI rely on these: `0` success (including dry runs and nothing to do), `1` one or more operations failed, `2` usage or configuration error, `3` authentication or authorization error, `130` interrupted.

## Testing and verification

Run both before every commit. CI (`.github/workflows/ci.yml`) runs them too, on Python 3.10 and 3.12, along with `pip-audit` and a gitleaks history scan.

```bash
python -m pytest tests/unit
```

```bash
ruff check .
```

- Unit tests mock HTTP with `responses`. Never make live PagerDuty or ServiceNow calls in unit tests.
- Integration tests are opt-in and read-only:

  ```bash
  PD_TEST_TOKEN=your_sandbox_token python -m pytest tests/integration
  ```

- For write commands, also run a `--dry-run` against a sandbox account or a narrow scope.
- Say in the PR how you verified the change: tests added, commands run, or "documentation only".

## Documentation

- Update the README and `docs/usage.md` when you add a command, change flags, or change environment variables. Add a `CHANGELOG.md` entry under "Unreleased".
- Keep usage examples copy-pasteable and accurate.

## Licensing

By contributing, you agree that your contributions will be licensed under the same terms as the project ([MIT License](LICENSE)).
