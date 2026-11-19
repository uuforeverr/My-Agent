import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.sandbox import DEFAULT_SANDBOX_IMAGE


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
    session_window_turns: int = 20
    memory_search_results: int = 5
    memory_embedding_provider: str = "local"
    memory_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    memory_embedding_device: str = "cpu"
    memory_embedding_local_files_only: bool = False
    memory_vector_backend: str = "auto"
    memory_candidate_limit: int = 30
    memory_rrf_k: int = 60
    memory_semantic_max_chars: int = 20000
    bash_timeout: int = 20
    sandbox_enabled: bool = True
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    sandbox_network: str = "none"
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 128
    sandbox_user: str = "65532:65532"
    channels: tuple[str, ...] = ("cli",)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    qianfan_api_key: str = ""
    qianfan_api_base: str = "https://qianfan.baidubce.com/v2"
    web_search_timeout: int = 15
    scheduler_enabled: bool = True
    scheduler_scan_interval: int = 30
    scheduler_tasks_path: Path = Path("data/scheduler/tasks.json")
    daily_dream_enabled: bool = True
    daily_dream_cron: str = "0 0 * * *"
    tool_approval_mode: str = "risk_based"
    model_context_window: Optional[int] = None
    raw_write_batch_size: int = 32
    raw_write_flush_interval_ms: int = 100
    raw_write_queue_capacity: int = 4096
    episodic_batch_turns: int = 20
    episodic_max_wait_seconds: int = 3600
    episodic_worker_poll_seconds: int = 60
    episodic_body_max_chars: int = 60
    state_db_path: Path = Path("data/state.db")
    scheduler_timezone: str = "Asia/Shanghai"
    scheduler_max_workers: int = 2
    outbox_relay_interval_seconds: float = 15.0
    outbox_relay_batch_size: int = 20
    outbox_delivery_lease_seconds: float = 120.0
    otel_enabled: bool = True
    otel_endpoint: str = "http://127.0.0.1:4318"
    otel_service_name: str = "my-agent"
    otel_environment: str = "local"
    otel_capture_content: bool = True
    otel_content_max_bytes: int = 1000000
    feishu_stream_flush_interval: float = 0.75
    feishu_stream_flush_chars: int = 120


def _parse_channels(raw: str) -> tuple[str, ...]:
    parsed = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parsed or ("cli",)


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_approval_mode(raw: str) -> str:
    value = str(raw).strip().lower()
    value = {"on": "always", "off": "never"}.get(value, value)
    if value not in {"always", "never", "risk_based"}:
        raise ValueError("TOOL_APPROVAL_MODE must be 'always', 'never', or 'risk_based'")
    return value


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
    state_db_path = Path(
        os.environ.get("STATE_DB_PATH", root / "data" / "state.db")
    ).expanduser()

    return Settings(
        project_root=root,
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/"),
        model=os.environ.get("MODEL", "gpt-4o-mini"),
        workspace=workspace,
        max_steps=int(os.environ.get("MAX_STEPS", "6")),
        session_window_turns=max(1, int(os.environ.get("SESSION_WINDOW_TURNS", "20"))),
        memory_search_results=int(os.environ.get("MEMORY_SEARCH_RESULTS", "5")),
        memory_embedding_provider=os.environ.get(
            "MEMORY_EMBEDDING_PROVIDER", "local"
        ).strip().lower(),
        memory_embedding_model=os.environ.get(
            "MEMORY_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        ).strip(),
        memory_embedding_device=os.environ.get("MEMORY_EMBEDDING_DEVICE", "cpu").strip() or "cpu",
        memory_embedding_local_files_only=_parse_bool(
            os.environ.get("MEMORY_EMBEDDING_LOCAL_FILES_ONLY", "false")
        ),
        memory_vector_backend=os.environ.get("MEMORY_VECTOR_BACKEND", "auto").strip().lower(),
        memory_candidate_limit=max(5, int(os.environ.get("MEMORY_CANDIDATE_LIMIT", "30"))),
        memory_rrf_k=max(1, int(os.environ.get("MEMORY_RRF_K", "60"))),
        memory_semantic_max_chars=max(
            1000, int(os.environ.get("MEMORY_SEMANTIC_MAX_CHARS", "20000"))
        ),
        bash_timeout=int(os.environ.get("BASH_TIMEOUT", "20")),
        sandbox_enabled=_parse_bool(os.environ.get("SANDBOX_ENABLED", "true")),
        sandbox_image=os.environ.get("SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE).strip()
        or DEFAULT_SANDBOX_IMAGE,
        sandbox_network=os.environ.get("SANDBOX_NETWORK", "none").strip() or "none",
        sandbox_memory=os.environ.get("SANDBOX_MEMORY", "512m").strip() or "512m",
        sandbox_cpus=max(0.1, float(os.environ.get("SANDBOX_CPUS", "1.0"))),
        sandbox_pids_limit=max(16, int(os.environ.get("SANDBOX_PIDS_LIMIT", "128"))),
        sandbox_user=os.environ.get("SANDBOX_USER", "65532:65532").strip(),
        channels=_parse_channels(os.environ.get("CHANNELS", "cli")),
        feishu_app_id=os.environ.get("FEISHU_APP_ID", ""),
        feishu_app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
        qianfan_api_key=os.environ.get("QIANFAN_API_KEY", ""),
        qianfan_api_base=os.environ.get("QIANFAN_API_BASE", "https://qianfan.baidubce.com/v2").rstrip("/"),
        web_search_timeout=int(os.environ.get("WEB_SEARCH_TIMEOUT", "15")),
        scheduler_enabled=_parse_bool(os.environ.get("SCHEDULER_ENABLED", "true")),
        scheduler_scan_interval=int(os.environ.get("SCHEDULER_SCAN_INTERVAL", "30")),
        scheduler_tasks_path=scheduler_tasks_path,
        daily_dream_enabled=_parse_bool(os.environ.get("DAILY_DREAM_ENABLED", "true")),
        daily_dream_cron=os.environ.get("DAILY_DREAM_CRON", "0 0 * * *").strip(),
        tool_approval_mode=_parse_approval_mode(os.environ.get("TOOL_APPROVAL_MODE", "risk_based")),
        model_context_window=_parse_optional_positive_int(os.environ.get("MODEL_CONTEXT_WINDOW", "")),
        raw_write_batch_size=max(1, int(os.environ.get("RAW_WRITE_BATCH_SIZE", "32"))),
        raw_write_flush_interval_ms=max(1, int(os.environ.get("RAW_WRITE_FLUSH_INTERVAL_MS", "100"))),
        raw_write_queue_capacity=max(1, int(os.environ.get("RAW_WRITE_QUEUE_CAPACITY", "4096"))),
        episodic_batch_turns=max(1, int(os.environ.get("EPISODIC_BATCH_TURNS", "20"))),
        episodic_max_wait_seconds=max(1, int(os.environ.get("EPISODIC_MAX_WAIT_SECONDS", "3600"))),
        episodic_worker_poll_seconds=max(1, int(os.environ.get("EPISODIC_WORKER_POLL_SECONDS", "60"))),
        episodic_body_max_chars=max(40, int(os.environ.get("EPISODIC_BODY_MAX_CHARS", "60"))),
        state_db_path=state_db_path,
        scheduler_timezone=os.environ.get("SCHEDULER_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai",
        scheduler_max_workers=max(1, int(os.environ.get("SCHEDULER_MAX_WORKERS", "2"))),
        outbox_relay_interval_seconds=max(
            0.1, float(os.environ.get("OUTBOX_RELAY_INTERVAL_SECONDS", "15"))
        ),
        outbox_relay_batch_size=max(
            1, int(os.environ.get("OUTBOX_RELAY_BATCH_SIZE", "20"))
        ),
        outbox_delivery_lease_seconds=max(
            1.0, float(os.environ.get("OUTBOX_DELIVERY_LEASE_SECONDS", "120"))
        ),
        otel_enabled=_parse_bool(os.environ.get("MY_AGENT_OTEL_ENABLED", "true")),
        otel_endpoint=os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"
        ).rstrip("/"),
        otel_service_name=os.environ.get("OTEL_SERVICE_NAME", "my-agent").strip() or "my-agent",
        otel_environment=os.environ.get("OTEL_RESOURCE_ENVIRONMENT", "local").strip() or "local",
        otel_capture_content=_parse_bool(os.environ.get("MY_AGENT_OTEL_CAPTURE_CONTENT", "true")),
        otel_content_max_bytes=max(
            4096, int(os.environ.get("MY_AGENT_OTEL_CONTENT_MAX_BYTES", "1000000"))
        ),
        feishu_stream_flush_interval=max(
            0.1, float(os.environ.get("FEISHU_STREAM_FLUSH_INTERVAL", "0.75"))
        ),
        feishu_stream_flush_chars=max(
            1, int(os.environ.get("FEISHU_STREAM_FLUSH_CHARS", "120"))
        ),
    )
