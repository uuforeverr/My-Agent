import threading
from datetime import datetime, timedelta

from scheduler.outbox_relay import OutboxRelay
from scheduler.repository import SchedulerRepository
from state.database import StateDatabase


def _repository(tmp_path):
    return SchedulerRepository(StateDatabase(tmp_path / "state.db"))


def _task(task_id="task-1"):
    now = datetime.now()
    return {
        "id": task_id,
        "name": "relay test",
        "enabled": True,
        "status": "enabled",
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "schedule": {"type": "once", "run_at": (now + timedelta(hours=1)).isoformat()},
        "action": {
            "type": "send_message",
            "content": "hello",
            "target": {"channel_type": "cli", "user_id": "local"},
        },
        "next_run_at": (now + timedelta(hours=1)).isoformat(),
    }


def _delivery(repository, *, task_id="task-1", scheduled_for=None, next_attempt_at=None):
    if repository.get(task_id) is None:
        repository.add(_task(task_id))
    scheduled_for = scheduled_for or datetime.now().isoformat()
    run, _ = repository.create_run(task_id, scheduled_for)
    delivery, _ = repository.enqueue_delivery(
        run["id"],
        {"channel_type": "cli", "user_id": "local"},
        {"content": scheduled_for},
        next_attempt_at=next_attempt_at,
    )
    return delivery


def test_run_once_submits_due_but_not_future_delivery(tmp_path):
    repository = _repository(tmp_path)
    now = datetime(2026, 11, 18, 12, 0, 0)
    due = _delivery(repository, scheduled_for="2026-11-18T11:59:00")
    future = _delivery(
        repository,
        scheduled_for="2026-11-18T12:01:00",
        next_attempt_at=(now + timedelta(minutes=1)).isoformat(),
    )
    submitted = []
    relay = OutboxRelay(repository, submitted.append, clock=lambda: now)

    stats = relay.run_once()

    assert stats == {"recovered": 0, "selected": 1, "submitted": 1}
    assert [item["id"] for item in submitted] == [due["id"]]
    assert future["id"] not in {item["id"] for item in submitted}


def test_relay_recovers_only_expired_sending_lease(tmp_path):
    repository = _repository(tmp_path)
    now = datetime(2026, 11, 18, 12, 0, 0)
    expired = _delivery(repository, scheduled_for="2026-11-18T11:58:00")
    active = _delivery(repository, scheduled_for="2026-11-18T11:59:00")
    assert repository.claim_delivery(expired["id"])
    assert repository.claim_delivery(active["id"])
    with repository.database.write() as conn:
        conn.execute(
            "UPDATE delivery_outbox SET started_at=? WHERE id=?",
            ((now - timedelta(seconds=121)).isoformat(), expired["id"]),
        )
        conn.execute(
            "UPDATE delivery_outbox SET started_at=? WHERE id=?",
            ((now - timedelta(seconds=119)).isoformat(), active["id"]),
        )
    submitted = []
    relay = OutboxRelay(
        repository,
        submitted.append,
        lease_seconds=120,
        clock=lambda: now,
    )

    stats = relay.run_once()

    assert stats == {"recovered": 1, "selected": 1, "submitted": 1}
    assert [item["id"] for item in submitted] == [expired["id"]]
    assert repository.get_delivery(expired["id"])["last_error"] == "delivery_lease_expired"
    assert repository.get_delivery(active["id"])["status"] == "sending"


def test_relay_respects_batch_size(tmp_path):
    repository = _repository(tmp_path)
    now = datetime(2026, 11, 18, 12, 0, 0)
    for index in range(25):
        _delivery(
            repository,
            task_id=f"task-{index}",
            scheduled_for=f"2026-11-18T11:59:{index:02d}",
        )
    submitted = []
    relay = OutboxRelay(repository, submitted.append, batch_size=20, clock=lambda: now)

    stats = relay.run_once()

    assert stats["selected"] == 20
    assert len(submitted) == 20


def test_atomic_claim_prevents_direct_and_relay_duplicate_send(tmp_path):
    repository = _repository(tmp_path)
    delivery = _delivery(repository)
    sends = []

    def submit(candidate):
        if repository.claim_delivery(candidate["id"]):
            sends.append(candidate["id"])

    # Simulate the normal direct submit winning immediately before the relay.
    submit(delivery)
    relay = OutboxRelay(repository, submit)
    stats = relay.run_once()

    assert sends == [delivery["id"]]
    assert stats["selected"] == 0


def test_terminal_delivery_states_are_not_relayed(tmp_path):
    repository = _repository(tmp_path)
    now = datetime(2026, 11, 18, 12, 0, 0)
    for index, status in enumerate(("delivered", "failed", "unknown")):
        delivery = _delivery(
            repository,
            task_id=f"task-{status}",
            scheduled_for=f"2026-11-18T11:59:0{index}",
        )
        repository.update_delivery(delivery["id"], status=status)
    submitted = []
    relay = OutboxRelay(repository, submitted.append, clock=lambda: now)

    stats = relay.run_once()

    assert stats["selected"] == 0
    assert submitted == []


def test_start_runs_immediate_first_pass_and_stop_is_idempotent(tmp_path):
    repository = _repository(tmp_path)
    delivery = _delivery(repository)
    observed = threading.Event()
    submitted = []

    def submit(candidate):
        submitted.append(candidate)
        observed.set()

    relay = OutboxRelay(repository, submit, interval_seconds=30)
    relay.start()
    try:
        assert observed.wait(1.0)
        assert [item["id"] for item in submitted] == [delivery["id"]]
    finally:
        relay.stop()
        relay.stop()
