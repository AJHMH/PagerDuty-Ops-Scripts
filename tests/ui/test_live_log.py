"""#68 the live Run log: while a Run executes, its log (the command's stderr) reaches the Run
page, which re-reads the Run until it finishes. Seam B: the HTTP API with a fixture command
that writes log lines slowly."""

import os
import signal

import pytest
from conftest import SECRET, VIEWER, wait, wait_until


def start(client, argv):
    response = client.post("/api/runs", json={"command": "ui-fixture", "argv": argv},
                           headers=VIEWER)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def read(client, run_id):
    return client.get(f"/api/runs/{run_id}", headers=VIEWER).json()


def test_log_lines_arrive_while_the_run_is_still_executing(client):
    run_id = start(client, ["--slow-log", "15"])  # 6 seconds of log lines
    snapshots = []

    def some_lines_while_running():
        run = read(client, run_id)
        snapshots.append(run)
        return run["status"] == "running" and "line 2 of 15" in run["stderr"]

    wait_until(some_lines_while_running, timeout=10)
    running = snapshots[-1]
    assert "line 15 of 15" not in running["stderr"]  # it really is part-way through
    assert running["outcome"] is None
    finished = wait(client, run_id)
    assert finished["stderr"] == "".join(f"line {n} of 15\n" for n in range(1, 16))


def test_a_finished_run_shows_its_stored_log(client):
    run_id = start(client, ["--slow-log", "2"])
    finished = wait(client, run_id)
    assert read(client, run_id)["stderr"] == finished["stderr"] == "line 1 of 2\nline 2 of 2\n"


def test_secrets_never_appear_in_the_streamed_or_stored_log(client):
    run_id = start(client, ["--slow-log", "6", "--slow-log-env", "PD_API_TOKEN"])
    seen = []

    def watch():
        response = client.get(f"/api/runs/{run_id}", headers=VIEWER)
        seen.append(response.text)
        return response.json()["status"] == "finished"

    wait_until(watch, timeout=15)
    assert any("line 1 of 6 token=[redacted]" in text for text in seen[:-1])  # streamed
    assert all(SECRET not in text for text in seen)
    assert "line 6 of 6 token=[redacted]" in read(client, run_id)["stderr"]


def test_a_cancelled_run_keeps_the_log_it_had_written(client):
    run_id = start(client, ["--slow-log", "30"])
    wait_until(lambda: "line 2 of 30" in read(client, run_id)["stderr"], timeout=10)
    assert client.post(f"/api/runs/{run_id}/cancel", headers=VIEWER).status_code == 200
    stopped = wait(client, run_id)
    assert stopped["outcome"] == "stopped"
    assert stopped["stderr"].startswith("line 1 of 30\nline 2 of 30\n")
    assert stopped["stderr"].endswith("[stopped: cancelled]\n")


def test_while_running_only_the_end_of_a_long_log_is_shown_but_all_of_it_is_kept(
        client, monkeypatch):
    import pagerduty_ops_ui.runner as runner

    monkeypatch.setattr(runner, "LIVE_LOG_LIMIT", 40)  # about two lines
    run_id = start(client, ["--slow-log", "12"])
    snapshots = []

    def trimmed_while_running():
        run = read(client, run_id)
        snapshots.append(run)
        return run["status"] == "running" and "line 6 of 12" in run["stderr"]

    wait_until(trimmed_while_running, timeout=15)
    live = snapshots[-1]["stderr"]
    assert live.startswith("[")  # says that earlier lines are hidden for now
    assert "hidden" in live and "line 1 of 12" not in live
    finished = wait(client, run_id)
    assert finished["stderr"] == "".join(f"line {n} of 12\n" for n in range(1, 13))


def test_a_failed_log_write_doesnt_stop_the_run(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from pagerduty_ops_ui.store import RunStore

    def unavailable(self, run_id, text):
        raise OperationalError("UPDATE", {}, Exception("database unavailable"))

    monkeypatch.setattr(RunStore, "update_log", unavailable)
    finished = wait(client, start(client, ["--slow-log", "4"]))
    assert finished["outcome"] == "succeeded"  # the Run went on; only the live view missed
    assert finished["stderr"].endswith("line 4 of 4\n")


@pytest.mark.skipif(os.name == "nt", reason="On Windows every process a Run starts stays in "
                                             "its Job Object and is killed with it, so none "
                                             "can hold the Run's output open.")
def test_a_run_whose_output_is_held_open_finishes_instead_of_hanging(client, settings,
                                                                     monkeypatch):
    import pagerduty_ops_ui.runner as runner

    monkeypatch.setattr(runner, "DRAIN_SECONDS", 1)
    run_id = start(client, ["--escape-child", "escaped.pid", "--print", "partial"])
    try:
        finished = wait(client, run_id, timeout=20)
        assert finished["outcome"] == "failed"  # never a silent success without its output
        assert "output" in finished["message"]
    finally:
        pid_file = settings.run_root / run_id / "escaped.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
