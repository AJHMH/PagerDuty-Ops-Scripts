"""Approvals (#77, ADRs 0003 and 0011): applying an Irreversible operation needs a second
admin's recorded consent. Whether a Run is irreversible is the engine's call (`assess()`);
this module holds the Approval's life cycle.

A requester asks for an Approval on their reviewed dry run ("awaiting-approval"). An Approver
other than the requester approves or rejects it, with a comment. A request nobody answers,
and an Approval once given, each expire 24 hours later. A rejection is final for that dry
run: the requester dry-runs again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

APPROVAL_LIFETIME = timedelta(hours=24)
AWAITING, APPROVED, REJECTED, EXPIRED = (
    "awaiting-approval", "approved", "rejected", "expired")
# Columns an apply copies from its dry run, so the approver is recorded on the apply too.
APPROVAL_COLUMNS = ("approval_status", "approval_reasons", "approval_requested_at",
                    "approval_expires_at", "approval_decided_by", "approval_decided_at",
                    "approval_comment")


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:  # SQLite drops the zone; it is UTC
        value = value.replace(tzinfo=timezone.utc)
    return value


def status(run, now: datetime) -> str | None:
    """The Approval's status at `now`: a request or an Approval past its time has expired."""
    stored = run.approval_status
    if stored in (AWAITING, APPROVED) and now >= _utc(run.approval_expires_at):
        return EXPIRED
    return stored


def approval_dict(run, now: datetime, iso) -> dict | None:
    if run.approval_status is None:
        return None
    return {
        "status": status(run, now), "reasons": list(run.approval_reasons or []),
        "requested_at": iso(run.approval_requested_at),
        "expires_at": iso(run.approval_expires_at),
        "decided_by": run.approval_decided_by, "decided_at": iso(run.approval_decided_at),
        "comment": run.approval_comment,
    }


def copied(run) -> dict:
    """The Approval columns of `run`, for an apply made from it."""
    return {column: getattr(run, column) for column in APPROVAL_COLUMNS}


def require_approved(verdict, run, now: datetime) -> None:
    """An Irreversible operation's apply needs its dry run's Approval, still valid."""
    if not verdict.irreversible:
        return
    current = status(run, now)
    reasons = "; ".join(verdict.reasons)
    if current == APPROVED:
        return
    if current == EXPIRED and run.approval_decided_at is not None:
        message = "Its Approval has expired (Approvals last 24 hours); ask for a new one."
    elif current == AWAITING:
        message = "It is waiting for an Approver."
    elif current == REJECTED:
        message = (f"Its Approval was rejected by {run.approval_decided_by}; run a new dry "
                   "run.")
    else:
        message = "Ask an Approver for an Approval first."
    raise HTTPException(403, f"This is an Irreversible operation ({reasons}), so applying it "
                             f"needs a second admin's Approval. {message}")
