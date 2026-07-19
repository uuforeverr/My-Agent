from pathlib import Path

from channel.cli.cli_channel import CONTINUE, CLIChannel
from memory.curator import MemoryCurator
from memory.manager import MemoryManager
from tests.conftest import FakeLLM


# ---- curator.distill_workflows / parse_sop_output --------------------------

def test_distill_without_llm_returns_empty():
    curator = MemoryCurator(llm=None)
    assert curator.distill_workflows(existing_sop="", episodic_recent="事件") == ("", "")


def test_distill_without_episodic_returns_empty():
    curator = MemoryCurator(llm=FakeLLM(["[SOP]\nx\n[INSIGHTS]\ny"]))
    assert curator.distill_workflows(existing_sop="old", episodic_recent="   ") == ("", "")


def test_distill_parses_two_sections():
    llm = FakeLLM(["[SOP]\n## Deploy\n1. build\n[INSIGHTS]\n- automate the build"])
    curator = MemoryCurator(llm=llm)
    sop, insights = curator.distill_workflows(existing_sop="", episodic_recent="今天又手动构建了三次")
    assert "## Deploy" in sop
    assert "1. build" in sop
    assert "automate the build" in insights


def test_distill_passes_existing_sop_into_prompt():
    llm = FakeLLM(["[SOP]\nnew\n[INSIGHTS]\n无"])
    curator = MemoryCurator(llm=llm)
    curator.distill_workflows(existing_sop="OLD_SOP_MARKER", episodic_recent="事件")
    user_msg = llm.calls[-1][-1]["content"]
    assert "OLD_SOP_MARKER" in user_msg
    assert "事件" in user_msg


def test_parse_sop_output_missing_insights():
    curator = MemoryCurator()
    sop, insights = curator.parse_sop_output("[SOP]\n## Flow\n1. a")
    assert "## Flow" in sop
    assert insights == ""


def test_parse_sop_output_no_tags_treats_all_as_sop():
    curator = MemoryCurator()
    sop, insights = curator.parse_sop_output("just some text")
    assert sop == "just some text"
    assert insights == ""


def test_parse_sop_output_sentinel_becomes_empty():
    curator = MemoryCurator()
    sop, insights = curator.parse_sop_output("[SOP]\n无\n[INSIGHTS]\n无")
    assert sop == ""
    assert insights == ""


# ---- manager.distill_workflows ---------------------------------------------

def _seed_episodic(manager: MemoryManager, text: str):
    manager.episodic_store.append_summary(text, reason="context_trim", session_id="s")


def test_manager_distill_writes_sop_and_log(tmp_memory_dir: Path):
    llm = FakeLLM(["[SOP]\n## Build\n1. run tests\n[INSIGHTS]\n- cache the deps"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    _seed_episodic(manager, "反复手动跑测试")

    result = manager.distill_workflows(lookback_days=3)

    assert result["ok"] is True
    assert "## Build" in manager.sop_store.read()
    assert Path(result["log_path"]).exists()
    assert "cache the deps" in result["insights"]
    manager.close()


def test_manager_distill_skips_when_no_output(tmp_memory_dir: Path):
    # LLM returns sentinel -> no SOP -> skip, SOP.md stays as skeleton.
    llm = FakeLLM(["[SOP]\n无\n[INSIGHTS]\n无"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    _seed_episodic(manager, "今天随便聊了聊")

    result = manager.distill_workflows(lookback_days=3)

    assert result["ok"] is False
    assert result["reason"] == "no_sop_output"
    assert "Standard Operating Procedures" in manager.sop_store.read()
    manager.close()


def test_manager_distill_is_incremental(tmp_memory_dir: Path):
    # Second call must feed the first version's SOP back into the curator.
    llm = FakeLLM([
        "[SOP]\n## V1\n1. step one\n[INSIGHTS]\n- x",
        "[SOP]\n## V2\n1. refined\n[INSIGHTS]\n- y",
    ])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    _seed_episodic(manager, "第一批事件")

    manager.distill_workflows(lookback_days=3)
    manager.distill_workflows(lookback_days=7)

    # The second LLM call should have received V1's SOP as existing_sop.
    second_prompt = llm.calls[-1][-1]["content"]
    assert "## V1" in second_prompt
    assert "## V2" in manager.sop_store.read()
    manager.close()


# ---- CLI /sop dispatch ------------------------------------------------------

class _SopAgent:
    def __init__(self):
        self.memory = self
        self.calls = []

    def distill_workflows(self, lookback_days=3):
        self.calls.append(lookback_days)
        return {"ok": True, "sop_path": "/tmp/SOP.md", "insights": "- do less"}


def test_cli_sop_default_days(settings_factory, tmp_path, capsys):
    settings = settings_factory(workspace=tmp_path / "w")
    agent = _SopAgent()
    channel = CLIChannel(agent=agent, settings=settings)

    assert channel._dispatch("/sop") == CONTINUE
    assert agent.calls == [3]
    out = capsys.readouterr().out
    assert "sop distilled" in out
    assert "do less" in out


def test_cli_sop_custom_days(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    agent = _SopAgent()
    channel = CLIChannel(agent=agent, settings=settings)

    assert channel._dispatch("/sop 7") == CONTINUE
    assert agent.calls == [7]


def test_cli_sop_bad_arg_uses_default(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    agent = _SopAgent()
    channel = CLIChannel(agent=agent, settings=settings)

    channel._dispatch("/sop abc")
    assert agent.calls == [3]
