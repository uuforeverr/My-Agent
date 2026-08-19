import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from agent.agent import Agent
from agent.run_context import RunContext
from channel.base import Channel
from channel.factory import create_channel
from channel.reply import Reply
from channel.target import ChannelTarget
from config import Settings
from observability import JsonlTraceStore
from scheduler.executor import SchedulerExecutor
from scheduler.schedule import calculate_next_run
from scheduler.service import SchedulerService

DAILY_DREAM_TASK_ID = "system_daily_dream"
EPISODIC_SCAN_TASK_ID = "system_episodic_scan"
RESTART_APPROVAL_MESSAGE = "进程重启，待审批已失效"


class ChannelManager:
    def __init__(self, settings: Settings, agent: Optional[Agent] = None):
        self.settings = settings
        self.agent = agent or Agent(settings)
        self.channels: list[Channel] = []
        self._channel_map: dict[str, Channel] = {}
        self.scheduler_service: Optional[SchedulerService] = None
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
        channel = self._channel_map.get(target.channel_type)
        if channel is None:
            return False
        try:
            channel.send(reply, target.to_incoming_message())
            return True
        except Exception:
            return False

    def start_scheduler(self) -> None:
        store = getattr(self.agent, "scheduler_store", None)
        if store is None or self.scheduler_service is not None:
            return
        self._ensure_daily_dream_task(store)
        self._ensure_episodic_scan_task(store)
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
            run_scan=getattr(self.agent.memory, "scan_raw_for_episodic", None),
            build_context=self._build_scheduler_context,
            trace_store=JsonlTraceStore.for_project(self.settings.project_root),
        )
        self.scheduler_service = SchedulerService(
            store,
            execute_callback=executor.execute,
            scan_interval=self.settings.scheduler_scan_interval,
            catchup_seconds=self.settings.scheduler_catchup_seconds,
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

        action = {
            "type": "memory_dream",
            "lookback_days": self.settings.daily_dream_lookback_days,
        }
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

    def _ensure_episodic_scan_task(self, store) -> None:
        interval = int(self.settings.episodic_scan_interval)
        if interval <= 0:
            return

        now = datetime.now()
        schedule = {"type": "interval", "seconds": interval}
        next_run = calculate_next_run(schedule, from_time=now)
        if next_run is None:
            return

        action = {
            "type": "memory_scan_episodic",
            "keep_recent_turns": self.settings.keep_recent_turns,
            "min_flush_turns": self.settings.episodic_flush_min_turns,
        }
        existing = store.get(EPISODIC_SCAN_TASK_ID)
        if existing is not None:
            updates = {}
            if existing.get("schedule") != schedule:
                updates["schedule"] = schedule
                updates["next_run_at"] = next_run.isoformat()
            if existing.get("action") != action:
                updates["action"] = action
            if updates:
                store.update(EPISODIC_SCAN_TASK_ID, updates)
            return

        task = {
            "id": EPISODIC_SCAN_TASK_ID,
            "name": "情景记忆水位扫描",
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
        trace = None
        try:
            if callable(resume):
                answer, trace = resume(approval_id)
                events = getattr(trace, "events", None) or []
                parked_again = any(event.get("name") == "waiting_approval" for event in events)
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
            new_id = ""
            if trace is not None:
                for event in reversed(trace.events or []):
                    if event.get("name") == "waiting_approval":
                        new_id = str(event.get("approval_id") or "")
                        break
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
