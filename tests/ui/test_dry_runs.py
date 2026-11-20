"""#72 dry runs of writing commands, with a review of the diff (ADRs 0003, 0010, 0011).
Seam B: the HTTP API with real subprocess Runs of test-only fixture commands."""

from conftest import APPROVER, OPERATOR, VIEWER


def run(client, command, argv=None, values=None, headers=OPERATOR, subcommand=None):
    body = {"command": command}
    if values is not None:
        body["values"] = values
        if subcommand:
            body["subcommand"] = subcommand
    else:
        body["argv"] = argv or []
    return client.post("/api/runs", json=body, headers=headers)


# ---------- who may dry-run, and the service always forces the dry run ----------

def test_operator_and_approver_dry_run_a_writing_command(client):
    for headers in (OPERATOR, APPROVER):
        created = run(client, "ui-writer", values={}, headers=headers).json()
        assert created["kind"] == "dry-run"
        assert created["argv"] == ["--dry-run"]
        assert created["outcome"] == "succeeded"
        assert "APPLIED" not in created["stdout"]


def test_a_viewer_cannot_dry_run_a_writing_command(client):
    response = run(client, "ui-writer", values={}, headers=VIEWER)
    assert response.status_code == 403
    assert "Operator" in response.json()["detail"]


def test_dry_run_flag_already_present_is_not_doubled(client):
    created = run(client, "ui-writer", argv=["--dry-run"]).json()
    assert created["argv"] == ["--dry-run"]


def test_a_diff_first_command_runs_without_apply(client):
    created = run(client, "ui-differ", values={}).json()
    assert (created["kind"], created["argv"], created["stdout"]) == (
        "dry-run", [], "+ new-template\n")


def test_an_apply_is_refused_for_everyone(client):
    for headers in (OPERATOR, APPROVER):
        response = run(client, "ui-differ", argv=["--apply", "-y"], headers=headers)
        assert response.status_code == 403
        assert "Apply" in response.json()["detail"]


def test_a_writing_subcommand_is_dry_run_and_a_read_only_one_is_a_report(client):
    apply = run(client, "ui-subcommands", values={}, subcommand="apply").json()
    assert (apply["kind"], apply["argv"]) == ("dry-run", ["apply", "--dry-run"])
    report = run(client, "ui-subcommands", values={}, subcommand="report", headers=VIEWER)
    assert report.json()["kind"] == "report"
    assert report.json()["review"] is None


# ---------- the review: additions, changes and deletions ----------

def test_the_review_classifies_the_diff_from_both_streams(client):
    created = run(client, "ui-writer", values={"note": "starting"}).json()
    review = created["review"]
    assert review["counts"] == {"add": 1, "change": 1, "delete": 2, "other": 0}
    kinds = [(line["kind"], line["text"]) for line in review["lines"]]
    assert ("add", "+ new-service") in kinds
    assert ("change", "~ api-service: name 'a'->'b'") in kinds
    assert ("delete", "- old-service (P1)") in kinds
    assert ("delete", "[dry-run] would DELETE /teams/PT1/members/PU1 for Ann") in kinds
    assert ("info", "[dry-run] previewing removal of Ann") in kinds
    assert ("context", "2026-11-20 21:00:00 INFO starting") in kinds


# ---------- hashes for matching a later apply to this dry run (#73) ----------

def test_identical_inputs_have_the_same_input_hash(client):
    first = run(client, "ui-writer", values={"note": "a"}).json()
    second = run(client, "ui-writer", values={"note": "a"}).json()
    other = run(client, "ui-writer", values={"note": "b"}).json()
    assert len(first["input_hash"]) == 64
    assert first["input_hash"] == second["input_hash"] != other["input_hash"]


def test_the_output_hash_covers_the_diff_not_the_log_context(client):
    first = run(client, "ui-writer", values={"note": "a"}).json()
    other_context = run(client, "ui-writer", values={"note": "b"}).json()
    other_diff = run(client, "ui-writer", values={"note": "a", "extra_delete": True}).json()
    assert first["output_hash"] == other_context["output_hash"]
    assert first["output_hash"] != other_diff["output_hash"]


def test_reports_get_hashes_too(client):
    created = run(client, "ui-fixture", values={"text": "hi"}, headers=VIEWER).json()
    assert created["kind"] == "report"
    assert len(created["input_hash"]) == len(created["output_hash"]) == 64


def test_preview_lines_the_review_cant_classify_still_count_as_changes(client):
    review = run(client, "ui-writer", values={"only_unrecognised": True}).json()["review"]
    assert review["counts"] == {"add": 0, "change": 0, "delete": 0, "other": 1}
    assert review["lines"][0]["kind"] == "other"


def test_the_input_hash_is_the_same_whatever_the_mode_flags(client):
    # #73 matches an apply (no --dry-run, with -y) to its reviewed dry run by input hash.
    plain = run(client, "ui-writer", argv=["--note", "a"]).json()
    flagged = run(client, "ui-writer", argv=["--note", "a", "--dry-run", "-y"]).json()
    assert plain["argv"] == ["--note", "a", "--dry-run"]
    assert plain["input_hash"] == flagged["input_hash"]
