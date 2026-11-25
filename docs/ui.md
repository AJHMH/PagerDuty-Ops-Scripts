# Team web UI (`pagerduty_ops_ui`)

A thin web layer over the CLI engine for the PagerDuty/ServiceNow admin team. The design is
recorded in [CONTEXT.md](../CONTEXT.md) and ADRs 0001–0015; the spec is issue #62.

**Status: Phase 1 complete (#66, #67, #72).**
- An admin signs in with Okta.
- They pick a command and fill in its generated form, uploading any input files.
- They see the stored Run, with its output files ready to download.
- Viewers run read-only commands.
- Operators and Approvers can also **dry-run** commands that write, and review what would change.

**Phase 2 has started (#73).** An Operator can **apply** a dry run they reviewed, if it isn't an Irreversible operation.

Commands that write are refused until Apply arrives in Phase 2.

## How it fits together

| Part | Where | Notes |
|---|---|---|
| HTTP API | `pagerduty_ops_ui/app.py` | FastAPI. `GET /api/me`, `GET /api/commands`, `GET /api/commands/{name}/form`, `POST /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/files/{file_id}` |
| Generated forms | `forms.py` | One field per argument, built from the command's own parser |
| Run files | `files.py` | Uploads and outputs stored in the Run's directory, each with a SHA-256 hash |
| Dry-run review | `review.py` | Classifies a dry run's preview into additions, changes and deletions, and computes the Run's input and output hashes |
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

Runs execute in the background (#74). `POST /api/runs` returns the Run at once (201), with `status: "running"`. The UI re-reads it with `GET /api/runs/{id}` until `status` is `"finished"`; only then are `outcome`, the output and the hashes set. Reports and dry runs never wait: each starts on its own thread.

**The live log (#68).** While a Run executes, its log (the command's stderr) is stored as it arrives: complete lines only, each normalised to LF and redacted on its own, written at most twice a second, and trimmed to its last 256 KB (with a note saying so) while the Run executes. So `GET /api/runs/{id}` shows the log so far, and the Run page, which re-reads the Run every second, shows it growing and follows the newest line. The output (stdout, which is data) and the hashes are stored when the Run finishes. A finished Run's page shows its whole stored log, and a Run that was cancelled, timed out or cut off by a restart keeps the log it had written. Storing the live log is best-effort: if a write fails, it is logged and retried, and the Run carries on. If a process the command started escapes the Run's process tree and holds its output open, the Run is recorded as failed, with a note, rather than as a success without all of its output.

`POST /api/runs` takes `{command, subcommand, values}`, either as JSON or as multipart with a `run` JSON part and `file.<field>` upload parts. Raw `{command, argv}` is still accepted.
- **The command line:** the service builds it from the form, in the parser's order, with long flag names. Values for fields the form doesn't offer are refused.
- **Uploads:** each upload is reduced to a plain file name and saved as `inputs/<field>/<name>` in the Run's directory.
  - Its hash is taken from the bytes as received, before the command runs.
  - Each upload is capped at 25 MB, and a whole request at 100 MB. The request cap is checked before the body is parsed; configure IIS's request limit to match.
  - An output can't be named into `inputs/`.
- **Outputs:** anything else the command writes to its directory is an output.
- **Hashes and downloads:** every file is recorded with its size and SHA-256. A download is refused (409) if the file has changed since the Run.
- **Messages:** a Run that didn't succeed carries the command's last log line as its `message`, for example a usage error.

## Dry runs (#72)

A command, or subcommand, whose risk declaration says it writes always runs as a dry run:
- **`--dry-run` commands:** the gate adds `--dry-run`.
- **Diff-first commands:** these preview unless given `--apply`. An explicit `--apply` is refused (403).
- **Final check:** the engine's `assess()` re-checks the final argv, and the Run only starts if the engine agrees it is a dry run.

Only Operators and Approvers can dry-run; Viewers get 403. The Run is stored with `kind: "dry-run"`.

**The review.** The engine previews in two styles:
- `+ `, `~ ` and `- ` lines from diff-first commands;
- `[dry-run] <verb> …` lines, classified by verb, for example "would DELETE" counts as a deletion.

Only lines that *start* with these markers count, in either stream. Log lines stay as context, which the UI hides until asked. The review gives counts and each classified line.

A `[dry-run] line N:` prefix is skipped, and the line is read by the verb that follows. Indented or bracketed lines under a preview header ending in `:` (for example the JSON payload after `[dry-run] would POST … with:`) are shown and hashed as part of that preview. If the dry run didn't succeed, the review says its changes couldn't be determined instead of "No changes".

A `[dry-run]` line whose verb isn't recognised counts as an "other change", unless it is a known narration line such as "previewing …". So the review never claims "No changes" for a preview style it doesn't recognise, for example `pd-sn-sync`'s `[dry-run] <table> <key>: <values>`.

**Hashes.** Every Run stores two, ready for #73 to match an apply to the reviewed dry run:
- `input_hash`: the command, the argv without its mode flags (`--dry-run`, `--apply`, `-y`, `--yes`), and each uploaded file's hash. A dry run and the apply of the same inputs therefore share it.
- `output_hash`: the classified diff lines and each output file's hash. Log timestamps don't change it.

## Cap overrides (#75, ADR 0003)

A command's write caps (`--max-deletes`, `--max-writes`, `--max-services`) are named in its risk declaration (`cap_flags`), and the engine's `assess()` reports which ones an argv raises above the parser's default (`raised_caps`). The UI keeps no list of its own.

- **A reason is required.** A Run that raises a cap needs `cap_override_reason` in its request (JSON, or the multipart `run` part): non-blank, at most 500 characters, and, like an argument, never containing a secret value. Without one it is refused (422) before anything starts. Lowering a cap, or leaving it at its default, needs no reason, and a reason sent with no raised cap is not recorded.
- **What is recorded.** The Run stores the reason, who gave it, and each raised cap as `{flag, default, value}`. `cap_override` is shown on the Run, in `GET /api/runs/{id}` and in the history, where each row spells it out.
- **Applies.** An apply reuses the reviewed dry run's arguments (the identical-inputs rule), so its caps are the reviewed ones, and it carries the dry run's override, as do its fresh dry run and any fresh dry run after a Stopped apply. Each of these also checks for itself that every cap the engine says it raises has a matching override on record; otherwise it is refused (409) and a new dry run with a reason is needed. That covers a dry run recorded before overrides existed, and a cap default changed in the engine since the review.
- **No silent caps.** A declared cap the parser doesn't have makes `assess()` fail closed, and a unit test checks that every declared cap is a numeric option of its command.
- **The form.** Each field says whether it is a cap (`cap: true`). The run form asks for the reason as soon as a cap is set above its default.

## Apply (#73, ADRs 0003 and 0011)

1. **Run and review.** The admin who ran a dry run that succeeded and completed records that they reviewed it: `POST /api/runs/{id}/review`. Only that admin can review it, and only Operators and Approvers can review at all.
2. **Apply.** That admin applies it with `POST /api/runs/{id}/apply`. The apply takes no new inputs: it reuses the dry run's command, arguments and uploaded files.
   - **Inputs:** each uploaded file is re-verified against its recorded hash. A changed file refuses the apply (409).
   - **Irreversible operations:** judged by the engine's `assess()`; applying one needs a valid Approval (see Approvals below), or it is refused (403).
   - **The queue:** the apply is stored with `kind: "apply"`, `applied_from` and `status: "queued"`, and returned at once (201). It waits for the one writing slot (below).
   - **Fresh dry run:** in its turn, inside the slot, a fresh dry run of the same inputs runs and is stored; the apply records it as `fresh_dry_run_id`. If it doesn't succeed, nothing is applied (`failed`). If its `output_hash` differs from the reviewed one, nothing is applied either: the outcome is `changed`, and that new dry run must be reviewed instead. Either way the reviewed dry run can be applied again.
   - **The command line:** only then is the apply run, with the mode-neutral argv plus `-y`, or `--apply -y` for diff-first commands. `-y` is added only after every gate has passed.
   - **Once only:** a dry run is applied at most once. An atomic claim on `applied_run_id` covers concurrent requests too.

## Run history (#69)

`GET /api/runs` is the searchable history, for every role. It returns `{runs, total, page, per_page}`: Runs newest first (by when they were requested), as summaries without their output, log or files; each links to its Run page in the UI. Filters combine:

- `command`: the exact command name;
- `requester`: part of the requester's email or name, ignoring case (`%` and `_` are literal);
- `since` and `before`: ISO 8601 dates and times with a timezone, on when the Run was requested; `since` is included, `before` is not;
- `outcome`: `succeeded`, `failed`, `usage-error`, `auth-error`, `stopped` or `changed`;
- `kind`: `report`, `dry-run` or `apply`;
- `page` (from 1) and `per_page` (1–200, default 50).

`outcome=unfinished` finds Runs still queued or running. An unknown outcome or kind, a date without a timezone, `page` below 1 or `per_page` outside 1–200 is refused (422); a page past the last one returns no rows, and the History page then offers a link back to the newest Runs. The History page keeps its filters in the URL (`#/history?…`), so a search can be shared; its From and To dates are whole days in the browser's time zone, which the page names, and To includes its day. A malformed date in a shared link is ignored, with a note. Migration `0002` indexes Runs by when they were requested.

## Approvals (#77, ADRs 0003, 0010 and 0011)

Applying an Irreversible operation needs a second admin's Approval. Whether an apply is irreversible comes only from the engine's `assess()`, judged on the apply's command line in the dry run's own directory, so a relative Plan path resolves; when it can't tell (an unreadable Plan, an unknown ServiceNow instance), it fails closed. A team reorg, even a pruning one, and a single-object delete are not irreversible.

1. **Ask.** When a dry run is reviewed, the engine's reasons are recorded, so its `approval` reads `{status: null, reasons}` and the Run page says up front that applying it needs an Approval. Its requester asks: `POST /api/runs/{id}/approval`. The dry run's `approval` becomes `awaiting-approval`, with the engine's `reasons`. A dry run that doesn't need one gets 409. The request expires if nobody answers within 24 hours; the requester can then ask again.
2. **Decide.** An Approver other than the requester approves or rejects, with a comment: `POST /api/runs/{id}/approval/approve` or `.../reject` with `{comment}` (non-blank, at most 500 characters, no secret values). Viewers, Operators and the requester get 403. A decision counts only while the request is open, checked and recorded in one step, so of two Approvers deciding at once, or a decision racing the expiry, only one counts (the other gets 409). The Run page shows the Approver the exact diff, the inputs, the requester, the reasons and any cap override.
3. **Apply.** The apply is refused (403) unless the Approval is `approved` and less than 24 hours old, and it is checked again when the apply reaches the writing slot, so an Approval that expires while queued isn't used. The apply records the approver (`approval.decided_by`) and comment.

A rejection is final for that dry run: the requester runs a new dry run and asks again. If the fresh dry run at apply time differs from the approved one, nothing is applied (`changed`), and the dry run's Approval is `withdrawn`, so it can't be applied again on that Approval; the fresh dry run is a new dry run, so it needs its own review and Approval. An apply shows the Approval it was made with as `used`. An Approval that expires while its apply waits in the queue isn't used: the apply is recorded as failed, saying so.

`GET /api/approvals` lists requests waiting for an Approver, oldest first; the home page shows them as "Waiting for approval". The Ledger (#76) isn't built yet; when it is, it takes the approver from the Run. Teams notifications for requests are #78.

## One writing Run at a time (#74, ADR 0012)

- **The queue.** Only one apply executes at a time across the account; the others wait, oldest first. Reports and dry runs never wait. The queue is the `queued` rows of the Run store, so it survives a restart. `GET /api/queue` (any role) returns `{executing, pending}`, and the home page shows it.
- **One process.** The single writer thread is what keeps applies apart, so the service must run as one process: `pd-ui` starts one uvicorn worker. Don't add workers (see ADR 0012's consequences).
- **Timeouts.** Every Run is killed at its timeout: 2 hours, or the command's declared `timeout_minutes`. The whole process tree is killed: the Run leads its own process group on Linux, and runs in a Job Object on Windows, which also holds processes whose parent has exited. The same kill runs when a command finishes, so a Run never leaves processes behind.
- **Cancel.** `POST /api/runs/{id}/cancel`: the requester can cancel their own Run, and an Approver can cancel anyone's (others get 403; a finished Run, 409). A queued apply is taken off the queue without running. An executing Run is killed; cancelling an apply's fresh dry run cancels the apply, before anything is written. A cancel that arrives as the Run finishes is refused (409) rather than recorded on a finished Run. `cancelled_by` records who.
- **Stopped.** A cancelled or timed-out Run is recorded as `stopped`, with the reason in its `message`. A Stopped apply may have been partly applied, so its page offers `POST /api/runs/{id}/fresh-dry-run`: a new dry run of the same command, arguments and inputs, by the admin who requested the apply. Applies are idempotent, so applying that dry run is safe.
- **Restarts.** At startup, whatever was executing is recorded as Stopped, because a restart cut it off. If the service died without shutting down, its Runs' processes must not keep writing: on Windows the OS kills each Run's Job Object with the service; on Linux the service records each Run's process group and kills any left over at startup, after checking from `/proc` that the process is still a Run. The writer retries with backoff after a database error, so an outage doesn't stop applies until a restart. Queued applies stay queued and resume in order; each still runs its fresh dry run first. A clean shutdown stops what is executing and leaves the queue as it is; an apply still in its fresh dry run goes back to the queue, since it hasn't written anything.

## Cross-site requests

Every state-changing `/api/` request (any method but GET, HEAD or OPTIONS) must carry `X-PD-Ops-Request: 1`, or it is refused (403). The front end always sends it. A browser can't add a custom header to a cross-origin request without a CORS preflight, which the service never grants. So a page on another site can't forge a Run, a review or an apply, even another `*.dallas.gov` site that the `SameSite=Lax` session cookie is sent to.

## What a Run is refused for

These are refused before any process starts, and no Run is recorded.

**422 (bad request):**
- Arguments the command's own parser rejects.
- `--help`.
- `-t`/`--token` or `--prompt`.
- `--log-file`: the service keeps every Run's log itself. A timestamped log file would also become an output that never matches a fresh dry run.
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
| `PD_UI_DATABASE_URL` | SQLAlchemy URL, e.g. `mssql+pyodbc://…?driver=ODBC+Driver+18+for+SQL+Server` (SQL Server, with Microsoft's ODBC Driver 18 installed) |
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
pd-ui-migrate
```

```bash
PD_UI_STATIC_DIR=ui/dist pd-ui
```

**The schema.** `pd-ui-migrate` applies the Alembic migrations in `pagerduty_ops_ui/migrations/` to `PD_UI_DATABASE_URL`. Run it on each deploy, before starting the service, with an account that may change the schema (for example a DBA's). It is safe to run again. The service never changes the schema: at startup it checks that the schema is the one it expects and refuses to start otherwise, naming `pd-ui-migrate`. So the service's own account needs only to read and write the tables.

A change to the models in `store.py` needs a new migration in `pagerduty_ops_ui/migrations/versions/`; `test_migrations.py` fails until the migrations build exactly the schema the models describe.

`pd-ui` listens on 127.0.0.1 only. IIS terminates TLS in front of it, and the service runs as a least-privilege Windows Service. To install it on a City Windows Server, follow [install-windows.md](install-windows.md) (#71).

For front-end work, `npm --prefix ui run dev` serves the UI with hot reload and proxies `/api` and `/auth` to a running `pd-ui`.

## Tests

`tests/ui/` drives the HTTP API in-process with FastAPI's `TestClient`:
- **Real Runs:** Runs are real subprocesses of test-only fixture commands (`tests/ui/fixture_*.py`). Runs finish in the background, so tests wait for them with `wait()`/`finished()` from `conftest.py`.
- **Test sign-in:** a header-based test identity provider stands in for Okta.
- **Database:** SQLite by default, migrated with the same migrations as production. CI also runs the whole suite on SQL Server (the `sqlserver` job), against a throwaway container whose password is generated in the job. To do the same locally, set `PD_UI_TEST_DATABASE_URL` to an empty database the tests may own; they migrate it once and clear its rows before each test.

The Okta adapter test stubs Okta's discovery document and checks the login redirect: PKCE, the `groups` scope and the client ID. The token exchange needs a live Okta.

## Not yet

These are later tickets:
- the Windows host packaging (#71);
- the rest of Phase 2: the Ledger and 90-day purge (#76);
- the rest of Phase 3: Teams notifications for Approval requests (#78), ServiceNow production applies with shared receipts (#79), and scheduled read-only Runs (#80).
