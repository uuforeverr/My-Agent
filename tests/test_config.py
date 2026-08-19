import os
from pathlib import Path

from config import _load_dotenv, load_settings


def test_load_dotenv_parses_values_and_skips_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nA=1\nB='two'\nC=\"three\"\nBADLINE\n", encoding="utf-8")
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("C", raising=False)

    _load_dotenv(env)

    assert os.environ["A"] == "1"
    assert os.environ["B"] == "two"
    assert os.environ["C"] == "three"


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=file\n", encoding="utf-8")
    monkeypatch.setenv("A", "existing")
    _load_dotenv(env)
    assert os.environ["A"] == "existing"


def test_load_settings_defaults_and_casts(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_BASE", "https://api.example/v1/")
    monkeypatch.setenv("MODEL", "m")
    monkeypatch.setenv("MAX_STEPS", "7")
    monkeypatch.setenv("MAX_HISTORY_MESSAGES", "8")
    monkeypatch.setenv("MEMORY_SEARCH_RESULTS", "9")
    monkeypatch.setenv("MEMORY_FLUSH_MESSAGES", "10")
    monkeypatch.setenv("BASH_TIMEOUT", "11")
    monkeypatch.setenv("QIANFAN_API_KEY", "qf-key")
    monkeypatch.setenv("QIANFAN_API_BASE", "https://qianfan.example/v2/")
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT", "12")
    monkeypatch.setenv("DAILY_DREAM_ENABLED", "true")
    monkeypatch.setenv("DAILY_DREAM_CRON", "0 3 * * *")
    monkeypatch.setenv("DAILY_DREAM_LOOKBACK_DAYS", "2")
    monkeypatch.delenv("TOOL_APPROVAL_MODE", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("KEEP_RECENT_TURNS", raising=False)
    monkeypatch.delenv("EPISODIC_SCAN_INTERVAL", raising=False)
    monkeypatch.delenv("EPISODIC_FLUSH_MIN_TURNS", raising=False)

    settings = load_settings()

    assert settings.openai_api_key == "k"
    assert settings.openai_api_base == "https://api.example/v1"
    assert settings.model == "m"
    assert settings.workspace == (tmp_path / "w").resolve()
    assert settings.workspace.exists()
    assert settings.max_steps == 7
    assert settings.max_history_messages == 8
    assert settings.memory_search_results == 9
    assert settings.memory_flush_messages == 10
    assert settings.bash_timeout == 11
    assert settings.qianfan_api_key == "qf-key"
    assert settings.qianfan_api_base == "https://qianfan.example/v2"
    assert settings.web_search_timeout == 12
    assert settings.daily_dream_enabled is True
    assert settings.daily_dream_cron == "0 3 * * *"
    assert settings.daily_dream_lookback_days == 2
    assert settings.tool_approval_mode == "off"
    assert settings.model_context_window is None
    assert settings.keep_recent_turns == 3
    assert settings.episodic_scan_interval == 60
    assert settings.episodic_flush_min_turns == 20


def test_load_settings_tool_approval_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("TOOL_APPROVAL_MODE", "on")
    assert load_settings().tool_approval_mode == "on"

    monkeypatch.setenv("TOOL_APPROVAL_MODE", "ON")
    assert load_settings().tool_approval_mode == "on"

    monkeypatch.setenv("TOOL_APPROVAL_MODE", "true")
    assert load_settings().tool_approval_mode == "off"


def test_load_settings_compaction_window_and_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "1000")
    monkeypatch.setenv("KEEP_RECENT_TURNS", "5")
    monkeypatch.setenv("EPISODIC_FLUSH_MIN_TURNS", "8")
    settings = load_settings()
    assert settings.model_context_window == 1000
    assert settings.keep_recent_turns == 5
    assert settings.episodic_flush_min_turns == 8

    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "not-a-number")
    monkeypatch.setenv("KEEP_RECENT_TURNS", "0")
    monkeypatch.setenv("EPISODIC_FLUSH_MIN_TURNS", "-1")
    settings = load_settings()
    assert settings.model_context_window is None
    assert settings.keep_recent_turns == 1
    assert settings.episodic_flush_min_turns == 0
