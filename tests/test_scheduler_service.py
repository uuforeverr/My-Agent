from datetime import datetime, timedelta

from scheduler.service import SchedulerService
from scheduler.task_store import TaskStore


def _store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "scheduler" / "tasks.json")


def _base_task(store, schedule, next_run_at, action_content="hi"):
    return {
        "id": store.new_task_id(),
        "name": "t",
        "enabled": True,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "schedule": schedule,
        "action": {"type": "send_message", "content": action_content, "target": {"channel_type": "cli", "user_id": "local"}},
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_error": None,
        "last_error_at": None,
    }


def test_not_due_does_not_execute(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    future = (now + timedelta(minutes=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, future)
    store.add(task)

    fired = []
    service = SchedulerService(store, execute_callback=lambda t: fired.append(t) or True)
    service.check_and_execute(now=now)
    assert fired == []


def test_due_executes_send_message(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    store.add(task)

    fired = []
    service = SchedulerService(store, execute_callback=lambda t: fired.append(t["id"]) or True)
    service.check_and_execute(now=now)
    assert fired == [task["id"]]


def test_once_deleted_after_execution(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "once", "run_at": due}, due)
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: True)
    service.check_and_execute(now=now)
    assert store.get(task["id"]) is None


def test_interval_advances_next_run(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: True)
    service.check_and_execute(now=now)
    updated = store.get(task["id"])
    assert updated is not None
    next_run = datetime.fromisoformat(updated["next_run_at"])
    assert next_run == now + timedelta(seconds=3600)
    assert updated["last_run_at"] is not None


def test_failed_delivery_keeps_next_run(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: False)
    service.check_and_execute(now=now)
    updated = store.get(task["id"])
    assert updated["next_run_at"] == due  # unchanged, will retry


def test_exception_records_last_error_and_retries(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    store.add(task)

    def boom(_task):
        raise RuntimeError("channel down")

    service = SchedulerService(store, execute_callback=boom)
    service.check_and_execute(now=now)
    updated = store.get(task["id"])
    assert updated["next_run_at"] == due
    assert "channel down" in updated["last_error"]


def test_missing_next_run_is_computed_not_executed(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    task = _base_task(store, {"type": "interval", "seconds": 60}, None)
    store.add(task)

    fired = []
    service = SchedulerService(store, execute_callback=lambda t: fired.append(t) or True)
    service.check_and_execute(now=now)
    assert fired == []
    updated = store.get(task["id"])
    assert updated["next_run_at"] is not None


def test_expired_once_beyond_catchup_is_deleted(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    stale = (now - timedelta(hours=2)).isoformat()
    task = _base_task(store, {"type": "once", "run_at": stale}, stale)
    store.add(task)

    fired = []
    service = SchedulerService(store, catchup_seconds=600, execute_callback=lambda t: fired.append(t) or True)
    service.check_and_execute(now=now)
    assert fired == []
    assert store.get(task["id"]) is None


def test_expired_interval_beyond_catchup_is_rescheduled(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    stale = (now - timedelta(hours=2)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, stale)
    store.add(task)

    fired = []
    service = SchedulerService(store, catchup_seconds=600, execute_callback=lambda t: fired.append(t) or True)
    service.check_and_execute(now=now)
    assert fired == []
    updated = store.get(task["id"])
    next_run = datetime.fromisoformat(updated["next_run_at"])
    assert next_run == now + timedelta(seconds=3600)


def test_disabled_tasks_are_skipped(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 60}, due)
    task["enabled"] = False
    store.add(task)

    fired = []
    service = SchedulerService(store, execute_callback=lambda t: fired.append(t) or True)
    service.check_and_execute(now=now)
    assert fired == []
