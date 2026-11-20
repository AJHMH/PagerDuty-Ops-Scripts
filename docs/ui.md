# Team web UI (`pagerduty_ops_ui`)

A thin web layer over the CLI engine for the PagerDuty/ServiceNow admin team. The design is
recorded in [CONTEXT.md](../CONTEXT.md) and ADRs 0001–0015; the spec is issue #62.

**Status: Phase 1 (#66, #67).**
- An admin signs in with Okta.
- They pick a command with a **read-only** mode and fill in its generated form, uploading any input files.
- They run it and see the stored Run, with its output files ready to download.

Commands that write are refused until Apply arrives in Phase 2.

## How it fits together

| Part | Where | Notes |
|---|---|---|
| HTTP API | `pagerduty_ops_ui/app.py` | FastAPI. `GET /api/me`, `GET /api/commands`, `GET /api/commands/{name}/form`, `POST /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/files/{file_id}` |
| Generated forms | `forms.py` | One field per argument, built from the command's own parser |
| Run files | `files.py` | Uploads and outputs stored in the Run's directory, each with a SHA-256 hash |
| Sign-in and roles | `identity.py`, `roles.py` | Okta OIDC with PKCE. Roles come from the `PD-Ops-Viewer`, `PD-Ops-Operator` and `PD-Ops-Approver` groups (ADR 0008) |
| What may run | `gate.py` | Uses only the engine's risk declarations and `assess()` (ADR 0010) |
| Running a command | `runner.py` | Each Run is the command's own entry point in its own process, through `cli.run()` (ADR 0004) |
| Command list | `catalog.py` | Built from `pagerduty_ops.console_scripts.SCRIPTS` |
| Run store | `store.py` | SQLAlchemy: SQL Server in production, SQLite in tests (ADR 0007) |
| Front end | `ui/` | React and TypeScript (Vite), built to `ui/dist` and served by the service |

The service never imports or calls command logic. It holds no risk table of its own.

## Generated forms

`GET /api/commands/{name}/form` describes every argument the UI offers:
- its kind: flag, count, choice, number, text, input file, input folder, output file or output folder;
- its flags, and whether it's required;
- its choices, default and help;
- whether it repeats;
- its mutually exclusive group.

A command with subcommands gets one section per subcommand, marked with whether it writes.

Some arguments are never offered:
- credentials and prompts: `-t`/`--token`, `--prompt`;
- `-y`, `--dry-run` and `--apply`, which the UI's own gates will set (ADRs 0003, 0011);
- `--log-file` and `--receipts-dir`, which the service manages itself.

argparse can't say which arguments are files, so `FILE_ROLES` in `forms.py` names them. Tests fail in two cases:
- a command has an argument shape the generator can't render;
- an argument whose name looks like a path isn't classified. A reviewed `NOT_FILES` list covers names that only look like paths.

## Running a command

`POST /api/runs` takes `{command, subcommand, values}`, either as JSON or as multipart with a `run` JSON part and `file.<field>` upload parts. Raw `{command, argv}` is still accepted.
- **The command line:** the service builds it from the form, in the parser's order, with long flag names. Values for fields the form doesn't offer are refused.
- **Uploads:** each upload is reduced to a plain file name and saved as `inputs/<field>/<name>` in the Run's directory.
  - Its hash is taken from the bytes as received, before the command runs.
  - Each upload is capped at 25 MB, and a whole request at 100 MB. The request cap is checked before the body is parsed; configure IIS's request limit to match.
  - An output can't be named into `inputs/`.
- **Outputs:** anything else the command writes to its directory is an output.
- **Hashes and downloads:** every file is recorded with its size and SHA-256. A download is refused (409) if the file has changed since the Run.
- **Messages:** a Run that didn't succeed carries the command's last log line as its `message`, for example a usage error.

## What a Run is refused for

These are refused before any process starts, and no Run is recorded.

**422 (bad request):**
- Arguments the command's own parser rejects.
- `--help`.
- `-t`/`--token` or `--prompt`.
- Any argument containing a configured secret value.
- Any path outside the Run's own directory: absolute, drive-qualified, UNC, or containing `..`.

These are judged on the arguments as the command's own parser reads them, not on the raw text. That way argparse's prefixes (`--tok`), attached values (`-oPATH`) and short-option clusters (`-qoPATH`) can't slip past.

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
- the live log (#68);
- Run history search (#69);
- the SQL Server CI job and migrations (#70);
- the Windows host packaging (#71);
- dry runs (#72);
- Apply and everything after it.
