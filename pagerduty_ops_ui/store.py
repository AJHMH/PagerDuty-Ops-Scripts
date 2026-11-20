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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


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
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Unicode(20))
    stdout: Mapped[str] = mapped_column(UnicodeText, default="")
    stderr: Mapped[str] = mapped_column(UnicodeText, default="")


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
    """The command's own last word on a Run that didn't succeed (e.g. a usage error)."""
    if run.outcome == "succeeded":
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
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "exit_code": run.exit_code, "outcome": run.outcome,
        "stdout": run.stdout, "stderr": run.stderr, "message": _message(run),
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

    def get_file(self, run_id: str, file_id: int) -> dict | None:
        with Session(self.engine) as session:
            f = session.scalar(select(RunFile).where(RunFile.id == file_id,
                                                     RunFile.run_id == run_id))
            return file_dict(f) if f else None
