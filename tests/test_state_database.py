import json
import sqlite3

from scheduler.repository import SchedulerRepository
from memory.manager import MemoryManager
from state.database import STATE_SCHEMA_VERSION, StateDatabase, default_state_db_path


def _task(task_id="t1"):
    return {
        "id": task_id,
        "name": "daily",
        "enabled": True,
        "created_at": "2026-10-14T10:00:00",
        "updated_at": "2026-10-14T10:00:00",
        "schedule": {"type": "cron", "expression": "0 9 * * *"},
        "action": {
            "type": "agent_task",
            "ai_task": "report",
            "target": {"channel_type": "cli", "user_id": "local"},
        },
        "next_run_at": "2026-10-15T09:00:00",
        "timing_semantics": "deliver_by",
    }


def test_state_database_initializes_schema(tmp_path):
    path = tmp_path / "state.db"
    StateDatabase(path)
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == STATE_SCHEMA_VERSION
    assert {"scheduled_tasks", "scheduled_runs", "delivery_outbox"} <= tables


def test_v4_migration_unifies_episodic_owner_and_resets_dream_cursor(tmp_path):
    path = tmp_path / "state.db"
    database = StateDatabase(path)
    with database.write() as conn:
        conn.execute(
            """
            INSERT INTO episodic_memories(
                id, user_id, session_id, start_turn_seq, end_turn_seq,
                occurred_at, body, stable_facts_json, lessons_json, created_at
            ) VALUES ('e1', 'feishu-user', 's', 1, 1, '2026-01-01', 'event', '[]', '[]', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO dream_cursors(
                user_id, last_episodic_created_at, last_episodic_id, updated_at
            ) VALUES ('feishu-user', '2026-01-01', 'e1', '2026-01-01')
            """
        )
        conn.execute("PRAGMA user_version = 3")

    StateDatabase(path)

    with database.read() as conn:
        owner = conn.execute(
            "SELECT user_id FROM episodic_memories WHERE id = 'e1'"
        ).fetchone()["user_id"]
        cursor_count = conn.execute("SELECT COUNT(*) AS n FROM dream_cursors").fetchone()["n"]
    assert owner == "local"
    assert cursor_count == 0


def test_v5_migration_removes_misfire_policy_without_losing_tasks(tmp_path):
    path = tmp_path / "state.db"
    database = StateDatabase(path)
    repo = SchedulerRepository(database)
    repo.add(_task())
    with database.write() as conn:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN misfire_policy TEXT NOT NULL DEFAULT 'latest'"
        )
        conn.execute("PRAGMA user_version = 4")

    StateDatabase(path)

    with database.read() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
        }
        task_count = conn.execute("SELECT COUNT(*) AS n FROM scheduled_tasks").fetchone()["n"]
    assert "misfire_policy" not in columns
    assert task_count == 1


def test_scheduler_repository_task_round_trip(tmp_path):
    repo = SchedulerRepository.from_path(tmp_path / "state.db")
    repo.add(_task())

    loaded = repo.get("t1")
    assert loaded["timing_semantics"] == "deliver_by"
    assert loaded["action"]["target"]["channel_type"] == "cli"
    assert repo.list(enabled_only=True)[0]["id"] == "t1"

    repo.update("t1", {"lead_time_seconds": 120.0})
    assert repo.get("t1")["lead_time_seconds"] == 120.0


def test_run_occurrence_is_idempotent(tmp_path):
    repo = SchedulerRepository.from_path(tmp_path / "state.db")
    repo.add(_task())

    first, created_first = repo.create_run("t1", "2026-10-15T09:00:00")
    second, created_second = repo.create_run("t1", "2026-10-15T09:00:00")

    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]
    assert repo.claim_run(first["id"]) is True
    assert repo.claim_run(first["id"]) is False


def test_delivery_outbox_is_idempotent(tmp_path):
    repo = SchedulerRepository.from_path(tmp_path / "state.db")
    repo.add(_task())
    run, _ = repo.create_run("t1", "2026-10-15T09:00:00")
    target = {"channel_type": "cli", "user_id": "local"}

    first, created_first = repo.enqueue_delivery(run["id"], target, {"content": "done"})
    second, created_second = repo.enqueue_delivery(run["id"], target, {"content": "done"})

    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]
    assert repo.claim_delivery(first["id"]) is True
    assert repo.claim_delivery(first["id"]) is False


def test_legacy_json_imports_once(tmp_path):
    legacy = tmp_path / "tasks.json"
    legacy.write_text(
        json.dumps({"tasks": {"t1": _task()}}, ensure_ascii=False),
        encoding="utf-8",
    )
    repo = SchedulerRepository.from_path(tmp_path / "state.db")

    assert repo.migrate_legacy_json(legacy) == 1
    assert repo.migrate_legacy_json(legacy) == 0
    assert repo.get("t1")["name"] == "daily"
    assert legacy.exists()


def test_default_state_db_path(tmp_path):
    assert default_state_db_path(tmp_path) == tmp_path / "data" / "state.db"


def test_metadata_round_trip_and_atomic_mapping_merge(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    assert database.get_metadata("missing") is None

    database.set_metadata("watermarks", {"a": {"turn": 1}})
    assert database.get_metadata("watermarks") == {"a": {"turn": 1}}

    merged = database.merge_metadata_mapping("conversations", {"cli:a": "one"})
    assert merged == {"cli:a": "one"}
    merged = database.merge_metadata_mapping("conversations", {"feishu:b": "two"})
    assert merged == {"cli:a": "one", "feishu:b": "two"}


def test_memory_index_and_watermark_state_share_state_db(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    manager = MemoryManager(tmp_path / "memory", state_database=database)
    try:
        manager._save_watermarks({"cli:local": {"flushed_user_turns": 3}})
        assert manager._load_watermarks() == {"cli:local": {"flushed_user_turns": 3}}
        with database.read() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
        assert "scheduled_tasks" in tables
        assert "memory_chunks" in tables
        assert "memory_chunks_fts" in tables
        assert not (tmp_path / "memory" / "index.db").exists()
        assert not (tmp_path / "memory" / "episodic_watermark.json").exists()
    finally:
        manager.close()
