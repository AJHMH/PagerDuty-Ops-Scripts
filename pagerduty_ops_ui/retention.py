"""The purge (#76, ADR 0013): Runs, their stored files and their records go once they are
older than the retention period (90 days); the Ledger is kept. The service purges in the
background at startup and then once a day."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import timedelta

PURGE_INTERVAL_SECONDS = 24 * 3600
# A folder no Run owns is swept once it is this old: younger, it may be a Run being created.
ORPHAN_AGE_SECONDS = 24 * 3600

log = logging.getLogger(__name__)


class Purger:
    def __init__(self, store, settings, clock):
        self.store = store
        self.settings = settings
        self.clock = clock
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def purge_now(self) -> int:
        """Purge what is due now, and sweep folders no Run owns; returns how many Runs
        went."""
        days = self.settings.retention_days
        purged = self.store.purge(self.clock() - timedelta(days=days))
        for run_id in purged:
            self._remove(run_id)
        if purged:
            log.info("Purged %d Run(s) older than %d days", len(purged), days)
        self._sweep_orphans()
        return len(purged)

    def _remove(self, name: str) -> None:
        try:
            shutil.rmtree(self.settings.run_root / name)
        except FileNotFoundError:
            pass
        except OSError as e:  # e.g. a file held open; swept as an orphan next time
            log.warning("Could not delete the folder of Run %s: %s", name, e)

    def _sweep_orphans(self) -> None:
        """Folders left by a crash or a failed delete, that no Run owns any more."""
        root = self.settings.run_root
        if not root.is_dir():
            return
        owned = self.store.run_ids()
        old = time.time() - ORPHAN_AGE_SECONDS
        for folder in root.iterdir():
            try:
                orphaned = (folder.is_dir() and folder.name not in owned
                            and folder.stat().st_mtime < old)
            except OSError:
                continue
            if orphaned:
                self._remove(folder.name)

    def start(self) -> None:
        """Purge now and then daily, in the background, so a large first purge doesn't hold
        up startup."""
        self._thread = threading.Thread(target=self._loop, name="pd-ops-purge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while True:
            self._safely_purge()
            if self._stopping.wait(PURGE_INTERVAL_SECONDS):
                return

    def _safely_purge(self) -> None:
        try:
            self.purge_now()
        except Exception:  # noqa: BLE001 - a failed purge is retried at the next one
            log.exception("The Run purge failed; it will be retried")
