"""#75 raising a write cap needs a recorded reason (ADR 0003). Which flags are caps comes from
the engine's risk declaration; the test-only writer declares --max-deletes (default 5).
Seam B: the HTTP API with real subprocess Runs of the fixture commands."""

import json

import pytest
from conftest import OPERATOR, VIEWER, finished, wait


def dry_run(client, values, reason=None, headers=OPERATOR):
    body = {"command": "ui-writer", "values": values}
    if reason is not None:
        body["cap_override_reason"] = reason
    return client.post("/api/runs", json=body, headers=headers)


def test_raising_a_cap_without_a_reason_is_refused(client):
    response = dry_run(client, {"max_deletes": 10})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "--max-deletes" in detail and "reason" in detail


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_a_blank_reason_doesnt_count(client, reason):
    assert dry_run(client, {"max_deletes": 10}, reason).status_code == 422


def test_an_overlong_reason_is_refused(client):
    assert dry_run(client, {"max_deletes": 10}, "x" * 501).status_code == 422


def test_raising_a_cap_with_a_reason_runs_and_records_the_override(client):
    run = finished(client, dry_run(client, {"max_deletes": 10},
                                   "  Decommissioning 8 legacy services (CHG0042)  "))
    assert run["outcome"] == "succeeded"
    assert run["argv"] == ["--max-deletes", "10", "--dry-run"]
    assert run["cap_override"] == {
        "reason": "Decommissioning 8 legacy services (CHG0042)",
        "by": "ann@dallas.gov",
        "caps": [{"flag": "--max-deletes", "default": 5, "value": 10}],
    }


@pytest.mark.parametrize("values", [{}, {"max_deletes": 5}, {"max_deletes": 2}])
def test_a_cap_at_or_below_its_default_needs_no_reason(client, values):
    run = finished(client, dry_run(client, values))
    assert (run["outcome"], run["cap_override"]) == ("succeeded", None)


def test_a_reason_without_a_raised_cap_is_not_recorded(client):
    run = finished(client, dry_run(client, {}, "just in case"))
    assert run["cap_override"] is None


def test_raw_argv_is_held_to_the_same_rule(client):
    body = {"command": "ui-writer", "argv": ["--max-del", "9"]}  # an argparse prefix
    assert client.post("/api/runs", json=body, headers=OPERATOR).status_code == 422
    body["cap_override_reason"] = "bulk cleanup"
    run = finished(client, client.post("/api/runs", json=body, headers=OPERATOR))
    assert run["cap_override"]["caps"] == [{"flag": "--max-deletes", "default": 5, "value": 9}]


def test_the_reason_can_come_with_uploaded_files(client):
    body = {"command": "ui-writer", "values": {"max_deletes": 7},
            "cap_override_reason": "one-off import"}
    files = [("file.input", ("services.csv", b"service_id\n", "text/csv"))]
    run = finished(client, client.post("/api/runs", data={"run": json.dumps(body)},
                                       files=files, headers=OPERATOR))
    assert run["cap_override"]["reason"] == "one-off import"


def test_the_override_is_shown_in_the_run_history(client):
    run = finished(client, dry_run(client, {"max_deletes": 10}, "CHG0042"))
    rows = client.get("/api/runs", params={"command": "ui-writer"}, headers=VIEWER).json()
    assert rows["runs"][0]["id"] == run["id"]
    assert rows["runs"][0]["cap_override"]["reason"] == "CHG0042"
    assert rows["runs"][0]["cap_override"]["by"] == "ann@dallas.gov"


def test_the_apply_uses_the_reviewed_caps_and_carries_the_override(client):
    dry = finished(client, dry_run(client, {"max_deletes": 10}, "CHG0042"))
    assert client.post(f"/api/runs/{dry['id']}/review", headers=OPERATOR).status_code == 200
    applied = wait(client, client.post(f"/api/runs/{dry['id']}/apply",
                                       headers=OPERATOR).json()["id"])
    assert applied["outcome"] == "succeeded"
    assert applied["argv"] == ["--max-deletes", "10", "-y"]  # the reviewed cap, unchanged
    assert applied["cap_override"] == dry["cap_override"]
    fresh = client.get(f"/api/runs/{applied['fresh_dry_run_id']}", headers=OPERATOR).json()
    assert fresh["cap_override"] == dry["cap_override"]


def test_the_form_marks_which_fields_are_caps(client):
    form = client.get("/api/commands/ui-writer/form", headers=OPERATOR).json()
    caps = {f["dest"]: f["cap"] for f in form["fields"]}
    assert caps["max_deletes"] is True
    assert caps["note"] is False
    report = client.get("/api/commands/ui-fixture/form", headers=VIEWER).json()
    assert not any(f["cap"] for f in report["fields"])
