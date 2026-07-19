from config import _parse_channels, load_settings


def test_parse_channels_splits_and_strips():
    assert _parse_channels("cli, feishu ") == ("cli", "feishu")


def test_parse_channels_empty_defaults_to_cli():
    assert _parse_channels("") == ("cli",)
    assert _parse_channels("  ,  ") == ("cli",)


def test_load_settings_parses_channel_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("CHANNELS", "cli,feishu")
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    settings = load_settings()

    assert settings.channels == ("cli", "feishu")
    assert settings.feishu_app_id == "app-id"
    assert settings.feishu_app_secret == "app-secret"


def test_load_settings_channel_defaults(monkeypatch, tmp_path):
    # Neutralise the repo .env so this exercises the in-code default, not
    # whatever the developer happens to have configured locally.
    monkeypatch.setattr("config._load_dotenv", lambda path: None)
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "w"))
    monkeypatch.delenv("CHANNELS", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    settings = load_settings()

    assert settings.channels == ("cli",)
    assert settings.feishu_app_id == ""
    assert settings.feishu_app_secret == ""
