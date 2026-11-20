"""The FastAPI application: the HTTP API that the React front end (and Seam B tests) use."""

import json
import shutil
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from . import files as run_files
from .catalog import describe, engine_catalog
from .forms import BadValues, build_argv, form_for
from .gate import gate
from .identity import Admin, IdentityProvider, OktaIdentity
from .review import input_hash, output_hash
from .roles import NO_ROLE_MESSAGE, Role, role_for
from .runner import run_command
from .settings import Settings
from .store import RunRecord, RunStore

# Roles are read from the session at sign-in; a short session means removing someone from a
# PD-Ops group in AD/Okta takes effect within a working day.
SESSION_MAX_AGE_SECONDS = 8 * 3600


class RunRequest(BaseModel):
    """Either raw `argv`, or a form's `values` (and `subcommand`) with any uploaded files."""

    command: str
    argv: list[str] | None = Field(default=None, max_length=200)
    subcommand: str | None = None
    values: dict | None = None


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(settings: Settings, *, identity: IdentityProvider | None = None,
               catalog=None) -> FastAPI:
    app = FastAPI(title="PagerDuty Ops", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       https_only=True, same_site="lax", max_age=SESSION_MAX_AGE_SECONDS)
    if identity is None:
        if settings.okta is None:
            raise ValueError("Okta settings are required when no identity provider is given.")
        identity = OktaIdentity(settings.okta)
    identity.install(app)
    catalog = engine_catalog() if catalog is None else catalog
    store = RunStore(settings.database_url)

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
        return {"name": admin.name, "email": admin.email, "role": role.label}

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
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        started = _now()
        timeout = (verdict.timeout_minutes * 60 if verdict.timeout_minutes
                   else settings.run_timeout_seconds)
        result = await run_in_threadpool(
            run_command, entry, argv, cwd=run_dir, extra_env=settings.extra_env,
            secret_env=settings.secret_env, timeout=timeout)
        outputs = await run_in_threadpool(run_files.collect_outputs, run_dir, inputs)
        return store.save(RunRecord(
            id=run_id, command=entry.name, argv=argv, requested_by=admin.email,
            requester_name=admin.name, requester_role=role.label, started_at=started,
            finished_at=_now(), exit_code=result.exit_code, outcome=result.outcome,
            stdout=result.stdout, stderr=result.stderr,
            kind="dry-run" if verdict.writes else "report",
            input_hash=input_hash(entry.name, argv, [(f.path, f.sha256) for f in inputs]),
            output_hash=output_hash(result.stdout, result.stderr,
                                    [(f.path, f.sha256) for f in outputs])),
            inputs + outputs)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, who: SignedIn):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        return run

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
