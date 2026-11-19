from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Any, Optional

from memory.index import MemoryRecord
from state.database import StateDatabase


class EpisodicMemoryStore:
    """Canonical SQLite episodic store with compact structured metadata."""

    def __init__(
        self,
        memory_dir: Path,
        state_database: Optional[StateDatabase] = None,
        *,
        body_max_chars: int = 60,
    ):
        self.memory_dir = Path(memory_dir)
        self.database = state_database or StateDatabase(self.memory_dir / "index.db")
        self.body_max_chars = max(40, int(body_max_chars))

    def append_summary(
        self,
        summary: Any,
        reason: str,
        session_id: str,
        user_id: str = "local",
        day: Optional[str] = None,
        *,
        start_turn_seq: int = 0,
        end_turn_seq: int = 0,
    ) -> list[MemoryRecord]:
        items = self._normalize_items(summary)
        if not items:
            return []
        occurred_at = (
            f"{day}T00:00:00" if day else datetime.now().astimezone().isoformat(timespec="seconds")
        )
        with self.database.write() as conn:
            return self.insert_batch(
                conn,
                items,
                session_id=session_id,
                user_id=user_id,
                start_turn_seq=start_turn_seq,
                end_turn_seq=end_turn_seq,
                occurred_at=occurred_at,
            )

    def insert_batch(
        self,
        conn,
        items: list[dict[str, Any]],
        *,
        session_id: str,
        user_id: str,
        start_turn_seq: int,
        end_turn_seq: int,
        occurred_at: str,
    ) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for index, item in enumerate(items):
            body = self._truncate_body(item.get("body"), self.body_max_chars)
            stable_facts = self._string_list(item.get("stable_facts"))
            lessons = self._string_list(item.get("lessons"))
            if not body and not stable_facts and not lessons:
                continue
            if not body:
                body = (stable_facts or lessons)[0][: self.body_max_chars]
            raw_id = f"{session_id}:{start_turn_seq}:{end_turn_seq}:{index}:{body}"
            episode_id = "episode_" + sha1(raw_id.encode("utf-8")).hexdigest()[:20]
            conn.execute(
                """
                INSERT OR IGNORE INTO episodic_memories(
                    id, user_id, session_id, start_turn_seq, end_turn_seq,
                    occurred_at, body, stable_facts_json, lessons_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id, str(user_id or "local"), session_id,
                    int(start_turn_seq), int(end_turn_seq), occurred_at, body,
                    json.dumps(stable_facts, ensure_ascii=False),
                    json.dumps(lessons, ensure_ascii=False), now,
                ),
            )
            records.append(self._record(
                episode_id, str(user_id or "local"), session_id,
                start_turn_seq, end_turn_seq, occurred_at, body,
                stable_facts, lessons, now,
            ))
        return records

    def read_day(self, day: Optional[str] = None, user_id: Optional[str] = None) -> str:
        target = day or datetime.now().strftime("%Y-%m-%d")
        where = ["substr(occurred_at, 1, 10) = ?"]
        params: list[Any] = [target]
        if user_id is not None:
            where.append("user_id = ?")
            params.append(str(user_id or "local"))
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM episodic_memories WHERE {' AND '.join(where)} "
                "ORDER BY occurred_at ASC, id ASC",
                params,
            ).fetchall()
        return self._render_rows(rows)

    def read_recent(self, days: int = 1, user_id: Optional[str] = None) -> str:
        start = (datetime.now() - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
        where = ["substr(occurred_at, 1, 10) >= ?"]
        params: list[Any] = [start]
        if user_id is not None:
            where.append("user_id = ?")
            params.append(str(user_id or "local"))
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM episodic_memories WHERE {' AND '.join(where)} "
                "ORDER BY occurred_at ASC, id ASC",
                params,
            ).fetchall()
        return self._render_rows(rows)

    def read_after_cursor(self, user_id: str) -> tuple[str, list[tuple[str, str]]]:
        with self.database.read() as conn:
            cursor = conn.execute(
                "SELECT last_episodic_created_at, last_episodic_id FROM dream_cursors WHERE user_id = ?",
                (str(user_id or "local"),),
            ).fetchone()
            created_at = cursor["last_episodic_created_at"] if cursor else ""
            episode_id = cursor["last_episodic_id"] if cursor else ""
            rows = conn.execute(
                """
                SELECT * FROM episodic_memories
                WHERE user_id = ?
                  AND (created_at > ? OR (created_at = ? AND id > ?))
                ORDER BY created_at ASC, id ASC
                """,
                (str(user_id or "local"), created_at, created_at, episode_id),
            ).fetchall()
        positions = [(row["created_at"], row["id"]) for row in rows]
        return self._render_rows(rows), positions

    def set_dream_cursor(self, user_id: str, position: tuple[str, str]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.database.write() as conn:
            conn.execute(
                """
                INSERT INTO dream_cursors(user_id, last_episodic_created_at, last_episodic_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_episodic_created_at=excluded.last_episodic_created_at,
                    last_episodic_id=excluded.last_episodic_id,
                    updated_at=excluded.updated_at
                """,
                (str(user_id or "local"), position[0], position[1], now),
            )

    def list_records(self, user_id: Optional[str] = None) -> list[MemoryRecord]:
        query = "SELECT * FROM episodic_memories"
        params: tuple[Any, ...] = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            params = (str(user_id or "local"),)
        query += " ORDER BY occurred_at ASC, id ASC"
        with self.database.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def get_records(self, record_ids: list[str]) -> dict[str, MemoryRecord]:
        ids = [str(record_id) for record_id in record_ids if str(record_id)]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM episodic_memories WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        return {
            str(row["id"]): self._record_from_row(row)
            for row in rows
        }

    def _normalize_items(self, summary: Any) -> list[dict[str, Any]]:
        if summary is None:
            return []
        if isinstance(summary, dict):
            raw_items = [summary]
        elif isinstance(summary, list):
            raw_items = summary
        else:
            raw_items = str(summary).splitlines()
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if isinstance(raw, dict):
                body = str(raw.get("body") or "").strip()
                facts = self._string_list(raw.get("stable_facts"))
                lessons = self._string_list(raw.get("lessons"))
            else:
                body = str(raw).strip()
                if body.startswith("- "):
                    body = body[2:].strip()
                facts, lessons = [], []
            if not body or body in {"无", "None"} or body.lower() == "none":
                if not facts and not lessons:
                    continue
            items.append({"body": body, "stable_facts": facts, "lessons": lessons})
        return items

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _truncate_body(value: Any, max_chars: int) -> str:
        text = str(value or "").strip()
        limit = max(40, int(max_chars))
        if len(text) <= limit:
            if not text or text.endswith(("。", "！", "？", "!", "?", "…")):
                return text
            if len(text) < limit:
                return text.rstrip("，,、：:；; ") + "。"
            return text[: limit - 1].rstrip("，,、：:；; ") + "…"
        prefix = text[:limit].rstrip()
        minimum = max(20, limit // 2)
        for delimiters, replacement in (("。！？!?", ""), ("；;", "。"), ("，,", "。")):
            cutoff = max((prefix.rfind(delimiter) for delimiter in delimiters), default=-1)
            if cutoff + 1 >= minimum:
                result = prefix[: cutoff + 1].rstrip()
                if replacement:
                    result = result[:-1].rstrip() + replacement
                return result
        return prefix[: limit - 1].rstrip("，,、：:；; ") + "…"

    @classmethod
    def _record_from_row(cls, row) -> MemoryRecord:
        try:
            facts = json.loads(row["stable_facts_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            facts = []
        try:
            lessons = json.loads(row["lessons_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            lessons = []
        return cls._record(
            row["id"], row["user_id"], row["session_id"],
            row["start_turn_seq"], row["end_turn_seq"], row["occurred_at"],
            row["body"], facts, lessons, row["created_at"],
        )

    @staticmethod
    def _record(
        episode_id: str,
        user_id: str,
        session_id: str,
        start_turn_seq: int,
        end_turn_seq: int,
        occurred_at: str,
        body: str,
        stable_facts: list[str],
        lessons: list[str],
        created_at: str,
    ) -> MemoryRecord:
        return MemoryRecord(
            id=episode_id,
            content=body,
            source="episodic",
            scope="user",
            created_at=created_at,
            updated_at=created_at,
            path=f"sqlite:episodic/{episode_id}",
            start_line=int(start_turn_seq),
            end_line=int(end_turn_seq),
            user_id=user_id,
            search_text=body,
            metadata={
                "occurred_at": str(occurred_at or ""),
                "stable_facts": list(stable_facts),
                "lessons": list(lessons),
            },
        )

    @staticmethod
    def render_record(record: MemoryRecord) -> str:
        """Render an episode for humans/LLMs without mixing metadata into retrieval text."""
        metadata = record.metadata or {}
        occurred_at = str(metadata.get("occurred_at") or "").strip()
        display_time = occurred_at.replace("T", " ", 1)
        stable_facts = EpisodicMemoryStore._string_list(metadata.get("stable_facts"))
        lessons = EpisodicMemoryStore._string_list(metadata.get("lessons"))
        parts = []
        if display_time:
            parts.append("时间：" + display_time)
        parts.append("事件：" + record.content)
        if stable_facts:
            parts.append("稳定事实：" + "；".join(stable_facts))
        if lessons:
            parts.append("经验：" + "；".join(lessons))
        return "\n".join(parts)

    @staticmethod
    def _render_rows(rows) -> str:
        return "\n\n".join(
            EpisodicMemoryStore.render_record(
                EpisodicMemoryStore._record_from_row(row)
            )
            for row in rows
        )
