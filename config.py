import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    openai_api_key: str
    openai_api_base: str
    model: str
    workspace: Path
    max_steps: int = 6
    max_history_messages: int = 40
    memory_search_results: int = 5
    memory_flush_messages: int = 12
    bash_timeout: int = 20
    channels: tuple[str, ...] = ("cli",)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    qianfan_api_key: str = ""
    qianfan_api_base: str = "https://qianfan.baidubce.com/v2"
    web_search_timeout: int = 15
    scheduler_enabled: bool = True
    scheduler_scan_interval: int = 30
    scheduler_catchup_seconds: int = 600
    scheduler_tasks_path: Path = Path("data/scheduler/tasks.json")
    daily_dream_enabled: bool = True
    daily_dream_cron: str = "0 0 * * *"
    daily_dream_lookback_days: int = 1
    tool_approval_mode: str = "off"
    model_context_window: Optional[int] = None
    keep_recent_turns: int = 3


def _parse_channels(raw: str) -> tuple[str, ...]:
    parsed = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parsed or ("cli",)


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_approval_mode(raw: str) -> str:
    return "on" if str(raw).strip().lower() == "on" else "off"


def _parse_optional_positive_int(raw: str) -> Optional[int]:
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent
    _load_dotenv(root / ".env")

    workspace = Path(os.environ.get("WORKSPACE", root / "workspace")).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    scheduler_tasks_path = Path(
        os.environ.get("SCHEDULER_TASKS_PATH", root / "data" / "scheduler" / "tasks.json")
    ).expanduser()

    return Settings(
        project_root=root,
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/"),
        model=os.environ.get("MODEL", "gpt-4o-mini"),
        workspace=workspace,
        max_steps=int(os.environ.get("MAX_STEPS", "6")),
        max_history_messages=int(os.environ.get("MAX_HISTORY_MESSAGES", "40")),
        memory_search_results=int(os.environ.get("MEMORY_SEARCH_RESULTS", "5")),
        memory_flush_messages=int(os.environ.get("MEMORY_FLUSH_MESSAGES", "12")),
        bash_timeout=int(os.environ.get("BASH_TIMEOUT", "20")),
        channels=_parse_channels(os.environ.get("CHANNELS", "cli")),
        feishu_app_id=os.environ.get("FEISHU_APP_ID", ""),
        feishu_app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
        qianfan_api_key=os.environ.get("QIANFAN_API_KEY", ""),
        qianfan_api_base=os.environ.get("QIANFAN_API_BASE", "https://qianfan.baidubce.com/v2").rstrip("/"),
        web_search_timeout=int(os.environ.get("WEB_SEARCH_TIMEOUT", "15")),
        scheduler_enabled=_parse_bool(os.environ.get("SCHEDULER_ENABLED", "true")),
        scheduler_scan_interval=int(os.environ.get("SCHEDULER_SCAN_INTERVAL", "30")),
        scheduler_catchup_seconds=int(os.environ.get("SCHEDULER_CATCHUP_SECONDS", "600")),
        scheduler_tasks_path=scheduler_tasks_path,
        daily_dream_enabled=_parse_bool(os.environ.get("DAILY_DREAM_ENABLED", "true")),
        daily_dream_cron=os.environ.get("DAILY_DREAM_CRON", "0 0 * * *").strip(),
        daily_dream_lookback_days=int(os.environ.get("DAILY_DREAM_LOOKBACK_DAYS", "1")),
        tool_approval_mode=_parse_approval_mode(os.environ.get("TOOL_APPROVAL_MODE", "off")),
        model_context_window=_parse_optional_positive_int(os.environ.get("MODEL_CONTEXT_WINDOW", "")),
        keep_recent_turns=max(1, int(os.environ.get("KEEP_RECENT_TURNS", "3"))),
    )
