from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from state.database import StateDatabase


_FAR_FUTURE = "9999-12-31T23:59:59"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _from_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class SchedulerRepository:
    """SQLite repository for schedules, occurrences and delivery outbox rows."""

    def __init__(self, database: StateDatabase):
        self.database = database

    @classmethod
    def from_path(cls, path: Path) -> "SchedulerRepository":
        return cls(StateDatabase(path))

    def new_task_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def add(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task.get("id") or "")
        if not task_id:
            raise ValueError("task must have an id")
        values = self._task_values(task)
        try:
            with self.database.write() as conn:
                conn.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        id, name, enabled, status, schedule_json, action_json,
                        delivery_target_json, timing_semantics, timezone,
                        next_run_at, next_prepare_at, lead_time_seconds,
                        min_lead_seconds, max_lead_seconds, overlap_policy,
                        last_run_at, last_error, last_error_at,
                        run_state, pending_approval_id, parked_session_id,
                        created_at, updated_at
                    ) VALUES (
                        :id, :name, :enabled, :status, :schedule_json, :action_json,
                        :delivery_target_json, :timing_semantics, :timezone,
                        :next_run_at, :next_prepare_at, :lead_time_seconds,
                        :min_lead_seconds, :max_lead_seconds, :overlap_policy,
                        :last_run_at, :last_error, :last_error_at,
                        :run_state, :pending_approval_id, :parked_session_id,
                        :created_at, :updated_at
                    )
                    """,
                    values,
                )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError(f"task already exists: {task_id}") from exc
            raise
        return self.get(task_id) or task

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scheduled_tasks"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY enabled DESC, COALESCE(next_prepare_at, next_run_at, ?) ASC"
        params = (_FAR_FUTURE,)
        with self.database.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_task(row) for row in rows]

    def update(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get(task_id)
        if current is None:
            raise KeyError(f"unknown task: {task_id}")
        current.update(updates)
        current["updated_at"] = _now()
        values = self._task_values(current)
        with self.database.write() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET
                    name=:name, enabled=:enabled, status=:status,
                    schedule_json=:schedule_json, action_json=:action_json,
                    delivery_target_json=:delivery_target_json,
                    timing_semantics=:timing_semantics, timezone=:timezone,
                    next_run_at=:next_run_at, next_prepare_at=:next_prepare_at,
                    lead_time_seconds=:lead_time_seconds,
                    min_lead_seconds=:min_lead_seconds,
                    max_lead_seconds=:max_lead_seconds,
                    overlap_policy=:overlap_policy,
                    last_run_at=:last_run_at, last_error=:last_error,
                    last_error_at=:last_error_at, run_state=:run_state,
                    pending_approval_id=:pending_approval_id,
                    parked_session_id=:parked_session_id,
                    updated_at=:updated_at
                WHERE id=:id
                """,
                values,
            )
        return self.get(task_id) or current

    def delete(self, task_id: str) -> bool:
        with self.database.write() as conn:
            cursor = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def set_enabled(self, task_id: str, enabled: bool) -> dict[str, Any]:
        return self.update(
            task_id,
            {
                "enabled": bool(enabled),
                "status": "enabled" if enabled else "disabled",
            },
        )

    def migrate_legacy_json(self, legacy_path: Path) -> int:
        """Import legacy tasks once without changing the source JSON file."""
        legacy_path = Path(legacy_path)
        if not legacy_path.exists():
            return 0
        with self.database.read() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0])
        if count:
            return 0
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, dict):
            return 0
        imported = 0
        for task in tasks.values():
            if not isinstance(task, dict) or not task.get("id"):
                continue
            migrated = dict(task)
            migrated.setdefault("status", "enabled" if migrated.get("enabled", True) else "disabled")
            migrated.setdefault("timing_semantics", self._default_timing_semantics(migrated))
            migrated.setdefault("timezone", "Asia/Shanghai")
            self.add(migrated)
            imported += 1
        if imported:
            with self.database.write() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO state_metadata(key, value_json, updated_at)
                    VALUES('legacy_scheduler_import', ?, ?)
                    """,
                    (_json({"source": str(legacy_path), "count": imported}), _now()),
                )
        return imported

    def create_run(
        self,
        task_id: str,
        scheduled_for: str,
        *,
        run_kind: str = "scheduled",
        prepare_at: Optional[str] = None,
        deliver_at: Optional[str] = None,
        status: str = "queued",
    ) -> tuple[dict[str, Any], bool]:
        run_id = uuid.uuid4().hex
        now = _now()
        with self.database.write() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO scheduled_runs (
                    id, task_id, run_kind, scheduled_for, prepare_at, deliver_at,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, task_id, run_kind, scheduled_for, prepare_at,
                    deliver_at, status, now, now,
                ),
            )
            created = cursor.rowcount > 0
            row = conn.execute(
                """
                SELECT * FROM scheduled_runs
                WHERE task_id = ? AND run_kind = ? AND scheduled_for = ?
                """,
                (task_id, run_kind, scheduled_for),
            ).fetchone()
        return self._run_row(row), created

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM scheduled_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        task_id: Optional[str] = None,
        statuses: Optional[tuple[str, ...]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM scheduled_runs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def claim_run(self, run_id: str) -> bool:
        with self.database.write() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_runs
                SET status='running', attempt=attempt+1, started_at=?, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (_now(), _now(), run_id),
            )
            return cursor.rowcount == 1

    def update_run(self, run_id: str, **updates: Any) -> Optional[dict[str, Any]]:
        if not updates:
            return self.get_run(run_id)
        allowed = {
            "status", "started_at", "completed_at", "duration_ms",
            "result_text", "result_json", "error_json", "trace_id",
            "deadline_missed", "prepare_at", "deliver_at",
        }
        selected = {key: value for key, value in updates.items() if key in allowed}
        if not selected:
            return self.get_run(run_id)
        selected["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        params = [selected[key] for key in selected]
        params.append(run_id)
        with self.database.write() as conn:
            conn.execute(f"UPDATE scheduled_runs SET {assignments} WHERE id = ?", params)
        return self.get_run(run_id)

    def mark_interrupted_runs(self) -> int:
        now = _now()
        with self.database.write() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_runs
                SET status='interrupted', completed_at=?, updated_at=?
                WHERE status='running'
                """,
                (now, now),
            )
            return cursor.rowcount

    def successful_durations(self, task_id: str, limit: int = 20) -> list[float]:
        with self.database.read() as conn:
            rows = conn.execute(
                """
                SELECT duration_ms FROM scheduled_runs
                WHERE task_id=? AND status='succeeded' AND duration_ms IS NOT NULL
                ORDER BY completed_at DESC LIMIT ?
                """,
                (task_id, max(1, int(limit))),
            ).fetchall()
        return [float(row[0]) for row in rows]

    def enqueue_delivery(
        self,
        run_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        *,
        delivery_kind: str = "result",
        status: str = "pending",
        next_attempt_at: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        channel_type = str(target.get("channel_type") or "")
        fingerprint = hashlib.sha256(_json(target).encode("utf-8")).hexdigest()
        delivery_id = uuid.uuid4().hex
        now = _now()
        with self.database.write() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO delivery_outbox (
                    id, run_id, delivery_kind, channel_type, target_fingerprint, target_json,
                    payload_json, status, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id, run_id, delivery_kind, channel_type, fingerprint, _json(target),
                    _json(payload), status, next_attempt_at, now, now,
                ),
            )
            created = cursor.rowcount > 0
            row = conn.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE run_id=? AND delivery_kind=? AND channel_type=? AND target_fingerprint=?
                """,
                (run_id, delivery_kind, channel_type, fingerprint),
            ).fetchone()
        return self._delivery_row(row), created

    def list_relayable_deliveries(
        self,
        now: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        now = now or _now()
        with self.database.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY COALESCE(next_attempt_at, created_at) ASC
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
        return [self._delivery_row(row) for row in rows]

    def list_due_deliveries(self, now: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        """Compatibility alias for callers outside the production coordinator."""
        return self.list_relayable_deliveries(now=now, limit=limit)

    def recover_expired_delivery_leases(
        self,
        *,
        now: Optional[str] = None,
        lease_seconds: float = 120.0,
        limit: int = 20,
    ) -> int:
        now = now or _now()
        cutoff = (
            datetime.fromisoformat(now) - timedelta(seconds=max(1.0, float(lease_seconds)))
        ).isoformat()
        with self.database.write() as conn:
            rows = conn.execute(
                """
                SELECT id FROM delivery_outbox
                WHERE status='sending'
                  AND started_at IS NOT NULL
                  AND started_at <= ?
                ORDER BY started_at ASC
                LIMIT ?
                """,
                (cutoff, max(1, int(limit))),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE delivery_outbox
                SET status='retry_wait', next_attempt_at=?,
                    last_error='delivery_lease_expired', updated_at=?
                WHERE id IN ({placeholders}) AND status='sending'
                """,
                [now, now, *ids],
            )
            return len(ids)

    def get_delivery(self, delivery_id: str) -> Optional[dict[str, Any]]:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        return self._delivery_row(row) if row is not None else None

    def list_deliveries(
        self,
        *,
        run_id: Optional[str] = None,
        statuses: Optional[tuple[str, ...]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM delivery_outbox {where} ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._delivery_row(row) for row in rows]

    def claim_delivery(self, delivery_id: str) -> bool:
        now = _now()
        with self.database.write() as conn:
            cursor = conn.execute(
                """
                UPDATE delivery_outbox
                SET status='sending', attempt=attempt+1, started_at=?, updated_at=?
                WHERE id=? AND status IN ('pending', 'retry_wait')
                """,
                (now, now, delivery_id),
            )
            return cursor.rowcount == 1

    def update_delivery(self, delivery_id: str, **updates: Any) -> Optional[dict[str, Any]]:
        allowed = {
            "status", "next_attempt_at", "provider_message_id",
            "delivered_at", "last_error",
        }
        selected = {key: value for key, value in updates.items() if key in allowed}
        if selected:
            selected["updated_at"] = _now()
            assignments = ", ".join(f"{key} = ?" for key in selected)
            params = [selected[key] for key in selected]
            params.append(delivery_id)
            with self.database.write() as conn:
                conn.execute(f"UPDATE delivery_outbox SET {assignments} WHERE id = ?", params)
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
        return self._delivery_row(row) if row is not None else None

    @staticmethod
    def _default_timing_semantics(task: dict[str, Any]) -> str:
        action_type = str((task.get("action") or {}).get("type") or "")
        return "notify_at" if action_type == "send_message" else "start_at"

    def _task_values(self, task: dict[str, Any]) -> dict[str, Any]:
        action = task.get("action") or {}
        target = action.get("target") or task.get("delivery_target") or {}
        created_at = task.get("created_at") or _now()
        return {
            "id": str(task.get("id") or ""),
            "name": str(task.get("name") or task.get("id") or "task"),
            "enabled": int(bool(task.get("enabled", True))),
            "status": str(task.get("status") or ("enabled" if task.get("enabled", True) else "disabled")),
            "schedule_json": _json(task.get("schedule") or {}),
            "action_json": _json(action),
            "delivery_target_json": _json(target) if target else None,
            "timing_semantics": str(task.get("timing_semantics") or self._default_timing_semantics(task)),
            "timezone": str(task.get("timezone") or "Asia/Shanghai"),
            "next_run_at": task.get("next_run_at"),
            "next_prepare_at": task.get("next_prepare_at"),
            "lead_time_seconds": task.get("lead_time_seconds"),
            "min_lead_seconds": float(task.get("min_lead_seconds", 30)),
            "max_lead_seconds": float(task.get("max_lead_seconds", 3600)),
            "overlap_policy": str(task.get("overlap_policy") or "queue_one"),
            "last_run_at": task.get("last_run_at"),
            "last_error": task.get("last_error"),
            "last_error_at": task.get("last_error_at"),
            "run_state": task.get("run_state"),
            "pending_approval_id": task.get("pending_approval_id"),
            "parked_session_id": task.get("parked_session_id"),
            "created_at": created_at,
            "updated_at": task.get("updated_at") or created_at,
        }

    @staticmethod
    def _row_to_task(row: Any) -> dict[str, Any]:
        action = _from_json(row["action_json"], {})
        target = _from_json(row["delivery_target_json"], {})
        if target and not action.get("target"):
            action["target"] = target
        return {
            "id": row["id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "schedule": _from_json(row["schedule_json"], {}),
            "action": action,
            "timing_semantics": row["timing_semantics"],
            "timezone": row["timezone"],
            "next_run_at": row["next_run_at"],
            "next_prepare_at": row["next_prepare_at"],
            "lead_time_seconds": row["lead_time_seconds"],
            "min_lead_seconds": row["min_lead_seconds"],
            "max_lead_seconds": row["max_lead_seconds"],
            "overlap_policy": row["overlap_policy"],
            "last_run_at": row["last_run_at"],
            "last_error": row["last_error"],
            "last_error_at": row["last_error_at"],
            "run_state": row["run_state"],
            "pending_approval_id": row["pending_approval_id"],
            "parked_session_id": row["parked_session_id"],
        }

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    @staticmethod
    def _delivery_row(row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        result = dict(row)
        result["target"] = _from_json(result.pop("target_json", None), {})
        result["payload"] = _from_json(result.pop("payload_json", None), {})
        return result
