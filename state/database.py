import sqlite3
import threading
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


STATE_SCHEMA_VERSION = 5


def default_state_db_path(project_root: Path) -> Path:
    return Path(project_root) / "data" / "state.db"


class StateDatabase:
    """Shared durable state for the single-process personal agent.

    Writes are serialized in-process and transactions are intentionally short.
    Network, model and tool calls must never run while a transaction is open.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            yield conn

    @contextmanager
    def write(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._write_lock, self.connect() as conn:
            previous_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'enabled',
                    schedule_json TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    delivery_target_json TEXT,
                    timing_semantics TEXT NOT NULL DEFAULT 'notify_at',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    next_run_at TEXT,
                    next_prepare_at TEXT,
                    lead_time_seconds REAL,
                    min_lead_seconds REAL NOT NULL DEFAULT 30,
                    max_lead_seconds REAL NOT NULL DEFAULT 3600,
                    overlap_policy TEXT NOT NULL DEFAULT 'queue_one',
                    last_run_at TEXT,
                    last_error TEXT,
                    last_error_at TEXT,
                    run_state TEXT,
                    pending_approval_id TEXT,
                    parked_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(enabled, next_prepare_at, next_run_at);

                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_kind TEXT NOT NULL DEFAULT 'scheduled',
                    scheduled_for TEXT NOT NULL,
                    prepare_at TEXT,
                    deliver_at TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_ms REAL,
                    result_text TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    trace_id TEXT,
                    deadline_missed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                    UNIQUE(task_id, run_kind, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status
                    ON scheduled_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task_duration
                    ON scheduled_runs(task_id, status, completed_at);

                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    delivery_kind TEXT NOT NULL DEFAULT 'result',
                    channel_type TEXT NOT NULL,
                    target_fingerprint TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    provider_message_id TEXT,
                    started_at TEXT,
                    delivered_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES scheduled_runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, delivery_kind, channel_type, target_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
                    ON delivery_outbox(status, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS state_metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_seq INTEGER NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    channel_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_raw_turns_session_recent
                    ON raw_turns(session_id, status, turn_seq DESC);

                CREATE TABLE IF NOT EXISTS raw_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id INTEGER NOT NULL,
                    message_seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    name TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES raw_turns(id) ON DELETE CASCADE,
                    UNIQUE(turn_id, message_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_raw_messages_turn_order
                    ON raw_messages(turn_id, message_seq);

                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    session_id TEXT NOT NULL,
                    start_turn_seq INTEGER NOT NULL,
                    end_turn_seq INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    body TEXT NOT NULL,
                    stable_facts_json TEXT NOT NULL DEFAULT '[]',
                    lessons_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episodic_memories_user_recent
                    ON episodic_memories(user_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_episodic_memories_session_range
                    ON episodic_memories(session_id, end_turn_seq);

                CREATE TABLE IF NOT EXISTS episodic_cursors (
                    session_id TEXT PRIMARY KEY,
                    last_turn_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dream_cursors (
                    user_id TEXT PRIMARY KEY,
                    last_episodic_created_at TEXT NOT NULL DEFAULT '',
                    last_episodic_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(delivery_outbox)").fetchall()
            }
            if "delivery_kind" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN delivery_kind TEXT NOT NULL DEFAULT 'result'"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_identity
                ON delivery_outbox(run_id, delivery_kind, channel_type, target_fingerprint)
                """
            )
            if previous_version < 4:
                # v4 adopts one memory owner across all channels. Replaying all
                # episodic rows on the next Dream is safer than retaining a
                # per-channel cursor that could skip another channel's events.
                conn.execute(
                    "UPDATE episodic_memories SET user_id = 'local' WHERE user_id <> 'local'"
                )
                conn.execute("DELETE FROM dream_cursors")
            task_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
            }
            if "misfire_policy" in task_columns:
                conn.execute("ALTER TABLE scheduled_tasks DROP COLUMN misfire_policy")
            conn.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")

    def get_metadata(self, key: str, default=None):
        with self.read() as conn:
            row = conn.execute(
                "SELECT value_json FROM state_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_metadata(self, key: str, value) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO state_metadata(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, payload, now),
            )

    def merge_metadata_mapping(self, key: str, updates: dict) -> dict:
        """Atomically merge a small JSON mapping and return the stored value."""
        now = datetime.now().isoformat(timespec="seconds")
        with self.write() as conn:
            row = conn.execute(
                "SELECT value_json FROM state_metadata WHERE key = ?",
                (key,),
            ).fetchone()
            current = {}
            if row is not None:
                try:
                    loaded = json.loads(row["value_json"])
                    if isinstance(loaded, dict):
                        current = loaded
                except (TypeError, json.JSONDecodeError):
                    pass
            current.update(updates)
            conn.execute(
                """
                INSERT INTO state_metadata(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(current, ensure_ascii=False, sort_keys=True, default=str), now),
            )
        return current
