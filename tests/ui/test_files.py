"""#67 Runs from form values, uploaded inputs, stored outputs with SHA-256 hashes, downloads
(ADR 0007). Seam B: the HTTP API, with real subprocess Runs of the fixture command."""

import hashlib
import json

from conftest import OPERATOR, VIEWER, finished


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_values(client, values, files=None, command="ui-fixture", subcommand=None,
               headers=VIEWER):
    body = {"command": command, "values": values}
    if subcommand:
        body["subcommand"] = subcommand
    if files is None:
        return client.post("/api/runs", json=body, headers=headers)
    return client.post("/api/runs", data={"run": json.dumps(body)}, files=files,
                       headers=headers)


# ---------- argv is built on the server from the form's values ----------

def test_values_become_the_commands_arguments(client):
    created = finished(client, run_values(client, {"text": "hi", "exit_code": 1,
                                                   "verbose": True,
                                                   "status": ["open", "closed"]}))
    # Emitted in the parser's own order, with the long flag names.
    assert created["argv"] == ["--verbose", "--print", "hi", "--exit-code", "1",
                               "--status", "open", "--status", "closed"]
    assert created["stdout"] == "hi\nstatus=open|closed\n"
    assert created["outcome"] == "failed"


def test_unknown_or_hidden_fields_are_refused(client):
    assert run_values(client, {"no_such_field": "x"}).status_code == 422
    assert run_values(client, {"token": "abc"}).status_code == 422


def test_values_go_through_the_same_gate_as_arguments(client):
    assert run_values(client, {"output": "../escape.txt"}).status_code == 422


def test_subcommand_values(client):
    created = finished(client, run_values(client, {}, command="ui-subcommands",
                                          subcommand="report"))
    assert (created["argv"], created["stdout"]) == (["report"], "report\n")
    refused = run_values(client, {}, command="ui-subcommands", subcommand="apply")
    assert refused.status_code == 403


# ---------- uploads, outputs, hashes and downloads ----------

def test_uploaded_input_and_written_output_are_stored_with_hashes(client):
    data = b"service_id\nP1\n"
    created = finished(client, run_values(
        client, {"output": "out.txt"},
        files=[("file.input", ("services.csv", data, "text/csv"))]))
    assert created["outcome"] == "succeeded"
    assert created["argv"] == ["--output", "out.txt", "--input", "inputs/input/services.csv"]
    assert created["stdout"] == data.decode()
    files = {(f["role"], f["path"]): f for f in created["files"]}
    uploaded = files[("input", "inputs/input/services.csv")]
    written = files[("output", "out.txt")]
    assert (uploaded["sha256"], uploaded["size"]) == (sha(data), len(data))
    assert written["sha256"] == sha(b"report\n")

    for f, expected in ((uploaded, data), (written, b"report\n")):
        response = client.get(f"/api/runs/{created['id']}/files/{f['id']}", headers=OPERATOR)
        assert response.status_code == 200
        assert response.content == expected
        assert "attachment" in response.headers["content-disposition"]


def test_an_input_directory_takes_several_files(client):
    created = finished(client, run_values(client, {}, files=[
        ("file.input_dir", ("a.json", b"{}", "application/json")),
        ("file.input_dir", ("b.json", b"[]", "application/json")),
    ]))
    assert created["stdout"] == "a.json,b.json\n"
    assert created["argv"] == ["--input-dir", "inputs/input_dir"]


def test_upload_names_are_reduced_to_a_safe_file_name(client):
    created = run_values(client, {}, files=[
        ("file.input", ("..\\..\\evil/services.csv", b"x\n", "text/csv"))]).json()
    assert created["argv"] == ["--input", "inputs/input/services.csv"]


def test_bad_uploads_are_refused(client, settings):
    hidden = [("file.input", (".env", b"x", "text/plain"))]
    assert run_values(client, {}, files=hidden).status_code == 422
    not_a_file_field = [("file.text", ("a.csv", b"x", "text/csv"))]
    assert run_values(client, {}, files=not_a_file_field).status_code == 422
    two_for_one = [("file.input", ("a.csv", b"x", "text/csv")),
                   ("file.input", ("b.csv", b"y", "text/csv"))]
    assert run_values(client, {}, files=two_for_one).status_code == 422
    settings.max_upload_bytes = 4
    too_big = [("file.input", ("a.csv", b"12345", "text/csv"))]
    assert run_values(client, {}, files=too_big).status_code == 413


def test_a_refused_run_leaves_nothing_behind(client, settings):
    refused = run_values(client, {}, command="ui-subcommands", subcommand="apply")
    assert refused.status_code == 403
    assert not settings.run_root.exists() or not any(settings.run_root.iterdir())


def test_a_file_changed_after_the_run_is_not_served(client, settings):
    created = finished(client, run_values(client, {"output": "out.txt"}))
    written = next(f for f in created["files"] if f["path"] == "out.txt")
    (settings.run_root / created["id"] / "out.txt").write_text("tampered\n", "utf-8")
    response = client.get(f"/api/runs/{created['id']}/files/{written['id']}", headers=VIEWER)
    assert response.status_code == 409


def test_files_are_only_served_from_their_own_run(client):
    first = finished(client, run_values(client, {"output": "out.txt"}))
    second = finished(client, run_values(client, {"text": "x"}))
    file_id = first["files"][0]["id"]
    assert client.get(f"/api/runs/{second['id']}/files/{file_id}",
                      headers=VIEWER).status_code == 404
    assert client.get(f"/api/runs/{first['id']}/files/99999", headers=VIEWER).status_code == 404


# ---------- a usage error is shown with the command's own message ----------

def test_a_usage_error_carries_the_commands_message(client):
    created = finished(client, run_values(client, {"exit_code": 2,
                                                   "log": "Error: CSV missing columns"}))
    assert created["outcome"] == "usage-error"
    assert created["message"] == "Error: CSV missing columns"
    assert finished(client, run_values(client, {"text": "ok"}))["message"] is None


def test_outputs_may_not_be_written_over_the_uploaded_inputs(client, settings):
    upload = [("file.input", ("services.csv", b"x\n", "text/csv"))]
    for target in ("inputs/input/services.csv", "inputs", "./inputs/x.txt",
                   ["inputs/input/services.csv"]):  # a one-item list is the same value
        assert run_values(client, {"output": target}, files=upload).status_code == 422
    assert not settings.run_root.exists() or not any(settings.run_root.iterdir())


def test_a_request_larger_than_the_cap_is_refused_before_parsing(client, settings):
    settings.max_request_bytes = 200
    big = [("file.input", ("a.csv", b"x" * 500, "text/csv"))]
    assert run_values(client, {}, files=big).status_code == 413


def test_the_run_part_must_be_text(client):
    response = client.post("/api/runs", files=[("run", ("run.json", b"{}", "application/json"))],
                           headers=VIEWER)
    assert response.status_code == 422
