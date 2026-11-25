"""The Run store (ADR 0007): SQL Server in production, SQLite in tests, one database layer."""

from __future__ import annotations

import functools
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Unicode,
    UnicodeText,
    create_engine,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, load_only, mapped_column

from .approvals import APPROVAL_LIFETIME, APPROVED, AWAITING, WITHDRAWN, Approval
from .migrate import check_schema
from .overrides import MAX_REASON_LENGTH, CapOverride
from .review import review

QUEUED, RUNNING, FINISHED = "queued", "running", "finished"
PURGE_BATCH = 500  # Run IDs per DELETE, well under SQL Server's 2100-parameter limit
APPLY = "apply"  # the kind of a Writing run
RUN_KINDS = ("report", "dry-run", APPLY)
UNFINISHED = "unfinished"  # history filter: Runs still queued or running (no outcome yet)
# A read that SQL Server picks as a deadlock victim (for example against the purge's locked
# deletes) is tried again this many times in all, a little later each time.
READ_ATTEMPTS = 3
READ_RETRY_SECONDS = 0.05

log = logging.getLogger(__name__)


def is_deadlock(error: DBAPIError) -> bool:
    """SQL Server chose this statement as a deadlock victim (SQLSTATE 40001, error 1205): its
    transaction was rolled back, so running it again is safe. SQLite never reports this."""
    orig = getattr(error, "orig", None)
    args = getattr(orig, "args", ())
    return (bool(args) and str(args[0]) == "40001") or "(1205)" in str(orig)


def retries_deadlocks(read):
    """Run a read-only store method again when it loses a deadlock. Only for methods that
    change nothing: a write's caller must decide whether its effects can be repeated."""
    @functools.wraps(read)
    def wrapper(*args, **kwargs):
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return read(*args, **kwargs)
            except DBAPIError as e:
                if attempt == READ_ATTEMPTS or not is_deadlock(e):
                    raise
                log.warning("%s lost a deadlock (attempt %d of %d); trying again",
                            read.__name__, attempt, READ_ATTEMPTS)
                time.sleep(READ_RETRY_SECONDS * attempt)
    return wrapper


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


now = utc_now  # the time stamped on Runs (queued, started, finished, reviewed)


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    """One execution of a command (see Run in CONTEXT.md)."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(Unicode(36), primary_key=True)
    command: Mapped[str] = mapped_column(Unicode(100))
    argv: Mapped[list] = mapped_column(JSON)
    requested_by: Mapped[str] = mapped_column(Unicode(320))   # Okta email
    requester_name: Mapped[str] = mapped_column(Unicode(200))
    requester_role: Mapped[str] = mapped_column(Unicode(20))
    kind: Mapped[str] = mapped_column(Unicode(10), default="report")  # | "dry-run" | "apply"
    # Runs execute in the background (#74): "queued" (an apply waiting for the one writing
    # slot), "running", then "finished" with an outcome. The queue is these rows, so it
    # survives a restart.
    status: Mapped[str] = mapped_column(Unicode(10), default="running", index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    # The Run's process (group) ID while it executes, so a restart after a crash can kill
    # whatever the dead service left running.
    pid: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(Unicode(20))
    # The service's own word on how a Run ended, when the command's log doesn't say (e.g.
    # who cancelled it, or why an apply didn't run).
    note: Mapped[str | None] = mapped_column(UnicodeText)
    cancelled_by: Mapped[str | None] = mapped_column(Unicode(320))
    stdout: Mapped[str] = mapped_column(UnicodeText, default="")
    stderr: Mapped[str] = mapped_column(UnicodeText, default="")
    # SHA-256 of the inputs (command, argv, uploaded files) and of the output (classified
    # diff lines and output files): a later apply must match a reviewed dry run (#73).
    input_hash: Mapped[str] = mapped_column(Unicode(64), default="")
    output_hash: Mapped[str] = mapped_column(Unicode(64), default="")
    # A dry run is reviewed by the admin who ran it, and applied at most once (#73).
    reviewed_by: Mapped[str | None] = mapped_column(Unicode(320))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_run_id: Mapped[str | None] = mapped_column(Unicode(36))
    # A write cap raised above its default needs a recorded reason (#75): the reason, who
    # gave it, and the caps as [{flag, default, value}]. An apply carries its dry run's.
    cap_override_reason: Mapped[str | None] = mapped_column(Unicode(MAX_REASON_LENGTH))
    cap_override_by: Mapped[str | None] = mapped_column(Unicode(320))
    cap_overrides: Mapped[list | None] = mapped_column(JSON)
    # An Approval (#77) of a dry run of an Irreversible operation: its status, the engine's
    # reasons, when it was asked for and when it expires, and the Approver's decision. An
    # apply carries its dry run's, so the approver is recorded on it too.
    approval_status: Mapped[str | None] = mapped_column(Unicode(20))
    approval_reasons: Mapped[list | None] = mapped_column(JSON)
    approval_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_decided_by: Mapped[str | None] = mapped_column(Unicode(320))
    approval_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_comment: Mapped[str | None] = mapped_column(Unicode(MAX_REASON_LENGTH))
    # When the Teams channel was told this request or Approval expired (#78), so it is told
    # once; cleared when the requester asks again.
    approval_expiry_announced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    # An apply records the reviewed dry run it came from, and the fresh dry run that was
    # compared with it inside the writing slot (ADR 0011).
    applied_from: Mapped[str | None] = mapped_column(Unicode(36))
    fresh_dry_run_id: Mapped[str | None] = mapped_column(Unicode(36))


class LedgerRow(Base):
    """The permanent record of a finished Writing run (#76, ADR 0013): who requested it, who
    approved it, when, and how it ended. Never its arguments, inputs, diff or output. Kept
    after the Run itself is purged."""

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Unicode(36), unique=True)
    command: Mapped[str] = mapped_column(Unicode(100))
    requested_by: Mapped[str] = mapped_column(Unicode(320))
    requester_name: Mapped[str] = mapped_column(Unicode(200))
    requester_role: Mapped[str] = mapped_column(Unicode(20))
    approved_by: Mapped[str | None] = mapped_column(Unicode(320))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Unicode(20))


def ledger_dict(row: LedgerRow, run_kept: bool) -> dict:
    """`run_kept`: the Run itself is still kept (not yet purged), so the row can link to it."""
    return {"run_id": row.run_id, "command": row.command, "requested_by": row.requested_by,
            "requester_name": row.requester_name, "requester_role": row.requester_role,
            "approved_by": row.approved_by, "queued_at": _iso(row.queued_at),
            "started_at": _iso(row.started_at), "finished_at": _iso(row.finished_at),
            "outcome": row.outcome, "run_kept": run_kept}


def _who_when_filters(model, *, command, requester, since, before) -> list:
    """The filters the Run history and the Ledger share: the command, part of the requester's
    email or name (ignoring case), and when it was requested (`since` inclusive, `before`
    exclusive)."""
    conditions = []
    if command:
        conditions.append(model.command == command)
    if requester:
        needle = requester.lower()
        conditions.append(or_(
            func.lower(model.requested_by).contains(needle, autoescape=True),
            func.lower(model.requester_name).contains(needle, autoescape=True)))
    if since is not None:
        conditions.append(model.queued_at >= since)
    if before is not None:
        conditions.append(model.queued_at < before)
    return conditions


def _linked_groups(runs) -> list[list]:
    """Runs that refer to each other (a dry run, the applies made from it and their fresh
    dry runs) as connected groups: the purge keeps or deletes a group whole (ADR 0013)."""
    parent = {r.id: r.id for r in runs}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in runs:
        for other in (r.applied_from, r.applied_run_id, r.fresh_dry_run_id):
            if other in parent:
                parent[root(other)] = root(r.id)
    groups: dict[str, list] = {}
    for r in runs:
        groups.setdefault(root(r.id), []).append(r)
    return list(groups.values())


def _batches(groups: list[list[str]]):
    """Whole groups at a time, about PURGE_BATCH IDs per batch."""
    batch: list[str] = []
    for group in groups:
        batch.extend(group)
        if len(batch) >= PURGE_BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


class RunFile(Base):
    """A file stored with a Run: an uploaded input or an output the command wrote. The file
    lives in the Run's directory; its SHA-256 shows whether it changed afterwards."""

    __tablename__ = "run_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Unicode(36), ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(Unicode(10))        # "input" or "output"
    path: Mapped[str] = mapped_column(Unicode(400))       # relative to the Run directory
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Unicode(64))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:  # SQLite drops the zone; everything is stored in UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _message(run: RunRecord) -> str | None:
    """The service's note, or else the command's own last word on a Run that didn't succeed
    (e.g. a usage error)."""
    if run.note:
        return run.note
    if run.outcome in (None, "succeeded"):
        return None
    lines = [line.strip() for line in (run.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def file_dict(f: RunFile) -> dict:
    return {"id": f.id, "role": f.role, "path": f.path, "size": f.size, "sha256": f.sha256}


# The columns summary_dict reads: lists of Runs load only these, never a Run's output or log.
SUMMARY_COLUMNS = (
    RunRecord.id, RunRecord.command, RunRecord.argv, RunRecord.kind, RunRecord.status,
    RunRecord.outcome, RunRecord.requested_by, RunRecord.requester_name,
    RunRecord.requester_role, RunRecord.queued_at, RunRecord.started_at,
    RunRecord.finished_at, RunRecord.applied_from, RunRecord.cap_override_reason,
    RunRecord.cap_override_by, RunRecord.cap_overrides,
    RunRecord.approval_status, RunRecord.approval_reasons, RunRecord.approval_requested_at,
    RunRecord.approval_expires_at, RunRecord.approval_decided_by,
    RunRecord.approval_decided_at, RunRecord.approval_comment,
)


def cap_override(run: RunRecord) -> dict | None:
    """A Run's cap override (#75) as the API shows it, or None if it raised no cap."""
    override = CapOverride.of(run)
    return override.as_dict() if override else None


def approval_view(run: RunRecord, now: datetime) -> dict | None:
    approval = Approval.of(run)
    return approval.as_dict(now, _iso, carried=run.kind == APPLY) if approval else None


def summary_dict(run: RunRecord, now: datetime | None = None) -> dict:
    """A Run as a row of the history (#69): what it was and how it ended, without its
    output, log or files."""
    return {
        "id": run.id, "command": run.command, "argv": list(run.argv), "kind": run.kind,
        "status": run.status, "outcome": run.outcome,
        "requested_by": run.requested_by, "requester_name": run.requester_name,
        "requester_role": run.requester_role, "queued_at": _iso(run.queued_at),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "applied_from": run.applied_from, "cap_override": cap_override(run),
        "approval": approval_view(run, now or utc_now()),
    }


def to_dict(run: RunRecord, files: list[RunFile], now: datetime | None = None) -> dict:
    """The whole Run, as its page shows it."""
    return {
        **summary_dict(run, now),
        "cancelled_by": run.cancelled_by, "fresh_dry_run_id": run.fresh_dry_run_id,
        "exit_code": run.exit_code, "stdout": run.stdout, "stderr": run.stderr,
        "message": _message(run), "input_hash": run.input_hash,
        "output_hash": run.output_hash, "reviewed_by": run.reviewed_by,
        "reviewed_at": _iso(run.reviewed_at), "applied_run_id": run.applied_run_id,
        "review": (review(run.stdout, run.stderr, run.outcome)
                   if run.kind == "dry-run" and run.status == FINISHED else None),
        "files": [file_dict(f) for f in files],
    }


class RunStore:
    def __init__(self, database_url: str, clock=None, ledger_enabled: bool = True):
        self.clock = clock or utc_now  # judges Approval expiry and Run age (tests move it on)
        self.ledger_enabled = ledger_enabled  # PD_UI_LEDGER; see ADR 0013 and #76
        self.engine = create_engine(database_url)
        # The schema comes from migrations, applied by pd-ui-migrate (#70); the service
        # only checks it, so its own account needs no rights to change the schema.
        check_schema(self.engine)

    def save(self, run: RunRecord, files: list[RunFile] = ()) -> dict:
        run_id = run.id
        with Session(self.engine) as session, session.begin():
            session.add(run)
            session.flush()
            for f in files:
                f.run_id = run_id
                session.add(f)
        return self.get(run_id)

    @retries_deadlocks
    def get(self, run_id: str) -> dict | None:
        with Session(self.engine) as session:
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id))
            if run is None:
                return None
            files = session.scalars(select(RunFile).where(RunFile.run_id == run_id)
                                    .order_by(RunFile.id)).all()
            return to_dict(run, list(files), self.clock())

    def mark_reviewed(self, run_id: str, email: str) -> None:
        self.update(run_id, reviewed_by=email, reviewed_at=self.clock())

    def claim_apply(self, dry_run_id: str, apply_id: str) -> bool:
        """Atomically reserve a dry run for one apply; False if it is already taken."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord)
                .where(RunRecord.id == dry_run_id, RunRecord.applied_run_id.is_(None))
                .values(applied_run_id=apply_id))
            return result.rowcount == 1

    def release_apply(self, dry_run_id: str, apply_id: str) -> None:
        """Undo a claim when the apply was refused before it ran."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord)
                            .where(RunRecord.id == dry_run_id,
                                   RunRecord.applied_run_id == apply_id)
                            .values(applied_run_id=None))

    @retries_deadlocks
    def record(self, run_id: str) -> RunRecord | None:
        """The Run's row itself (detached), for the executor."""
        with Session(self.engine, expire_on_commit=False) as session:
            return session.get(RunRecord, run_id)

    @retries_deadlocks
    def files(self, run_id: str, role: str | None = None) -> list[RunFile]:
        with Session(self.engine, expire_on_commit=False) as session:
            query = select(RunFile).where(RunFile.run_id == run_id)
            if role:
                query = query.where(RunFile.role == role)
            return list(session.scalars(query.order_by(RunFile.id)).all())

    def start(self, run_id: str) -> bool:
        """Atomically move a queued Run to running; False if it was cancelled meanwhile."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord).where(RunRecord.id == run_id, RunRecord.status == QUEUED)
                .values(status=RUNNING, started_at=self.clock()))
            return result.rowcount == 1

    def update(self, run_id: str, **values) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord).where(RunRecord.id == run_id).values(**values))

    def update_log(self, run_id: str, text: str) -> None:
        """The log so far of a Run that is still executing (#68); finish() stores the rest."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord)
                            .where(RunRecord.id == run_id, RunRecord.status == RUNNING)
                            .values(stderr=text))

    def finish(self, run_id: str, files: list[RunFile] | None = None, **values) -> bool:
        """Record how a running Run ended, with the output files it wrote. False (and
        nothing recorded) if it had already finished."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord).where(RunRecord.id == run_id, RunRecord.status == RUNNING)
                .values(status=FINISHED, finished_at=self.clock(), **values))
            if result.rowcount != 1:
                return False
            for f in files or []:
                f.run_id = run_id
                session.add(f)
            self._write_ledger(session, [run_id])
            return True

    def _write_ledger(self, session: Session, run_ids) -> None:
        """In the transaction that finishes them: one Ledger row for each finished Writing
        run among `run_ids` (an apply, whatever its outcome), if the Ledger is switched on."""
        if not self.ledger_enabled or not run_ids:
            return
        recorded = select(LedgerRow.run_id).where(LedgerRow.run_id.in_(list(run_ids)))
        runs = session.scalars(select(RunRecord).where(  # one row per Run, whatever happens
            RunRecord.id.in_(list(run_ids)), RunRecord.kind == APPLY,
            RunRecord.status == FINISHED, RunRecord.id.not_in(recorded))).all()
        for run in runs:
            session.add(LedgerRow(
                run_id=run.id, command=run.command, requested_by=run.requested_by,
                requester_name=run.requester_name, requester_role=run.requester_role,
                approved_by=(run.approval_decided_by if run.approval_status == APPROVED
                             else None),
                queued_at=run.queued_at, started_at=run.started_at,
                finished_at=run.finished_at, outcome=run.outcome))

    def request_cancel(self, run_id: str, email: str) -> bool:
        """Record who cancelled a running Run; False if it has already finished."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord).where(RunRecord.id == run_id, RunRecord.status == RUNNING,
                                        RunRecord.cancelled_by.is_(None))
                .values(cancelled_by=email))
            return result.rowcount == 1

    def requeue(self, run_id: str) -> None:
        """Put a Writing run that hadn't started writing back in the queue, in its place."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord)
                            .where(RunRecord.id == run_id, RunRecord.status == RUNNING)
                            .values(status=QUEUED, started_at=None, fresh_dry_run_id=None))

    def cancel_queued(self, run_id: str, email: str) -> bool:
        """Atomically stop a Run that hasn't started; False if it already has."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord).where(RunRecord.id == run_id, RunRecord.status == QUEUED)
                .values(status=FINISHED, outcome="stopped", finished_at=self.clock(),
                        cancelled_by=email,
                        note=f"Cancelled by {email} before it started; nothing was run."))
            if result.rowcount != 1:
                return False
            self._write_ledger(session, [run_id])
            return True

    @retries_deadlocks
    def next_queued(self) -> str | None:
        """The Writing run that has waited longest."""
        with Session(self.engine) as session:
            return session.scalar(select(RunRecord.id).where(RunRecord.status == QUEUED)
                                  .order_by(RunRecord.queued_at, RunRecord.id).limit(1))

    @retries_deadlocks
    def queue(self) -> dict:
        """The executing Writing run, if any, and the ones waiting, oldest first."""
        with Session(self.engine) as session:
            summaries = select(RunRecord).options(load_only(*SUMMARY_COLUMNS))
            executing = session.scalar(summaries.where(
                RunRecord.kind == APPLY, RunRecord.status == RUNNING))
            pending = session.scalars(summaries.where(RunRecord.status == QUEUED)
                                      .order_by(RunRecord.queued_at, RunRecord.id)).all()
            now = self.clock()
            return {"executing": summary_dict(executing, now) if executing else None,
                    "pending": [summary_dict(r, now) for r in pending]}

    def stop_interrupted(self) -> list[int]:
        """At startup: whatever was executing when the service stopped was cut off. Returns
        the process IDs those Runs had, for the caller to make sure they are gone."""
        note = "Stopped: the service restarted while this Run was executing."
        with Session(self.engine) as session, session.begin():
            pids = list(session.scalars(select(RunRecord.pid).where(
                RunRecord.status == RUNNING, RunRecord.pid.is_not(None))))
            interrupted_ids = list(session.scalars(select(RunRecord.id).where(
                RunRecord.status == RUNNING)))
            for kind_is_apply, text in ((True, f"{note} It may have been partly applied."),
                                        (False, note)):
                kind = RunRecord.kind == APPLY
                session.execute(
                    update(RunRecord)
                    .where(RunRecord.status == RUNNING, kind if kind_is_apply else ~kind)
                    .values(status=FINISHED, outcome="stopped", finished_at=self.clock(),
                            note=text))
            self._write_ledger(session, interrupted_ids)
        return pids

    @retries_deadlocks
    def search(self, *, command: str | None = None, requester: str | None = None,
               since: datetime | None = None, before: datetime | None = None,
               outcome: str | None = None, kind: str | None = None,
               page: int = 1, per_page: int = 50) -> dict:
        """The Run history (#69): Runs matching every filter given, newest first, one page
        at a time. `requester` matches part of the requester's email or name, ignoring
        case; `since` is inclusive and `before` exclusive, on when the Run was requested."""
        conditions = _who_when_filters(RunRecord, command=command, requester=requester,
                                       since=since, before=before)
        if outcome == UNFINISHED:
            conditions.append(RunRecord.status != FINISHED)
        elif outcome:
            conditions.append(RunRecord.outcome == outcome)
        if kind:
            conditions.append(RunRecord.kind == kind)
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(RunRecord)
                                   .where(*conditions))
            runs = session.scalars(
                select(RunRecord).options(load_only(*SUMMARY_COLUMNS)).where(*conditions)
                .order_by(RunRecord.queued_at.desc(), RunRecord.id.desc())
                .offset((page - 1) * per_page).limit(per_page)).all()
            now = self.clock()
            return {"runs": [summary_dict(r, now) for r in runs], "total": total, "page": page,
                    "per_page": per_page}

    def decide_approval(self, run_id: str, decision: str, by: str, comment: str) -> bool:
        """Record an Approver's decision, atomically: only while the dry run's request is
        open (waiting, unexpired, not applied), so of two racing decisions, or a decision
        racing the expiry, at most one counts. False if it wasn't open."""
        now = self.clock()
        values = {"approval_status": decision, "approval_decided_by": by,
                  "approval_decided_at": now, "approval_comment": comment}
        if decision == APPROVED:
            values["approval_expires_at"] = now + APPROVAL_LIFETIME
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, RunRecord.kind == "dry-run",
                       RunRecord.applied_run_id.is_(None),
                       RunRecord.approval_status == AWAITING,
                       RunRecord.approval_expires_at > now.astimezone(timezone.utc))
                .values(**values))
            return result.rowcount == 1

    def withdraw_approval(self, run_id: str) -> None:
        """A mismatch at apply time withdraws the dry run's Approval (ADR 0011)."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord)
                            .where(RunRecord.id == run_id,
                                   RunRecord.approval_status == APPROVED)
                            .values(approval_status=WITHDRAWN))

    def _expired_unannounced(self, now: datetime):
        """Requests nobody answered, and Approvals never used, that are past their expiry
        and not yet announced (#78)."""
        return (RunRecord.kind == "dry-run", RunRecord.applied_run_id.is_(None),
                RunRecord.approval_status.in_((AWAITING, APPROVED)),
                RunRecord.approval_expires_at <= now.astimezone(timezone.utc),
                RunRecord.approval_expiry_announced_at.is_(None))

    @retries_deadlocks
    def expired_approvals_to_announce(self) -> list[str]:
        """The dry runs whose request or Approval has expired and not been announced."""
        with Session(self.engine) as session:
            return list(session.scalars(
                select(RunRecord.id).where(*self._expired_unannounced(self.clock()))
                .order_by(RunRecord.approval_expires_at, RunRecord.id)))

    def claim_expiry_announcement(self, run_id: str) -> bool:
        """Atomically mark one expiry as announced; False if it was announced already (by
        another sweep), was asked for again, or isn't expired after all."""
        now = self.clock()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, *self._expired_unannounced(now))
                .values(approval_expiry_announced_at=now))
            return result.rowcount == 1

    @retries_deadlocks
    def awaiting_approval(self) -> list[dict]:
        """Requests waiting for an Approver (not yet expired), oldest first (#77)."""
        now = self.clock().astimezone(timezone.utc)  # stored times are UTC
        with Session(self.engine) as session:
            rows = session.scalars(
                select(RunRecord).options(load_only(*SUMMARY_COLUMNS))
                .where(RunRecord.approval_status == AWAITING,
                       RunRecord.approval_expires_at > now)
                .order_by(RunRecord.approval_requested_at, RunRecord.id)).all()
            return [summary_dict(r, now) for r in rows]

    def purge(self, cutoff: datetime) -> list[str]:
        """Delete finished Runs requested before `cutoff`, with their file records (ADR
        0013); the caller deletes their directories. Runs that refer to each other go
        together: a dry run is kept while any apply made from it is younger or unfinished.
        Deletes in batches, each committed, so a failure part way loses nothing it hadn't
        already deleted. Never the Ledger, unless it is switched off, when its old rows go
        too. Returns the purged Run IDs."""
        cutoff = cutoff.astimezone(timezone.utc)
        with Session(self.engine) as session:
            runs = session.execute(select(
                RunRecord.id, RunRecord.queued_at, RunRecord.status, RunRecord.applied_from,
                RunRecord.applied_run_id, RunRecord.fresh_dry_run_id)).all()
        due = [[r.id for r in group] for group in _linked_groups(runs)
               if all(r.status == FINISHED and _as_utc(r.queued_at) < cutoff for r in group)]
        purged: list[str] = []
        for batch in _batches(due):
            with Session(self.engine) as session, session.begin():
                # A dry run an apply was claimed from since the read above stays. The rows
                # are locked until the deletes commit, so a claim racing this purge waits,
                # finds the dry run gone and is refused, rather than queueing an apply
                # whose dry run is then deleted.
                gone = list(session.scalars(select(RunRecord.id).where(
                    RunRecord.id.in_(batch), RunRecord.status == FINISHED,
                    or_(RunRecord.applied_run_id.is_(None),
                        RunRecord.applied_run_id.in_(batch)))
                    # SQL Server's dialect ignores FOR UPDATE: it needs the table hint.
                    .with_hint(RunRecord, "WITH (UPDLOCK, ROWLOCK)", "mssql")
                    .with_for_update()))
                if gone:
                    session.execute(delete(RunFile).where(RunFile.run_id.in_(gone)))
                    session.execute(delete(RunRecord).where(RunRecord.id.in_(gone)))
            purged.extend(gone)
        if not self.ledger_enabled:
            with Session(self.engine) as session, session.begin():
                session.execute(delete(LedgerRow).where(LedgerRow.queued_at < cutoff))
        return purged

    @retries_deadlocks
    def run_ids(self) -> set[str]:
        """Every Run's ID, for the purge to find folders no Run owns."""
        with Session(self.engine) as session:
            return set(session.scalars(select(RunRecord.id)))

    @retries_deadlocks
    def ledger(self, *, command: str | None = None, requester: str | None = None,
               since: datetime | None = None, before: datetime | None = None,
               outcome: str | None = None, page: int = 1, per_page: int = 50) -> dict:
        """The Ledger, newest first, filtered like the Run history (#76)."""
        conditions = _who_when_filters(LedgerRow, command=command, requester=requester,
                                       since=since, before=before)
        if outcome:
            conditions.append(LedgerRow.outcome == outcome)
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(LedgerRow)
                                   .where(*conditions))
            rows = session.scalars(
                select(LedgerRow).where(*conditions)
                .order_by(LedgerRow.queued_at.desc(), LedgerRow.id.desc())
                .offset((page - 1) * per_page).limit(per_page)).all()
            kept = set(session.scalars(select(RunRecord.id).where(
                RunRecord.id.in_([r.run_id for r in rows])))) if rows else set()
            return {"enabled": self.ledger_enabled,
                    "rows": [ledger_dict(r, r.run_id in kept) for r in rows],
                    "total": total, "page": page, "per_page": per_page}

    @retries_deadlocks
    def get_file(self, run_id: str, file_id: int) -> dict | None:
        with Session(self.engine) as session:
            f = session.scalar(select(RunFile).where(RunFile.id == file_id,
                                                     RunFile.run_id == run_id))
            return file_dict(f) if f else None
