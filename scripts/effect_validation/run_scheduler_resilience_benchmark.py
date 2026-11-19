from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.run_context import RunContext
from channel.delivery import DeliveryReceipt
from scheduler.coordinator import APSchedulerCoordinator
from scheduler.executor import SchedulerExecutor
from scheduler.repository import SchedulerRepository
from state.database import StateDatabase
from tools.base import ToolResult


P95_DELIVERY_TARGET_MS = 2000.0
MAX_DELIVERY_TARGET_MS = 3000.0
RELAY_INTERVAL_SECONDS = 15.0


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int(len(ordered) * percentile + 0.999999) - 1)
    return round(ordered[index], 3)


def _target() -> dict[str, Any]:
    return {"channel_type": "formal", "user_id": "local"}


def _task(
    task_id: str,
    name: str,
    deadline: datetime,
    *,
    schedule_type: str,
    action_type: str,
) -> tuple[dict[str, Any], str]:
    if schedule_type == "once":
        schedule = {"type": "once", "run_at": _iso(deadline)}
    elif schedule_type == "interval":
        schedule = {"type": "interval", "seconds": 3600}
    elif schedule_type == "cron":
        schedule = {"type": "cron", "expression": "0 0 1 1 *"}
    else:
        raise ValueError(f"unsupported schedule type: {schedule_type}")

    expected_content = f"benchmark-result:{task_id}"
    if action_type == "send_message":
        action = {"type": "send_message", "content": expected_content, "target": _target()}
    elif action_type == "agent_task":
        action = {"type": "agent_task", "ai_task": name, "target": _target()}
    elif action_type == "tool_call":
        action = {
            "type": "tool_call",
            "tool_name": "benchmark_tool",
            "tool_arguments": {"task_id": task_id},
            "target": _target(),
        }
    else:
        raise ValueError(f"unsupported action type: {action_type}")

    now = _iso(datetime.now())
    return ({
        "id": task_id,
        "name": name,
        "enabled": True,
        "status": "enabled",
        "created_at": now,
        "updated_at": now,
        "schedule": schedule,
        "action": action,
        "timing_semantics": "notify_at",
        "next_run_at": _iso(deadline),
    }, expected_content)


class FileDeliveryRecorder:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(self, _target, reply) -> DeliveryReceipt:
        event = {
            "content": reply.content,
            "observed_at": _iso(datetime.now()),
            "pid": os.getpid(),
        }
        encoded = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return DeliveryReceipt.success(f"benchmark-{os.getpid()}-{time.time_ns()}")


class CrashAfterOutboxCoordinator(APSchedulerCoordinator):
    def __init__(self, *args, crash_marker: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.crash_marker = crash_marker

    def _submit_delivery(self, delivery: dict[str, Any]) -> None:
        self.crash_marker.write_text(
            json.dumps({
                "delivery_id": delivery.get("id"),
                "crashed_at": _iso(datetime.now()),
                "pid": os.getpid(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        os._exit(73)


def _executor(workspace: Path) -> SchedulerExecutor:
    def build_context(session_id: str, **metadata: Any) -> RunContext:
        return RunContext.create(session_id, workspace, **metadata)

    def run_agent(_message: str, *, context: RunContext):
        task_id = str(context.metadata.get("task_id") or "unknown")
        return f"benchmark-result:{task_id}", None

    def run_tool(_name: str, _arguments: dict[str, Any], context: RunContext):
        task_id = str(context.metadata.get("task_id") or "unknown")
        return ToolResult(True, f"benchmark-result:{task_id}")

    return SchedulerExecutor(
        send_reply=lambda _target, _reply: True,
        run_agent=run_agent,
        run_tool=run_tool,
        build_context=build_context,
    )


def _worker(
    db_path: Path,
    events_path: Path,
    ready_path: Path,
    *,
    crash_marker: Path | None = None,
) -> int:
    repository = SchedulerRepository(StateDatabase(db_path))
    recorder = FileDeliveryRecorder(events_path)
    kwargs = {
        "timezone": "Asia/Shanghai",
        "max_workers": 2,
        "outbox_relay_interval_seconds": RELAY_INTERVAL_SECONDS,
        "outbox_relay_batch_size": 20,
        "outbox_delivery_lease_seconds": 120.0,
    }
    if crash_marker is not None:
        coordinator = CrashAfterOutboxCoordinator(
            repository,
            _executor(db_path.parent / "workspace"),
            recorder.send,
            crash_marker=crash_marker,
            **kwargs,
        )
    else:
        coordinator = APSchedulerCoordinator(
            repository,
            _executor(db_path.parent / "workspace"),
            recorder.send,
            **kwargs,
        )
    coordinator.start()
    ready_path.write_text(
        json.dumps({"pid": os.getpid(), "ready_at": _iso(datetime.now())}),
        encoding="utf-8",
    )
    try:
        while True:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.stop()
    return 0


def _launch_worker(
    db_path: Path,
    events_path: Path,
    ready_path: Path,
    log_path: Path,
    *,
    crash_marker: Path | None = None,
) -> subprocess.Popen:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--db-path", str(db_path),
        "--events-path", str(events_path),
        "--ready-path", str(ready_path),
    ]
    if crash_marker is not None:
        command.extend(["--crash-marker", str(crash_marker)])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_handle.close()
    return process


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _event_matches(path: Path, contents: set[str]) -> list[dict[str, Any]]:
    return [event for event in _events(path) if event.get("content") in contents]


def _task_result(
    repository: SchedulerRepository,
    events_path: Path,
    task: dict[str, Any],
    expected_content: str,
    scenario: str,
) -> dict[str, Any]:
    runs = repository.list_runs(task_id=task["id"])
    deliveries = []
    for run in runs:
        deliveries.extend(repository.list_deliveries(run_id=run["id"]))
    matched = [event for event in _events(events_path) if event.get("content") == expected_content]
    observed_at = datetime.fromisoformat(matched[0]["observed_at"]) if matched else None
    scheduled_for = datetime.fromisoformat(task["next_run_at"])
    drift_ms = (
        round((observed_at - scheduled_for).total_seconds() * 1000, 3)
        if observed_at is not None else None
    )
    passed = bool(
        len(runs) == 1
        and len(deliveries) == 1
        and len(matched) == 1
        and runs[0].get("status") == "succeeded"
        and deliveries[0].get("status") == "delivered"
        and drift_ms is not None
        and drift_ms >= 0
    )
    return {
        "scenario": scenario,
        "task_id": task["id"],
        "task_name": task["name"],
        "schedule_type": task["schedule"]["type"],
        "action_type": task["action"]["type"],
        "scheduled_for": task["next_run_at"],
        "delivered_at": matched[0]["observed_at"] if matched else "",
        "delivery_drift_ms": drift_ms,
        "run_count": len(runs),
        "delivery_count": len(deliveries),
        "observed_result_count": len(matched),
        "run_status": runs[0].get("status") if runs else "missing",
        "delivery_status": deliveries[0].get("status") if deliveries else "missing",
        "passed": passed,
    }


def _restart_before_deadline(result_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_dir = result_dir / "restart-before-deadline"
    case_dir.mkdir(parents=True, exist_ok=True)
    db_path = case_dir / "state.db"
    events_path = case_dir / "deliveries.jsonl"
    if db_path.exists():
        raise RuntimeError(f"refusing to overwrite existing database: {db_path}")
    repository = SchedulerRepository(StateDatabase(db_path))
    base = datetime.now() + timedelta(seconds=8)
    specs = [
        ("restart-once-1", "会议提醒", "once"),
        ("restart-once-2", "服药提醒", "once"),
        ("restart-interval-1", "周期喝水提醒", "interval"),
        ("restart-interval-2", "周期备份提醒", "interval"),
        ("restart-cron-1", "每日摘要任务", "cron"),
        ("restart-cron-2", "定期账单提醒", "cron"),
    ]
    tasks: list[tuple[dict[str, Any], str]] = []
    for index, (task_id, name, schedule_type) in enumerate(specs):
        task, expected = _task(
            task_id,
            name,
            base + timedelta(milliseconds=index * 120),
            schedule_type=schedule_type,
            action_type="send_message",
        )
        repository.add(task)
        tasks.append((task, expected))

    ready_one = case_dir / "worker-1.ready.json"
    first = _launch_worker(db_path, events_path, ready_one, case_dir / "worker.log")
    if not _wait_until(ready_one.exists, timeout=5):
        _terminate(first)
        raise RuntimeError("first scheduler process did not become ready")
    time.sleep(1.0)
    killed_at = datetime.now()
    _terminate(first)
    ready_two = case_dir / "worker-2.ready.json"
    second = _launch_worker(db_path, events_path, ready_two, case_dir / "worker.log")
    if not _wait_until(ready_two.exists, timeout=5):
        _terminate(second)
        raise RuntimeError("restarted scheduler process did not become ready")
    expected_contents = {expected for _task_row, expected in tasks}
    delivered = _wait_until(
        lambda: len(_event_matches(events_path, expected_contents)) >= len(tasks),
        timeout=15,
    )
    _terminate(second)
    rows = [
        _task_result(repository, events_path, task, expected, "process_restart")
        for task, expected in tasks
    ]
    drifts = [float(row["delivery_drift_ms"]) for row in rows if row["delivery_drift_ms"] is not None]
    return ({
        "case": "restart_before_deadline",
        "task_count": len(tasks),
        "first_process_exit_code": first.returncode,
        "killed_at": _iso(killed_at),
        "restarted_at": json.loads(ready_two.read_text(encoding="utf-8"))["ready_at"],
        "all_results_observed": delivered,
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "p95_delivery_drift_ms": _percentile_nearest_rank(drifts, 0.95),
        "max_delivery_drift_ms": round(max(drifts), 3) if drifts else None,
        "all_passed": all(bool(row["passed"]) for row in rows),
    }, rows)


def _outbox_after_crash(result_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_dir = result_dir / "outbox-after-crash"
    case_dir.mkdir(parents=True, exist_ok=True)
    db_path = case_dir / "state.db"
    events_path = case_dir / "deliveries.jsonl"
    crash_marker = case_dir / "crash-marker.json"
    if db_path.exists():
        raise RuntimeError(f"refusing to overwrite existing database: {db_path}")
    repository = SchedulerRepository(StateDatabase(db_path))
    deadline = datetime.now() + timedelta(seconds=4)
    task, expected = _task(
        "crash-after-outbox",
        "结果落库后崩溃",
        deadline,
        schedule_type="once",
        action_type="send_message",
    )
    repository.add(task)
    ready_one = case_dir / "worker-1.ready.json"
    first = _launch_worker(
        db_path,
        events_path,
        ready_one,
        case_dir / "worker.log",
        crash_marker=crash_marker,
    )
    if not _wait_until(ready_one.exists, timeout=5):
        _terminate(first)
        raise RuntimeError("crash worker did not become ready")
    if not _wait_until(lambda: crash_marker.exists() and first.poll() is not None, timeout=10):
        _terminate(first)
        raise RuntimeError("worker did not crash after persisting the outbox row")

    runs_before = repository.list_runs(task_id=task["id"])
    deliveries_before = repository.list_deliveries(run_id=runs_before[0]["id"]) if runs_before else []
    persisted_before_restart = bool(
        len(runs_before) == 1
        and runs_before[0].get("status") == "succeeded"
        and len(deliveries_before) == 1
        and deliveries_before[0].get("status") == "pending"
    )
    restarted_at = datetime.now()
    ready_two = case_dir / "worker-2.ready.json"
    second = _launch_worker(db_path, events_path, ready_two, case_dir / "worker.log")
    if not _wait_until(ready_two.exists, timeout=5):
        _terminate(second)
        raise RuntimeError("recovery worker did not become ready")
    recovered = _wait_until(
        lambda: len(_event_matches(events_path, {expected})) == 1,
        timeout=RELAY_INTERVAL_SECONDS + 3,
    )
    _terminate(second)
    rows = [_task_result(repository, events_path, task, expected, "outbox_recovery")]
    observed = _event_matches(events_path, {expected})
    recovery_ms = (
        round((datetime.fromisoformat(observed[0]["observed_at"]) - restarted_at).total_seconds() * 1000, 3)
        if observed else None
    )
    summary = {
        "case": "outbox_after_crash",
        "crash_exit_code": first.returncode,
        "persisted_before_restart": persisted_before_restart,
        "result_observed_after_restart": recovered,
        "recovery_delivery_ms": recovery_ms,
        "observed_result_count": len(observed),
        "all_passed": bool(persisted_before_restart and recovered and rows[0]["passed"]),
    }
    return summary, rows


BATCH_TASKS = [
    ("batch-01", "会议提醒", "once", "send_message"),
    ("batch-02", "服药提醒", "once", "send_message"),
    ("batch-03", "账单到期提醒", "once", "send_message"),
    ("batch-04", "接送提醒", "once", "send_message"),
    ("batch-05", "专注时间提醒", "once", "send_message"),
    ("batch-06", "喝水提醒", "interval", "send_message"),
    ("batch-07", "运动提醒", "interval", "send_message"),
    ("batch-08", "通勤提醒", "cron", "send_message"),
    ("batch-09", "邮件摘要", "once", "agent_task"),
    ("batch-10", "今日任务摘要", "once", "agent_task"),
    ("batch-11", "日程回顾", "interval", "agent_task"),
    ("batch-12", "知识摘要", "interval", "agent_task"),
    ("batch-13", "周计划生成", "cron", "agent_task"),
    ("batch-14", "消费回顾", "cron", "agent_task"),
    ("batch-15", "睡前总结", "cron", "agent_task"),
    ("batch-16", "文件备份", "once", "tool_call"),
    ("batch-17", "报告生成", "interval", "tool_call"),
    ("batch-18", "工作区整理", "interval", "tool_call"),
    ("batch-19", "数据导出", "cron", "tool_call"),
    ("batch-20", "状态检查", "cron", "tool_call"),
]


def _simultaneous_twenty(result_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_dir = result_dir / "simultaneous-20"
    case_dir.mkdir(parents=True, exist_ok=True)
    db_path = case_dir / "state.db"
    events_path = case_dir / "deliveries.jsonl"
    if db_path.exists():
        raise RuntimeError(f"refusing to overwrite existing database: {db_path}")
    repository = SchedulerRepository(StateDatabase(db_path))
    deadline = datetime.now() + timedelta(seconds=5)
    tasks: list[tuple[dict[str, Any], str]] = []
    for task_id, name, schedule_type, action_type in BATCH_TASKS:
        task, expected = _task(
            task_id,
            name,
            deadline,
            schedule_type=schedule_type,
            action_type=action_type,
        )
        repository.add(task)
        tasks.append((task, expected))
    ready = case_dir / "worker.ready.json"
    worker = _launch_worker(db_path, events_path, ready, case_dir / "worker.log")
    if not _wait_until(ready.exists, timeout=5):
        _terminate(worker)
        raise RuntimeError("batch worker did not become ready")
    expected_contents = {expected for _task_row, expected in tasks}
    all_observed = _wait_until(
        lambda: len(_event_matches(events_path, expected_contents)) >= len(tasks),
        timeout=12,
    )
    _terminate(worker)
    rows = [
        _task_result(repository, events_path, task, expected, "simultaneous_20")
        for task, expected in tasks
    ]
    drifts = [float(row["delivery_drift_ms"]) for row in rows if row["delivery_drift_ms"] is not None]
    delivered_times = [
        datetime.fromisoformat(row["delivered_at"])
        for row in rows if row["delivered_at"]
    ]
    p95 = _percentile_nearest_rank(drifts, 0.95)
    maximum = round(max(drifts), 3) if drifts else None
    spread = (
        round((max(delivered_times) - min(delivered_times)).total_seconds() * 1000, 3)
        if len(delivered_times) == len(tasks) else None
    )
    summary = {
        "case": "simultaneous_twenty",
        "task_count": len(tasks),
        "schedule_types": sorted({task["schedule"]["type"] for task, _ in tasks}),
        "action_types": sorted({task["action"]["type"] for task, _ in tasks}),
        "all_results_observed": all_observed,
        "delivered_count": sum(row["delivery_status"] == "delivered" for row in rows),
        "missing_delivery_count": sum(row["delivery_status"] != "delivered" for row in rows),
        "duplicate_result_count": sum(max(0, int(row["observed_result_count"]) - 1) for row in rows),
        "p95_delivery_drift_ms": p95,
        "max_delivery_drift_ms": maximum,
        "delivery_spread_ms": spread,
        "p95_target_ms": P95_DELIVERY_TARGET_MS,
        "max_target_ms": MAX_DELIVERY_TARGET_MS,
        "all_passed": bool(
            all_observed
            and all(bool(row["passed"]) for row in rows)
            and p95 is not None and p95 <= P95_DELIVERY_TARGET_MS
            and maximum is not None and maximum <= MAX_DELIVERY_TARGET_MS
        ),
    }
    return summary, rows


def run(output_dir: Path) -> dict[str, Any]:
    result_dir = output_dir.resolve() / "scheduler-resilience"
    result_dir.mkdir(parents=True, exist_ok=True)
    summary_path = result_dir / "summary.json"
    if summary_path.exists():
        raise RuntimeError(f"refusing to overwrite existing results: {summary_path}")

    restart_summary, restart_rows = _restart_before_deadline(result_dir)
    outbox_summary, outbox_rows = _outbox_after_crash(result_dir)
    batch_summary, batch_rows = _simultaneous_twenty(result_dir)
    rows = restart_rows + outbox_rows + batch_rows
    with (result_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "mode": "formal",
        "generated_at": _iso(datetime.now()),
        "restart_before_deadline": restart_summary,
        "outbox_after_crash": outbox_summary,
        "simultaneous_20": batch_summary,
        "case_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "all_passed": bool(
            restart_summary["all_passed"]
            and outbox_summary["all_passed"]
            and batch_summary["all_passed"]
        ),
        "scope_note": (
            "External LLM and tool bodies use deterministic stand-ins; APScheduler, separate-process "
            "restart, SQLite state, execution pools, durable outbox and delivery workers are real."
        ),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--ready-path", type=Path)
    parser.add_argument("--crash-marker", type=Path)
    args = parser.parse_args()
    if args.worker:
        if not args.db_path or not args.events_path or not args.ready_path:
            parser.error("--worker requires --db-path, --events-path and --ready-path")
        return _worker(
            args.db_path.resolve(),
            args.events_path.resolve(),
            args.ready_path.resolve(),
            crash_marker=args.crash_marker.resolve() if args.crash_marker else None,
        )
    if not args.output_dir:
        parser.error("--output-dir is required")
    summary = run(args.output_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
