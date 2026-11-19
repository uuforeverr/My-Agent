from __future__ import annotations

import argparse
import csv
import json
import platform
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from channel.delivery import DeliveryReceipt
from scheduler.coordinator import APSchedulerCoordinator
from scheduler.executor import ExecutionResult, SchedulerExecutor
from scheduler.repository import SchedulerRepository
from state.database import StateDatabase


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _task(task_id: str, deadline: datetime, content: str) -> dict[str, Any]:
    now = _iso(datetime.now())
    return {
        "id": task_id,
        "name": task_id,
        "enabled": True,
        "status": "enabled",
        "created_at": now,
        "updated_at": now,
        "schedule": {"type": "once", "run_at": _iso(deadline)},
        "action": {
            "type": "send_message",
            "content": content,
            "target": {"channel_type": "dry-run", "user_id": "local"},
        },
        "timing_semantics": "notify_at",
        "next_run_at": _iso(deadline),
    }


def _interval_task(task_id: str, first_run: datetime, content: str, seconds: int = 3600) -> dict[str, Any]:
    task = _task(task_id, first_run, content)
    task["schedule"] = {"type": "interval", "seconds": seconds}
    return task


class DeliveryRecorder:
    def __init__(self):
        self._lock = Lock()
        self.events: list[dict[str, Any]] = []

    def send(self, _target, reply) -> DeliveryReceipt:
        with self._lock:
            self.events.append(
                {
                    "content": reply.content,
                    "observed_at": _iso(datetime.now()),
                    "monotonic": time.monotonic(),
                }
            )
            message_id = f"dry-{len(self.events)}"
        return DeliveryReceipt.success(message_id)

    def find(self, content: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.events if item["content"] == content]


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _coordinator(repo: SchedulerRepository, recorder: DeliveryRecorder) -> APSchedulerCoordinator:
    executor = SchedulerExecutor(send_reply=lambda _target, _reply: True)
    return APSchedulerCoordinator(
        repo,
        executor,
        recorder.send,
        max_workers=2,
        outbox_relay_interval_seconds=15,
        outbox_relay_batch_size=20,
        outbox_delivery_lease_seconds=120,
    )


def _occurrence_row(
    repo: SchedulerRepository,
    recorder: DeliveryRecorder,
    case_id: str,
    task_id: str,
    scheduled_for: datetime,
    content: str,
    scenario: str,
) -> dict[str, Any]:
    runs = repo.list_runs(task_id=task_id)
    deliveries = []
    for run in runs:
        deliveries.extend(repo.list_deliveries(run_id=run["id"]))
    observed = recorder.find(content)
    observed_at = datetime.fromisoformat(observed[0]["observed_at"]) if observed else None
    drift_ms = (
        round((observed_at - scheduled_for).total_seconds() * 1000, 3)
        if observed_at is not None
        else None
    )
    return {
        "case_id": case_id,
        "scenario": scenario,
        "task_id": task_id,
        "scheduled_for": _iso(scheduled_for),
        "delivery_observed_at": _iso(observed_at) if observed_at else "",
        "delivery_drift_ms": drift_ms,
        "run_count": len(runs),
        "delivery_count": len(deliveries),
        "observed_send_count": len(observed),
        "run_status": runs[0]["status"] if runs else "missing",
        "delivery_status": deliveries[0]["status"] if deliveries else "missing",
        "passed": bool(
            len(runs) == 1
            and len(deliveries) == 1
            and len(observed) == 1
            and runs[0]["status"] == "succeeded"
            and deliveries[0]["status"] == "delivered"
            and drift_ms is not None
            and drift_ms >= 0
        ),
    }


def run(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    scheduler_dir = output_dir / "scheduler"
    scheduler_dir.mkdir(parents=True, exist_ok=True)
    db_path = scheduler_dir / "dry-run-state.db"
    if db_path.exists():
        raise RuntimeError(f"refusing to overwrite existing dry-run database: {db_path}")
    repo = SchedulerRepository(StateDatabase(db_path))
    recorder = DeliveryRecorder()
    rows: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    # Three staggered tasks plus two tasks sharing one deadline.
    base = datetime.now() + timedelta(seconds=1.2)
    deadlines = [base, base + timedelta(seconds=0.45), base + timedelta(seconds=0.9)]
    shared = base + timedelta(seconds=1.35)
    schedule = [
        ("stagger-1", deadlines[0], "dry-stagger-1", "staggered"),
        ("stagger-2", deadlines[1], "dry-stagger-2", "staggered"),
        ("stagger-3", deadlines[2], "dry-stagger-3", "staggered"),
        ("same-1", shared, "dry-same-1", "same_deadline"),
        ("same-2", shared, "dry-same-2", "same_deadline"),
    ]
    for task_id, deadline, content, scenario in schedule:
        repo.add(_task(task_id, deadline, content))
        cases.append({"case_id": task_id, "scenario": scenario, "scheduled_for": _iso(deadline)})
    coordinator = _coordinator(repo, recorder)
    try:
        coordinator.start()
        _wait_until(lambda: len(recorder.events) >= 5, timeout=7)
    finally:
        coordinator.stop()
    for task_id, deadline, content, scenario in schedule:
        rows.append(_occurrence_row(repo, recorder, task_id, task_id, deadline, content, scenario))

    # Persist before deadline, stop the first process-equivalent coordinator,
    # then rebuild jobs from the same SQLite database.
    restart_deadline = datetime.now() + timedelta(seconds=1.5)
    repo.add(_task("restart-1", restart_deadline, "dry-restart-1"))
    first = _coordinator(repo, recorder)
    first.start()
    time.sleep(0.25)
    first.stop()
    second = _coordinator(repo, recorder)
    try:
        second.start()
        _wait_until(lambda: bool(recorder.find("dry-restart-1")), timeout=5)
    finally:
        second.stop()
    cases.append({"case_id": "restart-1", "scenario": "restart", "scheduled_for": _iso(restart_deadline)})
    rows.append(
        _occurrence_row(
            repo, recorder, "restart-1", "restart-1", restart_deadline,
            "dry-restart-1", "restart",
        )
    )

    # Seed an outbox row without direct submit; invoke the real Relay once.
    relay_task = _task("relay-1", datetime.now() + timedelta(days=1), "unused")
    relay_task.update({"enabled": False, "status": "disabled"})
    repo.add(relay_task)
    seeded_at = datetime.now()
    run_row, _ = repo.create_run(
        "relay-1", _iso(seeded_at), status="succeeded", deliver_at=_iso(seeded_at)
    )
    delivery, _ = repo.enqueue_delivery(
        run_row["id"],
        {"channel_type": "dry-run", "user_id": "local"},
        {"content": "dry-relay-1"},
    )
    relay_coordinator = _coordinator(repo, recorder)
    try:
        relay_stats = relay_coordinator.outbox_relay.run_once()
        _wait_until(lambda: bool(recorder.find("dry-relay-1")), timeout=3)
    finally:
        relay_coordinator.stop()
    relay_delivery = repo.get_delivery(delivery["id"]) or {}
    relay_observed = recorder.find("dry-relay-1")
    relay_row = {
        "case_id": "relay-1",
        "scenario": "relay_recovery",
        "task_id": "relay-1",
        "scheduled_for": _iso(seeded_at),
        "delivery_observed_at": relay_observed[0]["observed_at"] if relay_observed else "",
        "delivery_drift_ms": round(
            (datetime.fromisoformat(relay_observed[0]["observed_at"]) - seeded_at).total_seconds() * 1000,
            3,
        ) if relay_observed else None,
        "run_count": 1,
        "delivery_count": 1,
        "observed_send_count": len(relay_observed),
        "run_status": run_row["status"],
        "delivery_status": relay_delivery.get("status", "missing"),
        "passed": bool(
            relay_stats.get("submitted") == 1
            and relay_delivery.get("status") == "delivered"
            and len(relay_observed) == 1
        ),
        "relay_selected": relay_stats.get("selected"),
        "relay_submitted": relay_stats.get("submitted"),
    }
    cases.append({"case_id": "relay-1", "scenario": "relay_recovery", "seeded_at": _iso(seeded_at)})
    rows.append(relay_row)

    drifts = sorted(float(row["delivery_drift_ms"]) for row in rows if row["delivery_drift_ms"] is not None)
    p95 = drifts[max(0, int(len(drifts) * 0.95 + 0.999999) - 1)] if drifts else None
    summary = {
        "mode": "dry-run",
        "case_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "failed_count": sum(not bool(row["passed"]) for row in rows),
        "delivery_success_rate": round(sum(bool(row["passed"]) for row in rows) / len(rows), 4),
        "missing_delivery_count": sum(row["delivery_status"] != "delivered" for row in rows),
        "duplicate_delivery_count": sum(max(0, int(row["observed_send_count"]) - 1) for row in rows),
        "p95_delivery_drift_ms": p95,
        "max_delivery_drift_ms": max(drifts) if drifts else None,
        "relay": relay_stats,
        "all_passed": all(bool(row["passed"]) for row in rows),
        "claim_scope": "dry-run only; not resume evidence",
    }
    (scheduler_dir / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    with (scheduler_dir / "occurrences.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)
    (scheduler_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


class SlowExecutor(SchedulerExecutor):
    def run_to_result(self, task: dict[str, Any], **kwargs) -> ExecutionResult:
        content = str((task.get("action") or {}).get("content") or "")
        if content == "formal-slow-task":
            time.sleep(5)
        return super().run_to_result(task, **kwargs)


class FaultDeliveryRecorder(DeliveryRecorder):
    def __init__(self):
        super().__init__()
        self.attempts: dict[str, int] = {}

    def send(self, target, reply) -> DeliveryReceipt:
        content = reply.content
        self.attempts[content] = self.attempts.get(content, 0) + 1
        if content == "formal-retry" and self.attempts[content] < 3:
            return DeliveryReceipt.failed("injected retryable", retryable=True)
        if content == "formal-permanent":
            return DeliveryReceipt.failed("injected permanent", retryable=False)
        if content == "formal-unknown":
            return DeliveryReceipt.unknown("injected ambiguous")
        return super().send(target, reply)


def _seed_delivery(
    repo: SchedulerRepository,
    task_id: str,
    content: str,
    *,
    due_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    task = _task(task_id, datetime.now() + timedelta(days=1), "unused")
    task.update({"enabled": False, "status": "disabled"})
    repo.add(task)
    now = datetime.now()
    run, _ = repo.create_run(task_id, _iso(now), status="succeeded", deliver_at=_iso(now))
    delivery, _ = repo.enqueue_delivery(
        run["id"],
        {"channel_type": "formal", "user_id": "local"},
        {"content": content},
        next_attempt_at=_iso(due_at) if due_at else None,
    )
    return run, delivery


def run_formal(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    scheduler_dir = output_dir / "scheduler"
    scheduler_dir.mkdir(parents=True, exist_ok=True)
    db_path = scheduler_dir / "formal-state.db"
    if db_path.exists():
        raise RuntimeError(f"refusing to overwrite existing formal database: {db_path}")
    repo = SchedulerRepository(StateDatabase(db_path))
    recorder = DeliveryRecorder()
    rows: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    # S1: 20 personal-scale tasks distributed over a compressed 120-second day.
    rng = random.Random(20261119)
    base = datetime.now()
    offsets = sorted(rng.uniform(3.0, 120.0) for _ in range(20))
    precision_schedule = []
    for index, offset in enumerate(offsets, 1):
        task_id = f"daily-{index:02d}"
        deadline = base + timedelta(seconds=offset)
        content = f"formal-daily-{index:02d}"
        precision_schedule.append((task_id, deadline, content, "daily_20"))

    # S2: five reminders sharing a deadline, still a personal-assistant scale.
    shared_deadline = base + timedelta(seconds=60)
    concurrency_schedule = [
        (f"same-{index}", shared_deadline, f"formal-same-{index}", "same_deadline_5")
        for index in range(1, 6)
    ]
    active_schedule = precision_schedule + concurrency_schedule
    for task_id, deadline, content, scenario in active_schedule:
        repo.add(_task(task_id, deadline, content))
        cases.append({"case_id": task_id, "scenario": scenario, "scheduled_for": _iso(deadline)})

    coordinator = _coordinator(repo, recorder)
    try:
        coordinator.start()
        _wait_until(lambda: len(recorder.events) >= len(active_schedule), timeout=128)
    finally:
        coordinator.stop()
    for task_id, deadline, content, scenario in active_schedule:
        rows.append(_occurrence_row(repo, recorder, task_id, task_id, deadline, content, scenario))

    # S3: five once tasks and two interval tasks survive coordinator rebuild.
    restart_base = datetime.now() + timedelta(seconds=2.0)
    restart_schedule = []
    for index in range(1, 6):
        deadline = restart_base + timedelta(seconds=index * 0.12)
        restart_schedule.append((f"restart-once-{index}", deadline, f"formal-restart-once-{index}", "restart_once"))
    for index in range(1, 3):
        deadline = restart_base + timedelta(seconds=0.75 + index * 0.12)
        restart_schedule.append((f"restart-interval-{index}", deadline, f"formal-restart-interval-{index}", "restart_interval"))
    for task_id, deadline, content, scenario in restart_schedule:
        task = _task(task_id, deadline, content) if scenario == "restart_once" else _interval_task(task_id, deadline, content)
        repo.add(task)
        cases.append({"case_id": task_id, "scenario": scenario, "scheduled_for": _iso(deadline)})
    first = _coordinator(repo, recorder)
    first.start()
    time.sleep(0.35)
    first.stop()
    second = _coordinator(repo, recorder)
    try:
        second.start()
        _wait_until(
            lambda: all(recorder.find(content) for _, _, content, _ in restart_schedule),
            timeout=7,
        )
    finally:
        second.stop()
    for task_id, deadline, content, scenario in restart_schedule:
        rows.append(_occurrence_row(repo, recorder, task_id, task_id, deadline, content, scenario))

    # S4: stale once runs once; interval misses five occurrences but creates only the latest.
    stale_once = datetime.now() - timedelta(days=1)
    stale_interval = datetime.now().replace(microsecond=0) - timedelta(hours=5)
    repo.add(_task("latest-once", stale_once, "formal-latest-once"))
    repo.add(_interval_task("latest-interval", stale_interval, "formal-latest-interval"))
    latest_coordinator = _coordinator(repo, recorder)
    try:
        latest_coordinator.start()
        _wait_until(
            lambda: bool(recorder.find("formal-latest-once")) and bool(recorder.find("formal-latest-interval")),
            timeout=4,
        )
    finally:
        latest_coordinator.stop()
    for task_id, deadline, content in (
        ("latest-once", stale_once, "formal-latest-once"),
        ("latest-interval", stale_interval + timedelta(hours=5), "formal-latest-interval"),
    ):
        row = _occurrence_row(repo, recorder, task_id, task_id, deadline, content, "latest")
        task = repo.get(task_id) or {}
        row["next_run_is_future"] = (
            task_id == "latest-once"
            or bool(task.get("next_run_at") and datetime.fromisoformat(task["next_run_at"]) > datetime.now())
        )
        row["passed"] = bool(row["passed"] and row["next_run_is_future"])
        rows.append(row)
        cases.append({"case_id": task_id, "scenario": "latest", "persisted_next_run": _iso(stale_once if task_id == "latest-once" else stale_interval)})

    # S5: Relay recovery, retryable/permanent/ambiguous outcomes, lease recovery,
    # and deliver_by gating use real repository/coordinator state transitions.
    fault_recorder = FaultDeliveryRecorder()
    fault_coordinator = _coordinator(repo, fault_recorder)
    fault_rows: list[dict[str, Any]] = []
    try:
        _, relay_delivery = _seed_delivery(repo, "fault-relay", "formal-relay")
        relay_stats = fault_coordinator.outbox_relay.run_once()
        _wait_until(lambda: (repo.get_delivery(relay_delivery["id"]) or {}).get("status") == "delivered")
        relay_final = repo.get_delivery(relay_delivery["id"]) or {}
        fault_rows.append({
            "case_id": "fault-relay", "scenario": "relay_recovery",
            "passed": relay_stats["submitted"] >= 1 and relay_final.get("status") == "delivered",
            "status": relay_final.get("status"), "attempt": relay_final.get("attempt"),
        })

        _, retry_delivery = _seed_delivery(repo, "fault-retry", "formal-retry")
        for _ in range(3):
            fault_coordinator._deliver(retry_delivery["id"])
            current = repo.get_delivery(retry_delivery["id"]) or {}
            if current.get("status") == "retry_wait":
                repo.update_delivery(retry_delivery["id"], next_attempt_at=_iso(datetime.now() - timedelta(seconds=1)))
        retry_final = repo.get_delivery(retry_delivery["id"]) or {}
        fault_rows.append({
            "case_id": "fault-retry", "scenario": "retryable_failure",
            "passed": retry_final.get("status") == "delivered" and retry_final.get("attempt") == 3,
            "status": retry_final.get("status"), "attempt": retry_final.get("attempt"),
        })

        for task_id, content, expected in (
            ("fault-permanent", "formal-permanent", "failed"),
            ("fault-unknown", "formal-unknown", "unknown"),
        ):
            _, delivery = _seed_delivery(repo, task_id, content)
            fault_coordinator._deliver(delivery["id"])
            final = repo.get_delivery(delivery["id"]) or {}
            fault_rows.append({
                "case_id": task_id, "scenario": expected,
                "passed": final.get("status") == expected and final.get("attempt") == 1,
                "status": final.get("status"), "attempt": final.get("attempt"),
            })

        _, lease_delivery = _seed_delivery(repo, "fault-lease", "formal-lease")
        repo.claim_delivery(lease_delivery["id"])
        with repo.database.write() as conn:
            conn.execute(
                "UPDATE delivery_outbox SET started_at=? WHERE id=?",
                (_iso(datetime.now() - timedelta(seconds=121)), lease_delivery["id"]),
            )
        lease_stats = fault_coordinator.outbox_relay.run_once()
        _wait_until(lambda: (repo.get_delivery(lease_delivery["id"]) or {}).get("status") == "delivered")
        lease_final = repo.get_delivery(lease_delivery["id"]) or {}
        fault_rows.append({
            "case_id": "fault-lease", "scenario": "expired_lease",
            "passed": lease_stats["recovered"] >= 1 and lease_final.get("status") == "delivered",
            "status": lease_final.get("status"), "attempt": lease_final.get("attempt"),
        })

        future_due = datetime.now() + timedelta(minutes=5)
        _, future_delivery = _seed_delivery(repo, "fault-deliver-by", "formal-future", due_at=future_due)
        before_events = len(fault_recorder.events)
        future_stats = fault_coordinator.outbox_relay.run_once()
        future_final = repo.get_delivery(future_delivery["id"]) or {}
        fault_rows.append({
            "case_id": "fault-deliver-by", "scenario": "deliver_by_not_early",
            "passed": future_final.get("status") == "pending" and len(fault_recorder.events) == before_events,
            "status": future_final.get("status"), "attempt": future_final.get("attempt"),
            "relay_selected": future_stats["selected"],
        })
    finally:
        fault_coordinator.stop()

    # S6: one five-second task must not block three lightweight reminders.
    slow_recorder = DeliveryRecorder()
    slow_repo = SchedulerRepository(StateDatabase(scheduler_dir / "slow-task-state.db"))
    slow_base = datetime.now() + timedelta(seconds=1)
    slow_schedule = [("slow", slow_base, "formal-slow-task", "slow_task")]
    for index in range(1, 4):
        slow_schedule.append((
            f"light-{index}", slow_base + timedelta(seconds=index * 0.4),
            f"formal-light-{index}", "light_during_slow",
        ))
    for task_id, deadline, content, _scenario in slow_schedule:
        slow_repo.add(_task(task_id, deadline, content))
    slow_coordinator = APSchedulerCoordinator(
        slow_repo,
        SlowExecutor(send_reply=lambda _target, _reply: True),
        slow_recorder.send,
        max_workers=2,
    )
    try:
        slow_coordinator.start()
        _wait_until(lambda: len(slow_recorder.events) >= 4, timeout=9)
    finally:
        slow_coordinator.stop()
    slow_rows = [
        _occurrence_row(slow_repo, slow_recorder, task_id, task_id, deadline, content, scenario)
        for task_id, deadline, content, scenario in slow_schedule
    ]
    rows.extend(slow_rows)
    cases.extend({"case_id": task_id, "scenario": scenario, "scheduled_for": _iso(deadline)} for task_id, deadline, _content, scenario in slow_schedule)

    drifts = sorted(float(row["delivery_drift_ms"]) for row in rows if row.get("delivery_drift_ms") is not None)
    p95 = drifts[max(0, int(len(drifts) * 0.95 + 0.999999) - 1)] if drifts else None
    precision_drifts = sorted(float(row["delivery_drift_ms"]) for row in rows if row["scenario"] == "daily_20")
    precision_p95 = precision_drifts[max(0, int(len(precision_drifts) * 0.95 + 0.999999) - 1)]
    same_rows = [row for row in rows if row["scenario"] == "same_deadline_5"]
    same_times = [datetime.fromisoformat(row["delivery_observed_at"]) for row in same_rows if row["delivery_observed_at"]]
    light_drifts = sorted(float(row["delivery_drift_ms"]) for row in slow_rows if row["scenario"] == "light_during_slow")
    light_p95 = light_drifts[-1] if light_drifts else None
    all_case_passed = all(bool(row["passed"]) for row in rows) and all(bool(row["passed"]) for row in fault_rows)
    summary = {
        "mode": "formal",
        "case_count": len(rows) + len(fault_rows),
        "occurrence_case_count": len(rows),
        "fault_case_count": len(fault_rows),
        "passed_count": sum(bool(row["passed"]) for row in rows) + sum(bool(row["passed"]) for row in fault_rows),
        "failed_count": sum(not bool(row["passed"]) for row in rows) + sum(not bool(row["passed"]) for row in fault_rows),
        "daily_20": {
            "count": len(precision_drifts),
            "delivered": sum(row["delivery_status"] == "delivered" for row in rows if row["scenario"] == "daily_20"),
            "p95_delivery_drift_ms": precision_p95,
            "max_delivery_drift_ms": max(precision_drifts),
        },
        "same_deadline_5": {
            "delivered": sum(row["delivery_status"] == "delivered" for row in same_rows),
            "spread_ms": round((max(same_times) - min(same_times)).total_seconds() * 1000, 3) if len(same_times) == 5 else None,
        },
        "missing_delivery_count": sum(row["delivery_status"] != "delivered" for row in rows),
        "duplicate_delivery_count": sum(max(0, int(row["observed_send_count"]) - 1) for row in rows),
        "p95_delivery_drift_ms_all_occurrences": p95,
        "max_delivery_drift_ms_all_occurrences": max(drifts) if drifts else None,
        "light_task_p95_drift_ms": light_p95,
        "light_task_p95_increase_vs_daily_ms": round(light_p95 - precision_p95, 3) if light_p95 is not None else None,
        "fault_results": fault_rows,
        "all_passed": all_case_passed,
        "seed": 20261119,
        "compressed_window_seconds": 120,
    }
    (scheduler_dir / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    with (scheduler_dir / "occurrences.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)
    with (scheduler_dir / "faults.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in fault_rows for key in row}))
        writer.writeheader()
        writer.writerows(fault_rows)
    (scheduler_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--formal", action="store_true")
    args = parser.parse_args()
    summary = run(args.output_dir) if args.dry_run else run_formal(args.output_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
