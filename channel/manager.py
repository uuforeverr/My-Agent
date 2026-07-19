import threading
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


class ChannelManager:
    def __init__(self, settings: Settings, agent: Optional[Agent] = None):
        self.settings = settings
        self.agent = agent or Agent(settings)
        self.channels: list[Channel] = []
        self._channel_map: dict[str, Channel] = {}
        self.scheduler_service: Optional[SchedulerService] = None

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
        executor = SchedulerExecutor(
            send_reply=self.send_to,
            run_agent=self.agent.run,
            run_tool=self.agent.tools.run,
            run_dream=self.agent.memory.deep_dream,
            build_context=self._build_scheduler_context,
            trace_store=JsonlTraceStore.for_project(self.settings.project_root),
        )
        self.scheduler_service = SchedulerService(
            store,
            execute_callback=executor.execute,
            scan_interval=self.settings.scheduler_scan_interval,
            catchup_seconds=self.settings.scheduler_catchup_seconds,
        )
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
        for channel in self.channels:
            try:
                channel.stop()
            except Exception:
                pass
        self.agent.memory.close(wait=True)
