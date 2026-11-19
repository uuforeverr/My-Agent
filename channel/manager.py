import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from agent.agent import Agent
from agent.run_context import RunContext
from channel.base import Channel
from channel.delivery import DeliveryReceipt
from channel.factory import create_channel
from channel.reply import Reply
from channel.target import ChannelTarget
from config import Settings
from scheduler.executor import SchedulerExecutor
from scheduler.coordinator import APSchedulerCoordinator
from scheduler.repository import SchedulerRepository
from scheduler.schedule import calculate_next_run
from scheduler.service import SchedulerService

DAILY_DREAM_TASK_ID = "system_daily_dream"
LEGACY_EPISODIC_SCAN_TASK_ID = "system_episodic_scan"
RESTART_APPROVAL_MESSAGE = "进程重启，待审批已失效"


class ChannelManager:
    def __init__(self, settings: Settings, agent: Optional[Agent] = None):
        self.settings = settings
        self.agent = agent or Agent(settings)
        self.channels: list[Channel] = []
        self._channel_map: dict[str, Channel] = {}
        self.scheduler_service: Optional[APSchedulerCoordinator | SchedulerService] = None
        self._resume_pool: Optional[ThreadPoolExecutor] = None

    def build_channels(self) -> list[Channel]:
        self.channels = [
            create_channel(channel_type, self.agent, self.settings)
            for channel_type in self.settings.channels
        ]
        self._channel_map = {channel.channel_type: channel for channel in self.channels}
        return self.channels

    def send_to(self, target: ChannelTarget, reply: Reply) -> bool:
        """Proactively deliver a reply to a ChannelTarget (no originating message).

        Reuses each channel's existing ``send()`` by synthesizing an
        IncomingMessage from the target. Returns False when the target channel
        is not available so the scheduler can retry later.
        """
        return bool(self.send_with_receipt(target, reply))

    def send_with_receipt(self, target: ChannelTarget, reply: Reply) -> DeliveryReceipt:
        channel = self._channel_map.get(target.channel_type)
        if channel is None:
            return DeliveryReceipt.failed(f"channel unavailable: {target.channel_type}", retryable=True)
        try:
            receipt = channel.send(reply, target.to_incoming_message())
            if isinstance(receipt, DeliveryReceipt):
                return receipt
            # Compatibility for custom channels still implementing the old
            # ``send() -> None`` contract.
            return DeliveryReceipt.success()
        except Exception as exc:
            return DeliveryReceipt.failed(f"{type(exc).__name__}: {exc}", retryable=True)

    def start_scheduler(self) -> None:
        store = getattr(self.agent, "scheduler_store", None)
        if store is None or self.scheduler_service is not None:
            return
        if store.get(LEGACY_EPISODIC_SCAN_TASK_ID) is not None:
            store.delete(LEGACY_EPISODIC_SCAN_TASK_ID)
        self._ensure_daily_dream_task(store)
        gate = getattr(getattr(self.agent, "tools", None), "approval_gate", None)
        if gate is not None:
            gate.on_scheduled_resolved = self._on_scheduled_resolved
        if self._resume_pool is None:
            self._resume_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="approval-resume")
        executor = SchedulerExecutor(
            send_reply=self.send_to,
            run_agent=self.agent.run,
            run_tool=self.agent.tools.run,
            run_dream=self.agent.memory.deep_dream,
            run_scan=None,
            build_context=self._build_scheduler_context,
            trace_store=None,
        )
        if isinstance(store, SchedulerRepository):
            self.scheduler_service = APSchedulerCoordinator(
                store,
                executor,
                self.send_with_receipt,
                timezone=self.settings.scheduler_timezone,
                max_workers=self.settings.scheduler_max_workers,
                outbox_relay_interval_seconds=self.settings.outbox_relay_interval_seconds,
                outbox_relay_batch_size=self.settings.outbox_relay_batch_size,
                outbox_delivery_lease_seconds=self.settings.outbox_delivery_lease_seconds,
            )
            scheduler_tool = getattr(getattr(self.agent, "tools", None), "tools", {}).get("scheduler")
            if scheduler_tool is not None:
                scheduler_tool.bind_control(self.scheduler_service)
        else:
            # Compatibility for callers that explicitly inject the legacy JSON
            # store. Production Agent instances always expose SchedulerRepository.
            self.scheduler_service = SchedulerService(
                store,
                executor.execute,
                approval_gate=gate,
            )
        self._sweep_stale_waiting_approvals(store)
        self.scheduler_service.start()

    def _ensure_daily_dream_task(self, store) -> None:
        if not self.settings.daily_dream_enabled:
            return

        now = datetime.now()
        schedule = {"type": "cron", "expression": self.settings.daily_dream_cron}
        try:
            next_run = calculate_next_run(schedule, from_time=now)
        except Exception:
            return
        if next_run is None:
            return

        action = {"type": "memory_dream"}
        existing = store.get(DAILY_DREAM_TASK_ID)
        if existing is not None:
            updates = {}
            if existing.get("schedule") != schedule:
                updates["schedule"] = schedule
                updates["next_run_at"] = next_run.isoformat()
            if existing.get("action") != action:
                updates["action"] = action
            if updates:
                store.update(DAILY_DREAM_TASK_ID, updates)
            return

        task = {
            "id": DAILY_DREAM_TASK_ID,
            "name": "每日凌晨做梦",
            "enabled": True,
            "created_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
            "schedule": schedule,
            "action": action,
            "next_run_at": next_run.isoformat(),
            "last_run_at": None,
            "last_error": None,
            "last_error_at": None,
        }
        store.add(task)

    def _on_scheduled_resolved(self, pending, outcome: str) -> None:
        approval_id = getattr(pending, "approval_id", "")
        if self._resume_pool is None:
            self._run_scheduled_resume(approval_id, outcome)
            return
        self._resume_pool.submit(self._run_scheduled_resume, approval_id, outcome)

    def _run_scheduled_resume(self, approval_id: str, outcome: str) -> None:
        resume = getattr(self.agent, "resume_after_approval", None)
        answer = ""
        parked_again = False
        turn_result = None
        try:
            if callable(resume):
                answer, turn_result = resume(approval_id)
                parked_again = getattr(turn_result, "outcome", "") == "waiting_approval"
        except Exception:
            answer = ""
        gate = getattr(getattr(self.agent, "tools", None), "approval_gate", None)
        pending = gate.get(approval_id) if gate is not None else None
        target_dict = (pending.target if pending is not None else None) or {}
        if not target_dict and pending is not None:
            target_dict = {
                "channel_type": pending.request.channel_type,
                "user_id": pending.request.user_id,
                "session_id": pending.request.session_id,
            }
        target = ChannelTarget.from_dict(target_dict)
        if answer and target.channel_type:
            self.send_to(target, Reply(answer))
        if parked_again:
            task_id = pending.request.task_id if pending is not None else ""
            new_id = str(getattr(turn_result, "waiting_approval_id", "") or "")
            if task_id and self.agent.scheduler_store is not None:
                self.agent.scheduler_store.update(
                    task_id,
                    {
                        "run_state": "waiting_approval",
                        "pending_approval_id": new_id or approval_id,
                        "parked_session_id": (
                            target.session_id
                            or (pending.request.session_id if pending is not None else "")
                        ),
                    },
                )
            if gate is not None:
                gate.forget(approval_id)
            return
        task_id = pending.request.task_id if pending is not None else ""
        if self.scheduler_service is not None and task_id:
            self.scheduler_service.complete_waiting(task_id, outcome=outcome)
        if gate is not None:
            gate.forget(approval_id)

    def _sweep_stale_waiting_approvals(self, store) -> None:
        for task in list(store.list()):
            if task.get("run_state") != "waiting_approval":
                continue
            target = ChannelTarget.from_dict((task.get("action") or {}).get("target") or {})
            if target.channel_type:
                self.send_to(target, Reply(RESTART_APPROVAL_MESSAGE))
            if self.scheduler_service is not None:
                self.scheduler_service.complete_waiting(task["id"], outcome="restart")

    def _build_scheduler_context(self, session_id: str, **metadata) -> RunContext:
        return RunContext.create(
            session_id=session_id,
            workspace=self.settings.workspace,
            **metadata,
        )

    def run(self) -> None:
        if not self.channels:
            self.build_channels()
        self.start_scheduler()
        threads = []
        for channel in self.channels:
            thread = threading.Thread(
                target=channel.startup,
                daemon=True,
                name=f"{channel.channel_type or 'channel'}-startup",
            )
            thread.start()
            threads.append(thread)
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.scheduler_service is not None:
            self.scheduler_service.stop()
        if self._resume_pool is not None:
            self._resume_pool.shutdown(wait=False)
            self._resume_pool = None
        for channel in self.channels:
            try:
                channel.stop()
            except Exception:
                pass
        self.agent.memory.close(wait=True)
