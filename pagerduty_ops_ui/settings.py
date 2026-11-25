"""Service configuration, read from the environment at startup.

Secrets (the PagerDuty account key, the ServiceNow/Okta client secrets) are fetched from the
City's vault into the service's environment (ADR 0008) and reach Runs only as environment
variables, never on a command line or in a file.

Rehearsal receipts live in one shared directory on the service (ADR 0007), so a plan rehearsed
in one Run unlocks the production apply in another. Each Run has its own working directory, so
the engine's default (``.pd_ops_receipts`` under the current directory) would give every Run
its own receipts; the service sets ``PD_OPS_RECEIPTS_DIR`` to an absolute path for every Run.
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
    "SN_RECEIPT_MAX_AGE_DAYS",
)
# Engine configuration the service sets for every Run itself (pagerduty_ops/receipts.py).
RECEIPTS_ENV = "PD_OPS_RECEIPTS_DIR"
SERVICE_ENV_NAMES = (RECEIPTS_ENV,)
# Where receipts go when the service's environment doesn't name a directory.
DEFAULT_RECEIPTS_SUBDIR = "receipts"


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
    receipts_dir: Path | None = None  # default <run_root>/receipts
    okta: OktaSettings | None = None
    static_dir: Path | None = None
    run_timeout_seconds: int = 2 * 3600  # ADR 0012; a command's declaration may extend it
    max_upload_bytes: int = 25 * 1024 * 1024
    max_request_bytes: int = 100 * 1024 * 1024
    # ADR 0013: Runs and their files are purged after this many days; the Ledger is kept,
    # unless Records Management declines it (PD_UI_LEDGER=off), when it is purged too.
    retention_days: int = 90
    ledger_enabled: bool = True

    def __post_init__(self):
        # Absolute, so it names the same directory whichever Run directory is the cwd.
        self.receipts_dir = Path(os.path.abspath(
            self.receipts_dir or self.run_root / DEFAULT_RECEIPTS_SUBDIR))
        self.extra_env[RECEIPTS_ENV] = str(self.receipts_dir)

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
            receipts_dir=Path(os.environ[RECEIPTS_ENV].strip())
            if os.environ.get(RECEIPTS_ENV, "").strip() else None,
            okta=OktaSettings(
                issuer=need("PD_UI_OKTA_ISSUER"),
                client_id=need("PD_UI_OKTA_CLIENT_ID"),
                client_secret=need("PD_UI_OKTA_CLIENT_SECRET"),
                redirect_uri=need("PD_UI_OKTA_REDIRECT_URI"),
            ),
            ledger_enabled=ledger_switch(),
            static_dir=Path(os.environ["PD_UI_STATIC_DIR"])
            if os.environ.get("PD_UI_STATIC_DIR") else None,
        )


LEDGER_ON = ("on", "true", "1", "yes", "")
LEDGER_OFF = ("off", "false", "0", "no")


def ledger_switch() -> bool:
    """PD_UI_LEDGER: on unless switched off. A value that is neither is a configuration error,
    not a guess either way: the Ledger is a records decision (ADR 0013)."""
    value = os.environ.get("PD_UI_LEDGER", "").strip().lower()
    if value in LEDGER_ON:
        return True
    if value in LEDGER_OFF:
        return False
    raise SystemExit(f"PD_UI_LEDGER must be on or off (or true/false, 1/0, yes/no), not "
                     f"{value!r}.")
