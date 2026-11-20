"""#66 tracer bullet: sign in, run a read-only command, see the Run (Seam B: the HTTP API)."""

import pytest
from conftest import APPROVER, NO_ROLE, OPERATOR, SECRET, VIEWER

# ---------- signing in and roles ----------

def test_not_signed_in_is_401(client):
    assert client.get("/api/me").status_code == 401


def test_role_comes_from_the_okta_group(client):
    assert client.get("/api/me", headers=VIEWER).json()["role"] == "viewer"
    assert client.get("/api/me", headers=OPERATOR).json()["role"] == "operator"
    assert client.get("/api/me", headers=APPROVER).json()["role"] == "approver"


def test_signed_in_without_a_pd_ops_group_is_refused_with_whom_to_ask(client):
    response = client.get("/api/me", headers=NO_ROLE)
    assert response.status_code == 403
    assert "PD-Ops-Viewer" in response.json()["detail"]


# ---------- running a read-only command ----------

def run(client, argv, headers=VIEWER, command="ui-fixture"):
    return client.post("/api/runs", json={"command": command, "argv": argv}, headers=headers)


def test_viewer_runs_a_read_only_command_and_sees_the_run(client):
    response = run(client, ["--print", "hello", "--log", "working"])
    assert response.status_code == 201
    created = response.json()
    assert created["outcome"] == "succeeded"
    assert created["exit_code"] == 0
    assert created["stdout"] == "hello\n"
    assert created["stderr"] == "working\n"
    assert created["requested_by"] == "ann@dallas.gov"
    assert created["command"] == "ui-fixture"
    assert created["argv"] == ["--print", "hello", "--log", "working"]
    assert created["started_at"] <= created["finished_at"]

    stored = client.get(f"/api/runs/{created['id']}", headers=OPERATOR)
    assert stored.status_code == 200
    assert stored.json() == created


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope", headers=VIEWER).status_code == 404


def test_exit_codes_map_to_outcomes(client):
    outcomes = {code: run(client, ["--exit-code", str(code)]).json()["outcome"]
                for code in (0, 1, 3, 9)}
    assert outcomes == {0: "succeeded", 1: "failed", 3: "auth-error", 9: "failed"}
    usage = run(client, ["--exit-code", "2"]).json()
    assert (usage["exit_code"], usage["outcome"]) == (2, "usage-error")


# ---------- what is refused (ADRs 0003, 0010) ----------

def test_a_command_that_writes_is_refused_for_every_role(client):
    for headers in (VIEWER, OPERATOR, APPROVER):
        response = run(client, ["tags.csv", "--dry-run"], headers, command="pd-apply-tags")
        assert response.status_code == 403
        assert "writes" in response.json()["detail"]


def test_a_read_only_subcommand_of_a_writing_command_runs(client):
    report = run(client, ["report"], command="ui-subcommands")
    assert (report.status_code, report.json()["stdout"]) == (201, "report\n")
    assert run(client, ["apply", "--dry-run"], command="ui-subcommands").status_code == 403


def test_unknown_command_is_404(client):
    assert run(client, [], command="pd-no-such").status_code == 404


def test_arguments_the_command_rejects_are_refused_before_running(client):
    response = run(client, ["--no-such-flag"])
    assert response.status_code == 422


def test_help_is_refused(client):
    assert run(client, ["--help"]).status_code == 422


@pytest.mark.parametrize("argv", [
    ["--token", "abc"],
    ["-t", "abc"],
    ["-tabc"],
    ["--prompt"],
    ["--tok", "abc"],          # argparse accepts unique prefixes of long options
    ["--tok=abc"],
    ["--promp"],
])
def test_secrets_and_prompts_on_the_command_line_are_refused(client, argv):
    # pd-list-users really has -t/--token and --prompt, so only the gate can refuse these.
    response = run(client, argv, command="pd-list-users")
    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/etc/passwd", "..\\..\\x.csv", "../x.csv", "C:\\x.csv",
                                  "--report=../x", "-r/etc/x", "-rC:\\x.csv", "-r..\\x"])
def test_paths_outside_the_run_directory_are_refused(client, path):
    argv = [path] if path.startswith("-") else ["--report", path]
    assert run(client, argv).status_code == 422


# ---------- secrets (ADRs 0006, 0008) ----------

def test_secrets_reach_the_run_only_through_its_environment_and_are_redacted(client):
    created = run(client, ["--print-env", "PD_API_TOKEN"]).json()
    assert created["outcome"] == "succeeded"
    assert created["stdout"] == "[redacted]\n"       # the child really had the secret...
    stored = client.get(f"/api/runs/{created['id']}", headers=VIEWER).text
    assert SECRET not in stored                      # ...and it is stored nowhere


def test_the_service_environment_does_not_leak_into_runs(client, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_SECRET", "leaked-value")
    created = run(client, ["--print-env", "SOME_OTHER_SECRET"]).json()
    assert created["stdout"] == "<unset>\n"


def test_a_secret_value_in_the_arguments_is_refused(client):
    assert run(client, ["--print", f"x{SECRET}x"]).status_code == 422


# ---------- the command list the front end shows ----------

def test_command_list_covers_every_engine_script_with_descriptions(client):
    from pagerduty_ops.console_scripts import SCRIPTS

    commands = {c["name"]: c for c in client.get("/api/commands", headers=VIEWER).json()}
    assert set(SCRIPTS) <= set(commands)
    assert commands["pd-list-users"]["description"]
    assert commands["pd-list-users"]["read_only"] is True
    assert commands["pd-apply-tags"]["read_only"] is False
    assert commands["ui-subcommands"]["read_only"] is False  # some subcommands write


def test_command_list_needs_a_role(client):
    assert client.get("/api/commands", headers=NO_ROLE).status_code == 403


def test_the_built_front_end_is_served_beside_the_api(settings, catalog, tmp_path):
    from conftest import HeaderIdentity
    from fastapi.testclient import TestClient

    from pagerduty_ops_ui.app import create_app

    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>PagerDuty Ops</title>", "utf-8")
    settings.static_dir = static
    with TestClient(create_app(settings, identity=HeaderIdentity(), catalog=catalog)) as c:
        assert "PagerDuty Ops" in c.get("/").text
        assert c.get("/api/me").status_code == 401  # the API still wins over static files


def test_a_run_that_outlives_its_timeout_is_stopped(client, settings):
    settings.run_timeout_seconds = 1
    created = run(client, ["--sleep", "5"]).json()
    assert created["outcome"] == "stopped"
    assert created["exit_code"] is None
