"""Viewer, Operator and Approver come from AD groups pushed to Okta (ADR 0008)."""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    VIEWER = 1    # read reports and the Run history; run nothing that writes
    OPERATOR = 2  # also dry-run and apply changes that can be undone
    APPROVER = 3  # also approve another admin's Irreversible operation

    @property
    def label(self) -> str:
        return self.name.lower()


GROUP_ROLES = {
    "PD-Ops-Viewer": Role.VIEWER,
    "PD-Ops-Operator": Role.OPERATOR,
    "PD-Ops-Approver": Role.APPROVER,
}

NO_ROLE_MESSAGE = (
    "You are signed in but not in any PagerDuty Ops group. Ask the PagerDuty/ServiceNow "
    "admin team to add you to PD-Ops-Viewer, PD-Ops-Operator or PD-Ops-Approver."
)


def role_for(groups) -> Role | None:
    """The highest role any of the admin's Okta groups grants, or None."""
    roles = [GROUP_ROLES[g] for g in groups or () if g in GROUP_ROLES]
    return max(roles) if roles else None
