"""Where Runs execute: in the background, with one Writing run at a time (ADR 0012).

Reports and dry runs start at once, each on its own thread. Applies wait in the queue, which
is the store's "queued" rows, so it survives a restart; one writer thread takes them oldest
first. Inside that slot an apply first runs a fresh dry run and goes ahead only if it matches
the reviewed one (ADR 0011), so nothing else can write in between.

Every Run has a timeout and can be cancelled; either way it is recorded as Stopped.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

from . import files as run_files
from .review import MODE_FLAGS, input_hash, output_hash
from .runner import Result, run_command
from .store import RUNNING, RunFile, RunRecord, RunStore, now

IDLE_POLL_SECONDS = 1.0  # the writer also re-reads the queue this often


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
        """Record what a restart cut off as Stopped, then resume the queue."""
        self.store.stop_interrupted()
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
        self._spawn(self._run_free, run_id)

    def enqueued(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def cancel(self, run_id: str, email: str) -> None:
        """Stop a Run: a queued one never starts; an executing one is killed."""
        if self.store.cancel_queued(run_id, email):
            return
        self.store.update(run_id, cancelled_by=email)
        with self._lock:
            event = self._cancels.get(run_id)
        if event is not None:
            event.set()

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

    def _cancel_event(self, run_id: str) -> threading.Event:
        """Register the Run's cancel signal. A cancel recorded before it was registered
        (between the Run starting and this) still counts."""
        event = threading.Event()
        with self._lock:
            self._cancels[run_id] = event
        run = self.store.record(run_id)
        if self._stopping.is_set() or (run is not None and run.cancelled_by):
            event.set()
        return event

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._cancels.pop(run_id, None)

    def _run_free(self, run_id: str) -> None:
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
                             timeout=run.timeout_seconds, cancel=cancel)
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
        while not self._stopping.is_set():
            run_id = self.store.next_queued()
            if run_id is None or not self.store.start(run_id):
                with self._wake:
                    if not self._stopping.is_set():
                        self._wake.wait(IDLE_POLL_SECONDS)
                continue
            cancel = self._cancel_event(run_id)
            try:
                self._apply(run_id, cancel)
            except Exception as e:  # noqa: BLE001 - record it, free the dry run, carry on
                self.store.finish(run_id, outcome="failed",
                                  note=f"The service couldn't run this apply: {e}. Nothing "
                                       "more will run for it; the dry run can be applied "
                                       "again.")
                self._release(run_id)
            finally:
                self._forget(run_id)

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
        dry_argv, _, _ = gate_apply(entry, neutral, dry_dir,
                                    self.settings.secret_env.values())

        fresh_id = str(uuid.uuid4())
        try:
            fresh_inputs = copy_inputs(dry_dir, self.settings.run_root / fresh_id,
                                       self.store.files(dry.id, "input"))
            verify_inputs(self.settings.run_root / apply_id, self.store.files(apply_id,
                                                                              "input"))
        except InputChanged as e:
            self._not_applied(apply_id, "changed", f"{e} Nothing was applied; run a new dry "
                                                    "run.")
            return
        self.store.save(new_run(fresh_id, entry.name, dry_argv, "dry-run", apply,
                                fresh_inputs), fresh_inputs)
        self.store.update(apply_id, fresh_dry_run_id=fresh_id)
        fresh = self._execute(fresh_id, cancel)
        fresh_record = self.store.record(fresh_id)

        if cancel.is_set():
            self._not_applied(apply_id, "stopped", self._stop_note(apply_id, fresh)
                              + " Nothing was applied.")
        elif fresh.outcome != "succeeded":
            self._not_applied(apply_id, "failed", "The fresh dry run before the apply didn't "
                                                  "succeed, so nothing was applied. See it "
                                                  "for why.")
        elif fresh_record.output_hash != dry.output_hash:
            self._not_applied(apply_id, "changed", "What this would change has changed since "
                                                   "it was reviewed, so nothing was applied. "
                                                   "Review the fresh dry run.")
        else:
            self._execute(apply_id, cancel)

    def _not_applied(self, apply_id: str, outcome: str, note: str) -> None:
        self.store.finish(apply_id, outcome=outcome, note=note)
        if outcome != "stopped":  # a Stopped apply offers a fresh dry run instead
            self._release(apply_id)


def new_run(run_id: str, command: str, argv: list[str], kind: str, requester, inputs,
            *, timeout_seconds: int | None = None, status: str = RUNNING,
            applied_from: str | None = None) -> RunRecord:
    """A Run about to execute (or queue), by the same admin as `requester`'s Run."""
    return RunRecord(
        id=run_id, command=command, argv=argv, kind=kind, status=status,
        requested_by=requester.requested_by, requester_name=requester.requester_name,
        requester_role=requester.requester_role, queued_at=now(),
        started_at=now() if status == RUNNING else None,
        timeout_seconds=timeout_seconds or requester.timeout_seconds,
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

