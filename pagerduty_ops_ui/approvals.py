"""Approvals (#77, ADRs 0003 and 0011): applying an Irreversible operation needs a second
admin's recorded consent. Whether a Run is irreversible is the engine's call (`assess()`);
this module holds the Approval's life cycle.

When a dry run is reviewed, the engine's reasons (if any) are recorded, so the page can say
up front that applying it needs an Approval. Its requester asks ("awaiting-approval"); an
Approver other than the requester approves or rejects, with a comment. A request nobody
answers, and an Approval once given, each expire 24 hours later. A rejection is final for
that dry run, and a mismatch at apply time withdraws its Approval: either way the requester
dry-runs again. An apply carries the Approval it was made with, shown as "used".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

APPROVAL_LIFETIME = timedelta(hours=24)
AWAITING, APPROVED, REJECTED = "awaiting-approval", "approved", "rejected"
EXPIRED, WITHDRAWN, USED = "expired", "withdrawn", "used"
FINAL = (REJECTED, WITHDRAWN)  # this dry run can't be approved again: dry-run again


def utc(value: datetime | None) -> datetime | None:
    """Stored times are UTC; SQLite drops the zone, so put it back."""
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class Approval:
    """A Run's Approval columns, as one value."""

    status: str | None          # None: needed (reasons given) but not asked for yet
    reasons: tuple[str, ...]    # why the engine counts the apply as irreversible
    requested_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None

    @classmethod
    def of(cls, run) -> Approval | None:
        if run.approval_status is None and not run.approval_reasons:
            return None
        return cls(run.approval_status, tuple(run.approval_reasons or ()),
                   utc(run.approval_requested_at), utc(run.approval_expires_at),
                   run.approval_decided_by, utc(run.approval_decided_at), run.approval_comment)

    def columns(self) -> dict:
        return {"approval_status": self.status, "approval_reasons": list(self.reasons),
                "approval_requested_at": self.requested_at,
                "approval_expires_at": self.expires_at,
                "approval_decided_by": self.decided_by,
                "approval_decided_at": self.decided_at, "approval_comment": self.comment}

    def status_at(self, now: datetime, *, carried: bool = False) -> str | None:
        """The status at `now`. A request or an Approval past its time has expired; one an
        apply carries (`carried`) was used, whatever the time is now."""
        if carried and self.status == APPROVED:
            return USED
        if self.status in (AWAITING, APPROVED) and now >= self.expires_at:
            return EXPIRED
        return self.status

    def as_dict(self, now: datetime, iso, *, carried: bool = False) -> dict:
        return {"status": self.status_at(now, carried=carried), "reasons": list(self.reasons),
                "requested_at": iso(self.requested_at), "expires_at": iso(self.expires_at),
                "decided_by": self.decided_by, "decided_at": iso(self.decided_at),
                "comment": self.comment}


def require_approved(verdict, approval: Approval | None, now: datetime) -> None:
    """An Irreversible operation's apply needs its dry run's Approval, still valid (403)."""
    if not verdict.irreversible:
        return
    current = approval.status_at(now) if approval else None
    if current == APPROVED:
        return
    if current == EXPIRED and approval.decided_at is not None:
        message = "Its Approval has expired (Approvals last 24 hours); ask for a new one."
    elif current == AWAITING:
        message = "It is waiting for an Approver."
    elif current == REJECTED:
        message = f"Its Approval was rejected by {approval.decided_by}; run a new dry run."
    elif current == WITHDRAWN:
        message = ("Its Approval was withdrawn because what it would change has changed; "
                   "review and approve the fresh dry run instead.")
    else:
        message = "Ask an Approver for an Approval first."
    raise HTTPException(403, f"This is an Irreversible operation ({'; '.join(verdict.reasons)}),"
                             f" so applying it needs a second admin's Approval. {message}")
