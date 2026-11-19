from datetime import datetime, timedelta

from scripts.effect_validation.run_scheduler_resilience_benchmark import (
    BATCH_TASKS,
    MAX_DELIVERY_TARGET_MS,
    P95_DELIVERY_TARGET_MS,
    _task,
)


def test_simultaneous_matrix_has_twenty_representative_tasks():
    assert len(BATCH_TASKS) == 20
    assert {row[2] for row in BATCH_TASKS} == {"once", "interval", "cron"}
    assert {row[3] for row in BATCH_TASKS} == {"send_message", "agent_task", "tool_call"}
    assert len({row[0] for row in BATCH_TASKS}) == 20
    assert len({row[1] for row in BATCH_TASKS}) == 20


def test_task_persists_explicit_next_run_for_each_schedule_type():
    deadline = datetime.now() + timedelta(minutes=5)
    for schedule_type in ("once", "interval", "cron"):
        task, expected = _task(
            f"task-{schedule_type}",
            "test",
            deadline,
            schedule_type=schedule_type,
            action_type="send_message",
        )
        assert task["schedule"]["type"] == schedule_type
        assert datetime.fromisoformat(task["next_run_at"]) == deadline
        assert task["action"]["content"] == expected


def test_latency_acceptance_targets_are_fixed_before_formal_run():
    assert P95_DELIVERY_TARGET_MS == 2000.0
    assert MAX_DELIVERY_TARGET_MS == 3000.0
