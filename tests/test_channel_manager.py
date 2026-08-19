import threading

import pytest

from channel.chat_channel import ChatChannel
from channel.cli.cli_channel import CLIChannel
from channel.factory import create_channel
from channel.feishu.feishu_channel import FeishuChannel
from channel.manager import DAILY_DREAM_TASK_ID, EPISODIC_SCAN_TASK_ID, ChannelManager
from channel.message import IncomingMessage
from channel.reply import Reply
from channel.target import ChannelTarget

WAIT_TIMEOUT = 2.0


def test_factory_creates_cli(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    channel = create_channel("cli", agent=object(), settings=settings)
    assert isinstance(channel, CLIChannel)
    assert channel.channel_type == "cli"


def test_factory_creates_feishu(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    channel = create_channel("feishu", agent=object(), settings=settings)
    assert isinstance(channel, FeishuChannel)
    assert channel.channel_type == "feishu"


def test_factory_unknown_raises(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    with pytest.raises(ValueError):
        create_channel("telegram", agent=object(), settings=settings)


def test_manager_builds_configured_channels(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w", channels=("cli", "feishu"))
    manager = ChannelManager(settings, agent=object())
    channels = manager.build_channels()
    assert [type(c) for c in channels] == [CLIChannel, FeishuChannel]


def test_manager_shutdown_stops_channels_and_closes_memory(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w", channels=())

    class FakeMemory:
        def __init__(self):
            self.closed_with = None

        def close(self, wait=False):
            self.closed_with = wait

    class FakeAgent:
        def __init__(self):
            self.memory = FakeMemory()

    class SpyChannel:
        channel_type = "spy"

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    manager = ChannelManager(settings, agent=FakeAgent())
    spy = SpyChannel()
    manager.channels = [spy]

    manager.shutdown()

    assert spy.stopped is True
    assert manager.agent.memory.closed_with is True


def test_manager_run_starts_and_shuts_down(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w", channels=())

    class FakeMemory:
        def __init__(self):
            self.closed = None

        def close(self, wait=False):
            self.closed = wait

    class FakeAgent:
        def __init__(self):
            self.memory = FakeMemory()

    class QuickChannel:
        channel_type = "quick"

        def __init__(self):
            self.started = threading.Event()
            self.stopped = False

        def startup(self):
            self.started.set()

        def stop(self):
            self.stopped = True

    manager = ChannelManager(settings, agent=FakeAgent())
    ch = QuickChannel()
    manager.channels = [ch]

    manager.run()

    assert ch.started.is_set()
    assert ch.stopped is True
    assert manager.agent.memory.closed is True


def test_cross_channel_parallel(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    barrier = threading.Barrier(2)
    met = threading.Event()

    class BarrierAgent:
        def run(self, content, context=None, on_event=None):
            barrier.wait(timeout=WAIT_TIMEOUT)
            met.set()
            return "ok", None

    agent = BarrierAgent()

    class SilentChannel(ChatChannel):
        def send(self, reply: Reply, message: IncomingMessage) -> None:
            pass

    ch1 = SilentChannel(agent=agent, settings=settings, poll_interval=0.01)
    ch1.channel_type = "cli"
    ch2 = SilentChannel(agent=agent, settings=settings, poll_interval=0.01)
    ch2.channel_type = "feishu"
    ch1.start_consuming()
    ch2.start_consuming()

    ch1.produce(IncomingMessage(channel_type="cli", user_id="a", content="x", msg_id="1"))
    ch2.produce(IncomingMessage(channel_type="feishu", user_id="b", content="y", msg_id="2"))

    # Both channels must have a handler in run() simultaneously to clear the
    # barrier; otherwise it times out and met stays unset.
    assert met.wait(timeout=WAIT_TIMEOUT)
    ch1.stop()
    ch2.stop()


class _FakeMemory:
    def deep_dream(self, lookback_days=1):
        return {"ok": True, "lookback_days": lookback_days}

    def close(self, wait=False):
        pass


class _FakeTools:
    def run(self, name, arguments, context=None):
        return None


class _FakeAgent:
    def __init__(self, scheduler_store=None):
        self.memory = _FakeMemory()
        self.scheduler_store = scheduler_store
        self.tools = _FakeTools()

    def run(self, message, context=None, on_event=None):
        return "ok", None


def test_send_to_delivers_via_matching_channel(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    manager = ChannelManager(settings, agent=_FakeAgent())

    delivered = []

    class RecordingChannel:
        channel_type = "cli"

        def send(self, reply, message):
            delivered.append((reply, message))

    manager._channel_map = {"cli": RecordingChannel()}
    target = ChannelTarget("cli", "local", session_id="cli:local")

    assert manager.send_to(target, Reply("hello")) is True
    reply, message = delivered[0]
    assert reply.content == "hello"
    assert isinstance(message, IncomingMessage)
    assert message.user_id == "local"


def test_send_to_unknown_channel_returns_false(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    manager = ChannelManager(settings, agent=_FakeAgent())
    manager._channel_map = {}
    target = ChannelTarget("telegram", "u1")
    assert manager.send_to(target, Reply("hi")) is False


def test_send_to_swallows_channel_error(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    manager = ChannelManager(settings, agent=_FakeAgent())

    class BrokenChannel:
        channel_type = "cli"

        def send(self, reply, message):
            raise RuntimeError("network down")

    manager._channel_map = {"cli": BrokenChannel()}
    assert manager.send_to(ChannelTarget("cli", "local"), Reply("x")) is False


def test_start_scheduler_noop_without_store(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=None))
    manager.start_scheduler()
    assert manager.scheduler_service is None


def test_start_scheduler_starts_service_with_store(settings_factory, tmp_path):
    from scheduler.task_store import TaskStore

    settings = settings_factory(workspace=tmp_path / "w")
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=store))
    try:
        manager.start_scheduler()
        assert manager.scheduler_service is not None
        # Idempotent: second call does not replace the running service.
        service = manager.scheduler_service
        manager.start_scheduler()
        assert manager.scheduler_service is service
    finally:
        manager.shutdown()


def test_start_scheduler_creates_daily_dream_task(settings_factory, tmp_path):
    from scheduler.task_store import TaskStore

    settings = settings_factory(
        workspace=tmp_path / "w",
        daily_dream_cron="0 0 * * *",
        daily_dream_lookback_days=2,
    )
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=store))
    try:
        manager.start_scheduler()
        task = store.get(DAILY_DREAM_TASK_ID)
        assert task is not None
        assert task["name"] == "每日凌晨做梦"
        assert task["schedule"] == {"type": "cron", "expression": "0 0 * * *"}
        assert task["action"] == {"type": "memory_dream", "lookback_days": 2}
        assert task["next_run_at"] is not None
    finally:
        manager.shutdown()


def test_start_scheduler_creates_episodic_scan_task(settings_factory, tmp_path):
    from scheduler.task_store import TaskStore

    settings = settings_factory(
        workspace=tmp_path / "w",
        episodic_scan_interval=60,
        keep_recent_turns=3,
    )
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=store))
    try:
        manager.start_scheduler()
        scan = store.get(EPISODIC_SCAN_TASK_ID)
        assert scan is not None
        assert scan["name"] == "情景记忆水位扫描"
        assert scan["schedule"] == {"type": "interval", "seconds": 60}
        assert scan["action"] == {
            "type": "memory_scan_episodic",
            "keep_recent_turns": 3,
            "min_flush_turns": 20,
        }
        assert scan["next_run_at"] is not None
    finally:
        manager.shutdown()


def test_daily_dream_disabled_does_not_create_task(settings_factory, tmp_path):
    from scheduler.task_store import TaskStore

    settings = settings_factory(workspace=tmp_path / "w", daily_dream_enabled=False)
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=store))
    try:
        manager.start_scheduler()
        assert store.get(DAILY_DREAM_TASK_ID) is None
    finally:
        manager.shutdown()
