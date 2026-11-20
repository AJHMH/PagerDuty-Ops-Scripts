"""pd-security-config: export/diff stability, strict validation, session-only writes."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import security_config as sec

IP = f"{PD_API_BASE}/ip_allow_lists"
SESS = f"{PD_API_BASE}/session_configurations"
ALLOW = {"ip_allow_lists": [{"id": "AGIS", "state": "enabled",
                             "cidr_entries": [{"cidr": "10.2.0.0/16"}, {"cidr": "10.1.0.0/16"}],
                             "targets": [{"type": "surface", "value": "web"}]}]}
SESSIONS = {"session_configurations": [
    {"type": "web", "absolute_session_ttl": 43200, "idle_session_ttl": 3600},
    {"type": "mobile", "absolute_session_ttl": 2592000, "idle_session_ttl": 86400}]}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock(sessions=SESSIONS, allow=ALLOW):
    responses.get(IP, json=allow)
    if sessions is None:
        responses.get(SESS, status=404, json={"error": {"message": "none"}})
    else:
        responses.get(SESS, json=sessions)


@responses.activate
def test_export_then_diff_is_clean_and_ids_stripped(tmp_path):
    mock()
    out = tmp_path / "s.json"
    assert sec.main(["export", "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["ip_allow_lists"][0]["cidr_entries"] == ["10.1.0.0/16", "10.2.0.0/16"]
    assert "id" not in doc["ip_allow_lists"][0]
    assert [c["type"] for c in doc["session_configurations"]] == ["mobile", "web"]
    assert sec.main(["diff", "-i", str(out), "--fail-on-drift"]) == 0


@responses.activate
def test_diff_reports_ip_allow_list_drift_but_never_writes(tmp_path, capsys):
    mock()
    out = tmp_path / "s.json"
    sec.main(["export", "-o", str(out)])
    responses.reset()
    mock(allow={"ip_allow_lists": [{**ALLOW["ip_allow_lists"][0],
                                    "cidr_entries": [{"cidr": "0.0.0.0/0"}]}]})
    assert sec.main(["diff", "-i", str(out), "--fail-on-drift"]) == 1
    assert "0.0.0.0/0" in capsys.readouterr().out
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_missing_session_configurations_are_treated_as_empty(tmp_path):
    mock(sessions=None)
    out = tmp_path / "s.json"
    assert sec.main(["export", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["session_configurations"] == []


@pytest.mark.parametrize("bad", [
    {"type": "desktop", "absolute_session_ttl": 43200, "idle_session_ttl": 3600},
    {"type": "web", "absolute_session_ttl": 10, "idle_session_ttl": 60},
    {"type": "web", "absolute_session_ttl": 3600, "idle_session_ttl": 7200},   # idle > absolute
    {"type": "web", "absolute_session_ttl": "43200", "idle_session_ttl": 3600},
])
def test_session_validation(tmp_path, bad):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"session_configurations": [bad]}))
    with pytest.raises(SystemExit) as exc:
        sec.load_file(str(p))
    assert exc.value.code == 2


@responses.activate
def test_apply_sessions_writes_only_changed_types(tmp_path):
    mock()
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"ip_allow_lists": [], "session_configurations": [
        {"type": "web", "absolute_session_ttl": 28800, "idle_session_ttl": 1800},   # changed
        SESSIONS["session_configurations"][1]]}))                                   # same
    assert sec.main(["apply-sessions", "-i", str(p), "--dry-run"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
    responses.put(SESS, json={})
    assert sec.main(["apply-sessions", "-i", str(p), "-y"]) == 0
    puts = [c for c in responses.calls if c.request.method == "PUT"]
    assert len(puts) == 1 and "type=web" in puts[0].request.url
    assert json.loads(puts[0].request.body) == {"session_configuration": {
        "absolute_session_ttl": 28800, "idle_session_ttl": 1800}}
    assert not [c for c in responses.calls if "ip_allow_lists" in c.request.url
                and c.request.method != "GET"]
