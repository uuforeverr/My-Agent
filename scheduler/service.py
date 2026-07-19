import logging
import threading
from datetime import datetime
from typing import Any, Callable

from scheduler.schedule import calculate_next_run, parse_naive_local
from scheduler.task_store import TaskStore

logger = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = 30
DEFAULT_CATCHUP_SECONDS = 600  # execute tasks up to 10 min late; skip if older

# execute_callback(task) -> bool: True delivered, False needs retry (raising = retry)
ExecuteCallback = Callable[[dict[str, Any]], bool]


class SchedulerService:
    """Background clock: periodically scans tasks and fires the ones that are due.

    A single daemon thread wakes every ``scan_interval`` seconds. Due tasks are
    handed to ``execute_callback``; on success the next run time is advanced
    (once tasks are deleted), on failure the task is left untouched to retry.
    """

    def __init__(
        self,
        task_store: TaskStore,
        execute_callback: ExecuteCallback,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        catchup_seconds: int = DEFAULT_CATCHUP_SECONDS,
    ):
        self.task_store = task_store
        self.execute_callback = execute_callback
        self.scan_interval = max(int(scan_interval), 1)
        self.catchup_seconds = max(int(catchup_seconds), 0)
        self._running = False
        self._thread = None
        self._wake = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="scheduler-service",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.check_and_execute()
            except Exception as e:  # pragma: no cover - defensive loop guard
                logger.error("scheduler loop error: %s", e)
            self._wake.wait(self.scan_interval)

    def check_and_execute(self, now: datetime = None) -> None:
        now = now or datetime.now()
        for task in self.task_store.list(enabled_only=True):
            try:
                if not self._is_due(task, now):
                    continue
                if not self._execute(task):
                    # Delivery failed; keep next_run_at and retry next tick.
                    continue
                self._advance(task, now)
            except Exception as e:  # pragma: no cover - defensive per-task guard
                logger.error("scheduler task %s error: %s", task.get("id"), e)

    def _is_due(self, task: dict[str, Any], now: datetime) -> bool:
        next_run_str = task.get("next_run_at")
        if not next_run_str:
            next_run = calculate_next_run(task.get("schedule") or {}, from_time=now)
            if next_run is not None:
                self.task_store.update(task["id"], {"next_run_at": next_run.isoformat()})
            return False

        next_run = parse_naive_local(next_run_str)
        if next_run > now:
            return False

        late_seconds = (now - next_run).total_seconds()
        if late_seconds <= self.catchup_seconds:
            return True

        # Missed by more than the catch-up window (e.g. process was down).
        schedule_type = (task.get("schedule") or {}).get("type")
        if schedule_type == "once":
            self.task_store.delete(task["id"])
        else:
            rescheduled = calculate_next_run(task.get("schedule") or {}, from_time=now)
            if rescheduled is not None:
                self.task_store.update(task["id"], {"next_run_at": rescheduled.isoformat()})
        return False

    def _execute(self, task: dict[str, Any]) -> bool:
        try:
            result = self.execute_callback(task)
            return result is not False
        except Exception as e:
            logger.error("scheduler execution failed for %s: %s", task.get("id"), e)
            self.task_store.update(
                task["id"],
                {"last_error": str(e), "last_error_at": datetime.now().isoformat(timespec="seconds")},
            )
            return False

    def _advance(self, task: dict[str, Any], now: datetime) -> None:
        next_run = calculate_next_run(task.get("schedule") or {}, from_time=now)
        if next_run is None:
            self.task_store.delete(task["id"])
            return
        self.task_store.update(
            task["id"],
            {
                "next_run_at": next_run.isoformat(),
                "last_run_at": now.isoformat(timespec="seconds"),
                "last_error": None,
                "last_error_at": None,
            },
        )
