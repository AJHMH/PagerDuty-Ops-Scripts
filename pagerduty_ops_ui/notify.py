"""Teams notifications for Approvals (#78, ADR 0011), and for Drift that a Scheduled run
finds (#80, ADR 0014).

When an Approval is asked for, approved or rejected, and when a request or an unused Approval
expires, the service posts a message to the admin team's Teams channel through a Workflows
webhook ("Post to a channel when a webhook request is received"), as an Adaptive Card.

- The message says what is waiting and links to its Run page. It carries the command, the
  requester, the counts from the dry run's review and the expiry time: never its arguments,
  inputs, diff lines or comments.
- The webhook URL is a secret (its query string signs the request). It comes from the vault
  through PD_UI_TEAMS_WEBHOOK_URL, is never passed to a Run, and is never logged or stored:
  a failure logs only the HTTP status or the error's type.
- Posts happen on a background thread. A failed post is retried a few times, a little later
  each time, and then logged; it never blocks or fails the Approval itself.
- Expiries have no request to hang on, so a sweep looks for them once a minute. The store
  marks each one announced before it is posted, so it is announced once, even across
  restarts.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from urllib.parse import quote

import requests

from .approvals import APPROVED, AWAITING, REJECTED

ENV_NAME = "PD_UI_TEAMS_WEBHOOK_URL"
RETRY_DELAYS = (5, 30, 120)  # seconds before the 2nd, 3rd and 4th attempts
SWEEP_SECONDS = 60
TIMEOUT_SECONDS = 10
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

log = logging.getLogger(__name__)


class TeamsRefused(Exception):
    """Teams answered with a status that trying again won't change (a 4xx other than 429)."""


def message(title: str, facts: list[tuple[str, str]], link: str | None) -> dict:
    """A Workflows webhook body: one Adaptive Card with a title, facts and a link."""
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium",
             "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]},
        ],
        "actions": ([{"type": "Action.OpenUrl", "title": "Open the Run", "url": link}]
                    if link else []),
    }
    return {"type": "message",
            "attachments": [{"contentType": CARD_CONTENT_TYPE, "contentUrl": None,
                             "content": card}]}


def _when(value: str | None) -> str:
    """The store's UTC timestamp ("...Z") as "YYYY-MM-DD HH:MM UTC". Python 3.10's
    fromisoformat() doesn't accept "Z", so it is spelled as "+00:00" first."""
    if not value:
        return "unknown"
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")


def _who(run: dict) -> str:
    name, email = run.get("requester_name"), run.get("requested_by")
    return f"{name} ({email})" if name and name != email else str(email)


def _changes(run: dict) -> str:
    counts = (run.get("review") or {}).get("counts") or {}
    return (f"{counts.get('add', 0)} to add, {counts.get('change', 0)} to change, "
            f"{counts.get('delete', 0)} to delete")


def describe(event: str, run: dict, base_url: str | None) -> dict:
    """The message for one event on a dry run's Approval: asked for, approved, rejected or
    expired. `run` is the store's view of the dry run."""
    approval = run.get("approval") or {}
    command = run["command"]
    link = f"{base_url.rstrip('/')}/#/runs/{quote(run['id'])}" if base_url else None
    if event == AWAITING:
        return message(f"Approval requested: {command}", [
            ("Requested by", _who(run)), ("Changes", _changes(run)),
            ("Expires", _when(approval.get("expires_at")))], link)
    if event == APPROVED:
        return message(f"Approved: {command}", [
            ("Requested by", _who(run)), ("Approved by", str(approval.get("decided_by"))),
            ("Changes", _changes(run)), ("Valid until", _when(approval.get("expires_at")))],
            link)
    if event == REJECTED:
        return message(f"Rejected: {command}", [
            ("Requested by", _who(run)), ("Rejected by", str(approval.get("decided_by")))],
            link)
    if approval.get("decided_by"):  # approved, then never applied
        title = f"Approval expired unused: {command}"
    else:
        title = f"Approval request expired: {command}"
    return message(title, [("Requested by", _who(run)),
                           ("Expired", _when(approval.get("expires_at")))], link)


def describe_drift(run: dict, schedule_name: str | None, base_url: str | None) -> dict:
    """The message for Drift found by a Scheduled run (#80): which schedule, the command's
    own summary line (e.g. "Findings: info=0, warning=3, error=1") and when it ran. Never
    the findings themselves: those are on the Run page."""
    link = f"{base_url.rstrip('/')}/#/runs/{quote(run['id'])}" if base_url else None
    return message(f"Drift found: {run['command']}", [
        ("Schedule", schedule_name or "(deleted)"),
        ("Summary", run.get("drift_summary") or "See the Run."),
        ("Ran", _when(run.get("started_at") or run.get("queued_at")))], link)


class TeamsNotifier:
    """Posts Approval events to Teams in the background. Without a webhook URL it does
    nothing, and the service runs as before."""

    def __init__(self, webhook_url: str | None, store, *, base_url: str | None,
                 delays=None, sweep_seconds: float | None = None):
        self._url = webhook_url
        self.store = store
        self.base_url = base_url
        self.delays = tuple(RETRY_DELAYS if delays is None else delays)
        self.sweep_seconds = SWEEP_SECONDS if sweep_seconds is None else sweep_seconds
        self._queue: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._http = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def __repr__(self) -> str:  # never show the URL
        return f"TeamsNotifier(enabled={self.enabled})"

    # ---------- events ----------

    def approval_event(self, event: str, run: dict) -> None:
        """Queue the message for `event` on this dry run's Approval; never raises."""
        if not self.enabled:
            return
        try:
            self._queue.put(describe(event, run, self.base_url))
        except Exception:  # noqa: BLE001 - a notification must never fail the Approval
            log.exception("Couldn't prepare the Teams notification for Run %s", run.get("id"))

    def drift_event(self, run: dict, schedule_name: str | None) -> None:
        """Queue "Drift found" for a finished Scheduled run (#80); never raises."""
        if not self.enabled:
            return
        try:
            self._queue.put(describe_drift(run, schedule_name, self.base_url))
        except Exception:  # noqa: BLE001 - a notification must never fail the schedule
            log.exception("Couldn't prepare the Drift notification for Run %s", run.get("id"))

    def sweep_expiries(self) -> int:
        """Announce each request or unused Approval that has expired since the last sweep;
        returns how many were queued."""
        if not self.enabled:
            return 0
        queued = 0
        for run_id in self.store.expired_approvals_to_announce():
            # The claim's own snapshot: the request as it expired, not as it is a moment later.
            claimed = self.store.claim_expiry_announcement(run_id)
            if claimed is not None:
                self.approval_event("expired", claimed)
                queued += 1
        return queued

    # ---------- delivery ----------

    def start(self) -> None:
        if not self.enabled:
            log.info("Teams notifications are off: %s is not set.", ENV_NAME)
            return
        for name, target in (("pd-ops-teams", self._send_loop),
                             ("pd-ops-teams-expiry", self._sweep_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        if not self._threads:
            return
        self._stopping.set()
        self._queue.put(None)  # wakes the sender
        for thread in self._threads:
            thread.join(timeout=10)
        left = 0
        while not self._queue.empty():
            left += self._queue.get_nowait() is not None  # the wake-up isn't a message
        if left:
            log.warning("%d Teams notification(s) not sent: the service is stopping.", left)

    def _sweep_loop(self) -> None:
        while not self._stopping.wait(self.sweep_seconds):
            try:
                self.sweep_expiries()
            except Exception:  # noqa: BLE001 - logged; the next sweep tries again
                log.exception("The Teams expiry sweep failed")

    def _send_loop(self) -> None:
        while not self._stopping.is_set():
            body = self._queue.get()
            if body is None:
                return
            self.deliver(body)

    def deliver(self, body: dict) -> bool:
        """Post one message, retrying a failure that may pass; True if Teams accepted it."""
        title = body["attachments"][0]["content"]["body"][0]["text"]
        for attempt in range(len(self.delays) + 1):
            if attempt and self._stopping.wait(self.delays[attempt - 1]):
                break
            try:
                self._post(body)
                return True
            except TeamsRefused as e:
                log.error("Teams refused the notification %r: %s. Not retrying.", title, e)
                return False
            except Exception as e:  # noqa: BLE001 - never log the error text: it has the URL
                log.warning("Teams notification %r failed (attempt %d of %d): %s", title,
                            attempt + 1, len(self.delays) + 1, _safe(e))
        log.error("Gave up on the Teams notification %r.", title)
        return False

    def _post(self, body: dict) -> None:
        response = self._http.post(self._url, json=body, timeout=TIMEOUT_SECONDS)
        if response.status_code == 429 or response.status_code >= 500:
            raise RuntimeError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise TeamsRefused(f"HTTP {response.status_code}")


def _safe(error: Exception) -> str:
    """What may be logged about a failed post: our own "HTTP <status>" text, or else only the
    error's type, since a requests error's message includes the webhook URL."""
    if isinstance(error, RuntimeError) and str(error).startswith("HTTP "):
        return str(error)
    return type(error).__name__
