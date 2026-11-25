"""The 90-day purge (#76, ADR 0013): Runs, their stored files and their records go once they
are 90 days old; the Ledger is kept. The service purges at startup and then once a day."""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import timedelta

PURGE_INTERVAL_SECONDS = 24 * 3600

log = logging.getLogger(__name__)


class Purger:
    def __init__(self, store, settings, clock):
        self.store = store
        self.settings = settings
        self.clock = clock
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def purge_now(self) -> int:
        """Purge what is due now; returns how many Runs went."""
        cutoff = self.clock() - timedelta(days=self.settings.retention_days)
        purged = self.store.purge(cutoff)
        for run_id in purged:
            shutil.rmtree(self.settings.run_root / run_id, ignore_errors=True)
        if purged:
            log.info("Purged %d Run(s) older than %d days", len(purged),
                     self.settings.retention_days)
        return len(purged)

    def start(self) -> None:
        self._safely_purge()
        self._thread = threading.Thread(target=self._loop, name="pd-ops-purge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stopping.wait(PURGE_INTERVAL_SECONDS):
            self._safely_purge()

    def _safely_purge(self) -> None:
        try:
            self.purge_now()
        except Exception:  # noqa: BLE001 - a failed purge is retried at the next one
            log.exception("The Run purge failed; it will be retried")
