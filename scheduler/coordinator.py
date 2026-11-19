from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from channel.delivery import DeliveryReceipt
from channel.reply import Reply
from channel.target import ChannelTarget
from scheduler.executor import ExecutionResult, SchedulerExecutor, WAITING_APPROVAL
from scheduler.lead_time import calculate_lead_time
from scheduler.outbox_relay import OutboxRelay
from scheduler.repository import SchedulerRepository
from scheduler.schedule import calculate_latest_due_and_next, calculate_next_run, parse_naive_local


logger = logging.getLogger(__name__)

SendWithReceipt = Callable[[ChannelTarget, Reply], DeliveryReceipt]


class APSchedulerCoordinator:
    """Deadline-driven, SQLite-backed scheduler for the local agent.

    APScheduler owns only in-memory timers. SQLite remains the source of truth
    for schedules, occurrences, results and delivery outcomes.
    """

    def __init__(
        self,
        repository: SchedulerRepository,
        executor: SchedulerExecutor,
        send_with_receipt: SendWithReceipt,
        *,
        timezone: str = "Asia/Shanghai",
        max_workers: int = 2,
        outbox_relay_interval_seconds: float = 15.0,
        outbox_relay_batch_size: int = 20,
        outbox_delivery_lease_seconds: float = 120.0,
    ):
        self.repository = repository
        self.executor = executor
        self.send_with_receipt = send_with_receipt
        self.timezone = timezone
        self.scheduler = BackgroundScheduler(
            timezone=timezone,
            executors={"default": APSchedulerThreadPoolExecutor(max_workers=2)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
        )
        self.execution_pool = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="scheduled-execution",
        )
        self.delivery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scheduled-delivery")
        self.outbox_relay = OutboxRelay(
            repository,
            self._submit_delivery,
            interval_seconds=outbox_relay_interval_seconds,
            batch_size=outbox_relay_batch_size,
            lease_seconds=outbox_delivery_lease_seconds,
        )
        self._active_tasks: set[str] = set()
        self._active_lock = threading.RLock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.repository.mark_interrupted_runs()
        self.scheduler.start(paused=True)
        for task in self.repository.list(enabled_only=True):
            self.sync_task(task["id"])
        self.outbox_relay.start()
        self.scheduler.resume()
        self._started = True

    def stop(self, wait: bool = True) -> None:
        self.outbox_relay.stop(wait=wait)
        if self._started:
            self.scheduler.shutdown(wait=wait)
            self._started = False
        self.execution_pool.shutdown(wait=wait)
        self.delivery_pool.shutdown(wait=wait)

    def sync_task(self, task_id: str) -> None:
        self._remove_job(self._task_job_id(task_id))
        task = self.repository.get(task_id)
        if not task or not task.get("enabled") or task.get("status") != "enabled":
            return
        next_run_text = task.get("next_run_at")
        if not next_run_text:
            next_run = calculate_next_run(task.get("schedule") or {}, from_time=datetime.now())
            if next_run is None:
                return
            next_run_text = next_run.isoformat()
            task = self.repository.update(task_id, {"next_run_at": next_run_text})
        scheduled_for = parse_naive_local(next_run_text)
        latest_due, _next_future, _due_count = calculate_latest_due_and_next(
            task.get("schedule") or {},
            scheduled_for,
            now=datetime.now(),
        )
        if latest_due is not None and latest_due != scheduled_for:
            scheduled_for = latest_due
            next_run_text = latest_due.isoformat()
            task = self.repository.update(task_id, {"next_run_at": next_run_text})
        wake_at = scheduled_for
        if task.get("timing_semantics") == "deliver_by":
            lead = self._lead_time(task)
            wake_at = scheduled_for - timedelta(seconds=lead)
            task = self.repository.update(
                task_id,
                {
                    "lead_time_seconds": lead,
                    "next_prepare_at": wake_at.isoformat(),
                },
            )
        else:
            if task.get("next_prepare_at"):
                self.repository.update(task_id, {"next_prepare_at": None})
        run_at = max(wake_at, datetime.now())
        self.scheduler.add_job(
            self._fire_task,
            trigger="date",
            run_date=run_at,
            id=self._task_job_id(task_id),
            replace_existing=True,
            kwargs={"task_id": task_id, "scheduled_for": next_run_text},
        )

    def remove_task(self, task_id: str) -> None:
        self._remove_job(self._task_job_id(task_id))

    def trigger_now(self, task_id: str, *, run_kind: str = "manual") -> dict[str, Any]:
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        scheduled_for = datetime.now().isoformat()
        run, created = self.repository.create_run(
            task_id,
            scheduled_for,
            run_kind=run_kind,
            deliver_at=scheduled_for,
        )
        if created:
            self._submit_run(task, run)
        return run

    def validate_task(self, task_id: str) -> dict[str, Any]:
        """Start the one-time execution and delivery check for a new task."""
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        now = datetime.now()
        scheduled_for = now.isoformat()
        run, created = self.repository.create_run(
            task_id,
            scheduled_for,
            run_kind="validation",
            prepare_at=scheduled_for,
            deliver_at=scheduled_for,
        )
        if created:
            self.repository.update(
                task_id,
                {"enabled": False, "status": "validating", "last_error": None, "last_error_at": None},
            )
            self._submit_run(task, run)
        estimate_seconds = self._lead_time(task)
        return {
            "status": "started" if created else run.get("status"),
            "run_id": run.get("id"),
            "estimated_seconds": round(estimate_seconds, 1),
            "estimated_completion_at": (now + timedelta(seconds=estimate_seconds)).isoformat(timespec="seconds"),
        }

    def complete_waiting(self, task_id: str, *, outcome: str) -> None:
        task = self.repository.get(task_id)
        if task is None:
            return
        waiting = self.repository.list_runs(
            task_id=task_id,
            statuses=(WAITING_APPROVAL,),
            limit=1,
        )
        if waiting:
            validation = waiting[0].get("run_kind") == "validation"
            self.repository.update_run(
                waiting[0]["id"],
                status="succeeded" if outcome == "approved" else "failed",
                completed_at=datetime.now().isoformat(timespec="seconds"),
                error_json=None if outcome == "approved" else json.dumps({"approval": outcome}),
            )
        else:
            validation = False
        updates: dict[str, Any] = {
            "run_state": None,
            "pending_approval_id": None,
            "parked_session_id": None,
        }
        if validation:
            updates.update(
                {
                    "enabled": outcome == "approved",
                    "status": "enabled" if outcome == "approved" else "validation_failed",
                    "last_error": None if outcome == "approved" else f"approval_{outcome}",
                    "last_error_at": None if outcome == "approved" else datetime.now().isoformat(timespec="seconds"),
                }
            )
        self.repository.update(task_id, updates)
        if validation and outcome == "approved":
            self.sync_task(task_id)
        self._finish_task_slot(task_id)

    def _fire_task(self, task_id: str, scheduled_for: str) -> None:
        task = self.repository.get(task_id)
        if task is None or not task.get("enabled"):
            return
        if task.get("next_run_at") != scheduled_for:
            return
        scheduled_dt = parse_naive_local(scheduled_for)
        latest_due, _next_future, _due_count = calculate_latest_due_and_next(
            task.get("schedule") or {},
            scheduled_dt,
            now=datetime.now(),
        )
        if latest_due is not None and latest_due != scheduled_dt:
            scheduled_dt = latest_due
            scheduled_for = latest_due.isoformat()
            task = self.repository.update(task_id, {"next_run_at": scheduled_for})
        semantics = task.get("timing_semantics") or "start_at"
        prepare_at = datetime.now().isoformat()
        run, created = self.repository.create_run(
            task_id,
            scheduled_for,
            run_kind="scheduled",
            prepare_at=prepare_at,
            deliver_at=scheduled_for,
        )
        self._advance_task(task, scheduled_dt)
        if not created:
            return
        if semantics == "deliver_by":
            self._schedule_release(run["id"], scheduled_dt)
        if semantics == "start_at":
            self._enqueue_content(task, run, "定时任务已开始执行，完成后会发送结果。", "progress")
        self._submit_run(task, run)

    def _advance_task(self, task: dict[str, Any], scheduled_dt: datetime) -> None:
        _latest_due, next_run, _due_count = calculate_latest_due_and_next(
            task.get("schedule") or {},
            scheduled_dt,
            now=datetime.now(),
        )
        updates: dict[str, Any] = {
            "last_run_at": scheduled_dt.isoformat(timespec="seconds"),
            "last_error": None,
            "last_error_at": None,
        }
        if next_run is None:
            updates.update({"enabled": False, "status": "completed", "next_run_at": None, "next_prepare_at": None})
        else:
            updates.update({"next_run_at": next_run.isoformat(), "next_prepare_at": None})
        self.repository.update(task["id"], updates)
        if next_run is not None:
            self.sync_task(task["id"])

    def _submit_run(self, task: dict[str, Any], run: dict[str, Any]) -> None:
        task_id = task["id"]
        with self._active_lock:
            if task_id in self._active_tasks:
                if task.get("overlap_policy") == "skip":
                    self.repository.update_run(
                        run["id"],
                        status="skipped",
                        completed_at=datetime.now().isoformat(timespec="seconds"),
                        error_json=json.dumps({"reason": "overlap"}),
                    )
                return
            self._active_tasks.add(task_id)
        self.execution_pool.submit(self._execute_run, task_id, run["id"])

    def _execute_run(self, task_id: str, run_id: str) -> None:
        if not self.repository.claim_run(run_id):
            self._finish_task_slot(task_id)
            return
        task = self.repository.get(task_id)
        run = self.repository.get_run(run_id)
        if task is None or run is None:
            self._finish_task_slot(task_id)
            return
        started = time.monotonic()
        result: ExecutionResult = self.executor.run_to_result(
            task,
            run_id=run_id,
            run_kind=str(run.get("run_kind") or ""),
            scheduled_for=str(run.get("scheduled_for") or ""),
            deliver_at=str(run.get("deliver_at") or ""),
        )
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        completed_at = datetime.now().isoformat(timespec="seconds")
        if result.status == WAITING_APPROVAL:
            self.repository.update_run(
                run_id,
                status=WAITING_APPROVAL,
                duration_ms=duration_ms,
                result_text=result.content,
                trace_id=result.trace_id,
            )
            self.repository.update(
                task_id,
                {
                    "run_state": WAITING_APPROVAL,
                    "status": "validation_waiting_approval" if run.get("run_kind") == "validation" else task.get("status"),
                    "pending_approval_id": result.pending_approval_id,
                    "parked_session_id": result.parked_session_id,
                },
            )
            if result.content:
                self._enqueue_content(task, run, result.content, "approval")
            return
        if result.status == "failed":
            self.repository.update_run(
                run_id,
                status="failed",
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_json=json.dumps({"message": result.error}, ensure_ascii=False),
                trace_id=result.trace_id,
            )
            self.repository.update(
                task_id,
                {
                    "status": "validation_failed" if run.get("run_kind") == "validation" else task.get("status"),
                    "last_error": result.error,
                    "last_error_at": completed_at,
                },
            )
            if self._target(task).channel_type:
                prefix = "定时任务测试失败" if run.get("run_kind") == "validation" else "定时任务执行失败"
                self._enqueue_content(task, run, f"{prefix}：{result.error}", "failure")
            self._finish_task_slot(task_id)
            return
        self.repository.update_run(
            run_id,
            status="succeeded",
            completed_at=completed_at,
            duration_ms=duration_ms,
            result_text=result.content,
            trace_id=result.trace_id,
        )
        if run.get("run_kind") == "validation":
            seconds = duration_ms / 1000.0
            lead = calculate_lead_time(
                [duration_ms],
                min_seconds=float(task.get("min_lead_seconds") or 30),
                max_seconds=float(task.get("max_lead_seconds") or 3600),
            )
            report = (
                "[定时任务测试成功]\n"
                f"{result.content}\n\n"
                f"测试耗时：{seconds:.2f} 秒；计划时间：{task.get('next_run_at')}；"
                f"建议提前量：{lead:.1f} 秒。"
            ).strip()
            delivery = self._enqueue_content(task, run, report, "validation", submit=False)
            if delivery:
                # Validation includes the actual channel send. Activation is
                # performed only after the delivery receipt is confirmed.
                self._deliver(delivery["id"])
            else:
                self.repository.update(
                    task_id,
                    {
                        "status": "validation_failed",
                        "last_error": "validation has no delivery target",
                        "last_error_at": completed_at,
                    },
                )
        elif result.content:
            delivery = self._enqueue_content(task, run, result.content, "result", submit=False)
            deliver_at = parse_naive_local(run.get("deliver_at") or completed_at)
            if task.get("timing_semantics") != "deliver_by" or deliver_at <= datetime.now():
                self._submit_delivery(delivery)
        if run.get("run_kind") != "validation" and task.get("timing_semantics") == "deliver_by":
            refreshed = self.repository.get(task_id)
            if refreshed and refreshed.get("enabled"):
                self.sync_task(task_id)
        self._finish_task_slot(task_id)

    def _finish_task_slot(self, task_id: str) -> None:
        with self._active_lock:
            self._active_tasks.discard(task_id)
        queued = self.repository.list_runs(task_id=task_id, statuses=("queued",), limit=1)
        task = self.repository.get(task_id)
        if queued and task:
            with self._active_lock:
                if task_id in self._active_tasks:
                    return
                self._active_tasks.add(task_id)
            self.execution_pool.submit(self._execute_run, task_id, queued[-1]["id"])

    def _schedule_release(self, run_id: str, deliver_at: datetime) -> None:
        self.scheduler.add_job(
            self._release_run,
            trigger="date",
            run_date=max(deliver_at, datetime.now()),
            id=f"release:{run_id}",
            replace_existing=True,
            kwargs={"run_id": run_id},
        )

    def _release_run(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run is None:
            return
        task = self.repository.get(run["task_id"])
        if task is None:
            return
        if run["status"] == "succeeded":
            for delivery in self.repository.list_deliveries(
                run_id=run_id,
                statuses=("pending", "retry_wait"),
            ):
                self._submit_delivery(delivery)
            return
        if run["status"] in {"queued", "running", WAITING_APPROVAL}:
            self.repository.update_run(run_id, deadline_missed=1)
            self._enqueue_content(task, run, "定时任务仍在执行，完成后会补发结果。", "deadline")

    def _enqueue_content(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        content: str,
        delivery_kind: str,
        *,
        submit: bool = True,
    ) -> dict[str, Any]:
        target = self._target(task)
        if not target.channel_type or not content.strip():
            return {}
        due_at: Optional[str] = None
        if delivery_kind == "result" and task.get("timing_semantics") == "deliver_by":
            due_at = run.get("deliver_at")
        delivery, _created = self.repository.enqueue_delivery(
            run["id"],
            target.to_dict(),
            {"content": content},
            delivery_kind=delivery_kind,
            next_attempt_at=due_at,
        )
        if submit and (not due_at or parse_naive_local(due_at) <= datetime.now()):
            self._submit_delivery(delivery)
        return delivery

    def _submit_delivery(self, delivery: dict[str, Any]) -> None:
        if delivery:
            self.delivery_pool.submit(self._deliver, delivery["id"])

    def _deliver(self, delivery_id: str) -> None:
        if not self.repository.claim_delivery(delivery_id):
            return
        delivery = self.repository.update_delivery(delivery_id)
        if not delivery:
            return
        target = ChannelTarget.from_dict(delivery.get("target") or {})
        content = str((delivery.get("payload") or {}).get("content") or "")
        run = self.repository.get_run(str(delivery.get("run_id") or "")) or {}
        task = self.repository.get(str(run.get("task_id") or "")) or {}
        receipt = self.send_with_receipt(
            target,
            Reply(content, type=str(delivery.get("delivery_kind") or "text")),
        )
        now = datetime.now().isoformat(timespec="seconds")
        if receipt.delivered:
            self.repository.update_delivery(
                delivery_id,
                status="delivered",
                next_attempt_at=None,
                delivered_at=now,
                provider_message_id=receipt.provider_message_id,
                last_error=None,
            )
            if delivery.get("delivery_kind") == "validation":
                self._activate_after_validation(delivery)
            return
        if receipt.ambiguous:
            self.repository.update_delivery(
                delivery_id,
                status="unknown",
                next_attempt_at=None,
                last_error=receipt.error,
            )
            self._set_validation_delivery_status(delivery, "validation_delivery_unknown", receipt.error)
            return
        if receipt.retryable and int(delivery.get("attempt") or 0) < 3:
            delay = 30 * (2 ** max(0, int(delivery.get("attempt") or 1) - 1))
            retry_at = datetime.now() + timedelta(seconds=delay)
            self.repository.update_delivery(
                delivery_id,
                status="retry_wait",
                next_attempt_at=retry_at.isoformat(),
                last_error=receipt.error,
            )
            return
        self.repository.update_delivery(
            delivery_id,
            status="failed",
            next_attempt_at=None,
            last_error=receipt.error,
        )
        self._set_validation_delivery_status(delivery, "validation_failed", receipt.error)

    def _lead_time(self, task: dict[str, Any]) -> float:
        samples = self.repository.successful_durations(task["id"], limit=20)
        if task.get("lead_time_seconds") is not None and not samples:
            return float(task["lead_time_seconds"])
        return calculate_lead_time(
            samples,
            min_seconds=float(task.get("min_lead_seconds") or 30),
            max_seconds=float(task.get("max_lead_seconds") or 3600),
        )

    def _activate_after_validation(self, delivery: dict[str, Any]) -> None:
        run = self.repository.get_run(str(delivery.get("run_id") or ""))
        if not run or run.get("run_kind") != "validation":
            return
        task = self.repository.get(run["task_id"])
        if not task:
            return
        lead = self._lead_time(task)
        self.repository.update(
            task["id"],
            {
                "enabled": True,
                "status": "enabled",
                "lead_time_seconds": lead,
                "last_error": None,
                "last_error_at": None,
            },
        )
        self.sync_task(task["id"])

    def _set_validation_delivery_status(
        self,
        delivery: dict[str, Any],
        status: str,
        error: str,
    ) -> None:
        if delivery.get("delivery_kind") != "validation":
            return
        run = self.repository.get_run(str(delivery.get("run_id") or ""))
        if not run or run.get("run_kind") != "validation":
            return
        self.repository.update(
            run["task_id"],
            {
                "enabled": False,
                "status": status,
                "last_error": error or status,
                "last_error_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    @staticmethod
    def _target(task: dict[str, Any]) -> ChannelTarget:
        return ChannelTarget.from_dict((task.get("action") or {}).get("target") or {})

    @staticmethod
    def _task_job_id(task_id: str) -> str:
        return f"task:{task_id}"

    def _remove_job(self, job_id: str) -> None:
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
