"""#77 Approvals for Irreversible operations (ADRs 0003, 0010, 0011). Seam B: the HTTP API with
real subprocess Runs of test-only fixtures; three fixtures carry real engine parsers and risk
declarations (pd-reorg, pd-business-services, pd-scim-user-sync) with harmless mains, so
irreversibility is judged by the engine's own assess(). The clock is controllable."""

import json

import pytest
from conftest import OPERATOR, VIEWER, admin_header, finished, wait

OTHER_APPROVER = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")
APPROVING_REQUESTER = admin_header("PD-Ops-Approver", email="ann@dallas.gov", name="Ann")
OTHER_OPERATOR = admin_header("PD-Ops-Operator", email="bob@dallas.gov", name="Bob")


def reviewed(client, command="ui-writer", values=None, files=None, headers=OPERATOR,
             reason=None, subcommand=None):
    body = {"command": command, "values": values or {}, "subcommand": subcommand}
    if reason:
        body["cap_override_reason"] = reason
    if files:
        response = client.post("/api/runs", data={"run": json.dumps(body)}, files=files,
                               headers=headers)
    else:
        response = client.post("/api/runs", json=body, headers=headers)
    dry = finished(client, response)
    assert dry["outcome"] == "succeeded", dry
    assert client.post(f"/api/runs/{dry['id']}/review", headers=headers).status_code == 200
    return dry


def irreversible(client, values=None, **kwargs):
    # --purge is declared irreversible by the test-only writer.
    return reviewed(client, values={"purge": True, **(values or {})}, **kwargs)


def ask(client, run_id, headers=OPERATOR):
    return client.post(f"/api/runs/{run_id}/approval", headers=headers)


def decide(client, run_id, verdict, headers=OTHER_APPROVER, comment="Checked the diff."):
    return client.post(f"/api/runs/{run_id}/approval/{verdict}", json={"comment": comment},
                       headers=headers)


def apply(client, run_id, headers=OPERATOR):
    return client.post(f"/api/runs/{run_id}/apply", headers=headers)


def run(client, run_id):
    return client.get(f"/api/runs/{run_id}", headers=VIEWER).json()


# ---------- what needs an Approval comes from the engine's assess() ----------

def test_an_irreversible_apply_needs_an_approval(client):
    dry = irreversible(client)
    response = apply(client, dry["id"])
    assert response.status_code == 403
    assert "Approval" in response.json()["detail"]
    asked = ask(client, dry["id"])
    assert asked.status_code == 200, asked.text
    approval = asked.json()["approval"]
    assert approval["status"] == "awaiting-approval"
    assert approval["reasons"] == ["--purge given"]


def test_a_reversible_apply_needs_no_approval(client):
    dry = reviewed(client)
    response = ask(client, dry["id"])
    assert response.status_code == 409
    assert "doesn't need" in response.json()["detail"]
    assert wait(client, apply(client, dry["id"]).json()["id"])["outcome"] == "succeeded"


def test_a_pruning_business_services_apply_needs_an_approval(client):
    upload = [("file.input", ("bs.json", b"[]", "application/json"))]
    dry = reviewed(client, "ui-business-services", values={"prune": True}, files=upload,
                   subcommand="apply")
    assert apply(client, dry["id"]).status_code == 403
    assert ask(client, dry["id"]).json()["approval"]["reasons"] == ["--prune given"]


def test_a_pruning_reorg_apply_needs_no_approval(client):
    # ADR 0010: what a reorg removes can be re-added, even when its plan prunes.
    plan = {"kind": "reorg", "options": {"prune": True},
            "items": [{"op": "remove_member", "team_id": "T1", "user_id": "U1"}]}
    upload = [("file.plan_file", ("plan.json", json.dumps(plan).encode(), "application/json"))]
    dry = reviewed(client, "ui-reorg", files=upload, subcommand="apply")
    assert ask(client, dry["id"]).status_code == 409
    assert wait(client, apply(client, dry["id"]).json()["id"])["outcome"] == "succeeded"


def test_a_plan_is_judged_from_the_runs_own_directory(client):
    # The uploaded plan's path is relative to the Run's directory; assess() must find it
    # there, and judges it by its items (a SCIM user deletion is irreversible).
    plan = {"kind": "scim", "items": [{"op": "delete_user", "scim_id": "s1"}]}
    upload = [("file.plan_file", ("plan.json", json.dumps(plan).encode(), "application/json"))]
    dry = reviewed(client, "ui-scim", files=upload, subcommand="apply")
    assert ask(client, dry["id"]).json()["approval"]["reasons"] == [
        "plan has 1 delete_user item(s)"]


def test_an_unreadable_plan_fails_closed_and_needs_an_approval(client):
    # The engine can't tell what an unreadable Plan would do, so it counts it irreversible.
    upload = [("file.plan_file", ("plan.json", b"not json", "application/json"))]
    dry = reviewed(client, "ui-scim", files=upload, subcommand="apply")
    assert apply(client, dry["id"]).status_code == 403
    assert ask(client, dry["id"]).json()["approval"]["reasons"] == [
        "plan file unreadable (fail closed)"]


def test_an_input_changed_since_the_review_blocks_the_request(client, settings):
    plan = {"kind": "scim", "items": [{"op": "delete_user", "scim_id": "s1"}]}
    upload = [("file.plan_file", ("plan.json", json.dumps(plan).encode(), "application/json"))]
    dry = reviewed(client, "ui-scim", files=upload, subcommand="apply")
    (settings.run_root / dry["id"] / "inputs/plan_file/plan.json").write_text("{}")
    response = ask(client, dry["id"])
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


# ---------- who may approve ----------

def test_the_requester_cannot_approve_their_own_run(client):
    dry = irreversible(client, headers=APPROVING_REQUESTER)
    ask(client, dry["id"], headers=APPROVING_REQUESTER)
    response = decide(client, dry["id"], "approve", headers=APPROVING_REQUESTER)
    assert response.status_code == 403
    assert "own" in response.json()["detail"]


@pytest.mark.parametrize("who", [OTHER_OPERATOR, VIEWER])
def test_only_approvers_can_approve_or_reject(client, who):
    dry = irreversible(client)
    ask(client, dry["id"])
    assert decide(client, dry["id"], "approve", headers=who).status_code == 403
    assert decide(client, dry["id"], "reject", headers=who).status_code == 403


def test_only_the_requester_can_ask_for_an_approval(client):
    dry = irreversible(client)
    assert ask(client, dry["id"], headers=OTHER_OPERATOR).status_code == 403


def test_a_decision_needs_a_comment(client):
    dry = irreversible(client)
    ask(client, dry["id"])
    for comment in ("", "   "):
        assert decide(client, dry["id"], "approve", comment=comment).status_code == 422
        assert decide(client, dry["id"], "reject", comment=comment).status_code == 422


# ---------- approve, reject, expire ----------

def test_an_approved_run_applies_and_records_its_approver(client):
    dry = irreversible(client)
    ask(client, dry["id"])
    response = decide(client, dry["id"], "approve", comment="Diff matches CHG0042.")
    assert response.status_code == 200
    approval = response.json()["approval"]
    assert (approval["status"], approval["decided_by"], approval["comment"]) == (
        "approved", "cat@dallas.gov", "Diff matches CHG0042.")
    applied = wait(client, apply(client, dry["id"]).json()["id"])
    assert applied["outcome"] == "succeeded"
    assert applied["approval"]["decided_by"] == "cat@dallas.gov"  # recorded on the apply


def test_a_rejection_is_final_for_that_dry_run(client):
    dry = irreversible(client)
    ask(client, dry["id"])
    rejected = decide(client, dry["id"], "reject", comment="Too broad; split it up.")
    assert rejected.json()["approval"]["status"] == "rejected"
    assert rejected.json()["approval"]["comment"] == "Too broad; split it up."
    assert apply(client, dry["id"]).status_code == 403
    assert ask(client, dry["id"]).status_code == 409


def test_an_approval_expires_24_hours_after_it_was_given(client, clock):
    dry = irreversible(client)
    ask(client, dry["id"])
    decide(client, dry["id"], "approve")
    clock.advance(hours=23, minutes=59)
    assert run(client, dry["id"])["approval"]["status"] == "approved"
    clock.advance(minutes=2)
    assert run(client, dry["id"])["approval"]["status"] == "expired"
    response = apply(client, dry["id"])
    assert response.status_code == 403
    assert "expired" in response.json()["detail"]
    assert ask(client, dry["id"]).status_code == 200  # the requester can ask again


def test_an_unanswered_request_expires_after_24_hours(client, clock):
    dry = irreversible(client)
    ask(client, dry["id"])
    clock.advance(hours=24, seconds=1)
    assert run(client, dry["id"])["approval"]["status"] == "expired"
    assert decide(client, dry["id"], "approve").status_code == 409
    assert ask(client, dry["id"]).status_code == 200


def test_an_approval_that_expires_while_queued_is_not_used(client, settings, clock):
    first = reviewed(client)                      # reversible, and slow to apply
    second = irreversible(client)
    ask(client, second["id"])
    decide(client, second["id"], "approve")
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "3"
    a = apply(client, first["id"]).json()
    b = apply(client, second["id"]).json()
    assert b["status"] == "queued"
    clock.advance(hours=25)                       # expires while it waits its turn
    blocked = wait(client, b["id"])
    assert blocked["outcome"] == "failed" and blocked["stdout"] == ""
    assert "expired" in blocked["message"]
    assert wait(client, a["id"])["outcome"] == "succeeded"


def test_a_mismatch_needs_a_new_review_and_a_new_approval(client, settings):
    dry = irreversible(client)
    ask(client, dry["id"])
    decide(client, dry["id"], "approve")
    settings.extra_env["UI_FIXTURE_DRIFT"] = "1"  # live state moves after the Approval
    blocked = wait(client, apply(client, dry["id"]).json()["id"])
    assert blocked["outcome"] == "changed"
    fresh_id = blocked["fresh_dry_run_id"]
    assert run(client, fresh_id)["approval"] is None  # the fresh dry run starts from scratch
    assert apply(client, fresh_id).status_code == 409  # not reviewed
    assert client.post(f"/api/runs/{fresh_id}/review", headers=OPERATOR).status_code == 200
    assert apply(client, fresh_id).status_code == 403  # reviewed, not approved
    ask(client, fresh_id)
    decide(client, fresh_id, "approve", comment="Re-checked with the drifted service.")
    assert wait(client, apply(client, fresh_id).json()["id"])["outcome"] == "succeeded"


# ---------- what the Approver sees, and the queue ----------

def test_the_approval_shows_the_reasons_requester_and_cap_override(client):
    dry = irreversible(client, values={"max_deletes": 9}, reason="CHG0042")
    ask(client, dry["id"])
    shown = run(client, dry["id"])
    assert shown["approval"]["reasons"] == ["--purge given"]
    assert shown["requested_by"] == "ann@dallas.gov"
    assert shown["cap_override"]["reason"] == "CHG0042"
    assert shown["review"]["counts"]["delete"] >= 1  # the exact diff being approved


def test_the_waiting_for_approval_queue(client, clock):
    first, second = irreversible(client), irreversible(client)
    ask(client, first["id"])
    ask(client, second["id"])
    pending = client.get("/api/approvals", headers=VIEWER).json()["pending"]
    assert [r["id"] for r in pending] == [first["id"], second["id"]]  # oldest first
    assert pending[0]["approval"]["reasons"] == ["--purge given"]
    decide(client, first["id"], "approve")
    pending = client.get("/api/approvals", headers=VIEWER).json()["pending"]
    assert [r["id"] for r in pending] == [second["id"]]
    clock.advance(hours=25)
    assert client.get("/api/approvals", headers=VIEWER).json()["pending"] == []
