"""Scheduled runs (#80, ADR 0014): read-only commands the service starts on a schedule, in
place of cron, so drift reports land in the same Run history as the fixes.

- **Only read-only Runs.** A schedule's command line is judged by the engine (`assess()`)
  when it is saved and again each time it fires; anything that writes, a dry run of a
  writing command included, is refused. A write nobody reviewed or approved would defeat
  ADRs 0003 and 0011.
- **Arguments only.** A schedule stores the form values a Run would take, without uploaded
  files.
- **Once per due time, at most.** A due schedule is claimed by moving its `next_run_at` to
  the first time after now, so after a restart (or any delay) missed times run once.
- **Drift.** A command declares what Drift looks like (`DRIFT_EXIT_CODE`, `DRIFT_SUMMARY`;
  `pd-validate` does). When a finished Scheduled run matches, the Teams channel is told,
  with the command's own summary line. Decided once per Run, so a restart neither misses
  nor repeats it.
"""

from __future__ import annotations

import importlib
import logging
import shutil
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from .forms import FILE_ROLES, BadValues, build_argv, form_for
from .gate import assess_run, check_arguments
from .store import ScheduleRecord, _iso
from .timing import BadTiming, Timing

TICK_SECONDS = 30
MAX_NAME_LENGTH = 100
INPUT_ROLES = ("input-file", "input-dir")

log = logging.getLogger(__name__)


def schedulable_argv(entry, subcommand: str | None, values: dict, cwd: Path, secrets):
    """The command line a schedule runs, or an HTTP refusal: 422 for unusable arguments,
    403 for anything that writes (ADR 0014)."""
    if not isinstance(values, dict):
        raise HTTPException(422, "values must be an object.")
    files = sorted(d for d in values if FILE_ROLES.get(d) in INPUT_ROLES)
    if files:
        raise HTTPException(422, f"Schedules take arguments only, not uploaded files "
                                 f"({', '.join(files)}).")
    try:
        argv = build_argv(form_for(entry), subcommand, values, {})
    except BadValues as e:
        raise HTTPException(422, str(e)) from None
    module = check_arguments(entry, argv, secrets)
    if getattr(module, "DRIFT_SUMMARY", None) and "--quiet" in argv:
        # --quiet logs errors only, so it would hide the summary line Drift is read from.
        raise HTTPException(422, f"--quiet would hide {entry.name}'s summary line "
                                 f"({module.DRIFT_SUMMARY.strip()} …), which is how a "
                                 "Scheduled run's Drift is found. Leave it off.")
    verdict = assess_run(module, entry, argv, cwd)
    if verdict.writes:
        what = "a dry run of a command that writes" if verdict.dry_run else "a command that writes"
        raise HTTPException(403, f"Only read-only commands can be scheduled; this is {what} "
                                 f"({entry.name}). Run it from the Run page instead, where it "
                                 "is reviewed and, if it needs one, approved (ADR 0014).")
    return argv, verdict


def check_schedulable(entry, subcommand, values, secrets):
    """Judge a schedule's command line as it is saved, in a scratch directory."""
    scratch = Path(tempfile.mkdtemp(prefix="pd-ops-schedule-"))
    try:
        return schedulable_argv(entry, subcommand, values, scratch, secrets)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def parse_timing(raw) -> Timing:
    try:
        return Timing.parse(raw)
    except BadTiming as e:
        raise HTTPException(422, str(e)) from None


def clean_name(raw) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or len(name) > MAX_NAME_LENGTH:
        raise HTTPException(422, f"A schedule needs a name of 1 to {MAX_NAME_LENGTH} "
                                 "characters.")
    return name


def schedule_dict(record: ScheduleRecord) -> dict:
    timing = Timing.parse(record.timing)
    return {
        "id": record.id, "name": record.name, "command": record.command,
        "subcommand": record.subcommand, "values": dict(record.values),
        "timing": timing.as_dict(), "description": timing.describe(),
        "paused": record.paused, "created_by": record.created_by,
        "creator_name": record.creator_name, "created_at": _iso(record.created_at),
        "updated_by": record.updated_by, "updated_at": _iso(record.updated_at),
        "next_run_at": None if record.paused else _iso(record.next_run_at),
        "last_run_id": record.last_run_id, "last_run_at": _iso(record.last_run_at),
    }


def drift_of(module, exit_code: int | None, stderr: str) -> tuple[bool, str | None]:
    """Whether a finished Run found Drift, by the command's own declaration, and its summary
    line. Commands that declare nothing never report Drift."""
    code = getattr(module, "DRIFT_EXIT_CODE", None)
    marker = getattr(module, "DRIFT_SUMMARY", None)
    if code is None or not marker or exit_code != code:
        return False, None
    lines = [line[line.index(marker):] for line in stderr.splitlines() if marker in line]
    if not lines:  # exited with the code, but before its summary: a failure, not Drift
        return False, None
    return True, lines[-1].strip()[:500]


class Scheduler:
    """Starts due schedules and decides each finished Scheduled run's Drift, every
    TICK_SECONDS, in the background. `start_run(entry, argv, verdict, record, run_id)`
    stores and starts one Run, as the API does for a report; `refuse_run(record, run_id,
    reason)` records a firing the engine refused, so the history shows why nothing ran."""

    def __init__(self, store, settings, catalog, notifier, *, clock, start_run, refuse_run,
                 tick_seconds: float | None = None):
        self.store = store
        self.settings = settings
        self.catalog = catalog
        self.notifier = notifier
        self.clock = clock
        self.start_run = start_run
        self.refuse_run = refuse_run
        self.tick_seconds = TICK_SECONDS if tick_seconds is None else tick_seconds
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="pd-ops-schedules",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        # The first tick comes one interval after startup: whatever was missed while the
        # service was down then runs once.
        while not self._stopping.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - logged; the next tick tries again
                log.exception("The schedule tick failed")

    def tick(self) -> list[str]:
        """Fire what is due and judge what has finished; returns the Run IDs started."""
        started = [run_id for record in self.store.due_schedules()
                   if (run_id := self.fire(record))]
        self.judge_finished()
        return started

    def fire(self, record: ScheduleRecord) -> str | None:
        """Start one Scheduled run of a due schedule; None if another tick claimed it."""
        now = self.clock()
        timing = Timing.parse(record.timing)
        if not self.store.claim_schedule(record.id, record.next_run_at,
                                         timing.next_after(now)):
            return None
        run_id = str(uuid.uuid4())
        run_dir = self.settings.run_root / run_id
        run_dir.mkdir(parents=True)
        entry = self.catalog.get(record.command)
        try:
            if entry is None:
                raise HTTPException(404, f"{record.command} is no longer a command here.")
            argv, verdict = schedulable_argv(entry, record.subcommand, record.values, run_dir,
                                             self.settings.secret_env.values())
        except HTTPException as e:
            # Judged again at every firing: the engine may have changed since it was saved.
            self.refuse_run(record, run_id, str(e.detail))
            self.store.update_schedule(record.id, last_run_id=run_id, last_run_at=now)
            log.warning("Schedule %s (%s) was refused: %s", record.id, record.name, e.detail)
            return run_id
        self.start_run(entry, argv, verdict, record, run_id)
        self.store.update_schedule(record.id, last_run_id=run_id, last_run_at=now)
        return run_id

    def judge_finished(self) -> int:
        """Decide Drift for each finished Scheduled run not yet judged; returns how many
        found it."""
        found = 0
        for run_id in self.store.scheduled_runs_to_judge():
            run = self.store.record(run_id)
            entry = self.catalog.get(run.command) if run else None
            module = importlib.import_module(entry.module) if entry else None
            drift, summary = drift_of(module, run.exit_code, run.stderr or "") if run \
                else (False, None)
            if not self.store.record_drift(run_id, drift, summary) or not drift:
                continue
            found += 1
            schedule = self.store.schedule(run.schedule_id)
            self.notifier.drift_event(self.store.get(run_id),
                                      schedule.name if schedule else None)
        return found


def new_schedule(*, name: str, entry, subcommand, values: dict, timing: Timing, admin, role,
                 now: datetime) -> ScheduleRecord:
    return ScheduleRecord(
        id=str(uuid.uuid4()), name=name, command=entry.name, subcommand=subcommand,
        values=values, timing=timing.as_dict(), paused=False, created_by=admin.email,
        creator_name=admin.name, creator_role=role.label, created_at=now,
        next_run_at=timing.next_after(now))
