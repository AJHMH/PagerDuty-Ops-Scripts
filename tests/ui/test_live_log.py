"""#68 the live Run log: while a Run executes, its log (the command's stderr) reaches the Run
page, which re-reads the Run until it finishes. Seam B: the HTTP API with a fixture command
that writes log lines slowly."""

from conftest import SECRET, VIEWER, wait, wait_until


def start(client, argv):
    response = client.post("/api/runs", json={"command": "ui-fixture", "argv": argv},
                           headers=VIEWER)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def read(client, run_id):
    return client.get(f"/api/runs/{run_id}", headers=VIEWER).json()


def test_log_lines_arrive_while_the_run_is_still_executing(client):
    run_id = start(client, ["--slow-log", "8"])
    snapshots = []

    def some_lines_while_running():
        run = read(client, run_id)
        snapshots.append(run)
        return run["status"] == "running" and "line 2 of 8" in run["stderr"]

    wait_until(some_lines_while_running, timeout=10)
    running = snapshots[-1]
    assert "line 8 of 8" not in running["stderr"]  # it really is part-way through
    assert running["outcome"] is None
    finished = wait(client, run_id)
    assert finished["stderr"] == "".join(f"line {n} of 8\n" for n in range(1, 9))


def test_a_finished_run_shows_its_stored_log(client):
    run_id = start(client, ["--slow-log", "2"])
    finished = wait(client, run_id)
    assert read(client, run_id)["stderr"] == finished["stderr"] == "line 1 of 2\nline 2 of 2\n"


def test_secrets_never_appear_in_the_streamed_or_stored_log(client):
    run_id = start(client, ["--slow-log", "6", "--slow-log-env", "PD_API_TOKEN"])
    seen = []

    def watch():
        run = read(client, run_id)
        seen.append(client.get(f"/api/runs/{run_id}", headers=VIEWER).text)
        return run["status"] == "finished"

    wait_until(watch, timeout=15)
    assert any("line 1 of 6 token=[redacted]" in text for text in seen[:-1])  # streamed
    assert all(SECRET not in text for text in seen)
    assert "line 6 of 6 token=[redacted]" in read(client, run_id)["stderr"]
