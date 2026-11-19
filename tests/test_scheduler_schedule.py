from datetime import datetime, timedelta

import pytest

from scheduler.schedule import (
    ScheduleError,
    calculate_latest_due_and_next,
    calculate_next_run,
    parse_naive_local,
    parse_schedule,
)


def test_parse_once_relative_minutes():
    schedule = parse_schedule("once", "+10m")
    assert schedule["type"] == "once"
    run_at = datetime.fromisoformat(schedule["run_at"])
    delta = run_at - datetime.now()
    assert timedelta(minutes=9) < delta <= timedelta(minutes=10, seconds=1)


def test_parse_once_relative_units():
    now = datetime.now()
    for value, kwarg, amount in [("+30s", "seconds", 30), ("+2h", "hours", 2), ("+3d", "days", 3)]:
        run_at = datetime.fromisoformat(parse_schedule("once", value)["run_at"])
        expected = now + timedelta(**{kwarg: amount})
        assert abs((run_at - expected).total_seconds()) < 2


def test_parse_once_iso_absolute():
    schedule = parse_schedule("once", "2999-01-01T09:00:00")
    assert schedule == {"type": "once", "run_at": "2999-01-01T09:00:00"}


def test_parse_once_aware_iso_normalized_to_naive():
    schedule = parse_schedule("once", "2999-01-01T09:00:00+08:00")
    run_at = datetime.fromisoformat(schedule["run_at"])
    assert run_at.tzinfo is None


def test_parse_once_bad_value_raises():
    with pytest.raises(ScheduleError):
        parse_schedule("once", "later")


def test_parse_interval_seconds():
    assert parse_schedule("interval", "3600") == {"type": "interval", "seconds": 3600}


def test_parse_interval_non_positive_raises():
    with pytest.raises(ScheduleError):
        parse_schedule("interval", "0")
    with pytest.raises(ScheduleError):
        parse_schedule("interval", "-5")


def test_parse_interval_non_integer_raises():
    with pytest.raises(ScheduleError):
        parse_schedule("interval", "abc")


def test_parse_unknown_type_raises():
    with pytest.raises(ScheduleError):
        parse_schedule("yearly", "1")


def test_parse_empty_value_raises():
    with pytest.raises(ScheduleError):
        parse_schedule("interval", "  ")


def test_calculate_next_run_interval():
    base = datetime(2026, 1, 1, 12, 0, 0)
    nxt = calculate_next_run({"type": "interval", "seconds": 90}, from_time=base)
    assert nxt == base + timedelta(seconds=90)


def test_calculate_next_run_once_future_and_past():
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    assert calculate_next_run({"type": "once", "run_at": future}) is not None

    past = (datetime.now() - timedelta(hours=1)).isoformat()
    assert calculate_next_run({"type": "once", "run_at": past}) is None


def test_calculate_next_run_unknown_returns_none():
    assert calculate_next_run({"type": "bogus"}) is None
    assert calculate_next_run({}) is None


def test_parse_naive_local_strips_tzinfo():
    dt = parse_naive_local("2026-07-13T09:00:00+09:00")
    assert dt.tzinfo is None


def test_cron_parsing_and_next_run():
    pytest.importorskip("croniter")
    schedule = parse_schedule("cron", "0 9 * * *")
    assert schedule == {"type": "cron", "expression": "0 9 * * *"}

    base = datetime(2026, 1, 1, 8, 0, 0)
    nxt = calculate_next_run(schedule, from_time=base)
    assert nxt == datetime(2026, 1, 1, 9, 0, 0)


def test_cron_invalid_expression_raises():
    pytest.importorskip("croniter")
    with pytest.raises(ScheduleError):
        parse_schedule("cron", "not a cron")


def test_latest_once_executes_stale_occurrence_once():
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = now - timedelta(days=1)
    latest, next_future, count = calculate_latest_due_and_next(
        {"type": "once", "run_at": due.isoformat()},
        due,
        now=now,
    )
    assert latest == due
    assert next_future is None
    assert count == 1


def test_latest_interval_collapses_five_due_occurrences():
    persisted = datetime(2026, 1, 1, 8, 0, 0)
    now = datetime(2026, 1, 1, 12, 20, 0)
    latest, next_future, count = calculate_latest_due_and_next(
        {"type": "interval", "seconds": 3600},
        persisted,
        now=now,
    )
    assert latest == datetime(2026, 1, 1, 12, 0, 0)
    assert next_future == datetime(2026, 1, 1, 13, 0, 0)
    assert count == 5


def test_latest_future_occurrence_is_not_due():
    now = datetime(2026, 1, 1, 12, 0, 0)
    future = now + timedelta(minutes=5)
    latest, next_future, count = calculate_latest_due_and_next(
        {"type": "interval", "seconds": 300},
        future,
        now=now,
    )
    assert latest is None
    assert next_future == future
    assert count == 0


def test_latest_cron_collapses_to_most_recent_occurrence():
    pytest.importorskip("croniter")
    persisted = datetime(2026, 1, 1, 8, 0, 0)
    now = datetime(2026, 1, 1, 12, 20, 0)
    latest, next_future, count = calculate_latest_due_and_next(
        {"type": "cron", "expression": "0 * * * *"},
        persisted,
        now=now,
    )
    assert latest == datetime(2026, 1, 1, 12, 0, 0)
    assert next_future == datetime(2026, 1, 1, 13, 0, 0)
    assert count == 5
