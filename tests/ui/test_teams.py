"""#78 Teams notifications for Approval requests (ADR 0011). Seam B: the HTTP API with real
subprocess Runs of the test-only writer, the controllable clock, and the Teams Workflows
webhook mocked with `responses`, so nothing leaves the machine."""

import dataclasses
import json
import logging

import pytest
import requests
import responses
from conftest import (
    HERE,
    OPERATOR,
    SECRET,
    HeaderIdentity,
    admin_header,
    finished,
    wait_until,
)
from fastapi.testclient import TestClient

from pagerduty_ops_ui import notify
from pagerduty_ops_ui.app import create_app
from pagerduty_ops_ui.settings import Settings, origin, teams_webhook

# The real thing's shape: the signature in the query string is what makes the URL a secret.
WEBHOOK = ("https://prod-00.westus.logic.azure.com/workflows/abc123/triggers/manual/paths/"
           "invoke?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=WEBHOOK-SIG")
BASE = "https://pd-ops.dallas.example"
CAT = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")


@pytest.fixture
def webhook():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def teams(settings, catalog, clock, monkeypatch, webhook):
    """The service with a Teams webhook configured and quick retries."""
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.01, 0.01, 0.01))
    monkeypatch.syspath_prepend(str(HERE))
    configured = dataclasses.replace(settings, teams_webhook_url=WEBHOOK, public_url=BASE)
    app = create_app(configured, identity=HeaderIdentity(), catalog=catalog, clock=clock)
    with TestClient(app, headers={"X-PD-Ops-Request": "1"}) as client:
        client.notifier = app.state.notifier
        yield client


def accept(webhook, *statuses):
    """Answer the webhook with these statuses in turn (202, as Workflows does, after)."""
    for status in (*statuses, 202):
        webhook.post(WEBHOOK, status=status)


def posts(webhook):
    return [json.loads(call.request.body) for call in webhook.calls]


def cards(webhook, count):
    wait_until(lambda: len(posts(webhook)) >= count, timeout=10)
    return [p["attachments"][0]["content"] for p in posts(webhook)]


def facts(card):
    return {f["title"]: f["value"] for f in card["body"][1]["facts"]}


def irreversible(client):
    # --purge is declared irreversible by the test-only writer.
    dry = finished(client, client.post("/api/runs", json={"command": "ui-writer",
                                                          "values": {"purge": True}},
                                       headers=OPERATOR))
    assert client.post(f"/api/runs/{dry['id']}/review", headers=OPERATOR).status_code == 200
    return dry


def ask(client, run_id):
    response = client.post(f"/api/runs/{run_id}/approval", headers=OPERATOR)
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, run_id, verdict):
    response = client.post(f"/api/runs/{run_id}/approval/{verdict}", headers=CAT,
                           json={"comment": "Checked the diff."})
    assert response.status_code == 200, response.text
    return response.json()


def changes(run):
    counts = run["review"]["counts"]
    return (f"{counts['add']} to add, {counts['change']} to change, "
            f"{counts['delete']} to delete")


# ---------- the messages ----------

def test_an_approval_request_posts_what_is_waiting_to_teams(teams, webhook):
    accept(webhook)
    asked = ask(teams, irreversible(teams)["id"])

    [card] = cards(webhook, 1)
    assert posts(webhook)[0]["type"] == "message"
    assert posts(webhook)[0]["attachments"][0]["contentType"] == notify.CARD_CONTENT_TYPE
    assert (card["type"], card["version"]) == ("AdaptiveCard", "1.4")
    assert card["body"][0]["text"] == "Approval requested: ui-writer"
    assert facts(card) == {
        "Requested by": "Ann (ann@dallas.gov)",
        "Changes": changes(asked),
        "Expires": notify._when(asked["approval"]["expires_at"]),
    }
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open the Run",
                                "url": f"{BASE}/#/runs/{asked['id']}"}]


def test_an_approval_posts_a_follow_up(teams, webhook):
    accept(webhook)
    dry = irreversible(teams)
    ask(teams, dry["id"])
    approved = decide(teams, dry["id"], "approve")

    card = cards(webhook, 2)[1]
    assert card["body"][0]["text"] == "Approved: ui-writer"
    assert facts(card) == {
        "Requested by": "Ann (ann@dallas.gov)", "Approved by": "cat@dallas.gov",
        "Changes": changes(approved),
        "Valid until": notify._when(approved["approval"]["expires_at"]),
    }


def test_a_rejection_posts_a_follow_up(teams, webhook):
    accept(webhook)
    dry = irreversible(teams)
    ask(teams, dry["id"])
    decide(teams, dry["id"], "reject")

    card = cards(webhook, 2)[1]
    assert card["body"][0]["text"] == "Rejected: ui-writer"
    assert facts(card) == {"Requested by": "Ann (ann@dallas.gov)",
                           "Rejected by": "cat@dallas.gov"}


def test_an_expired_request_is_announced_once(teams, webhook, clock):
    accept(webhook)
    asked = ask(teams, irreversible(teams)["id"])
    cards(webhook, 1)
    assert teams.notifier.sweep_expiries() == 0  # not yet

    clock.advance(hours=25)
    assert teams.notifier.sweep_expiries() == 1
    assert teams.notifier.sweep_expiries() == 0  # announced once, even if swept again
    card = cards(webhook, 2)[1]
    assert card["body"][0]["text"] == "Approval request expired: ui-writer"
    assert facts(card) == {"Requested by": "Ann (ann@dallas.gov)",
                           "Expired": notify._when(asked["approval"]["expires_at"])}


def test_an_approval_never_used_is_announced_when_it_expires(teams, webhook, clock):
    accept(webhook)
    dry = irreversible(teams)
    ask(teams, dry["id"])
    decide(teams, dry["id"], "approve")
    clock.advance(hours=25)

    assert teams.notifier.sweep_expiries() == 1
    assert cards(webhook, 3)[2]["body"][0]["text"] == "Approval expired unused: ui-writer"


def test_asking_again_after_an_expiry_is_announced_again(teams, webhook, clock):
    accept(webhook)
    dry = irreversible(teams)
    ask(teams, dry["id"])
    clock.advance(hours=25)
    assert teams.notifier.sweep_expiries() == 1

    ask(teams, dry["id"])  # a fresh request, with its own expiry
    clock.advance(hours=25)
    assert teams.notifier.sweep_expiries() == 1
    titles = [c["body"][0]["text"] for c in cards(webhook, 4)]
    assert titles == ["Approval requested: ui-writer", "Approval request expired: ui-writer",
                      "Approval requested: ui-writer", "Approval request expired: ui-writer"]


# ---------- failures never block the Approval ----------

def test_a_failed_post_is_retried(teams, webhook):
    accept(webhook, 500, 429)
    ask(teams, irreversible(teams)["id"])
    wait_until(lambda: len(webhook.calls) == 3, timeout=10)
    assert [c.response.status_code for c in webhook.calls] == [500, 429, 202]


def test_teams_being_down_never_fails_the_approval(teams, webhook, caplog):
    caplog.set_level(logging.WARNING, logger="pagerduty_ops_ui.notify")
    webhook.post(WEBHOOK, body=requests.ConnectionError(f"Max retries exceeded: {WEBHOOK}"))
    dry = irreversible(teams)

    asked = ask(teams, dry["id"])  # answered at once, whatever Teams does
    assert asked["approval"]["status"] == "awaiting-approval"
    wait_until(lambda: "Gave up" in caplog.text, timeout=10)
    assert len(webhook.calls) == len(notify.RETRY_DELAYS) + 1
    assert "ConnectionError" in caplog.text
    assert "WEBHOOK-SIG" not in caplog.text and "logic.azure.com" not in caplog.text
    assert decide(teams, dry["id"], "approve")["approval"]["status"] == "approved"


def test_a_refusal_from_teams_is_not_retried(teams, webhook, caplog):
    caplog.set_level(logging.WARNING, logger="pagerduty_ops_ui.notify")
    webhook.post(WEBHOOK, status=400)
    ask(teams, irreversible(teams)["id"])
    wait_until(lambda: "Not retrying" in caplog.text, timeout=10)
    assert len(webhook.calls) == 1
    assert "HTTP 400" in caplog.text and "WEBHOOK-SIG" not in caplog.text


# ---------- what the messages never carry ----------

def test_the_messages_carry_no_inputs_diffs_or_secrets(teams, webhook, clock):
    accept(webhook)
    dry = irreversible(teams)
    ask(teams, dry["id"])
    decide(teams, dry["id"], "approve")
    clock.advance(hours=25)
    teams.notifier.sweep_expiries()
    sent = json.dumps(posts(webhook) if cards(webhook, 3) else [])

    assert SECRET not in sent and "WEBHOOK-SIG" not in sent
    assert "--purge" not in sent  # no arguments: the command's name only
    assert "Checked the diff." not in sent  # no comments
    for line in dry["review"]["lines"]:
        assert line["text"] not in sent  # no diff lines, only the counts


def test_without_a_webhook_nothing_is_posted(client, webhook):
    # The default service: no PD_UI_TEAMS_WEBHOOK_URL. Any post would hit `responses`
    # unregistered and fail the Approval request.
    dry = irreversible(client)
    assert ask(client, dry["id"])["approval"]["status"] == "awaiting-approval"
    assert len(webhook.calls) == 0


# ---------- configuration ----------

def test_the_webhook_url_never_appears_in_the_settings_repr(settings):
    configured = dataclasses.replace(settings, teams_webhook_url=WEBHOOK)
    assert "WEBHOOK-SIG" not in repr(configured)
    assert "WEBHOOK-SIG" not in repr(notify.TeamsNotifier(WEBHOOK, None, base_url=BASE))


def test_the_webhook_must_be_https_and_isnt_echoed(monkeypatch):
    monkeypatch.setenv(notify.ENV_NAME, WEBHOOK.replace("https://", "http://"))
    with pytest.raises(SystemExit) as stopped:
        teams_webhook()
    assert "https" in str(stopped.value) and "WEBHOOK-SIG" not in str(stopped.value)
    monkeypatch.setenv(notify.ENV_NAME, f"  {WEBHOOK}  ")
    assert teams_webhook() == WEBHOOK
    monkeypatch.delenv(notify.ENV_NAME)
    assert teams_webhook() is None


def test_times_are_shown_in_utc_on_every_supported_python():
    # The store writes "...Z"; Python 3.10's fromisoformat() rejects that spelling.
    assert notify._when("2026-11-26T04:35:33.849250Z") == "2026-11-26 04:35 UTC"
    assert notify._when("2026-11-26T04:35:33+00:00") == "2026-11-26 04:35 UTC"
    assert notify._when(None) == "unknown"


def test_links_use_the_services_own_origin():
    assert origin("https://pd-ops.dallas.gov/auth/callback") == "https://pd-ops.dallas.gov"


def test_the_webhook_url_is_never_passed_to_a_run():
    from pagerduty_ops_ui.settings import ENGINE_ENV_NAMES, SECRET_ENV_NAMES
    assert notify.ENV_NAME not in (*ENGINE_ENV_NAMES, *SECRET_ENV_NAMES)
    assert "teams_webhook_url" in {f.name for f in dataclasses.fields(Settings)}


def test_a_request_renewed_during_the_sweep_is_still_announced_as_the_one_that_expired(
        teams, webhook, clock, monkeypatch):
    # The sweep must describe the request it claimed, not whatever the Run holds a moment
    # later: here the requester asks again right after the claim.
    from pagerduty_ops_ui.store import RunStore

    accept(webhook)
    dry = irreversible(teams)
    expired = ask(teams, dry["id"])["approval"]["expires_at"]
    clock.advance(hours=25)

    claim = RunStore.claim_expiry_announcement

    def claim_then_renew(store, run_id):
        claimed = claim(store, run_id)
        ask(teams, run_id)  # the requester asks again at that very moment
        return claimed

    monkeypatch.setattr(RunStore, "claim_expiry_announcement", claim_then_renew)
    assert teams.notifier.sweep_expiries() == 1
    by_title = {c["body"][0]["text"]: c for c in cards(webhook, 3)}
    expiry = by_title["Approval request expired: ui-writer"]
    assert facts(expiry)["Expired"] == notify._when(expired)
