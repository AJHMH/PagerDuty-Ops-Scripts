"""#79 ServiceNow production applies with shared Rehearsal receipts (ADR 0007). Seam B: the HTTP
API with real subprocess Runs of the pd-sn-sync fixture, which has the real parser, risk
declaration and the engine's own receipt steps. The service only points every Run at one
shared receipts store; the engine still does the check, and nothing in the UI skips it."""

import json

from conftest import OPERATOR, admin_header, finished, wait

ANN = OPERATOR
BOB = admin_header("PD-Ops-Operator", email="bob@dallas.gov", name="Bob")
CAT = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")
SETTING = [{"op": "sn_set_field", "table": "sys_user_group", "key": "Network",
            "field": "x_pd_integration_pagerduty_team", "value": "PABC123"}]


def plan(items=SETTING):
    body = json.dumps({"kind": "sn-sync", "items": items}).encode()
    return [("file.plan_file", ("sn_plan.json", body, "application/json"))]


def dry_run(client, instance, headers=ANN, items=SETTING):
    body = {"command": "ui-sn-sync", "subcommand": "apply", "values": {"instance": instance}}
    return finished(client, client.post("/api/runs", data={"run": json.dumps(body)},
                                        files=plan(items), headers=headers))


def reviewed(client, instance, headers=ANN, items=SETTING):
    dry = dry_run(client, instance, headers, items)
    assert dry["outcome"] == "succeeded", dry
    assert client.post(f"/api/runs/{dry['id']}/review", headers=headers).status_code == 200
    return dry


def apply(client, run_id, headers=ANN):
    return client.post(f"/api/runs/{run_id}/apply", headers=headers)


def rehearse(client, headers=ANN, items=SETTING):
    applied = wait(client, apply(client, reviewed(client, "dallastx-test", headers, items)["id"],
                                 headers).json()["id"])
    assert (applied["outcome"], applied["stdout"]) == ("succeeded", "APPLIED\n"), applied
    return applied


def receipts(settings):
    return sorted(p.name for p in settings.receipts_dir.glob("*.json"))


def test_a_receipt_written_by_one_admin_unlocks_production_for_another(client, settings,
                                                                       sn_instances):
    rehearsal = rehearse(client, headers=ANN)
    assert len(receipts(settings)) == 1  # in the shared store...
    assert not (settings.run_root / rehearsal["id"] / ".pd_ops_receipts").exists()  # not the Run's

    production = reviewed(client, "dallastx", headers=BOB)
    # A production apply is an Irreversible operation: it needs an Approval first.
    assert apply(client, production["id"], headers=BOB).status_code == 403
    asked = client.post(f"/api/runs/{production['id']}/approval", headers=BOB).json()
    assert asked["approval"]["reasons"] == ["ServiceNow production instance dallastx"]
    assert client.post(f"/api/runs/{production['id']}/approval/approve", headers=CAT,
                       json={"comment": "Rehearsed on dallastx-test."}).status_code == 200

    applied = wait(client, apply(client, production["id"], headers=BOB).json()["id"])
    assert (applied["outcome"], applied["stdout"]) == ("succeeded", "APPLIED\n"), applied
    assert applied["requested_by"] == "bob@dallas.gov"


def test_production_without_a_receipt_is_refused_with_the_engines_reason(client, settings,
                                                                         sn_instances):
    refused = dry_run(client, "dallastx")
    assert (refused["outcome"], refused["exit_code"]) == ("usage-error", 2)
    assert "refusing to write to production: no rehearsal receipt" in refused["stderr"]
    assert refused["stdout"] == ""
    # A failed dry run can't be reviewed, so it can never be applied.
    assert client.post(f"/api/runs/{refused['id']}/review", headers=ANN).status_code == 409


def test_a_rehearsal_dry_run_writes_no_receipt(client, settings, sn_instances):
    reviewed(client, "dallastx-test")
    assert receipts(settings) == []
    assert dry_run(client, "dallastx")["outcome"] == "usage-error"


def test_a_receipt_unlocks_only_the_plan_that_was_rehearsed(client, settings, sn_instances):
    rehearse(client)
    edited = [{**SETTING[0], "value": "PXYZ999"}]
    refused = dry_run(client, "dallastx", items=edited)
    assert refused["outcome"] == "usage-error"
    assert "no rehearsal receipt" in refused["stderr"]

