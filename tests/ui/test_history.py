"""#69 the searchable Run history (Seam B: the HTTP API over the real Run store). Runs are
seeded straight into the store, so each filter can be checked against known rows; one test
also finds a Run started through the API."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import APPROVER, NO_ROLE, OPERATOR, VIEWER, finished, wait

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def seed(settings, rows):
    from pagerduty_ops_ui.store import RunRecord, RunStore

    store = RunStore(settings.database_url)
    for n, row in enumerate(rows):
        when = T0 + timedelta(days=row.get("day", n))
        store.save(RunRecord(
            id=row["id"], command=row.get("command", "pd-list-users"), argv=[],
            requested_by=row.get("email", "ann@dallas.gov"),
            requester_name=row.get("name", "Ann Lee"), requester_role="operator",
            kind=row.get("kind", "report"), status=row.get("status", "finished"),
            outcome=row.get("outcome", "succeeded"), queued_at=when, started_at=when,
            finished_at=when, timeout_seconds=60, stdout="data", stderr="log"))


ROWS = [
    {"id": "r1", "day": 0, "command": "pd-list-users"},
    {"id": "r2", "day": 1, "command": "pd-apply-tags", "kind": "dry-run"},
    {"id": "r3", "day": 2, "command": "pd-apply-tags", "kind": "apply", "outcome": "failed"},
    {"id": "r4", "day": 3, "command": "pd-list-users", "email": "bob@dallas.gov",
     "name": "Bob Ruiz", "outcome": "stopped"},
    {"id": "r5", "day": 4, "command": "pd-apply-tags", "kind": "apply",
     "email": "bob@dallas.gov", "name": "Bob Ruiz"},
]


@pytest.fixture
def history(client, settings):
    seed(settings, ROWS)

    def search(headers=VIEWER, **params):
        response = client.get("/api/runs", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    return search


def ids(page):
    return [run["id"] for run in page["runs"]]


def test_the_history_lists_runs_newest_first_without_their_output(history):
    page = history()
    assert ids(page) == ["r5", "r4", "r3", "r2", "r1"]
    assert page["total"] == 5
    first = page["runs"][0]
    assert {"id", "command", "kind", "outcome", "requested_by", "requester_name",
            "queued_at", "finished_at", "status"} <= set(first)
    assert "stdout" not in first and "stderr" not in first  # a list, not five Run pages


@pytest.mark.parametrize("headers", [VIEWER, OPERATOR, APPROVER])
def test_every_role_can_see_the_history(history, headers):
    assert history(headers=headers)["total"] == 5


def test_the_history_needs_a_role(client):
    assert client.get("/api/runs", headers=NO_ROLE).status_code == 403
    assert client.get("/api/runs").status_code == 401


def test_filter_by_command(history):
    assert ids(history(command="pd-apply-tags")) == ["r5", "r3", "r2"]


@pytest.mark.parametrize("requester", ["bob@dallas.gov", "BOB", "ruiz"])
def test_filter_by_admin_matches_part_of_the_email_or_name(history, requester):
    assert ids(history(requester=requester)) == ["r5", "r4"]


def test_the_admin_filter_takes_wildcard_characters_literally(history):
    assert history(requester="%")["total"] == 0
    assert history(requester="_")["total"] == 0


def test_filter_by_date_range_includes_the_start_and_excludes_the_end(history):
    since = (T0 + timedelta(days=1)).isoformat()
    before = (T0 + timedelta(days=3)).isoformat()
    assert ids(history(since=since, before=before)) == ["r3", "r2"]
    assert ids(history(since=since)) == ["r5", "r4", "r3", "r2"]
    assert ids(history(before=before)) == ["r3", "r2", "r1"]


def test_dates_in_utc_with_a_z_work_as_the_page_sends_them(history):
    since = (T0 + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    before = (T0 + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert ids(history(since=since, before=before)) == ["r3", "r2"]


def test_dates_need_a_timezone(client, history):
    response = client.get("/api/runs", params={"since": "2026-11-02T00:00:00"},
                          headers=VIEWER)
    assert response.status_code == 422
    assert "timezone" in response.json()["detail"]


def test_filter_by_outcome(history):
    assert ids(history(outcome="failed")) == ["r3"]
    assert ids(history(outcome="stopped")) == ["r4"]


@pytest.mark.parametrize("outcome", ["usage-error", "auth-error", "changed"])
def test_every_outcome_can_be_found(client, settings, outcome):
    seed(settings, [{"id": "x1", "outcome": outcome}, {"id": "x2"}])
    page = client.get("/api/runs", params={"outcome": outcome}, headers=VIEWER).json()
    assert ids(page) == ["x1"]


def test_runs_still_in_flight_can_be_found(client, settings):
    seed(settings, [{"id": "q1", "status": "queued", "outcome": None, "kind": "apply"},
                    {"id": "q2", "status": "running", "outcome": None},
                    {"id": "q3"}])
    page = client.get("/api/runs", params={"outcome": "unfinished"}, headers=VIEWER).json()
    assert ids(page) == ["q2", "q1"]


def test_filter_by_dry_run_or_apply(history):
    assert ids(history(kind="apply")) == ["r5", "r3"]
    assert ids(history(kind="dry-run")) == ["r2"]
    assert ids(history(kind="report")) == ["r4", "r1"]


def test_filters_combine(history):
    assert ids(history(command="pd-apply-tags", kind="apply")) == ["r5", "r3"]
    assert ids(history(command="pd-apply-tags", kind="apply", requester="bob")) == ["r5"]
    assert ids(history(command="pd-apply-tags", kind="apply", outcome="failed",
                       since=(T0 + timedelta(days=2)).isoformat())) == ["r3"]
    assert history(command="pd-list-users", kind="apply")["total"] == 0


def test_unknown_filter_values_are_refused(client):
    for params in ({"outcome": "exploded"}, {"kind": "nightly"}, {"page": "0"},
                   {"per_page": "1000"}):
        assert client.get("/api/runs", params=params, headers=VIEWER).status_code == 422


def test_paging(history):
    first = history(per_page=2)
    assert (ids(first), first["total"], first["page"], first["per_page"]) == (
        ["r5", "r4"], 5, 1, 2)
    assert ids(history(per_page=2, page=2)) == ["r3", "r2"]
    assert ids(history(per_page=2, page=3)) == ["r1"]
    assert ids(history(per_page=2, page=4)) == []
    assert history(per_page=2, page=2, kind="apply")["total"] == 2  # total counts filters


def test_a_run_started_through_the_api_appears_in_the_history(client, history):
    run = finished(client, client.post("/api/runs", json={"command": "ui-fixture",
                                                          "argv": ["--print", "x"]},
                                       headers=VIEWER))
    page = history(command="ui-fixture")
    assert ids(page) == [run["id"]]
    assert page["runs"][0]["outcome"] == "succeeded"


def test_a_real_dry_run_and_its_apply_are_found_by_kind(client):
    response = client.post("/api/runs", json={"command": "ui-writer", "values": {}},
                           headers=OPERATOR)
    dry = finished(client, response)
    assert client.post(f"/api/runs/{dry['id']}/review", headers=OPERATOR).status_code == 200
    applied = client.post(f"/api/runs/{dry['id']}/apply", headers=OPERATOR).json()
    applied = wait(client, applied["id"])

    def search(**params):
        page = client.get("/api/runs", params={"command": "ui-writer", **params},
                          headers=VIEWER).json()
        return ids(page)

    fresh = applied["fresh_dry_run_id"]  # the check inside the writing slot is a dry run too
    assert search(kind="apply") == [applied["id"]]
    assert search(kind="dry-run") == [fresh, dry["id"]]
    assert search(kind="apply", outcome="succeeded", requester="ann") == [applied["id"]]


def test_the_pages_outcome_list_matches_the_services():
    # The History page offers one choice per outcome; a new outcome must reach both.
    import re
    from pathlib import Path

    from pagerduty_ops_ui.executor import FINAL_OUTCOMES

    api = (Path(__file__).parents[2] / "ui" / "src" / "api.ts").read_text(encoding="utf-8")
    listed = re.search(r"export const OUTCOMES = \[(.*?)\] as const", api, re.S).group(1)
    assert set(re.findall(r'"([^"]+)"', listed)) == set(FINAL_OUTCOMES)


def test_history_and_queue_reads_dont_fetch_output_or_logs(settings):
    # A finished Run's output and log are stored whole; a list must not drag them along.
    from sqlalchemy import event

    from pagerduty_ops_ui.store import RunStore

    seed(settings, [{"id": "big"}, {"id": "q", "status": "queued", "outcome": None,
                                    "kind": "apply"}])
    store = RunStore(settings.database_url)
    statements = []
    event.listen(store.engine, "before_cursor_execute",
                 lambda conn, cursor, sql, *rest: statements.append(sql.lower()))
    assert [r["id"] for r in store.search()["runs"]] == ["q", "big"]
    assert [r["id"] for r in store.queue()["pending"]] == ["q"]
    selects = [s for s in statements if s.lstrip().startswith("select")]
    assert selects
    for column in ("stdout", "stderr", "note"):
        assert not any(f"runs.{column}" in s for s in selects), column


def test_the_queue_lists_summaries(client, settings):
    seed(settings, [{"id": "q", "status": "queued", "outcome": None, "kind": "apply"}])
    pending = client.get("/api/queue", headers=VIEWER).json()["pending"]
    assert [r["id"] for r in pending] == ["q"]
    assert "stdout" not in pending[0] and "stderr" not in pending[0]
