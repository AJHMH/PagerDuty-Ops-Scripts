"""The service passes a Run exactly the environment the engine reads.

A Run only sees the variables named in ``SECRET_ENV_NAMES`` and ``ENGINE_ENV_NAMES`` (plus
``runner.PASSTHROUGH_ENV``). If those names drift from what ``pagerduty_ops.servicenow``
reads, ``pd-sn-sync`` Runs fail with a configuration error, so this test records every
variable the engine's ServiceNow configuration, auth and rehearsal-receipt code looks up and
checks each one is passed through (or, for the receipts directory, set by the service).
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from pagerduty_ops import receipts, servicenow
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


def names_the_receipts_code_reads(monkeypatch) -> set[str]:
    environ = RecordingEnviron()
    monkeypatch.setattr(receipts, "os", types.SimpleNamespace(environ=environ,
                                                               path=receipts.os.path))
    receipts.receipts_dir()
    receipts.max_age_days()
    return environ.read


def passed_to_runs() -> set[str]:
    return (set(ui_settings.SECRET_ENV_NAMES) | set(ui_settings.ENGINE_ENV_NAMES)
            | set(ui_settings.SERVICE_ENV_NAMES))


def test_every_servicenow_setting_the_engine_reads_reaches_a_run(monkeypatch):
    read = names_the_engine_reads(monkeypatch)
    # Guard against the recorder silently seeing nothing.
    assert {"SN_INSTANCE", "OKTA_DOMAIN", "SN_USER", "SN_AUTH_METHOD"} <= read

    assert read - passed_to_runs() == set(), "engine settings a Run never receives"


def test_every_receipt_setting_the_engine_reads_reaches_a_run(monkeypatch):
    read = names_the_receipts_code_reads(monkeypatch)
    assert {"PD_OPS_RECEIPTS_DIR", "SN_RECEIPT_MAX_AGE_DAYS"} <= read

    assert read - passed_to_runs() == set(), "receipt settings a Run never receives"
    assert "SN_RECEIPT_MAX_AGE_DAYS" in ui_settings.ENGINE_ENV_NAMES


@pytest.mark.parametrize("name", ["OKTA_TOKEN_URL", "SN_USERNAME"])
def test_names_the_engine_never_reads_are_not_passed(name):
    assert name not in passed_to_runs()


SERVICE_ENV = {
    "PD_UI_DATABASE_URL": "sqlite://", "PD_UI_RUN_ROOT": "runs",
    "PD_UI_SESSION_SECRET": "s" * 32, "PD_UI_OKTA_ISSUER": "https://okta.example",
    "PD_UI_OKTA_CLIENT_ID": "ui-id", "PD_UI_OKTA_CLIENT_SECRET": "ui-secret",
    "PD_UI_OKTA_REDIRECT_URI": "https://pd-ops.example/auth/callback",
}


def service_env(monkeypatch, **extra):
    monkeypatch.delenv("PD_OPS_RECEIPTS_DIR", raising=False)
    for name, value in {**SERVICE_ENV, **extra}.items():
        monkeypatch.setenv(name, value)


def test_settings_from_env_hand_the_servicenow_settings_to_a_run(monkeypatch):
    service_env(monkeypatch, OKTA_DOMAIN="dallas.okta.example", OKTA_AUTH_SERVER_ID="aus123",
                SN_USER="integration", SN_PASSWORD="sn-password", SN_RECEIPT_MAX_AGE_DAYS="3")

    loaded = ui_settings.Settings.from_env()
    env = child_env(loaded.extra_env, loaded.secret_env)

    assert env["OKTA_DOMAIN"] == "dallas.okta.example"
    assert env["OKTA_AUTH_SERVER_ID"] == "aus123"
    assert env["SN_USER"] == "integration"
    assert loaded.secret_env["SN_PASSWORD"] == "sn-password"
    assert env["SN_RECEIPT_MAX_AGE_DAYS"] == "3"
    assert "PD_UI_OKTA_CLIENT_SECRET" not in env


# ---------- one shared receipts directory (ADR 0007) ----------

def test_runs_get_an_absolute_receipts_directory_under_the_run_root_by_default(monkeypatch,
                                                                               tmp_path):
    monkeypatch.chdir(tmp_path)
    service_env(monkeypatch)  # PD_UI_RUN_ROOT=runs, relative to the service's directory

    env = child_env(ui_settings.Settings.from_env().extra_env, {})

    # Absolute: each Run's cwd is its own directory, so a relative path would differ per Run.
    assert env["PD_OPS_RECEIPTS_DIR"] == str(tmp_path / "runs" / "receipts")


def test_the_service_environment_can_name_the_receipts_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    service_env(monkeypatch, PD_OPS_RECEIPTS_DIR="shared-receipts")

    env = child_env(ui_settings.Settings.from_env().extra_env, {})

    assert env["PD_OPS_RECEIPTS_DIR"] == str(tmp_path / "shared-receipts")


def test_settings_built_directly_also_set_the_receipts_directory(tmp_path):
    built = ui_settings.Settings(database_url="sqlite://", run_root=tmp_path / "runs",
                                 session_secret="s")
    assert built.extra_env["PD_OPS_RECEIPTS_DIR"] == str(tmp_path / "runs" / "receipts")


ENGINE_RECEIPT = """
import sys
from pagerduty_ops import receipts
directory = receipts.receipts_dir()
if sys.argv[1] == "rehearse":
    receipts.write_receipt(directory, sha="ab" * 32, kind="ids", instance="dallastx-test",
                           succeeded=1, skipped=0)
else:
    receipts.require_receipt(directory, sha="ab" * 32, rehearsal_instances={"dallastx-test"})
    print("receipt OK")
"""


def test_a_receipt_written_in_one_run_directory_unlocks_a_run_in_another(tmp_path):
    loaded = ui_settings.Settings(database_url="sqlite://", run_root=tmp_path / "runs",
                                  session_secret="s")
    env = child_env(loaded.extra_env, {})
    env["PYTHONPATH"] = str(Path(receipts.__file__).resolve().parents[1])

    def run(run_id, step):
        cwd = loaded.run_root / run_id
        cwd.mkdir(parents=True)
        return subprocess.run([sys.executable, "-c", ENGINE_RECEIPT, step], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=60)

    rehearsal = run("rehearsal-run", "rehearse")
    assert rehearsal.returncode == 0, rehearsal.stderr
    production = run("production-run", "production")
    assert production.returncode == 0, production.stderr
    assert production.stdout.strip() == "receipt OK"
    assert not (loaded.run_root / "rehearsal-run" / ".pd_ops_receipts").exists()
