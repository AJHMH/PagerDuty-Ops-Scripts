"""The Run store (ADR 0007): SQL Server in production, SQLite in tests, one database layer."""

from __future__ import annotations

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
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, load_only, mapped_column

from .migrate import check_schema
from .review import review

QUEUED, RUNNING, FINISHED = "queued", "running", "finished"
RUN_KINDS = ("report", "dry-run", "apply")
UNFINISHED = "unfinished"  # history filter: Runs still queued or running (no outcome yet)


def now() -> datetime:
    return datetime.now(timezone.utc)


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
    cap_override_reason: Mapped[str | None] = mapped_column(Unicode(500))
    cap_override_by: Mapped[str | None] = mapped_column(Unicode(320))
    cap_overrides: Mapped[list | None] = mapped_column(JSON)
    # An apply records the reviewed dry run it came from, and the fresh dry run that was
    # compared with it inside the writing slot (ADR 0011).
    applied_from: Mapped[str | None] = mapped_column(Unicode(36))
    fresh_dry_run_id: Mapped[str | None] = mapped_column(Unicode(36))


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


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite drops the zone; everything is stored in UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
)


def cap_override(run: RunRecord) -> dict | None:
    """A Run's cap override (#75), or None if it raised no cap."""
    if run.cap_override_reason is None:
        return None
    return {"reason": run.cap_override_reason, "by": run.cap_override_by,
            "caps": list(run.cap_overrides or [])}


def summary_dict(run: RunRecord) -> dict:
    """A Run as a row of the history (#69): what it was and how it ended, without its
    output, log or files."""
    return {
        "id": run.id, "command": run.command, "argv": list(run.argv), "kind": run.kind,
        "status": run.status, "outcome": run.outcome,
        "requested_by": run.requested_by, "requester_name": run.requester_name,
        "requester_role": run.requester_role, "queued_at": _iso(run.queued_at),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "applied_from": run.applied_from, "cap_override": cap_override(run),
    }


def to_dict(run: RunRecord, files: list[RunFile]) -> dict:
    """The whole Run, as its page shows it."""
    return {
        **summary_dict(run),
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
    def __init__(self, database_url: str):
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

    def get(self, run_id: str) -> dict | None:
        with Session(self.engine) as session:
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id))
            if run is None:
                return None
            files = session.scalars(select(RunFile).where(RunFile.run_id == run_id)
                                    .order_by(RunFile.id)).all()
            return to_dict(run, list(files))

    def mark_reviewed(self, run_id: str, email: str) -> None:
        self.update(run_id, reviewed_by=email, reviewed_at=now())

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

    def record(self, run_id: str) -> RunRecord | None:
        """The Run's row itself (detached), for the executor."""
        with Session(self.engine, expire_on_commit=False) as session:
            return session.get(RunRecord, run_id)

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
                .values(status=RUNNING, started_at=now()))
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
                .values(status=FINISHED, finished_at=now(), **values))
            if result.rowcount != 1:
                return False
            for f in files or []:
                f.run_id = run_id
                session.add(f)
            return True

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
                .values(status=FINISHED, outcome="stopped", finished_at=now(),
                        cancelled_by=email,
                        note=f"Cancelled by {email} before it started; nothing was run."))
            return result.rowcount == 1

    def next_queued(self) -> str | None:
        """The Writing run that has waited longest."""
        with Session(self.engine) as session:
            return session.scalar(select(RunRecord.id).where(RunRecord.status == QUEUED)
                                  .order_by(RunRecord.queued_at, RunRecord.id).limit(1))

    def queue(self) -> dict:
        """The executing Writing run, if any, and the ones waiting, oldest first."""
        with Session(self.engine) as session:
            summaries = select(RunRecord).options(load_only(*SUMMARY_COLUMNS))
            executing = session.scalar(summaries.where(
                RunRecord.kind == "apply", RunRecord.status == RUNNING))
            pending = session.scalars(summaries.where(RunRecord.status == QUEUED)
                                      .order_by(RunRecord.queued_at, RunRecord.id)).all()
            return {"executing": summary_dict(executing) if executing else None,
                    "pending": [summary_dict(r) for r in pending]}

    def stop_interrupted(self) -> list[int]:
        """At startup: whatever was executing when the service stopped was cut off. Returns
        the process IDs those Runs had, for the caller to make sure they are gone."""
        note = "Stopped: the service restarted while this Run was executing."
        with Session(self.engine) as session, session.begin():
            pids = list(session.scalars(select(RunRecord.pid).where(
                RunRecord.status == RUNNING, RunRecord.pid.is_not(None))))
            for kind_is_apply, text in ((True, f"{note} It may have been partly applied."),
                                        (False, note)):
                kind = RunRecord.kind == "apply"
                session.execute(
                    update(RunRecord)
                    .where(RunRecord.status == RUNNING, kind if kind_is_apply else ~kind)
                    .values(status=FINISHED, outcome="stopped", finished_at=now(), note=text))
        return pids

    def search(self, *, command: str | None = None, requester: str | None = None,
               since: datetime | None = None, before: datetime | None = None,
               outcome: str | None = None, kind: str | None = None,
               page: int = 1, per_page: int = 50) -> dict:
        """The Run history (#69): Runs matching every filter given, newest first, one page
        at a time. `requester` matches part of the requester's email or name, ignoring
        case; `since` is inclusive and `before` exclusive, on when the Run was requested."""
        conditions = []
        if command:
            conditions.append(RunRecord.command == command)
        if requester:
            needle = requester.lower()
            conditions.append(or_(
                func.lower(RunRecord.requested_by).contains(needle, autoescape=True),
                func.lower(RunRecord.requester_name).contains(needle, autoescape=True)))
        if since is not None:
            conditions.append(RunRecord.queued_at >= since)
        if before is not None:
            conditions.append(RunRecord.queued_at < before)
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
            return {"runs": [summary_dict(r) for r in runs], "total": total, "page": page,
                    "per_page": per_page}

    def get_file(self, run_id: str, file_id: int) -> dict | None:
        with Session(self.engine) as session:
            f = session.scalar(select(RunFile).where(RunFile.id == file_id,
                                                     RunFile.run_id == run_id))
            return file_dict(f) if f else None
