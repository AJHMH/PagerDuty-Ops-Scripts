"""Lazily-loaded, cached snapshot of a PagerDuty account for cross-object checks.

Used by the ServiceNow ID sync, the consistency validator, and reorg planning so
each collection is fetched at most once per run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import cached_property

from .api import PDApiError, paginate, request
from .log import get_logger

log = get_logger("pdstate")


def _by_id(items) -> dict:
    return {i["id"]: i for i in items if i.get("id")}


def ref_ids(refs) -> list[str]:
    return [r["id"] for r in (refs or []) if isinstance(r, dict) and r.get("id")]


class PDState:
    def __init__(self, token: str):
        self.token = token
        self._members: dict[str, dict[str, str]] = {}
        self._schedule_details: dict[str, dict] = {}

    def _list(self, resource, params=None, label=None, items_key=None) -> list:
        log.info("Fetching %s...", label or resource)
        items = list(paginate(resource, self.token, params=params, items_key=items_key))
        log.info("Found %d %s.", len(items), label or resource)
        return items

    @cached_property
    def users(self) -> dict:
        return _by_id(self._list(
            "users", params={"include[]": ["contact_methods", "notification_rules", "teams"]}))

    @cached_property
    def teams(self) -> dict:
        return _by_id(self._list("teams"))

    @cached_property
    def services(self) -> dict:
        return _by_id(self._list("services"))

    @cached_property
    def escalation_policies(self) -> dict:
        return _by_id(self._list("escalation_policies"))

    @cached_property
    def schedules(self) -> dict:
        return _by_id(self._list("schedules"))

    @cached_property
    def webhooks(self) -> list:
        return self._list("webhook_subscriptions", label="webhook subscriptions")

    @cached_property
    def webhook_ids(self) -> set:
        return {w["id"] for w in self.webhooks if w.get("id")}

    @cached_property
    def extensions(self) -> list:
        return self._list("extensions", params={"include[]": ["extension_schemas"]})

    def team_members(self, team_id: str) -> dict[str, str]:
        """{user_id: team_role} for one team (fully paginated)."""
        if team_id not in self._members:
            members = paginate(f"teams/{team_id}/members", self.token, items_key="members")
            self._members[team_id] = {
                m["user"]["id"]: m.get("role", "")
                for m in members if (m.get("user") or {}).get("id")
            }
        return self._members[team_id]

    def all_team_members(self) -> dict[str, dict[str, str]]:
        return {tid: self.team_members(tid) for tid in self.teams}

    @cached_property
    def user_team_ids(self) -> dict[str, set[str]]:
        """{user_id: {team_id,...}} computed from team membership lists."""
        out: dict[str, set[str]] = {}
        for tid, members in self.all_team_members().items():
            for uid in members:
                out.setdefault(uid, set()).add(tid)
        return out

    def schedule_detail(self, schedule_id: str, days: int = 14) -> dict | None:
        """Full schedule (layers + rendered final schedule) for the next `days`."""
        if schedule_id not in self._schedule_details:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            params = {"since": now.isoformat(), "until": (now + timedelta(days=days)).isoformat()}
            try:
                body = request(f"schedules/{schedule_id}", self.token, params=params)
            except PDApiError as e:
                if e.is_auth_error:
                    raise
                log.error("Failed to fetch schedule %s: %s", schedule_id, e)
                body = {}
            self._schedule_details[schedule_id] = body.get("schedule")
        return self._schedule_details[schedule_id]

    def kind_of(self, object_id: str) -> str | None:
        """Best-effort: which collection an ID belongs to (for wrong-type reports)."""
        for kind, coll in (("service", self.services), ("escalation_policy",
                           self.escalation_policies), ("schedule", self.schedules),
                           ("team", self.teams), ("user", self.users)):
            if object_id in coll:
                return kind
        if object_id in self.webhook_ids:
            return "webhook"
        return None


def v3_schedule_references(token, user_id: str) -> list[str]:
    """Shift-based (v3) schedules are not in the v2 /schedules list; query them by user.

    Raises PDApiError if membership cannot be verified, so callers fail closed.
    """
    return [f"v3 schedule {s.get('name') or s.get('id')}"
            for s in paginate("v3/schedules", token, params={"user_id": user_id},
                              items_key="schedules")]
