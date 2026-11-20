"""pd-templates: name-keyed export, validation, diff-first apply, capped prune."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import templates as tpl

T = f"{PD_API_BASE}/templates"
LIVE = [{"id": "T1", "name": "Major outage ", "description": "P1", "template_type": "status_update",
         "templated_fields": {"email_subject": "Outage: {{incident.title}}",
                              "email_body": "<p>We are investigating.</p>", "message": "Outage"}},
        {"id": "T2", "name": "Resolved", "description": None, "template_type": "status_update",
         "templated_fields": {"message": "Resolved"}}]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock(live=None):
    responses.get(T, json={"templates": live if live is not None else LIVE, "more": False})


def write(tmp_path, entries):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"templates": entries}))
    return str(p)


def writes():
    return [c.request.method for c in responses.calls if c.request.method != "GET"]


@responses.activate
def test_export_then_apply_with_prune_is_a_noop(tmp_path):
    mock()
    out = tmp_path / "t.json"
    assert tpl.main(["export", "-o", str(out)]) == 0
    doc = json.loads(out.read_text())["templates"]
    assert [t["name"] for t in doc] == ["Major outage ", "Resolved"]     # verbatim, sorted
    assert doc[1]["templated_fields"] == {"email_body": "", "email_subject": "",
                                          "message": "Resolved"}
    assert "T1" not in out.read_text()
    assert tpl.main(["apply", "-i", str(out), "--apply", "-y", "--prune"]) == 0
    assert writes() == []


@responses.activate
def test_validation_errors_exit_2_before_writes(tmp_path):
    mock()
    ok_fields = {"message": "x"}
    for entries in ([{"name": "A", "templated_fields": ok_fields},
                     {"name": "a", "templated_fields": ok_fields}],          # duplicate
                    [{"name": "A", "template_type": "incident",
                      "templated_fields": ok_fields}],                       # bad type
                    [{"name": "A", "templated_fields": {"sms": "x"}}],      # unknown field
                    [{"name": "A", "templated_fields": {}}],                # all empty
                    [{"name": "A", "templated_fields": ok_fields, "owner": "x"}]):
        with pytest.raises(SystemExit) as exc:
            tpl.main(["apply", "-i", write(tmp_path, entries), "--apply", "-y"])
        assert exc.value.code == 2
    assert writes() == []


@responses.activate
def test_diff_then_apply_create_update_prune(tmp_path, capsys):
    mock()
    path = write(tmp_path, [
        {"name": "Major outage ", "description": "P1", "templated_fields": {
            "email_subject": "Outage: {{incident.title}}",
            "email_body": "<p>We are investigating. Next update in 30 minutes.</p>",
            "message": "Outage"}},
        {"name": "Maintenance", "templated_fields": {"message": "Planned maintenance"}}])
    assert tpl.main(["apply", "-i", path, "--prune"]) == 0
    out = capsys.readouterr().out
    assert "~ Major outage : templated_fields.email_body" in out
    assert "+ Maintenance" in out and "- Resolved (T2)" in out
    assert writes() == []
    responses.put(f"{T}/T1", json={})
    responses.post(T, json={})
    responses.delete(f"{T}/T2", status=204)
    assert tpl.main(["apply", "-i", path, "--prune", "--apply", "-y"]) == 0
    assert writes() == ["PUT", "POST", "DELETE"]
    post = next(c for c in responses.calls if c.request.method == "POST")
    assert json.loads(post.request.body) == {"template": {
        "name": "Maintenance", "description": "", "template_type": "status_update",
        "templated_fields": {"email_subject": None, "email_body": None,
                             "message": "Planned maintenance"}}}


@responses.activate
def test_prune_cap_and_apply_requires_yes(tmp_path):
    mock()
    path = write(tmp_path, [])
    assert tpl.main(["apply", "-i", path, "--prune", "--apply", "-y",
                     "--max-deletes", "1"]) == 2
    assert tpl.main(["apply", "-i", path, "--apply"]) == 2
    assert writes() == []
