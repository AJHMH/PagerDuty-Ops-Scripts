"""Who is signed in. Production uses Okta OIDC; tests supply their own provider.

A provider has two methods: install(app) adds any routes it needs (login, callback,
logout), and current(request) returns the signed-in Admin or None.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from .settings import OktaSettings

SESSION_KEY = "admin"


@dataclass
class Admin:
    subject: str
    email: str
    name: str
    groups: list[str] = field(default_factory=list)


class IdentityProvider(Protocol):
    def install(self, app: FastAPI) -> None: ...

    def current(self, request: Request) -> Admin | None: ...


class OktaIdentity:
    """Okta OIDC authorization-code flow with PKCE; the admin lives in a signed session
    cookie. Okta's ID token must carry a ``groups`` claim (filtered to PD-Ops-*)."""

    def __init__(self, okta: OktaSettings):
        from authlib.integrations.starlette_client import OAuth

        self.okta = okta
        self.oauth = OAuth()
        self.oauth.register(
            name="okta",
            client_id=okta.client_id,
            client_secret=okta.client_secret,
            server_metadata_url=f"{okta.issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile email groups",
                           "code_challenge_method": "S256"},
        )

    def install(self, app: FastAPI) -> None:
        @app.get("/auth/login", include_in_schema=False)
        async def login(request: Request):
            return await self.oauth.okta.authorize_redirect(request, self.okta.redirect_uri)

        @app.get("/auth/callback", include_in_schema=False)
        async def callback(request: Request):
            token = await self.oauth.okta.authorize_access_token(request)
            claims = token.get("userinfo") or {}
            request.session[SESSION_KEY] = asdict(Admin(
                subject=claims.get("sub", ""),
                email=claims.get("email", ""),
                name=claims.get("name") or claims.get("email", ""),
                groups=list(claims.get("groups") or []),
            ))
            return RedirectResponse("/")

        @app.get("/auth/logout", include_in_schema=False)
        async def logout(request: Request):
            request.session.pop(SESSION_KEY, None)
            return RedirectResponse("/")

    def current(self, request: Request) -> Admin | None:
        data = request.session.get(SESSION_KEY)
        return Admin(**data) if data else None
