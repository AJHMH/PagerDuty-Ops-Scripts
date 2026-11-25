"""The FastAPI application: the HTTP API that the React front end (and Seam B tests) use."""

import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from . import files as run_files
from .approvals import (
    APPROVAL_LIFETIME,
    APPROVED,
    AWAITING,
    FINAL,
    REJECTED,
    Approval,
    require_approved,
)
from .catalog import describe, engine_catalog
from .executor import (
    FINAL_OUTCOMES,
    Executor,
    InputChanged,
    Requester,
    copy_inputs,
    new_run,
    verify_inputs,
)
from .forms import BadValues, build_argv, form_for
from .gate import gate, gate_apply
from .identity import Admin, IdentityProvider, OktaIdentity
from .notify import TeamsNotifier
from .overrides import (
    MAX_REASON_LENGTH,
    CapOverride,
    check_reviewed,
    record_override,
    recorded_text,
)
from .phase import require_apply, require_approvals
from .retention import Purger
from .review import MODE_FLAGS
from .roles import NO_ROLE_MESSAGE, Role, role_for
from .schedules import (
    Scheduler,
    check_schedulable,
    clean_name,
    new_schedule,
    parse_timing,
    schedule_dict,
)
from .settings import Settings
from .store import (
    FINISHED,
    QUEUED,
    RUN_KINDS,
    UNFINISHED,
    RunFile,
    RunRecord,
    RunStore,
    utc_now,
)

# Every state-changing API request must carry this header. A browser can't add a custom
# header to a cross-origin request without a CORS preflight, which this API never grants, so
# a page on another site (even another *.dallas.gov site the session cookie is sent to) can't
# forge a Run, a review or an apply.
UI_REQUEST_HEADER = "X-PD-Ops-Request"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Roles are read from the session at sign-in; a short session means removing someone from a
# PD-Ops group in AD/Okta takes effect within a working day.
SESSION_MAX_AGE_SECONDS = 8 * 3600

MAX_HISTORY_PAGE = 200
HISTORY_OUTCOMES = FINAL_OUTCOMES | {UNFINISHED}


def parse_instant(name: str, value: str | None) -> datetime | None:
    """An ISO 8601 date and time with a timezone, as UTC (like the engine's date inputs)."""
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{name} must be an ISO 8601 date and time with a "
                                 "timezone, e.g. 2026-11-02T00:00:00-06:00.") from None
    if instant.tzinfo is None:
        raise HTTPException(422, f"{name} needs a timezone, e.g. 2026-11-02T00:00:00-06:00.")
    return instant.astimezone(timezone.utc)


class ScheduleBody(BaseModel):
    """A schedule for a read-only command (#80): its name, the command and form values a Run
    would take (no files), and when it runs."""

    name: str
    command: str
    subcommand: str | None = None
    values: dict = Field(default_factory=dict)
    timing: dict


class Decision(BaseModel):
    """An Approver's approve or reject (#77); both need a comment."""

    comment: str = Field(default="", max_length=MAX_REASON_LENGTH)


class RunRequest(BaseModel):
    """Either raw `argv`, or a form's `values` (and `subcommand`) with any uploaded files."""

    command: str
    argv: list[str] | None = Field(default=None, max_length=200)
    subcommand: str | None = None
    values: dict | None = None
    # Why a write cap is raised above its default (#75); required exactly when one is.
    cap_override_reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


async def read_run_request(request: Request, max_upload_bytes: int, max_request_bytes: int):
    """JSON body, or multipart with a `run` JSON part and `file.<field>` upload parts.

    The whole request is capped before it is parsed, so an oversized upload is refused
    without first being spooled to disk (IIS should cap request size too).
    """
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > max_request_bytes:
        raise HTTPException(413 if length.isdigit() else 411,
                            f"Requests are limited to {max_request_bytes} bytes and must "
                            "state their length.")
    uploads: list[tuple[str, str, bytes]] = []
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            run_part = form.get("run") or "{}"
            if not isinstance(run_part, str):
                raise HTTPException(422, "The `run` part must be text (JSON), not a file.")
            body = RunRequest.model_validate(json.loads(run_part))
            for key, value in form.multi_items():
                if key == "run":
                    continue
                if not key.startswith("file.") or isinstance(value, str):
                    raise HTTPException(422, f"Unexpected form part {key!r}.")
                data = await value.read(max_upload_bytes + 1)
                if len(data) > max_upload_bytes:
                    raise HTTPException(413, f"{value.filename} is larger than the "
                                             f"{max_upload_bytes}-byte upload limit.")
                uploads.append((key.removeprefix("file."), value.filename or "", data))
        else:
            body = RunRequest.model_validate(await request.json())
    except (ValueError, TypeError, ValidationError) as e:
        raise HTTPException(422, f"Malformed Run request: {e}") from None
    return body, uploads


def create_app(settings: Settings, *, identity: IdentityProvider | None = None,
               catalog=None, clock=None) -> FastAPI:
    """`clock` (UTC now) times Approvals; tests move it on to check expiry (#77)."""
    clock = clock or utc_now
    catalog = engine_catalog() if catalog is None else catalog
    store = RunStore(settings.database_url, clock=clock,
                     ledger_enabled=settings.ledger_enabled)
    executor = Executor(store, settings, catalog, clock=clock)
    purger = Purger(store, settings, clock)
    notifier = TeamsNotifier(settings.teams_webhook_url, store, base_url=settings.public_url)

    @asynccontextmanager
    async def lifespan(_app):
        executor.start()  # Stopped for what a restart cut off; the queue resumes
        purger.start()  # the purge, in the background: now, then daily (#76)
        notifier.start()  # Approval messages to Teams, and the expiry sweep (#78)
        scheduler.start()  # Scheduled runs, and their Drift (#80)
        try:
            yield
        finally:
            await run_in_threadpool(scheduler.stop)
            await run_in_threadpool(notifier.stop)
            await run_in_threadpool(purger.stop)
            await run_in_threadpool(executor.stop)

    app = FastAPI(title="PagerDuty Ops", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.notifier = notifier  # tests drive the expiry sweep directly
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       https_only=True, same_site="lax", max_age=SESSION_MAX_AGE_SECONDS)
    if identity is None:
        if settings.okta is None:
            raise ValueError("Okta settings are required when no identity provider is given.")
        identity = OktaIdentity(settings.okta)
    identity.install(app)

    @app.middleware("http")
    async def require_ui_header(request: Request, call_next):
        if (request.method not in SAFE_METHODS and request.url.path.startswith("/api/")
                and request.headers.get(UI_REQUEST_HEADER) != "1"):
            return JSONResponse({"detail": f"Missing {UI_REQUEST_HEADER} header."}, 403)
        return await call_next(request)

    def signed_in(request: Request) -> tuple[Admin, Role]:
        admin = identity.current(request)
        if admin is None:
            raise HTTPException(401, "Not signed in.")
        role = role_for(admin.groups)
        if role is None:
            raise HTTPException(403, NO_ROLE_MESSAGE)
        return admin, role

    SignedIn = Annotated[tuple[Admin, Role], Depends(signed_in)]

    @app.get("/api/me")
    def me(who: SignedIn):
        admin, role = who
        return {"name": admin.name, "email": admin.email, "role": role.label,
                "max_phase": settings.max_phase}

    descriptions = None

    @app.get("/api/commands")
    def commands(who: SignedIn):
        nonlocal descriptions
        if descriptions is None:
            descriptions = [describe(e) for e in sorted(catalog.values(), key=lambda e: e.name)]
        return descriptions

    @app.get("/api/commands/{name}/form")
    def command_form(name: str, who: SignedIn):
        entry = catalog.get(name)
        if entry is None:
            raise HTTPException(404, f"Unknown command {name!r}.")
        return form_for(entry)

    @app.post("/api/runs", status_code=201)
    async def start_run(request: Request, who: SignedIn):
        admin, role = who
        body, uploads = await read_run_request(request, settings.max_upload_bytes,
                                               settings.max_request_bytes)
        entry = catalog.get(body.command)
        if entry is None:
            raise HTTPException(404, f"Unknown command {body.command!r}.")
        upload_paths = run_files.plan_uploads(uploads)
        if body.argv is not None:
            if body.values is not None or body.subcommand or uploads:
                raise HTTPException(422, "Send either argv or form values, not both.")
            argv = body.argv
        else:
            try:
                argv = build_argv(form_for(entry), body.subcommand, body.values or {},
                                  upload_paths)
            except BadValues as e:
                raise HTTPException(422, str(e)) from None

        run_id = str(uuid.uuid4())
        run_dir = settings.run_root / run_id
        run_dir.mkdir(parents=True)
        inputs = run_files.input_records(uploads, upload_paths)  # hashed as received
        try:  # uploads first: the gate's assessment may read an uploaded Plan
            await run_in_threadpool(run_files.write_uploads, run_dir, uploads, upload_paths)
            argv, verdict = gate(entry, argv, run_dir, settings.secret_env.values(), role)
            override = record_override(verdict, body.cap_override_reason, admin.email,
                                       settings.secret_env.values())
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        return start_now(new_run(run_id, entry.name, argv,
                                 "dry-run" if verdict.writes else "report",
                                 Requester(admin.email, admin.name, role.label), inputs,
                                 at=clock(), timeout_seconds=timeout_for(verdict),
                                 override=override),
                         inputs)

    def timeout_for(verdict) -> int:
        """2 hours, or the command's declared timeout (ADRs 0010, 0012)."""
        return (verdict.timeout_minutes * 60 if verdict.timeout_minutes
                else settings.run_timeout_seconds)

    def start_now(run: RunRecord, inputs: list[RunFile]) -> dict:
        """Store a report or dry run and start it in the background (it never queues)."""
        stored = store.save(run, inputs)
        executor.submit(stored["id"])
        return stored

    def own_dry_run(run_id: str, admin: Admin, role: Role, action: str) -> dict:
        if role < Role.OPERATOR:
            raise HTTPException(403, f"Only Operators and Approvers can {action} dry runs.")
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        if run["kind"] != "dry-run":
            raise HTTPException(409, f"Only a dry run can be {action}ed.")
        if run["requested_by"] != admin.email:
            raise HTTPException(403, f"Only the admin who ran this dry run can {action} it.")
        return run

    @app.post("/api/runs/{run_id}/review")
    def review_run(run_id: str, who: SignedIn):
        """The admin who ran a successful dry run records that they reviewed its diff."""
        admin, role = who
        run = own_dry_run(run_id, admin, role, "review")
        if run["outcome"] != "succeeded":
            raise HTTPException(409, "Only a dry run that succeeded can be reviewed: its "
                                     "changes weren't fully determined.")
        _, _, verdict = judge_apply(run)
        store.mark_reviewed(run_id, admin.email)
        if verdict.irreversible and run["approval"] is None:
            # Say up front that applying it needs an Approval, and why (#77).
            store.update(run_id, approval_reasons=list(verdict.reasons))
        return store.get(run_id)

    def judge_apply(dry: dict):
        """The engine's verdict on applying a dry run: same command, arguments and inputs,
        judged in its own directory so a relative Plan path resolves (ADR 0010)."""
        entry = catalog.get(dry["command"])
        if entry is None:
            raise HTTPException(404, f"Unknown command {dry['command']!r}.")
        neutral = [a for a in dry["argv"] if a not in MODE_FLAGS]
        return entry, neutral, gate_apply(entry, neutral, settings.run_root / dry["id"],
                                          settings.secret_env.values())[2]

    def copied_inputs(source_id: str, target_id: str) -> list[RunFile]:
        try:
            return copy_inputs(settings.run_root / source_id, settings.run_root / target_id,
                               store.files(source_id, "input"))
        except InputChanged as e:
            raise HTTPException(409, f"{e} Run a new dry run.") from None

    @app.post("/api/runs/{run_id}/apply", status_code=201)
    def apply_run(run_id: str, who: SignedIn):
        """Queue the apply of a reviewed dry run: same command, arguments and inputs (ADR
        0003). In its turn, a fresh dry run must first produce the reviewed output, or
        nothing is applied (ADRs 0011, 0012)."""
        admin, role = who
        dry = own_dry_run(run_id, admin, role, "apply")
        require_apply(settings.max_phase)  # Phase 1: no apply at all (ADR 0016)
        if dry["reviewed_by"] != admin.email:
            raise HTTPException(409, "This dry run hasn't been reviewed: review its changes "
                                     "before applying it.")
        entry = catalog.get(dry["command"])
        if entry is None:
            raise HTTPException(404, f"Unknown command {dry['command']!r}.")
        neutral = [a for a in dry["argv"] if a not in MODE_FLAGS]
        apply_id = str(uuid.uuid4())
        if not store.claim_apply(run_id, apply_id):  # atomic: at most one apply, even racing
            raise HTTPException(409, "This dry run has already been applied.")
        try:
            _, apply_argv, verdict = gate_apply(entry, neutral, settings.run_root / run_id,
                                                settings.secret_env.values())
            require_apply(settings.max_phase, verdict)  # Phase 2: not if it's irreversible
            # The apply raises the reviewed caps (identical inputs); check their reason is on
            # record rather than trusting the dry run, which an older version may have made.
            dry_record = store.record(run_id)
            check_reviewed(verdict, CapOverride.of(dry_record))
            approval = Approval.of(dry_record)
            require_approved(verdict, approval, clock())  # Irreversible: a valid Approval
            inputs = copied_inputs(run_id, apply_id)
            record = new_run(
                apply_id, entry.name, apply_argv, "apply",
                Requester(admin.email, admin.name, role.label), inputs,
                at=clock(), timeout_seconds=timeout_for(verdict), status=QUEUED,
                applied_from=run_id,
                override=CapOverride.of(dry_record))
            if verdict.irreversible:  # the approver is recorded on the apply too
                for column, value in approval.columns().items():
                    setattr(record, column, value)
            queued = store.save(record, inputs)
        except BaseException:
            store.release_apply(run_id, apply_id)
            shutil.rmtree(settings.run_root / apply_id, ignore_errors=True)
            raise
        executor.wake_writer()
        return queued

    # ---------- Approvals (#77) ----------

    @app.post("/api/runs/{run_id}/approval")
    def ask_for_approval(run_id: str, who: SignedIn):
        """The requester asks for an Approval on their reviewed dry run of an Irreversible
        operation. Whether it is irreversible is the engine's call (fails closed)."""
        admin, role = who
        dry = own_dry_run(run_id, admin, role, "ask for an Approval of")
        require_approvals(settings.max_phase)
        if dry["reviewed_by"] != admin.email:
            raise HTTPException(409, "Review this dry run's changes before asking for an "
                                     "Approval.")
        if dry["applied_run_id"]:
            raise HTTPException(409, "This dry run has already been applied.")
        current = dry["approval"]["status"] if dry["approval"] else None
        if current in (AWAITING, APPROVED):
            raise HTTPException(409, f"This dry run is already {current}.")
        if current in FINAL:
            raise HTTPException(409, f"This dry run's Approval was {current}; run a new dry "
                                     "run and ask again.")
        try:
            verify_inputs(settings.run_root / run_id, store.files(run_id, "input"))
        except InputChanged as e:
            raise HTTPException(409, f"{e} Run a new dry run.") from None
        _, _, verdict = judge_apply(dry)
        if not verdict.irreversible:
            raise HTTPException(409, "Applying this doesn't need an Approval: the engine "
                                     "doesn't count it as an Irreversible operation.")
        now = clock()
        store.update(run_id, **Approval(AWAITING, tuple(verdict.reasons), now,
                                        now + APPROVAL_LIFETIME).columns(),
                     approval_expiry_announced_at=None)  # a new request expires afresh
        asked = store.get(run_id)
        notifier.approval_event(AWAITING, asked)  # in the background; never fails this
        return asked

    def record_decision(run_id: str, who, body: Decision, decision: str) -> dict:
        admin, role = who
        if role < Role.APPROVER:
            raise HTTPException(403, "Only Approvers can approve or reject.")
        require_approvals(settings.max_phase)
        run = store.record(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        if run.requested_by == admin.email:
            raise HTTPException(403, "You can't approve or reject your own Run: an Approval "
                                     "is a second admin's consent.")
        comment = recorded_text("comment", body.comment, settings.secret_env.values())
        if not store.decide_approval(run_id, decision, admin.email, comment):
            raise HTTPException(409, "This Run isn't waiting for an Approval: it may have been "
                                     "decided already or expired (the requester can ask "
                                     "again).")
        decided = store.get(run_id)
        notifier.approval_event(decision, decided)  # in the background; never fails this
        return decided

    @app.post("/api/runs/{run_id}/approval/approve")
    def approve(run_id: str, body: Decision, who: SignedIn):
        """An Approver other than the requester consents, with a comment. Valid 24 hours."""
        return record_decision(run_id, who, body, APPROVED)

    @app.post("/api/runs/{run_id}/approval/reject")
    def reject(run_id: str, body: Decision, who: SignedIn):
        """An Approver other than the requester refuses, with a comment. Final for this
        dry run."""
        return record_decision(run_id, who, body, REJECTED)

    @app.get("/api/approvals")
    def waiting_for_approval(who: SignedIn):
        """Requests waiting for an Approver, oldest first."""
        return {"pending": store.awaiting_approval()}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, who: SignedIn):
        """The requester can cancel their own Run; an Approver can cancel anyone's."""
        admin, role = who
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        if run["requested_by"] != admin.email and role < Role.APPROVER:
            raise HTTPException(403, "Only the admin who requested this Run, or an Approver, "
                                     "can cancel it.")
        if run["status"] == FINISHED or not executor.cancel(run_id, admin.email):
            raise HTTPException(409, "This Run has already finished, or is already being "
                                     "cancelled.")
        return store.get(run_id)

    @app.post("/api/runs/{run_id}/fresh-dry-run", status_code=201)
    def fresh_dry_run(run_id: str, who: SignedIn):
        """After a Stopped apply, dry-run the same command and inputs again to show what
        remains (safe because applies are idempotent, ADR 0012)."""
        admin, role = who
        if role < Role.OPERATOR:
            raise HTTPException(403, "Only Operators and Approvers can run dry runs.")
        stopped = store.get(run_id)
        if stopped is None:
            raise HTTPException(404, "No such Run.")
        if stopped["kind"] != "apply" or stopped["outcome"] != "stopped":
            raise HTTPException(409, "Only a Stopped apply offers a fresh dry run.")
        if stopped["requested_by"] != admin.email:
            raise HTTPException(403, "Only the admin who requested this apply can dry-run "
                                     "it again.")
        dry = store.record(stopped["applied_from"])
        entry = catalog.get(dry.command)
        if entry is None:
            raise HTTPException(404, f"Unknown command {dry.command!r}.")
        fresh_id = str(uuid.uuid4())
        inputs = copied_inputs(dry.id, fresh_id)
        try:  # the same rules as any dry run, and its caps' reason must be on record
            argv, verdict = gate(entry, list(dry.argv), settings.run_root / fresh_id,
                                 settings.secret_env.values(), role)
            check_reviewed(verdict, CapOverride.of(dry))
        except BaseException:
            shutil.rmtree(settings.run_root / fresh_id, ignore_errors=True)
            raise
        return start_now(new_run(fresh_id, entry.name, argv, "dry-run",
                                 Requester(admin.email, admin.name, role.label), inputs,
                                 at=clock(), timeout_seconds=dry.timeout_seconds,
                                 override=CapOverride.of(dry)), inputs)

    @app.get("/api/queue")
    def queue(who: SignedIn):
        """The Writing run executing now, and those waiting for it, oldest first."""
        return store.queue()

    @app.get("/api/ledger")
    def ledger(who: SignedIn, command: str | None = None, requester: str | None = None,
               since: str | None = None, before: str | None = None,
               outcome: str | None = None, page: Annotated[int, Query(ge=1)] = 1,
               per_page: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE)] = 50):
        """The permanent Ledger of Writing runs (#76), for every role, filtered like the
        history. `enabled` is false while it is switched off (PD_UI_LEDGER=off);
        `retention_days` is how long the Runs themselves are kept."""
        if outcome is not None and outcome not in FINAL_OUTCOMES:
            raise HTTPException(422, f"outcome must be one of {sorted(FINAL_OUTCOMES)}.")
        rows = store.ledger(command=command or None, requester=(requester or "").strip(),
                            since=parse_instant("since", since),
                            before=parse_instant("before", before), outcome=outcome,
                            page=page, per_page=per_page)
        return {**rows, "retention_days": settings.retention_days}

    @app.get("/api/runs")
    def run_history(who: SignedIn, command: str | None = None, requester: str | None = None,
                    since: str | None = None, before: str | None = None,
                    outcome: str | None = None, kind: str | None = None,
                    page: Annotated[int, Query(ge=1)] = 1,
                    per_page: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE)] = 50):
        """The searchable Run history (#69), for every role: filters combine; newest first."""
        if outcome is not None and outcome not in HISTORY_OUTCOMES:
            raise HTTPException(422, f"outcome must be one of {sorted(HISTORY_OUTCOMES)}.")
        if kind is not None and kind not in RUN_KINDS:
            raise HTTPException(422, f"kind must be one of {sorted(RUN_KINDS)}.")
        return store.search(command=command or None, requester=(requester or "").strip(),
                            since=parse_instant("since", since),
                            before=parse_instant("before", before), outcome=outcome,
                            kind=kind, page=page, per_page=per_page)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, who: SignedIn):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        if run["schedule_id"]:  # a Scheduled run names its schedule, if it still exists
            schedule = store.schedule(run["schedule_id"])
            run["schedule_name"] = schedule.name if schedule else None
        return run

    # ---------- Scheduled runs (#80, ADR 0014) ----------

    def start_scheduled(entry, argv, verdict, record, run_id) -> None:
        """A Scheduled run is a report Run, requested by the schedule's creator."""
        start_now(new_run(run_id, entry.name, argv, "report", scheduled_by(record), [],
                          at=clock(), timeout_seconds=timeout_for(verdict),
                          schedule_id=record.id), [])

    def refuse_scheduled(record, run_id, reason: str) -> None:
        """A firing the engine refused is still recorded, so the history says why."""
        store.save(new_run(run_id, record.command, [], "report", scheduled_by(record), [],
                           at=clock(), timeout_seconds=settings.run_timeout_seconds,
                           schedule_id=record.id))
        store.finish(run_id, outcome="usage-error",
                     note=f"Not run: the schedule was refused. {reason}")

    def scheduled_by(record) -> Requester:
        return Requester(record.created_by, record.creator_name, record.creator_role)

    scheduler = Scheduler(store, settings, catalog, notifier, clock=clock,
                          start_run=start_scheduled, refuse_run=refuse_scheduled)
    app.state.scheduler = scheduler  # tests drive the ticks directly

    def operator(role: Role, action: str) -> None:
        if role < Role.OPERATOR:
            raise HTTPException(403, f"Only Operators and Approvers can {action} schedules.")

    def existing_schedule(schedule_id: str):
        record = store.schedule(schedule_id)
        if record is None:
            raise HTTPException(404, "No such schedule.")
        return record

    def judged(body: ScheduleBody):
        """The schedule's name, command and timing, once the engine has judged its command
        line read-only (403 otherwise)."""
        entry = catalog.get(body.command)
        if entry is None:
            raise HTTPException(404, f"Unknown command {body.command!r}.")
        name, timing = clean_name(body.name), parse_timing(body.timing)
        check_schedulable(entry, body.subcommand, body.values, settings.secret_env.values())
        return entry, name, timing

    @app.get("/api/schedules")
    def list_schedules(who: SignedIn):
        return {"schedules": [schedule_dict(r) for r in store.schedules()]}

    @app.get("/api/schedules/{schedule_id}")
    def get_schedule(schedule_id: str, who: SignedIn):
        return schedule_dict(existing_schedule(schedule_id))

    @app.post("/api/schedules", status_code=201)
    def create_schedule(body: ScheduleBody, who: SignedIn):
        admin, role = who
        operator(role, "create")
        entry, name, timing = judged(body)
        record = new_schedule(name=name, entry=entry, subcommand=body.subcommand,
                              values=body.values, timing=timing, admin=admin, role=role,
                              now=clock())
        return schedule_dict(store.save_schedule(record))

    @app.put("/api/schedules/{schedule_id}")
    def edit_schedule(schedule_id: str, body: ScheduleBody, who: SignedIn):
        """Replace a schedule's name, command line and timing. It stays its creator's: the
        editor is recorded as the last to change it."""
        admin, role = who
        operator(role, "edit")
        record = existing_schedule(schedule_id)
        entry, name, timing = judged(body)
        now = clock()
        store.update_schedule(schedule_id, name=name, command=entry.name,
                              subcommand=body.subcommand, values=body.values,
                              timing=timing.as_dict(), next_run_at=timing.next_after(now),
                              updated_by=admin.email, updated_at=now)
        return schedule_dict(existing_schedule(record.id))

    @app.post("/api/schedules/{schedule_id}/pause")
    def pause_schedule(schedule_id: str, who: SignedIn):
        admin, role = who
        operator(role, "pause")
        existing_schedule(schedule_id)
        store.update_schedule(schedule_id, paused=True, updated_by=admin.email,
                              updated_at=clock())
        return schedule_dict(existing_schedule(schedule_id))

    @app.post("/api/schedules/{schedule_id}/resume")
    def resume_schedule(schedule_id: str, who: SignedIn):
        """Times missed while paused don't run: it is next due after now."""
        admin, role = who
        operator(role, "resume")
        record = existing_schedule(schedule_id)
        now = clock()
        store.update_schedule(schedule_id, paused=False,
                              next_run_at=parse_timing(record.timing).next_after(now),
                              updated_by=admin.email, updated_at=now)
        return schedule_dict(existing_schedule(schedule_id))

    @app.delete("/api/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str, who: SignedIn):
        """Its Scheduled runs stay in the history."""
        _, role = who
        operator(role, "delete")
        if not store.delete_schedule(schedule_id):
            raise HTTPException(404, "No such schedule.")

    @app.get("/api/runs/{run_id}/files/{file_id}")
    def download(run_id: str, file_id: int, who: SignedIn):
        stored = store.get_file(run_id, file_id)
        if stored is None:
            raise HTTPException(404, "No such file.")
        path = run_files.resolve(settings.run_root / run_id, stored["path"])
        if not path.is_file() or run_files.sha256_file(path) != stored["sha256"]:
            raise HTTPException(409, "This file has changed or gone since the Run; it is "
                                     "not served.")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    if settings.static_dir is not None:  # the built React front end, mounted last
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")

    return app
