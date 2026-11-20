"""Unit tests for config (token/team resolution) and output rendering."""

import json

import pytest

from pagerduty_ops.config import get_api_token, get_from_email, get_team_id
from pagerduty_ops.output import render_rows

# ---------- config ----------

def test_token_cli_arg_wins(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "env-token")
    assert get_api_token("cli-token") == "cli-token"


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "env-token")
    assert get_api_token(None) == "env-token"


def test_missing_token_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("PD_API_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        get_api_token(None, allow_prompt=False)
    assert exc.value.code == 2
    assert "No API token" in capsys.readouterr().err


def test_team_id_from_env(monkeypatch):
    monkeypatch.setenv("PD_TEAM_ID", "  PTEAM1 ")
    assert get_team_id(None, allow_prompt=False) == "PTEAM1"


def test_missing_team_id_exits_2(monkeypatch):
    monkeypatch.delenv("PD_TEAM_ID", raising=False)
    with pytest.raises(SystemExit):
        get_team_id(None, allow_prompt=False)


def test_from_email_validation(monkeypatch):
    monkeypatch.delenv("PD_FROM_EMAIL", raising=False)
    assert get_from_email("ops@example.com") == "ops@example.com"
    with pytest.raises(SystemExit):
        get_from_email("not-an-email")
    with pytest.raises(SystemExit):
        get_from_email(None, required=True)
    assert get_from_email(None, required=False) is None


# ---------- output ----------

ROWS = [
    {"id": "P1", "name": "Alpha"},
    {"id": "P2", "name": "Beta, with comma"},
]
FIELDS = ["id", "name"]


def test_render_csv_quotes_correctly():
    out = render_rows(ROWS, FIELDS, "csv")
    lines = out.strip().splitlines()
    assert lines[0] == "id,name"
    assert lines[2] == 'P2,"Beta, with comma"'


def test_render_json_rows():
    assert json.loads(render_rows(ROWS, FIELDS, "json")) == ROWS


def test_render_json_raw_fidelity():
    raw = [{"id": "P1", "name": "Alpha", "extra": {"nested": True}}]
    assert json.loads(render_rows(ROWS, FIELDS, "json", raw=raw)) == raw


def test_render_table_contains_all_values():
    out = render_rows(ROWS, FIELDS, "table")
    for value in ("P1", "Alpha", "P2"):
        assert value in out
    assert "Total: 2" in out


def test_render_csv_ignores_extra_keys():
    rows = [{"id": "P1", "name": "A", "unexpected": "x"}]
    out = render_rows(rows, FIELDS, "csv")
    assert "unexpected" not in out


# ---------- stdout bytes are the same on every platform (ADR 0005) ----------

def _stdout_bytes(fmt, rows, env_extra=None):
    """Run a command entry point (cli.run) in a real subprocess, so the real stdout is used."""
    import os
    import subprocess
    import sys

    code = ("import json, sys\n"
            "from pagerduty_ops.cli import run\n"
            "from pagerduty_ops.output import render_rows, write_payload\n"
            "rows = json.loads(sys.argv[1])\n"
            "def main(argv):\n"
            f"    write_payload(render_rows(rows, list(rows[0]), {fmt!r}))\n"
            "    print('print:', rows[0][list(rows[0])[0]])\n"
            "    return 0\n"
            "run(main)\n")
    env = {**os.environ, **(env_extra or {})}
    done = subprocess.run([sys.executable, "-c", code, json.dumps(rows)],
                          capture_output=True, env=env, check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout


def test_csv_on_stdout_has_one_crlf_per_row():
    # Windows text stdout turns "\n" into "\r\n", so csv's "\r\n" became "\r\r\n".
    out = _stdout_bytes("csv", [{"name": "Ada"}, {"name": "Grace"}])
    assert out == b"name\r\nAda\r\nGrace\r\nprint: Ada\n"


def test_stdout_is_utf8_even_when_the_console_code_page_is_not():
    # Windows pipes default to a legacy code page (e.g. cp1252) that cannot encode many
    # names; data on stdout is always UTF-8.
    out = _stdout_bytes("json", [{"name": "Łukasz Żółć"}], {"PYTHONIOENCODING": "cp1252"})
    payload = out.decode("utf-8").rsplit("print:", 1)[0]
    assert json.loads(payload) == [{"name": "Łukasz Żółć"}]
    out = _stdout_bytes("table", [{"name": "Łukasz"}], {"PYTHONIOENCODING": "cp1252"})
    assert "Łukasz" in out.decode("utf-8")
    assert out.decode("utf-8").endswith("print: Łukasz\n")
