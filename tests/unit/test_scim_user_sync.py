"""pd-scim-user-sync: strict CSV, guarded deletions, live re-checks, plan age and caps."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import scim_user_audit, scim_user_sync
from pagerduty_ops.pdstate import PDState
from pagerduty_ops.plan import new_plan, write_plan

SCIM = f"{PD_API_BASE}/scim/v2/Users"


def scim_user(sid, email, name, active=True):
    return {"id": sid, "displayName": name, "active": active,
            "emails": [{"value": email, "primary": True}]}


SCIM_USERS = [scim_user("S" * 26, "ann@dallas.gov", "Ann"),           # stays, rename
              scim_user("T" * 26, "bo@dallas.gov", "Bo"),             # IdP inactive -> delete
              scim_user("U" * 26, "cy@dallas.gov", "Cy"),             # inactive but on-call
              scim_user("V" * 26, "orphan@dallas.gov", "Orphan"),     # not in CSV
              scim_user("W" * 26, "boss@dallas.gov", "Boss")]         # inactive owner


def pd_state():
    pd = PDState("t")
    pd.users = {"PA": {"id": "PA", "email": "ann@dallas.gov", "role": "user"},
                "PB": {"id": "PB", "email": "bo@dallas.gov", "role": "user"},
                "PC": {"id": "PC", "email": "cy@dallas.gov", "role": "user"},
                "PO": {"id": "PO", "email": "orphan@dallas.gov", "role": "user"},
                "PW": {"id": "PW", "email": "boss@dallas.gov", "role": "owner"}}
    pd.schedules = {"PS1": {"id": "PS1", "name": "Primary", "users": [{"id": "PC"}]}}
    pd.escalation_policies = {}
    return pd


CSV = ("email,displayName,active\nann@dallas.gov,Ann Lee,true\nbo@dallas.gov,Bo,false\n"
       "cy@dallas.gov,Cy,false\nboss@dallas.gov,Boss,no\n")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    monkeypatch.setattr(scim_user_sync, "PDState", lambda token: pd_state())


def mock_scim(users=None):
    responses.get(SCIM, json={"Resources": users or SCIM_USERS,
                              "totalResults": len(users or SCIM_USERS)})


def test_load_expected_is_strict(tmp_path):
    p = tmp_path / "e.csv"
    for bad in ("email,displayName,active\nann@x,Ann,ture\n",        # typo must not = inactive
                "email,displayName,active\n,Ann,true\n",              # missing email
                "email,displayName,active\nann@x,Ann,true\nANN@x,A,true\n"):  # duplicate
        p.write_text(bad)
        with pytest.raises(SystemExit) as exc:
            scim_user_audit.load_expected(str(p))
        assert exc.value.code == 2


@responses.activate
def test_plan_guards_deletions_and_reports_orphans(tmp_path):
    mock_scim()
    (tmp_path / "e.csv").write_text(CSV)
    out = tmp_path / "plan.json"
    assert scim_user_sync.main(["plan", str(tmp_path / "e.csv"), "--plan-out", str(out)]) == 0
    items = {i["email"]: i for i in json.loads(out.read_text())["items"]}
    assert items["ann@dallas.gov"]["op"] == "set_display_name"
    assert items["bo@dallas.gov"]["op"] == "delete_user"
    assert items["cy@dallas.gov"]["op"] == "blocked_delete"
    assert "schedule Primary" in items["cy@dallas.gov"]["detail"]
    assert items["boss@dallas.gov"]["op"] == "blocked_delete"
    assert "orphan@dallas.gov" not in items                      # report only by default
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_plan_delete_orphans_is_opt_in(tmp_path):
    mock_scim()
    (tmp_path / "e.csv").write_text(CSV)
    out = tmp_path / "plan.json"
    scim_user_sync.main(["plan", str(tmp_path / "e.csv"), "--plan-out", str(out),
                         "--delete-orphans"])
    items = {i["email"]: i for i in json.loads(out.read_text())["items"]}
    assert items["orphan@dallas.gov"]["op"] == "delete_user"


def _plan(tmp_path, items, generated_at=None):
    p = new_plan("scim-sync", items, generated_by="test")
    if generated_at:
        p["generated_at"] = generated_at
    path = tmp_path / "plan.json"
    write_plan(p, str(path))
    return str(path)


DELETE_BO = {"op": "delete_user", "scim_id": "T" * 26, "email": "bo@dallas.gov",
             "rest_user_id": "PB", "detail": "IdP marks inactive"}
RENAME_ANN = {"op": "set_display_name", "scim_id": "S" * 26, "email": "ann@dallas.gov",
              "value": "Ann Lee", "expected": "Ann", "detail": "rename"}
BLOCKED_CY = {"op": "blocked_delete", "scim_id": "U" * 26, "email": "cy@dallas.gov",
              "rest_user_id": "PC", "detail": "on call"}


@responses.activate
def test_apply_deletes_and_renames_but_never_blocked(tmp_path):
    mock_scim()
    responses.patch(f"{SCIM}/{'T' * 26}", status=204)
    responses.patch(f"{SCIM}/{'S' * 26}", json={})
    path = _plan(tmp_path, [DELETE_BO, RENAME_ANN, BLOCKED_CY])
    assert scim_user_sync.main(["apply", path, "--dry-run"]) == 0
    assert not [c for c in responses.calls if c.request.method == "PATCH"]
    assert scim_user_sync.main(["apply", path, "-y"]) == 0
    patches = {c.request.url.rsplit("/", 1)[1]: json.loads(c.request.body)
               for c in responses.calls if c.request.method == "PATCH"}
    assert set(patches) == {"T" * 26, "S" * 26}
    assert patches["T" * 26]["Operations"] == [{"op": "replace", "value": {"active": False}}]
    assert patches["S" * 26]["Operations"] == [
        {"op": "replace", "path": "displayName", "value": "Ann Lee"}]
    assert patches["T" * 26]["schemas"] == [scim_user_sync.PATCH_SCHEMA]


@responses.activate
def test_apply_rechecks_live_and_skips_newly_blocked(tmp_path, monkeypatch):
    mock_scim()
    def blocked_state(token):
        pd = pd_state()
        pd.schedules["PS2"] = {"id": "PS2", "name": "New", "users": [{"id": "PB"}]}
        return pd
    monkeypatch.setattr(scim_user_sync, "PDState", blocked_state)
    path = _plan(tmp_path, [DELETE_BO])
    assert scim_user_sync.main(["apply", path, "-y"]) == 1
    assert not [c for c in responses.calls if c.request.method == "PATCH"]


@responses.activate
def test_apply_refuses_stale_plans_and_enforces_delete_cap(tmp_path):
    path = _plan(tmp_path, [DELETE_BO], generated_at="2026-01-01T00:00:00Z")
    assert scim_user_sync.main(["apply", path, "-y"]) == 2
    path = _plan(tmp_path, [DELETE_BO])
    assert scim_user_sync.main(["apply", path, "-y", "--max-deletes", "0"]) == 2
    assert not responses.calls


@responses.activate
def test_apply_conflict_when_display_name_changed(tmp_path):
    mock_scim([scim_user("S" * 26, "ann@dallas.gov", "Someone Else")])
    path = _plan(tmp_path, [RENAME_ANN])
    assert scim_user_sync.main(["apply", path, "-y"]) == 1
    assert not [c for c in responses.calls if c.request.method == "PATCH"]
