# Team web UI (`pagerduty_ops_ui`)

A thin web layer over the CLI engine for the PagerDuty/ServiceNow admin team. The design is
recorded in [CONTEXT.md](../CONTEXT.md) and ADRs 0001–0015; the spec is issue #62.

**Status: Phase 1 tracer bullet (#66).**
- An admin signs in with Okta.
- They pick a **read-only** command, give its arguments, and run it.
- They see the stored Run.

Commands that write are refused until Apply arrives in Phase 2.

## How it fits together

| Part | Where | Notes |
|---|---|---|
| HTTP API | `pagerduty_ops_ui/app.py` | FastAPI. `GET /api/me`, `GET /api/commands`, `POST /api/runs`, `GET /api/runs/{id}` |
| Sign-in and roles | `identity.py`, `roles.py` | Okta OIDC with PKCE. Roles come from the `PD-Ops-Viewer`, `PD-Ops-Operator` and `PD-Ops-Approver` groups (ADR 0008) |
| What may run | `gate.py` | Uses only the engine's risk declarations and `assess()` (ADR 0010) |
| Running a command | `runner.py` | Each Run is the command's own entry point in its own process, through `cli.run()` (ADR 0004) |
| Command list | `catalog.py` | Built from `pagerduty_ops.console_scripts.SCRIPTS` |
| Run store | `store.py` | SQLAlchemy: SQL Server in production, SQLite in tests (ADR 0007) |
| Front end | `ui/` | React and TypeScript (Vite), built to `ui/dist` and served by the service |

The service never imports or calls command logic. It holds no risk table of its own.

## What a Run is refused for

These are refused before any process starts, and no Run is recorded.

**422 (bad request):**
- Arguments the command's own parser rejects.
- `--help`.
- `-t`/`--token` or `--prompt`.
- Any argument containing a configured secret value.
- Any path outside the Run's own directory: absolute, drive-qualified, UNC, or containing `..`.

**403 (not allowed):**
- Any command or subcommand whose risk declaration says it writes, for every role.

## Secrets

These come from the City's vault into the service's environment (ADR 0008):
- the PagerDuty account key;
- the ServiceNow credentials;
- the Okta client credentials.

A Run receives them only as environment variables. Apart from them, it gets a short allowlist of the service's environment (`PATH`, `TEMP`, and so on) and the non-secret engine settings. Secret values are redacted from the stdout and stderr that are stored.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `PD_UI_DATABASE_URL` | SQLAlchemy URL, e.g. `mssql+pyodbc://…` (SQL Server) |
| `PD_UI_RUN_ROOT` | Directory holding each Run's working directory |
| `PD_UI_SESSION_SECRET` | Key that signs the session cookie |
| `PD_UI_OKTA_ISSUER`, `PD_UI_OKTA_CLIENT_ID`, `PD_UI_OKTA_CLIENT_SECRET`, `PD_UI_OKTA_REDIRECT_URI` | Okta OIDC app. The ID token must include a `groups` claim |
| `PD_UI_STATIC_DIR` | The built front end (`ui/dist`) |
| `PD_UI_PORT` | Port on 127.0.0.1 (default 8080). IIS proxies to it |
| `PD_API_TOKEN`, `SN_*`, `OKTA_*` | Engine credentials and settings, passed through to Runs |

## Running it

```bash
pip install -e ".[ui]"
```

```bash
npm --prefix ui ci
```

```bash
npm --prefix ui run build
```

```bash
PD_UI_STATIC_DIR=ui/dist pd-ui
```

`pd-ui` listens on 127.0.0.1 only. IIS terminates TLS in front of it, and the service runs as a least-privilege Windows Service. The Windows host packaging is #71.

For front-end work, `npm --prefix ui run dev` serves the UI with hot reload and proxies `/api` and `/auth` to a running `pd-ui`.

## Tests

`tests/ui/` drives the HTTP API in-process with FastAPI's `TestClient`:
- **Real Runs:** Runs are real subprocesses of test-only fixture commands (`tests/ui/fixture_*.py`).
- **Test sign-in:** a header-based test identity provider stands in for Okta.
- **Database:** SQLite, through the production database layer.

The Okta adapter test stubs Okta's discovery document and checks the login redirect: PKCE, the `groups` scope and the client ID. The token exchange needs a live Okta.

## Not yet

These are later tickets:
- generated forms and file uploads (#67);
- the live log (#68);
- Run history search (#69);
- the SQL Server CI job and migrations (#70);
- the Windows host packaging (#71);
- dry runs (#72);
- Apply and everything after it.
