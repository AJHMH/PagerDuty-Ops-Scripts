"""#73 Apply from a reviewed dry run (ADRs 0003, 0011). Seam B: the HTTP API with real
subprocess Runs of test-only fixture commands, which print APPLIED only when they write."""

import json

import pytest
from conftest import APPROVER, OPERATOR, VIEWER, admin_header, finished, wait

OTHER_OPERATOR = admin_header("PD-Ops-Operator", email="bob@dallas.gov", name="Bob")


def dry_run(client, command="ui-writer", values=None, files=None, headers=OPERATOR):
    body = {"command": command, "values": values or {}}
    if files:
        response = client.post("/api/runs", data={"run": json.dumps(body)}, files=files,
                               headers=headers)
    else:
        response = client.post("/api/runs", json=body, headers=headers)
    created = finished(client, response)
    assert created["kind"] == "dry-run"
    return created


def review(client, run_id, headers=OPERATOR):
    return client.post(f"/api/runs/{run_id}/review", headers=headers)


def apply(client, run_id, headers=OPERATOR):
    return client.post(f"/api/runs/{run_id}/apply", headers=headers)


def reviewed(client, **kwargs):
    created = dry_run(client, **kwargs)
    assert review(client, created["id"]).status_code == 200
    return created


# ---------- the happy path ----------

def test_a_reviewed_dry_run_is_applied_with_yes_and_without_dry_run(client):
    dry = reviewed(client, values={"note": "x"})
    response = apply(client, dry["id"])
    assert response.status_code == 201, response.text
    assert response.json()["status"] in ("queued", "running")
    applied = wait(client, response.json()["id"])
    assert applied["kind"] == "apply"
    assert applied["argv"] == ["--note", "x", "-y"]
    assert (applied["outcome"], applied["stdout"]) == ("succeeded", "APPLIED\n")
    assert applied["applied_from"] == dry["id"]
    assert applied["input_hash"] == dry["input_hash"]
    stored = client.get(f"/api/runs/{dry['id']}", headers=OPERATOR).json()
    assert stored["reviewed_by"] == "ann@dallas.gov"
    assert stored["applied_run_id"] == applied["id"]


def test_a_diff_first_command_is_applied_with_apply_and_yes(client):
    dry = reviewed(client, command="ui-differ")
    applied = finished(client, apply(client, dry["id"]))
    assert (applied["argv"], applied["stdout"]) == (["--apply", "-y"], "APPLIED\n")


def test_uploaded_inputs_are_reused_for_the_apply(client):
    upload = [("file.input", ("services.csv", b"service_id\n", "text/csv"))]
    dry = reviewed(client, files=upload)
    applied = finished(client, apply(client, dry["id"]))
    assert applied["argv"] == ["--input", "inputs/input/services.csv", "-y"]
    inputs = [f for f in applied["files"] if f["role"] == "input"]
    assert [(f["path"], f["sha256"]) for f in inputs] == [
        (f["path"], f["sha256"]) for f in dry["files"] if f["role"] == "input"]


# ---------- the gates ----------

def test_an_unreviewed_dry_run_cannot_be_applied(client):
    dry = dry_run(client)
    response = apply(client, dry["id"])
    assert response.status_code == 409
    assert "review" in response.json()["detail"]


def test_only_the_admin_who_ran_the_dry_run_reviews_and_applies_it(client):
    dry = dry_run(client)
    assert review(client, dry["id"], headers=OTHER_OPERATOR).status_code == 403
    review(client, dry["id"])
    assert apply(client, dry["id"], headers=OTHER_OPERATOR).status_code == 403


def test_viewers_can_neither_review_nor_apply(client):
    report = client.post("/api/runs", json={"command": "ui-fixture", "values": {}},
                         headers=VIEWER).json()
    assert review(client, report["id"], headers=VIEWER).status_code == 403
    assert apply(client, report["id"], headers=VIEWER).status_code == 403


@pytest.mark.parametrize("values", [{"fail": True}])
def test_only_a_successful_dry_run_can_be_reviewed(client, values):
    failed = dry_run(client, values=values)
    assert review(client, failed["id"]).status_code == 409


def test_a_report_is_not_a_dry_run(client):
    report = client.post("/api/runs", json={"command": "ui-fixture", "values": {}},
                         headers=APPROVER).json()
    assert review(client, report["id"], headers=APPROVER).status_code == 409


def test_a_dry_run_is_applied_only_once(client):
    dry = reviewed(client)
    assert apply(client, dry["id"]).status_code == 201
    assert apply(client, dry["id"]).status_code == 409


def test_an_input_changed_since_the_review_blocks_the_apply(client, settings):
    upload = [("file.input", ("services.csv", b"service_id\n", "text/csv"))]
    dry = reviewed(client, files=upload)
    (settings.run_root / dry["id"] / "inputs/input/services.csv").write_text("tampered\n")
    response = apply(client, dry["id"])
    assert response.status_code == 409
    assert "input" in response.json()["detail"]


def test_a_fresh_dry_run_that_differs_blocks_the_apply_and_is_stored_for_review(client,
                                                                                 settings):
    # The fresh dry run runs inside the writing slot, just before the apply (ADR 0012).
    dry = reviewed(client)
    settings.extra_env["UI_FIXTURE_DRIFT"] = "1"   # live state changes after the review
    blocked = finished(client, apply(client, dry["id"]))
    assert (blocked["kind"], blocked["outcome"]) == ("apply", "changed")
    assert "changed" in blocked["message"]
    assert blocked["stdout"] == ""                 # the apply itself never ran
    fresh = client.get(f"/api/runs/{blocked['fresh_dry_run_id']}", headers=OPERATOR).json()
    assert fresh["kind"] == "dry-run"
    assert "+ drifted-service" in fresh["stdout"]
    assert fresh["output_hash"] != dry["output_hash"]
    assert fresh["reviewed_by"] is None
    # Nothing was applied, and the original dry run can be applied again.
    assert client.get(f"/api/runs/{dry['id']}", headers=OPERATOR).json()["applied_run_id"] is None


def test_an_irreversible_operation_is_refused_until_approvals_exist(client):
    dry = reviewed(client, values={"purge": True})
    response = apply(client, dry["id"])
    assert response.status_code == 403
    assert "Approval" in response.json()["detail"]


# ---------- outcomes of the apply ----------

@pytest.mark.parametrize("code, outcome", [(1, "failed"), (3, "auth-error")])
def test_an_apply_that_fails_is_recorded_with_its_outcome_and_message(client, settings, code,
                                                                       outcome):
    dry = reviewed(client)
    settings.extra_env["UI_FIXTURE_APPLY_EXIT"] = str(code)
    applied = finished(client, apply(client, dry["id"]))
    assert (applied["kind"], applied["outcome"]) == ("apply", outcome)
    assert applied["message"] == f"Error: apply failed with exit {code}"


# ---------- requests from other sites can't change anything ----------

def test_state_changing_requests_need_the_ui_header(settings, catalog):
    from conftest import HeaderIdentity
    from fastapi.testclient import TestClient

    from pagerduty_ops_ui.app import create_app

    app = create_app(settings, identity=HeaderIdentity(), catalog=catalog)
    with TestClient(app) as bare:  # like a form posted from another *.dallas.gov site
        response = bare.post("/api/runs", data={"run": '{"command": "ui-writer"}'},
                             headers=OPERATOR)
        assert response.status_code == 403
        assert bare.post("/api/runs/x/apply", headers=OPERATOR).status_code == 403
        assert bare.get("/api/me", headers=OPERATOR).status_code == 200  # reads are fine


# ---------- a failure while starting the apply doesn't strand the dry run ----------

def test_an_apply_that_cannot_start_leaves_the_dry_run_applicable(client, monkeypatch):
    import pagerduty_ops_ui.executor as executor_module

    dry = reviewed(client)
    real = executor_module.run_command

    def flaky(entry, argv, **kwargs):
        if "-y" in argv:  # the apply itself, after the fresh dry run
            raise OSError("no space left on device")
        return real(entry, argv, **kwargs)

    monkeypatch.setattr(executor_module, "run_command", flaky)
    failed = finished(client, apply(client, dry["id"]))
    assert failed["outcome"] == "failed"
    assert "no space left on device" in failed["message"]
    assert client.get(f"/api/runs/{dry['id']}", headers=OPERATOR).json()["applied_run_id"] is None
    monkeypatch.setattr(executor_module, "run_command", real)
    assert finished(client, apply(client, dry["id"]))["outcome"] == "succeeded"


def test_a_log_file_argument_is_refused_because_the_service_captures_the_log(client):
    # A timestamped log in the Run directory would be an output, so a fresh dry run could
    # never match the reviewed one; the service already stores every Run's log.
    for argv in (["--log-file", "run.log"], ["--log-file=run.log"], ["--log-f", "run.log"]):
        response = client.post("/api/runs", json={"command": "ui-writer", "argv": argv},
                               headers=OPERATOR)
        assert response.status_code == 422
        assert "log" in response.json()["detail"]
