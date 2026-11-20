"""ServiceNow Table API client, authentication, and instance configuration.

Design rules (see docs/servicenow.md):
- Secrets come from the environment only, never CLI arguments, never logged.
- Auth is pluggable: ``okta`` (default; Okta client-credentials token accepted by
  ServiceNow as an inbound OIDC bearer), ``sn-oauth`` (ServiceNow's own OAuth
  client-credentials grant), or ``basic`` (integration user).
- Instances are classified as production / rehearsal / other from environment
  configuration. Writes to production require a rehearsal receipt (receipts.py).
- Errors raise ``SNApiError`` (a ``PDApiError`` subclass) so the shared CLI
  wrapper maps 401/403 to exit 3 and other failures to exit 1.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .api import MAX_RETRY_AFTER_SECONDS, REQUEST_TIMEOUT, PDApiError
from .config import ConfigError
from .log import get_logger

log = get_logger("servicenow")

TABLE_PAGE_SIZE = 500
PATCH_429_ATTEMPTS = 4
INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
AUTH_METHODS = ("okta", "sn-oauth", "basic")

# Column names written by the PagerDuty ServiceNow app (scope x_pd_integration).
DEFAULT_FIELD_MAP = {
    "user_pd_id": "x_pd_integration_pagerduty_id",
    "group_team": "x_pd_integration_pagerduty_team",
    "group_escalation": "x_pd_integration_pagerduty_escalation",
    "group_schedule": "x_pd_integration_pagerduty_schedule",
    "group_service": "x_pd_integration_pagerduty_service",
    "group_webhook": "x_pd_integration_pagerduty_webhook",
    "group_error": "x_pd_integration_pd_error_message",
}


class SNApiError(PDApiError):
    """A ServiceNow API call failed."""


# ---------------- configuration ----------------

def _csv_env(name: str) -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()}


@dataclass
class InstanceConfig:
    name: str
    prod_instances: set[str] = field(default_factory=set)
    rehearsal_instances: set[str] = field(default_factory=set)

    @property
    def base_url(self) -> str:
        return f"https://{self.name}.service-now.com"

    @property
    def is_prod(self) -> bool:
        return self.name in self.prod_instances

    @property
    def is_rehearsal(self) -> bool:
        return self.name in self.rehearsal_instances


def get_instance_config(cli_instance: str | None = None, *, for_write: bool = False):
    """Resolve the target instance (``--instance`` or ``SN_INSTANCE``).

    For writes, ``SN_PROD_INSTANCES`` must be configured so a production instance
    can never be mistaken for a sandbox (fail closed).
    """
    name = (cli_instance or os.environ.get("SN_INSTANCE") or "").strip().lower()
    if not name:
        raise ConfigError("No ServiceNow instance. Pass --instance or set SN_INSTANCE.")
    if name.startswith("https://"):
        name = name[len("https://"):].split(".", 1)[0]
    if not INSTANCE_RE.match(name):
        raise ConfigError(
            f"Invalid ServiceNow instance name {name!r} (expected e.g. 'dallastx-test')."
        )
    cfg = InstanceConfig(
        name=name,
        prod_instances=_csv_env("SN_PROD_INSTANCES"),
        rehearsal_instances=_csv_env("SN_REHEARSAL_INSTANCES"),
    )
    if for_write and not cfg.prod_instances:
        raise ConfigError(
            "SN_PROD_INSTANCES is not set. Refusing ServiceNow writes until production "
            "instances are declared (e.g. SN_PROD_INSTANCES=dallastx)."
        )
    if cfg.prod_instances & cfg.rehearsal_instances:
        raise ConfigError("An instance cannot be both in SN_PROD_INSTANCES and "
                          "SN_REHEARSAL_INSTANCES.")
    return cfg


def load_field_map(path: str | None = None) -> dict:
    """Default x_pd_integration column names, overridable by a JSON file
    (``--field-map`` or ``SN_FIELD_MAP``) containing any subset of the keys."""
    path = path or os.environ.get("SN_FIELD_MAP")
    fields = dict(DEFAULT_FIELD_MAP)
    if not path:
        return fields
    try:
        with open(path, encoding="utf-8") as f:
            overrides = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"Cannot read field map {path}: {e}") from e
    unknown = set(overrides) - set(DEFAULT_FIELD_MAP)
    if unknown:
        raise ConfigError(f"Unknown field-map keys {sorted(unknown)}; "
                          f"valid: {sorted(DEFAULT_FIELD_MAP)}")
    for key, value in overrides.items():
        if not isinstance(value, str) or not re.match(r"^[a-z0-9_]+$", value):
            raise ConfigError(f"Field map {key}: {value!r} is not a valid column name.")
    fields.update(overrides)
    return fields


# ---------------- authentication ----------------

def _require_env(*names: str) -> list[str]:
    values = [os.environ.get(n, "").strip() for n in names]
    missing = [n for n, v in zip(names, values, strict=True) if not v]
    if missing:
        raise ConfigError(f"Missing ServiceNow auth configuration: {', '.join(missing)}")
    return values


class _TokenAuth:
    """Client-credentials bearer token with in-process caching until expiry."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str | None):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._token: str | None = None
        self._expires_at = 0.0

    def headers(self) -> dict:
        if not self._token or time.monotonic() >= self._expires_at - 60:
            self._fetch()
        return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> None:
        self._token = None

    def _fetch(self) -> None:
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        try:
            resp = requests.post(
                self.token_url, data=data, auth=(self.client_id, self.client_secret),
                headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise SNApiError(f"token request failed: network error: {e}") from e
        if resp.status_code >= 400:
            # Never log the body: token endpoints can echo client details.
            raise SNApiError(f"token request to {self.token_url} -> HTTP {resp.status_code}",
                             status_code=401 if resp.status_code in (400, 401) else
                             resp.status_code)
        try:
            body = resp.json()
            self._token = body["access_token"]
        except (ValueError, KeyError) as e:
            raise SNApiError("token response did not contain access_token") from e
        self._expires_at = time.monotonic() + float(body.get("expires_in", 300))


class _BasicAuth:
    def __init__(self, user: str, password: str):
        self.auth = (user, password)

    def headers(self) -> dict:
        return {}

    def invalidate(self) -> None:
        pass


def build_auth(instance: InstanceConfig, method: str | None = None):
    method = (method or os.environ.get("SN_AUTH_METHOD") or "okta").strip().lower()
    if method not in AUTH_METHODS:
        raise ConfigError(f"SN_AUTH_METHOD must be one of {', '.join(AUTH_METHODS)}.")
    if method == "okta":
        domain, server, cid, secret = _require_env(
            "OKTA_DOMAIN", "OKTA_AUTH_SERVER_ID", "OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET")
        domain = domain.removeprefix("https://").rstrip("/")
        return _TokenAuth(f"https://{domain}/oauth2/{server}/v1/token", cid, secret,
                          os.environ.get("OKTA_SCOPE"))
    if method == "sn-oauth":
        cid, secret = _require_env("SN_CLIENT_ID", "SN_CLIENT_SECRET")
        return _TokenAuth(f"{instance.base_url}/oauth_token.do", cid, secret, None)
    user, password = _require_env("SN_USER", "SN_PASSWORD")
    return _BasicAuth(user, password)


# ---------------- Table API client ----------------

def _redacted_sn_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        if isinstance(err, dict):
            return str(err.get("message") or "")
    except ValueError:
        pass
    return ""


class ServiceNowClient:
    def __init__(self, instance: InstanceConfig, auth):
        self.instance = instance
        self.auth = auth
        retry = Retry(
            total=5, connect=3, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",), respect_retry_after_header=True, raise_on_status=False,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        if isinstance(auth, _BasicAuth):
            self.session.auth = auth.auth

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        url = f"{self.instance.base_url}/{path.lstrip('/')}"
        attempts = PATCH_429_ATTEMPTS if method != "GET" else 1
        resp = None
        for attempt in range(attempts + 1):
            headers = {"Accept": "application/json", "Content-Type": "application/json",
                       **self.auth.headers()}
            try:
                resp = self.session.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                raise SNApiError(f"{method} {path}: network error: {e}") from e
            if resp.status_code == 401 and attempt == 0 and not isinstance(self.auth, _BasicAuth):
                self.auth.invalidate()  # expired token: refresh once
                continue
            if resp.status_code == 429 and attempt < attempts:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = float(2 ** attempt)
                time.sleep(min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS))
                continue
            break
        if resp.status_code >= 400:
            detail = _redacted_sn_error(resp)
            raise SNApiError(
                f"ServiceNow {method} {path} -> HTTP {resp.status_code}"
                + (f": {detail}" if detail else ""),
                status_code=resp.status_code,
            )
        if not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise SNApiError(f"ServiceNow {method} {path}: invalid JSON") from e

    def get_records(self, table: str, *, query: str = "", fields: list[str] | None = None,
                    page_size: int = TABLE_PAGE_SIZE) -> list[dict]:
        """All records of ``table`` matching an encoded ``query`` (offset-paginated)."""
        records: list[dict] = []
        offset = 0
        while True:
            params = {
                "sysparm_limit": page_size,
                "sysparm_offset": offset,
                "sysparm_exclude_reference_link": "true",
                "sysparm_display_value": "false",
            }
            if query:
                params["sysparm_query"] = query
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            page = self._request("GET", f"api/now/table/{table}", params=params).get(
                "result", [])
            records.extend(page)
            if len(page) < page_size:
                return records
            offset += page_size

    def patch_record(self, table: str, sys_id: str, values: dict) -> dict:
        if not re.match(r"^[0-9a-f]{32}$", sys_id or ""):
            raise SNApiError(f"refusing to PATCH {table}: invalid sys_id {sys_id!r}")
        return self._request("PATCH", f"api/now/table/{table}/{sys_id}",
                             params={"sysparm_fields": ",".join(["sys_id", *values])},
                             json_body=values).get("result", {})


def connect(cli_instance: str | None = None, *, for_write: bool = False,
            auth_method: str | None = None) -> ServiceNowClient:
    instance = get_instance_config(cli_instance, for_write=for_write)
    return ServiceNowClient(instance, build_auth(instance, auth_method))
