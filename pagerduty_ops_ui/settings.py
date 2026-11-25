"""Service configuration, read from the environment at startup.

Secrets (the PagerDuty account key, the ServiceNow/Okta client secrets) are fetched from the
City's vault into the service's environment (ADR 0008) and reach Runs only as environment
variables, never on a command line or in a file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Environment variables that are secrets: passed to Runs, redacted from everything stored.
SECRET_ENV_NAMES = (
    "PD_API_TOKEN",
    "SN_CLIENT_ID", "SN_CLIENT_SECRET", "SN_USER", "SN_PASSWORD",
    "OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET",
)
# Non-secret engine configuration passed through to Runs. Names must match what the engine
# reads (pagerduty_ops/servicenow.py); tests/ui/test_settings.py checks.
ENGINE_ENV_NAMES = (
    "PD_FROM_EMAIL", "SN_INSTANCE", "SN_PROD_INSTANCES", "SN_REHEARSAL_INSTANCES",
    "SN_AUTH_METHOD", "SN_FIELD_MAP", "OKTA_DOMAIN", "OKTA_AUTH_SERVER_ID", "OKTA_SCOPE",
)


@dataclass
class OktaSettings:
    issuer: str            # e.g. https://dallas.okta.com/oauth2/default
    client_id: str
    client_secret: str
    redirect_uri: str      # e.g. https://pd-ops.dallas.gov/auth/callback


@dataclass
class Settings:
    database_url: str
    run_root: Path
    session_secret: str
    secret_env: dict[str, str] = field(default_factory=dict)
    extra_env: dict[str, str] = field(default_factory=dict)
    okta: OktaSettings | None = None
    static_dir: Path | None = None
    run_timeout_seconds: int = 2 * 3600  # ADR 0012; a command's declaration may extend it
    max_upload_bytes: int = 25 * 1024 * 1024
    max_request_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        def need(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise SystemExit(f"{name} is not set.")
            return value

        return cls(
            database_url=need("PD_UI_DATABASE_URL"),
            run_root=Path(need("PD_UI_RUN_ROOT")),
            session_secret=need("PD_UI_SESSION_SECRET"),
            secret_env={n: os.environ[n] for n in SECRET_ENV_NAMES if os.environ.get(n)},
            extra_env={n: os.environ[n] for n in ENGINE_ENV_NAMES if os.environ.get(n)},
            okta=OktaSettings(
                issuer=need("PD_UI_OKTA_ISSUER"),
                client_id=need("PD_UI_OKTA_CLIENT_ID"),
                client_secret=need("PD_UI_OKTA_CLIENT_SECRET"),
                redirect_uri=need("PD_UI_OKTA_REDIRECT_URI"),
            ),
            static_dir=Path(os.environ["PD_UI_STATIC_DIR"])
            if os.environ.get("PD_UI_STATIC_DIR") else None,
        )
