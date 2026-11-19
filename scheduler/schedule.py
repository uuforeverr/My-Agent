import re
from datetime import datetime, timedelta
from typing import Any, Optional

RELATIVE_ONCE_PATTERN = re.compile(r"^\+(\d+)([smhd])$")
_UNIT_TO_KWARG = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

SCHEDULE_TYPES = ("once", "interval", "cron")


class ScheduleError(ValueError):
    """Raised when a schedule_type/schedule_value pair cannot be parsed."""


def parse_naive_local(value: Any) -> datetime:
    """Normalize an ISO string or datetime into tz-naive local time.

    The whole scheduler compares against ``datetime.now()`` (tz-naive). Mixing
    aware and naive datetimes raises TypeError, so every persisted timestamp is
    funneled through here on the way back in.
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def parse_schedule(schedule_type: str, schedule_value: str) -> dict[str, Any]:
    """Parse a (type, value) pair into a persisted schedule dict.

    once     -> {"type": "once", "run_at": ISO}
    interval -> {"type": "interval", "seconds": N}
    cron     -> {"type": "cron", "expression": EXPR}
    """
    schedule_type = (schedule_type or "").strip()
    value = (schedule_value or "").strip()
    if schedule_type not in SCHEDULE_TYPES:
        raise ScheduleError(
            f"schedule_type must be one of {SCHEDULE_TYPES}, got {schedule_type!r}"
        )
    if not value:
        raise ScheduleError("schedule_value is required")

    if schedule_type == "once":
        return {"type": "once", "run_at": _parse_once(value).isoformat()}
    if schedule_type == "interval":
        return {"type": "interval", "seconds": _parse_interval_seconds(value)}
    return {"type": "cron", "expression": _validate_cron(value)}


def calculate_next_run(
    schedule: dict[str, Any],
    from_time: Optional[datetime] = None,
) -> Optional[datetime]:
    """Compute the next fire time at or after ``from_time``.

    Returns None to signal "no further runs" (a once task whose time has
    passed, or an unparseable schedule) — the service treats None as delete.
    """
    from_time = from_time or datetime.now()
    schedule_type = (schedule or {}).get("type")

    if schedule_type == "cron":
        expression = schedule.get("expression")
        if not expression:
            return None
        croniter = _load_croniter()
        return croniter(expression, from_time).get_next(datetime)

    if schedule_type == "interval":
        seconds = int(schedule.get("seconds") or 0)
        if seconds <= 0:
            return None
        return from_time + timedelta(seconds=seconds)

    if schedule_type == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        target = parse_naive_local(run_at)
        return target if target > from_time else None

    return None


def calculate_latest_due_and_next(
    schedule: dict[str, Any],
    persisted_next_run: Any,
    *,
    now: Optional[datetime] = None,
    count_cap: int = 1000,
) -> tuple[Optional[datetime], Optional[datetime], int]:
    """Collapse all due occurrences into the latest one.

    Returns ``(latest_due, next_future, missed_count)``.  ``latest_due`` is
    None when the persisted occurrence is still in the future.  The count is
    capped because it is diagnostic only and must never cause an unbounded
    catch-up loop.
    """
    now = now or datetime.now()
    persisted = parse_naive_local(persisted_next_run)
    if persisted > now:
        return None, persisted, 0

    schedule_type = (schedule or {}).get("type")
    if schedule_type == "once":
        return persisted, None, 1

    if schedule_type == "interval":
        seconds = int((schedule or {}).get("seconds") or 0)
        if seconds <= 0:
            return persisted, None, 1
        elapsed = max(0.0, (now - persisted).total_seconds())
        skipped = int(elapsed // seconds)
        latest = persisted + timedelta(seconds=skipped * seconds)
        return latest, latest + timedelta(seconds=seconds), min(skipped + 1, max(1, int(count_cap)))

    if schedule_type == "cron":
        expression = (schedule or {}).get("expression")
        if not expression:
            return persisted, None, 1
        croniter = _load_croniter()
        # Adding one microsecond makes an occurrence exactly at ``now`` count
        # as due; croniter.get_prev() is otherwise strictly-before.
        latest = croniter(expression, now + timedelta(microseconds=1)).get_prev(datetime)
        if latest < persisted:
            latest = persisted
        next_future = croniter(expression, now).get_next(datetime)

        cap = max(1, int(count_cap))
        count = 1
        cursor = persisted
        while count < cap and cursor < latest:
            candidate = croniter(expression, cursor).get_next(datetime)
            if candidate > latest:
                break
            count += 1
            cursor = candidate
        return latest, next_future, count

    return persisted, None, 1


def _parse_once(value: str) -> datetime:
    match = RELATIVE_ONCE_PATTERN.match(value)
    if match:
        amount = int(match.group(1))
        kwarg = _UNIT_TO_KWARG[match.group(2)]
        return datetime.now() + timedelta(**{kwarg: amount})
    try:
        return parse_naive_local(value)
    except ValueError as e:
        raise ScheduleError(
            f"once schedule_value must be +<n>[smhd] or ISO datetime, got {value!r}"
        ) from e


def _parse_interval_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as e:
        raise ScheduleError(f"interval schedule_value must be integer seconds, got {value!r}") from e
    if seconds <= 0:
        raise ScheduleError("interval seconds must be > 0")
    return seconds


def _validate_cron(expression: str) -> str:
    croniter = _load_croniter()
    try:
        croniter(expression)
    except Exception as e:
        raise ScheduleError(f"invalid cron expression {expression!r}: {e}") from e
    return expression


def _load_croniter():
    try:
        from croniter import croniter
    except ImportError as e:
        raise ScheduleError(
            "cron schedules require the 'croniter' package; run `pip install croniter`"
        ) from e
    return croniter
