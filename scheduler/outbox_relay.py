from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from scheduler.repository import SchedulerRepository


logger = logging.getLogger(__name__)

SubmitDelivery = Callable[[dict[str, Any]], None]
Clock = Callable[[], datetime]


class OutboxRelay:
    """Low-frequency safety net for durable delivery-outbox rows.

    Normal delivery still submits directly after the outbox transaction.  The
    relay recovers rows whose direct in-memory submit was lost, retryable rows
    whose deadline has arrived, and workers that died while holding a sending
    lease.
    """

    def __init__(
        self,
        repository: SchedulerRepository,
        submit_delivery: SubmitDelivery,
        *,
        interval_seconds: float = 15.0,
        batch_size: int = 20,
        lease_seconds: float = 120.0,
        clock: Optional[Clock] = None,
    ):
        self.repository = repository
        self.submit_delivery = submit_delivery
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.batch_size = max(1, int(batch_size))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.clock = clock or datetime.now
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="outbox-relay",
                daemon=True,
            )
            self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds + 1.0))

    def run_once(self) -> dict[str, int]:
        now = self.clock()
        recovered = self.repository.recover_expired_delivery_leases(
            now=now.isoformat(),
            lease_seconds=self.lease_seconds,
            limit=self.batch_size,
        )
        deliveries = self.repository.list_relayable_deliveries(
            now=now.isoformat(),
            limit=self.batch_size,
        )
        submitted = 0
        for delivery in deliveries:
            try:
                self.submit_delivery(delivery)
                submitted += 1
            except Exception:
                # The row remains durable and relayable for a later pass.
                logger.exception("failed to submit outbox delivery %s", delivery.get("id"))
        return {"recovered": recovered, "selected": len(deliveries), "submitted": submitted}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("outbox relay pass failed")
            if self._stop.wait(self.interval_seconds):
                break
