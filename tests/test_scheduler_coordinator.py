import time
from datetime import datetime, timedelta

from channel.delivery import DeliveryReceipt
from scheduler.coordinator import APSchedulerCoordinator
from scheduler.executor import ExecutionResult, SchedulerExecutor
from scheduler.repository import SchedulerRepository
from state.database import StateDatabase


class _Executor:
    def __init__(self, result=None):
        self.result = result or ExecutionResult("succeeded", content="validation output")

    def run_to_result(self, task, run_id="", **correlation):
        return self.result


def _repository(tmp_path):
    return SchedulerRepository(StateDatabase(tmp_path / "state.db"))


def _task(task_id="task-1", *, enabled=False, status="validating", semantics="deliver_by"):
    now = datetime.now()
    return {
        "id": task_id,
        "name": "test task",
        "enabled": enabled,
        "status": status,
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "schedule": {"type": "once", "run_at": (now + timedelta(minutes=10)).isoformat()},
        "action": {
            "type": "agent_task",
            "ai_task": "test",
            "target": {"channel_type": "cli", "user_id": "local", "session_id": "cli:local"},
        },
        "timing_semantics": semantics,
        "next_run_at": (now + timedelta(minutes=10)).isoformat(),
        "min_lead_seconds": 30,
        "max_lead_seconds": 3600,
    }


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_validation_enables_task_only_after_confirmed_delivery(tmp_path):
    repository = _repository(tmp_path)
    repository.add(_task())
    sent = []

    def send(target, reply):
        sent.append((target, reply.content))
        return DeliveryReceipt.success("provider-message-1")

    coordinator = APSchedulerCoordinator(repository, _Executor(), send)
    try:
        response = coordinator.validate_task("task-1")
        assert response["status"] == "started"
        assert _wait_until(lambda: repository.get("task-1")["status"] == "enabled")

        task = repository.get("task-1")
        runs = repository.list_runs(task_id="task-1")
        deliveries = repository.list_deliveries(run_id=runs[0]["id"])
        assert task["enabled"] is True
        assert task["lead_time_seconds"] >= 30
        assert runs[0]["run_kind"] == "validation"
        assert runs[0]["status"] == "succeeded"
        assert deliveries[0]["status"] == "delivered"
        assert deliveries[0]["provider_message_id"] == "provider-message-1"
        assert sent[0][0].channel_type == "cli"
        assert "定时任务测试成功" in sent[0][1]
    finally:
        coordinator.stop()
def test_validation_failure_keeps_task_disabled(tmp_path):
    repository = _repository(tmp_path)
    repository.add(_task())
    coordinator = APSchedulerCoordinator(
        repository,
        _Executor(ExecutionResult("failed", error="model unavailable")),
        lambda target, reply: DeliveryReceipt.success(),
    )
    try:
        coordinator.validate_task("task-1")
        assert _wait_until(lambda: repository.get("task-1")["status"] == "validation_failed")
        task = repository.get("task-1")
        assert task["enabled"] is False
        assert "model unavailable" in task["last_error"]
    finally:
        coordinator.stop()


def test_deliver_by_release_uses_run_specific_outbox_query(tmp_path):
    repository = _repository(tmp_path)
    repository.add(_task(enabled=True, status="enabled"))
    now = datetime.now().isoformat()
    run, _ = repository.create_run(
        "task-1",
        now,
        deliver_at=now,
        status="succeeded",
    )
    repository.enqueue_delivery(
        run["id"],
        {"channel_type": "cli", "user_id": "local"},
        {"content": "ready"},
        next_attempt_at=(datetime.now() + timedelta(microseconds=500)).isoformat(),
    )
    sent = []
    coordinator = APSchedulerCoordinator(
        repository,
        _Executor(),
        lambda target, reply: sent.append(reply.content) or DeliveryReceipt.success(),
    )
    try:
        coordinator._release_run(run["id"])
        assert _wait_until(lambda: sent == ["ready"])
    finally:
        coordinator.stop()


def test_retryable_delivery_waits_for_relay_instead_of_apscheduler_job(tmp_path):
    repository = _repository(tmp_path)
    repository.add(_task(enabled=True, status="enabled", semantics="notify_at"))
    now = datetime.now().isoformat()
    run, _ = repository.create_run("task-1", now, deliver_at=now, status="succeeded")
    delivery, _ = repository.enqueue_delivery(
        run["id"],
        {"channel_type": "cli", "user_id": "local"},
        {"content": "retry me"},
    )
    attempts = []

    def send(_target, reply):
        attempts.append(reply.content)
        if len(attempts) == 1:
            return DeliveryReceipt.failed("temporary", retryable=True)
        return DeliveryReceipt.success("provider-2")

    coordinator = APSchedulerCoordinator(repository, _Executor(), send)
    try:
        coordinator._deliver(delivery["id"])
        waiting = repository.get_delivery(delivery["id"])
        assert waiting["status"] == "retry_wait"
        assert waiting["next_attempt_at"] is not None
        assert coordinator.scheduler.get_job(f"delivery:{delivery['id']}") is None

        repository.update_delivery(
            delivery["id"],
            next_attempt_at=(datetime.now() - timedelta(seconds=1)).isoformat(),
        )
        coordinator.outbox_relay.run_once()
        assert _wait_until(lambda: repository.get_delivery(delivery["id"])["status"] == "delivered")
        assert attempts == ["retry me", "retry me"]
    finally:
        coordinator.stop()


def test_stale_interval_runs_only_latest_occurrence_and_advances_to_future(tmp_path):
    repository = _repository(tmp_path)
    now = datetime.now().replace(microsecond=0)
    stale = now - timedelta(days=5)
    task = _task(enabled=True, status="enabled", semantics="notify_at")
    task.update(
        {
            "schedule": {"type": "interval", "seconds": 86400},
            "next_run_at": stale.isoformat(),
        }
    )
    repository.add(task)
    sent = []
    coordinator = APSchedulerCoordinator(
        repository,
        _Executor(ExecutionResult("succeeded", content="latest result")),
        lambda _target, reply: sent.append(reply.content) or DeliveryReceipt.success(),
    )
    try:
        coordinator._fire_task("task-1", stale.isoformat())
        # A stale callback repeated after the task has advanced must not create
        # another occurrence.
        coordinator._fire_task("task-1", stale.isoformat())
        assert _wait_until(
            lambda: bool(repository.list_runs(task_id="task-1", statuses=("succeeded",)))
        )

        runs = repository.list_runs(task_id="task-1")
        updated = repository.get("task-1")
        assert len(runs) == 1
        assert datetime.fromisoformat(runs[0]["scheduled_for"]) == now
        assert datetime.fromisoformat(updated["last_run_at"]) == now
        assert datetime.fromisoformat(updated["next_run_at"]) == now + timedelta(days=1)
    finally:
        coordinator.stop()
