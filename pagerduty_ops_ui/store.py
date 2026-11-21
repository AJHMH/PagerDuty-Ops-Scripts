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
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .review import review

QUEUED, RUNNING, FINISHED = "queued", "running", "finished"


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
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
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


def to_dict(run: RunRecord, files: list[RunFile]) -> dict:
    return {
        "id": run.id, "command": run.command, "argv": list(run.argv),
        "requested_by": run.requested_by, "requester_name": run.requester_name,
        "requester_role": run.requester_role,
        "status": run.status, "queued_at": _iso(run.queued_at),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "cancelled_by": run.cancelled_by, "fresh_dry_run_id": run.fresh_dry_run_id,
        "exit_code": run.exit_code, "outcome": run.outcome,
        "stdout": run.stdout, "stderr": run.stderr, "message": _message(run),
        "kind": run.kind, "input_hash": run.input_hash, "output_hash": run.output_hash,
        "reviewed_by": run.reviewed_by, "reviewed_at": _iso(run.reviewed_at),
        "applied_run_id": run.applied_run_id, "applied_from": run.applied_from,
        "review": (review(run.stdout, run.stderr, run.outcome)
                   if run.kind == "dry-run" and run.status == FINISHED else None),
        "files": [file_dict(f) for f in files],
    }


class RunStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        # Schema migrations (Alembic) arrive with the SQL Server job (#70).
        Base.metadata.create_all(self.engine)

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

    def finish(self, run_id: str, files: list[RunFile] = (), **values) -> None:
        """Record how a running Run ended, with the output files it wrote."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(RunRecord)
                            .where(RunRecord.id == run_id, RunRecord.status == RUNNING)
                            .values(status=FINISHED, finished_at=now(), **values))
            for f in files:
                f.run_id = run_id
                session.add(f)

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
            executing = session.scalar(select(RunRecord).where(
                RunRecord.kind == "apply", RunRecord.status == RUNNING))
            pending = session.scalars(select(RunRecord).where(RunRecord.status == QUEUED)
                                      .order_by(RunRecord.queued_at, RunRecord.id)).all()
            return {"executing": to_dict(executing, []) if executing else None,
                    "pending": [to_dict(r, []) for r in pending]}

    def stop_interrupted(self) -> None:
        """At startup: whatever was executing when the service stopped was cut off."""
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(RunRecord).where(RunRecord.status == RUNNING)
                .values(status=FINISHED, outcome="stopped", finished_at=now(),
                        note="Stopped: the service restarted while this Run was executing. "
                             "An apply may have been partly applied."))

    def get_file(self, run_id: str, file_id: int) -> dict | None:
        with Session(self.engine) as session:
            f = session.scalar(select(RunFile).where(RunFile.id == file_id,
                                                     RunFile.run_id == run_id))
            return file_dict(f) if f else None
