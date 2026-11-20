"""The FastAPI application: the HTTP API that the React front end (and Seam B tests) use."""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .catalog import describe, engine_catalog
from .gate import gate
from .identity import Admin, IdentityProvider, OktaIdentity
from .roles import NO_ROLE_MESSAGE, Role, role_for
from .runner import run_command
from .settings import Settings
from .store import RunRecord, RunStore


class RunRequest(BaseModel):
    command: str
    argv: list[str] = Field(default_factory=list, max_length=200)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(settings: Settings, *, identity: IdentityProvider | None = None,
               catalog=None) -> FastAPI:
    app = FastAPI(title="PagerDuty Ops", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       https_only=True, same_site="lax")
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

    @app.post("/api/runs", status_code=201)
    def start_run(body: RunRequest, who: SignedIn):
        admin, role = who
        entry = catalog.get(body.command)
        if entry is None:
            raise HTTPException(404, f"Unknown command {body.command!r}.")
        run_id = str(uuid.uuid4())
        run_dir = settings.run_root / run_id
        gate(entry, body.argv, run_dir, settings.secret_env.values())
        run_dir.mkdir(parents=True)
        started = _now()
        result = run_command(entry, body.argv, cwd=run_dir, extra_env=settings.extra_env,
                             secret_env=settings.secret_env)
        return store.save(RunRecord(
            id=run_id, command=entry.name, argv=body.argv, requested_by=admin.email,
            requester_name=admin.name, requester_role=role.label, started_at=started,
            finished_at=_now(), exit_code=result.exit_code, outcome=result.outcome,
            stdout=result.stdout, stderr=result.stderr))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, who: SignedIn):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such Run.")
        return run

    if settings.static_dir is not None:  # the built React front end, mounted last
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")

    return app
