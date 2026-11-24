"""Where Runs execute: in the background, with one Writing run at a time (ADR 0012).

Reports and dry runs start at once, each on its own thread. Applies wait in the queue, which
is the store's "queued" rows, so it survives a restart; one writer thread takes them oldest
first. Inside that slot an apply first runs a fresh dry run and goes ahead only if it matches
the reviewed one (ADR 0011), so nothing else can write in between.

Every Run has a timeout and can be cancelled; either way it is recorded as Stopped.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import files as run_files
from .overrides import CapOverride, check_reviewed
from .review import MODE_FLAGS, input_hash, output_hash
from .runner import OUTCOMES, Result, kill_leftover, run_command
from .store import RUNNING, RunFile, RunRecord, RunStore, now

# An apply whose fresh dry run no longer matched the review: nothing was applied (#74).
CHANGED = "changed"
# Every outcome a finished Run can have: the command's own (from its exit code), or Changed.
FINAL_OUTCOMES = frozenset(OUTCOMES.values()) | {CHANGED}

IDLE_POLL_SECONDS = 1.0  # the writer also re-reads the queue this often
MAX_BACKOFF_SECONDS = 30.0  # longest wait between retries after a database error

log = logging.getLogger(__name__)


class InputChanged(Exception):
    pass


class Executor:
    def __init__(self, store: RunStore, settings, catalog):
        self.store = store
        self.settings = settings
        self.catalog = catalog
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition()
        self._stopping = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._writer: threading.Thread | None = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Record what a restart cut off as Stopped, make sure a crashed service left none
        of its processes running, then resume the queue."""
        for pid in self.store.stop_interrupted():
            kill_leftover(pid)
        self._writer = threading.Thread(target=self._write_loop, name="pd-ops-writer",
                                        daemon=True)
        self._writer.start()

    def stop(self) -> None:
        """Stop every executing Run (recorded as Stopped); queued applies stay queued."""
        self._stopping.set()
        with self._lock:
            for event in self._cancels.values():
                event.set()
            threads = list(self._threads)
        with self._wake:
            self._wake.notify_all()
        for thread in [*threads, self._writer]:
            if thread is not None:
                thread.join(timeout=30)

    # ---------- what the API calls ----------

    def submit(self, run_id: str) -> None:
        """Start a report or dry run now, on its own thread."""
        self._spawn(self._run_unqueued, run_id)

    def wake_writer(self) -> None:
        """An apply was queued: let the writer look at the queue now."""
        with self._wake:
            self._wake.notify_all()

    def cancel(self, run_id: str, email: str) -> bool:
        """Stop a Run: a queued one never starts; an executing one is killed. False if it
        had already finished (or was already being cancelled)."""
        if self.store.cancel_queued(run_id, email):
            return True
        if not self.store.request_cancel(run_id, email):
            return False
        with self._lock:
            event = self._cancels.get(run_id)
        if event is not None:
            event.set()
        return True

    # ---------- execution ----------

    def _spawn(self, target, run_id: str) -> None:
        def body():
            try:
                target(run_id)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=body, name=f"pd-ops-run-{run_id[:8]}", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def _cancel_event(self, run_id: str, event: threading.Event | None = None
                      ) -> threading.Event:
        """Register the Run's cancel signal (an apply's fresh dry run shares the apply's).
        A cancel recorded before it was registered still counts."""
        event = event or threading.Event()
        with self._lock:
            self._cancels[run_id] = event
        run = self.store.record(run_id)
        if self._stopping.is_set() or (run is not None and run.cancelled_by):
            event.set()
        return event

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._cancels.pop(run_id, None)

    def _run_unqueued(self, run_id: str) -> None:
        cancel = self._cancel_event(run_id)
        try:
            self._execute(run_id, cancel)
        except Exception as e:  # noqa: BLE001 - record it; the Run must not stay "running"
            self.store.finish(run_id, outcome="failed",
                              note=f"The service couldn't run this command: {e}")
        finally:
            self._forget(run_id)

    def _execute(self, run_id: str, cancel: threading.Event) -> Result:
        """Run one stored Run's command in its directory and record how it ended."""
        run = self.store.record(run_id)
        entry = self.catalog[run.command]
        run_dir = self.settings.run_root / run_id
        inputs = self.store.files(run_id, "input")
        result = run_command(entry, list(run.argv), cwd=run_dir,
                             extra_env=self.settings.extra_env,
                             secret_env=self.settings.secret_env,
                             timeout=run.timeout_seconds, cancel=cancel,
                             on_start=lambda pid: self.store.update(run_id, pid=pid),
                             on_log=lambda text: self.store.update_log(run_id, text))
        outputs = run_files.collect_outputs(run_dir, inputs)
        values = dict(exit_code=result.exit_code, outcome=result.outcome,
                      stdout=result.stdout, stderr=result.stderr,
                      output_hash=output_hash(result.stdout, result.stderr,
                                              [(f.path, f.sha256) for f in outputs]))
        if result.outcome == "stopped":
            values["note"] = self._stop_note(run_id, result)
        self.store.finish(run_id, outputs, **values)
        return result

    def _stop_note(self, run_id: str, result: Result) -> str:
        cancelled_by = self.store.record(run_id).cancelled_by
        if cancelled_by:
            return f"Stopped: cancelled by {cancelled_by}."
        if self._stopping.is_set():
            return "Stopped: the service was shutting down."
        return f"Stopped: {result.stop_reason or 'cancelled'}."

    # ---------- the one writing slot ----------

    def _write_loop(self) -> None:
        """Take queued applies one at a time. A database error is logged and retried with
        backoff, so the writer outlives a database outage."""
        failures = 0
        while not self._stopping.is_set():
            try:
                self._write_next()
                failures = 0
            except Exception:  # noqa: BLE001 - the one writer must keep going
                failures += 1
                delay = min(MAX_BACKOFF_SECONDS, 0.5 * 2 ** (failures - 1))
                log.exception("Writer: couldn't read the queue; retrying in %gs", delay)
                with self._wake:
                    if not self._stopping.is_set():
                        self._wake.wait(delay)

    def _write_next(self) -> None:
        """Execute the next queued apply, or wait a moment if there is none."""
        run_id = self.store.next_queued()
        if run_id is None:
            self._idle()
            return
        try:
            # Registered before the Run starts, so a stop() from here on reaches it.
            cancel = self._cancel_event(run_id)
            started = not self._stopping.is_set() and self.store.start(run_id)
        except BaseException:
            self._forget(run_id)
            raise
        if not started:
            self._forget(run_id)
            self._idle()
            return
        try:
            self._apply(run_id, cancel)
        except Exception as e:  # noqa: BLE001 - record it, free the dry run, carry on
            self.store.finish(run_id, outcome="failed",
                              note=f"The service couldn't run this apply: {e}. Nothing "
                                   "more will run for it; the dry run can be applied again.")
            self._release(run_id)
        finally:
            self._forget(run_id)

    def _idle(self) -> None:
        with self._wake:
            if not self._stopping.is_set():
                self._wake.wait(IDLE_POLL_SECONDS)

    def _release(self, apply_id: str) -> None:
        run = self.store.record(apply_id)
        if run is not None and run.applied_from:
            self.store.release_apply(run.applied_from, apply_id)

    def _apply(self, apply_id: str, cancel: threading.Event) -> None:
        """Inside the slot: a fresh dry run, then the apply only if it matches the review."""
        from .gate import gate_apply  # gate imports fastapi; keep this module light

        apply = self.store.record(apply_id)
        dry = self.store.record(apply.applied_from)
        entry = self.catalog[apply.command]
        dry_dir = self.settings.run_root / dry.id
        neutral = [a for a in dry.argv if a not in MODE_FLAGS]
        dry_argv, _, verdict = gate_apply(entry, neutral, dry_dir,
                                          self.settings.secret_env.values())
        check_reviewed(verdict, CapOverride.of(apply))  # cap defaults can change meanwhile

        fresh_id = str(uuid.uuid4())
        try:
            fresh_inputs = copy_inputs(dry_dir, self.settings.run_root / fresh_id,
                                       self.store.files(dry.id, "input"))
            verify_inputs(self.settings.run_root / apply_id, self.store.files(apply_id,
                                                                              "input"))
        except InputChanged as e:
            self._not_applied(apply_id, CHANGED, f"{e} Nothing was applied; run a new dry "
                                                    "run.")
            return
        self.store.save(new_run(fresh_id, entry.name, dry_argv, "dry-run",
                                Requester.of(apply), fresh_inputs,
                                timeout_seconds=apply.timeout_seconds,
                                override=CapOverride.of(apply)), fresh_inputs)
        self.store.update(apply_id, fresh_dry_run_id=fresh_id)
        self._cancel_event(fresh_id, cancel)  # cancelling the check cancels the apply
        try:
            fresh = self._execute(fresh_id, cancel)
        finally:
            self._forget(fresh_id)
        fresh_record = self.store.record(fresh_id)

        if fresh.outcome == "stopped" or cancel.is_set():
            cancelled_by = self.store.record(apply_id).cancelled_by or fresh_record.cancelled_by
            if cancelled_by is None and self._stopping.is_set():
                self.store.requeue(apply_id)  # nothing was written: it resumes after restart
                return
            if cancelled_by:
                self.store.update(apply_id, cancelled_by=cancelled_by)
                reason = f"cancelled by {cancelled_by}"
            else:
                reason = f"its fresh dry run was stopped ({fresh.stop_reason})"
            self._not_applied(apply_id, "stopped", f"Stopped: {reason}. Nothing was applied.")
        elif fresh.outcome != "succeeded":
            self._not_applied(apply_id, "failed", "The fresh dry run before the apply didn't "
                                                  "succeed, so nothing was applied. See it "
                                                  "for why.")
        elif fresh_record.output_hash != dry.output_hash:
            self._not_applied(apply_id, CHANGED, "What this would change has changed since "
                                                   "it was reviewed, so nothing was applied. "
                                                   "Review the fresh dry run.")
        else:
            self._execute(apply_id, cancel)

    def _not_applied(self, apply_id: str, outcome: str, note: str) -> None:
        self.store.finish(apply_id, outcome=outcome, note=note)
        if outcome != "stopped":  # a Stopped apply offers a fresh dry run instead
            self._release(apply_id)


@dataclass(frozen=True)
class Requester:
    """Who a Run is for, as recorded on it."""

    email: str
    name: str
    role: str

    @classmethod
    def of(cls, run: RunRecord) -> Requester:
        return cls(run.requested_by, run.requester_name, run.requester_role)


def new_run(run_id: str, command: str, argv: list[str], kind: str, requester: Requester,
            inputs, *, timeout_seconds: int, status: str = RUNNING,
            applied_from: str | None = None,
            override: CapOverride | None = None) -> RunRecord:
    """A Run about to execute (or, for an apply, to queue), with its cap override (#75)."""
    return RunRecord(
        **(override.columns() if override else {}),
        id=run_id, command=command, argv=argv, kind=kind, status=status,
        requested_by=requester.email, requester_name=requester.name,
        requester_role=requester.role, queued_at=now(),
        started_at=now() if status == RUNNING else None, timeout_seconds=timeout_seconds,
        applied_from=applied_from,
        input_hash=input_hash(command, argv, [(f.path, f.sha256) for f in inputs]))


def verify_inputs(run_dir: Path, inputs) -> None:
    for f in inputs:
        path = run_files.resolve(run_dir, f.path)
        if not path.is_file() or run_files.sha256_file(path) != f.sha256:
            raise InputChanged(f"The input {f.path} has changed since the dry run was "
                               "reviewed.")


def copy_inputs(source_dir: Path, target_dir: Path, inputs) -> list[RunFile]:
    """A Run's uploads, verified unchanged, copied into a new Run's directory."""
    verify_inputs(source_dir, inputs)
    target_dir.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        for f in inputs:
            source = run_files.resolve(source_dir, f.path)
            target = target_dir / source.relative_to(source_dir.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            records.append(RunFile(role="input", path=f.path, size=f.size, sha256=f.sha256))
    except BaseException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return records

