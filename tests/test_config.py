import os
from pathlib import Path

import pytest

from config import Settings, _load_dotenv, load_settings
from tools.sandbox import DEFAULT_SANDBOX_IMAGE


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
    monkeypatch.setenv("SESSION_WINDOW_TURNS", "8")
    monkeypatch.setenv("MEMORY_SEARCH_RESULTS", "9")
    monkeypatch.setenv("RAW_WRITE_BATCH_SIZE", "10")
    monkeypatch.setenv("EPISODIC_BODY_MAX_CHARS", "120")
    monkeypatch.setenv("BASH_TIMEOUT", "11")
    monkeypatch.setenv("QIANFAN_API_KEY", "qf-key")
    monkeypatch.setenv("QIANFAN_API_BASE", "https://qianfan.example/v2/")
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT", "12")
    monkeypatch.setenv("DAILY_DREAM_ENABLED", "true")
    monkeypatch.setenv("DAILY_DREAM_CRON", "0 3 * * *")
    monkeypatch.setenv("TOOL_APPROVAL_MODE", "never")
    monkeypatch.delenv("MODEL_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("EPISODIC_BATCH_TURNS", raising=False)

    settings = load_settings()

    assert settings.openai_api_key == "k"
    assert settings.openai_api_base == "https://api.example/v1"
    assert settings.model == "m"
    assert settings.workspace == (tmp_path / "w").resolve()
    assert settings.workspace.exists()
    assert settings.max_steps == 7
    assert settings.session_window_turns == 8
    assert settings.memory_search_results == 9
    assert settings.raw_write_batch_size == 10
    assert settings.episodic_body_max_chars == 120
    assert settings.bash_timeout == 11
    assert settings.qianfan_api_key == "qf-key"
    assert settings.qianfan_api_base == "https://qianfan.example/v2"
    assert settings.web_search_timeout == 12
    assert settings.daily_dream_enabled is True
    assert settings.daily_dream_cron == "0 3 * * *"
    assert settings.tool_approval_mode == "never"
    assert settings.model_context_window is None
    assert settings.episodic_worker_poll_seconds == 60
    assert settings.episodic_batch_turns == 20


def test_settings_default_to_project_owned_sandbox_image(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        openai_api_key="",
        openai_api_base="https://example.test/v1",
        model="test",
        workspace=tmp_path / "workspace",
    )

    assert settings.sandbox_image == DEFAULT_SANDBOX_IMAGE


def test_load_settings_tool_approval_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("TOOL_APPROVAL_MODE", "always")
    assert load_settings().tool_approval_mode == "always"

    monkeypatch.setenv("TOOL_APPROVAL_MODE", "never")
    assert load_settings().tool_approval_mode == "never"

    monkeypatch.setenv("TOOL_APPROVAL_MODE", "risk_based")
    assert load_settings().tool_approval_mode == "risk_based"

    monkeypatch.setenv("TOOL_APPROVAL_MODE", "true")
    with pytest.raises(ValueError, match="TOOL_APPROVAL_MODE"):
        load_settings()


def test_load_settings_compaction_window_and_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "1000")
    monkeypatch.setenv("SESSION_WINDOW_TURNS", "5")
    monkeypatch.setenv("EPISODIC_BATCH_TURNS", "8")
    settings = load_settings()
    assert settings.model_context_window == 1000
    assert settings.session_window_turns == 5
    assert settings.episodic_batch_turns == 8

    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "not-a-number")
    monkeypatch.setenv("SESSION_WINDOW_TURNS", "0")
    monkeypatch.setenv("EPISODIC_BATCH_TURNS", "-1")
    settings = load_settings()
    assert settings.model_context_window is None
    assert settings.session_window_turns == 1
    assert settings.episodic_batch_turns == 1
