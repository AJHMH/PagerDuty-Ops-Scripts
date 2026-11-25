"""The service passes a Run exactly the environment the engine reads.

A Run only sees the variables named in ``SECRET_ENV_NAMES`` and ``ENGINE_ENV_NAMES`` (plus
``runner.PASSTHROUGH_ENV``). If those names drift from what ``pagerduty_ops.servicenow``
reads, ``pd-sn-sync`` Runs fail with a configuration error, so this test records every
variable the engine's ServiceNow configuration and auth code looks up and checks each one
is passed through.
"""

import types

import pytest

from pagerduty_ops import servicenow
from pagerduty_ops_ui import settings as ui_settings
from pagerduty_ops_ui.runner import child_env


class RecordingEnviron(dict):
    """A dict standing in for ``os.environ`` that remembers every name looked up."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


def names_the_engine_reads(monkeypatch) -> set[str]:
    environ = RecordingEnviron({
        "SN_INSTANCE": "dallastx-test",
        "SN_PROD_INSTANCES": "dallastx",
        "SN_REHEARSAL_INSTANCES": "dallastx-test",
        "OKTA_DOMAIN": "dallas.okta.example",
        "OKTA_AUTH_SERVER_ID": "aus123",
        "OKTA_CLIENT_ID": "okta-id",
        "OKTA_CLIENT_SECRET": "okta-secret",
        "OKTA_SCOPE": "servicenow.table",
        "SN_CLIENT_ID": "sn-id",
        "SN_CLIENT_SECRET": "sn-secret",
        "SN_USER": "integration",
        "SN_PASSWORD": "sn-password",
    })
    monkeypatch.setattr(servicenow, "os", types.SimpleNamespace(environ=environ))

    instance = servicenow.get_instance_config(for_write=True)
    servicenow.load_field_map()
    for method in servicenow.AUTH_METHODS:
        servicenow.build_auth(instance, method)
    servicenow.build_auth(instance)  # the SN_AUTH_METHOD lookup and its default
    return environ.read


def test_every_servicenow_setting_the_engine_reads_reaches_a_run(monkeypatch):
    read = names_the_engine_reads(monkeypatch)
    # Guard against the recorder silently seeing nothing.
    assert {"SN_INSTANCE", "OKTA_DOMAIN", "SN_USER", "SN_AUTH_METHOD"} <= read

    passed = set(ui_settings.SECRET_ENV_NAMES) | set(ui_settings.ENGINE_ENV_NAMES)
    assert read - passed == set(), "engine settings a Run never receives"


@pytest.mark.parametrize("name", ["OKTA_TOKEN_URL", "SN_USERNAME"])
def test_names_the_engine_never_reads_are_not_passed(name):
    passed = set(ui_settings.SECRET_ENV_NAMES) | set(ui_settings.ENGINE_ENV_NAMES)
    assert name not in passed


def test_settings_from_env_hand_the_servicenow_settings_to_a_run(monkeypatch):
    for name, value in {
        "PD_UI_DATABASE_URL": "sqlite://", "PD_UI_RUN_ROOT": "runs",
        "PD_UI_SESSION_SECRET": "s" * 32, "PD_UI_OKTA_ISSUER": "https://okta.example",
        "PD_UI_OKTA_CLIENT_ID": "ui-id", "PD_UI_OKTA_CLIENT_SECRET": "ui-secret",
        "PD_UI_OKTA_REDIRECT_URI": "https://pd-ops.example/auth/callback",
        "OKTA_DOMAIN": "dallas.okta.example", "OKTA_AUTH_SERVER_ID": "aus123",
        "SN_USER": "integration", "SN_PASSWORD": "sn-password",
    }.items():
        monkeypatch.setenv(name, value)

    loaded = ui_settings.Settings.from_env()
    env = child_env(loaded.extra_env, loaded.secret_env)

    assert env["OKTA_DOMAIN"] == "dallas.okta.example"
    assert env["OKTA_AUTH_SERVER_ID"] == "aus123"
    assert env["SN_USER"] == "integration"
    assert loaded.secret_env["SN_PASSWORD"] == "sn-password"
    assert "PD_UI_OKTA_CLIENT_SECRET" not in env
