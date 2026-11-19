from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from state.database import StateDatabase


@dataclass(frozen=True)
class RawWriteReceipt:
    session_id: str
    turn_seq: int
    message_seq: int


class RawWriteQueueFull(RuntimeError):
    pass


class RawConversationStore:
    """Asynchronous SQLite store for complete conversation turns."""

    def __init__(
        self,
        memory_dir: Path,
        state_database: Optional[StateDatabase] = None,
        *,
        batch_size: int = 32,
        flush_interval_ms: int = 100,
        queue_capacity: int = 4096,
    ):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.database = state_database or StateDatabase(self.memory_dir / "index.db")
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.001, int(flush_interval_ms) / 1000.0)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(queue_capacity)))
        self._sequence_lock = threading.RLock()
        self._turn_sequences: dict[str, int] = {}
        self._message_sequences: dict[tuple[str, int], int] = {}
        self._active_turns: dict[str, int] = {}
        self._closed = False
        self.last_error = ""
        self._migrate_legacy_jsonl_once()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="raw-sqlite-writer",
            daemon=True,
        )
        self._writer.start()

    def begin_turn(
        self,
        session_id: str,
        *,
        user_id: str = "local",
        channel_type: str = "",
        created_at: Optional[str] = None,
    ) -> int:
        session = str(session_id or "default")
        with self._sequence_lock:
            current = self._turn_sequences.get(session)
            if current is None:
                with self.database.read() as conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(turn_seq), 0) AS value FROM raw_turns WHERE session_id = ?",
                        (session,),
                    ).fetchone()
                current = int(row["value"] if row is not None else 0)
            turn_seq = current + 1
            self._turn_sequences[session] = turn_seq
            self._message_sequences[(session, turn_seq)] = 0
            self._active_turns[session] = turn_seq
        now = created_at or self._now()
        self._enqueue({
            "kind": "begin",
            "session_id": session,
            "turn_seq": turn_seq,
            "user_id": str(user_id or "local"),
            "channel_type": str(channel_type or ""),
            "created_at": now,
        })
        return turn_seq

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        *,
        turn_seq: Optional[int] = None,
    ) -> RawWriteReceipt:
        session = str(session_id or "default")
        meta = dict(metadata or {})
        if turn_seq is None:
            if role == "user":
                turn_seq = self.begin_turn(
                    session,
                    user_id=str(meta.get("user_id") or "local"),
                    channel_type=str(meta.get("channel_type") or meta.get("entrypoint") or ""),
                )
            else:
                with self._sequence_lock:
                    turn_seq = self._active_turns.get(session)
                if turn_seq is None:
                    turn_seq = self.begin_turn(session)

        key = (session, int(turn_seq))
        with self._sequence_lock:
            message_seq = self._message_sequences.get(key, 0) + 1
            self._message_sequences[key] = message_seq
            self._active_turns[session] = int(turn_seq)
        self._enqueue({
            "kind": "message",
            "session_id": session,
            "turn_seq": int(turn_seq),
            "message_seq": message_seq,
            "role": str(role),
            "content": content,
            "name": name,
            "metadata": meta,
            "created_at": self._now(),
        })

        if role == "assistant" and not (meta.get("tool_calls") or []):
            self.complete_turn(session, int(turn_seq))
        return RawWriteReceipt(session, int(turn_seq), message_seq)

    def complete_turn(
        self,
        session_id: str,
        turn_seq: Optional[int] = None,
        *,
        completed_at: Optional[str] = None,
    ) -> None:
        session = str(session_id or "default")
        with self._sequence_lock:
            seq = int(turn_seq or self._active_turns.get(session) or 0)
            if not seq:
                return
            if self._active_turns.get(session) == seq:
                self._active_turns.pop(session, None)
        self._enqueue({
            "kind": "complete",
            "session_id": session,
            "turn_seq": seq,
            "completed_at": completed_at or self._now(),
        })

    def current_turn_seq(self, session_id: str) -> Optional[int]:
        with self._sequence_lock:
            return self._active_turns.get(str(session_id or "default"))

    def seed_session_sequence(self, session_id: str, turn_seq: int) -> None:
        """Seed the in-memory allocator from an already-read durable snapshot."""
        session = str(session_id or "default")
        with self._sequence_lock:
            self._turn_sequences[session] = max(
                int(turn_seq),
                int(self._turn_sequences.get(session) or 0),
            )

    def read_recent_turns(
        self,
        session_id: str,
        limit: int,
        *,
        flush: bool = True,
    ) -> list[dict[str, Any]]:
        if flush:
            self.flush()
        session = str(session_id or "default")
        with self.database.read() as conn:
            rows = conn.execute(
                """
                WITH recent AS (
                    SELECT id, turn_seq, started_at, completed_at
                    FROM raw_turns
                    WHERE session_id = ? AND status = 'complete'
                    ORDER BY turn_seq DESC
                    LIMIT ?
                )
                SELECT recent.turn_seq, recent.started_at, recent.completed_at,
                       m.message_seq, m.role, m.content_json, m.name,
                       m.metadata_json, m.created_at
                FROM recent
                JOIN raw_messages AS m ON m.turn_id = recent.id
                ORDER BY recent.turn_seq ASC, m.message_seq ASC
                """,
                (session, max(0, int(limit))),
            ).fetchall()
        return self._rows_to_turns(rows, session)

    def read_pending_turns(
        self,
        session_id: str,
        *,
        after_turn_seq: int,
        exclude_recent: int,
        through_turn_seq: int = 0,
        flush: bool = True,
    ) -> list[dict[str, Any]]:
        if flush:
            self.flush()
        session = str(session_id or "default")
        offset = max(0, int(exclude_recent))
        with self.database.read() as conn:
            cutoff_row = conn.execute(
                """
                SELECT turn_seq FROM raw_turns
                WHERE session_id = ? AND status = 'complete'
                ORDER BY turn_seq DESC LIMIT 1 OFFSET ?
                """,
                (session, offset),
            ).fetchone()
            cutoff = max(
                int(cutoff_row["turn_seq"] if cutoff_row is not None else 0),
                max(0, int(through_turn_seq)),
            )
            if cutoff <= int(after_turn_seq):
                return []
            rows = conn.execute(
                """
                SELECT t.turn_seq, t.started_at, t.completed_at,
                       m.message_seq, m.role, m.content_json, m.name,
                       m.metadata_json, m.created_at
                FROM raw_turns AS t
                JOIN raw_messages AS m ON m.turn_id = t.id
                WHERE t.session_id = ? AND t.status = 'complete'
                  AND t.turn_seq > ? AND t.turn_seq <= ?
                ORDER BY t.turn_seq ASC, m.message_seq ASC
                """,
                (session, int(after_turn_seq), cutoff),
            ).fetchall()
        return self._rows_to_turns(rows, session)

    def read_session(self, session_id: str) -> list[dict[str, Any]]:
        turns = self.read_recent_turns(session_id, 1_000_000)
        return [message for turn in turns for message in turn["messages"]]

    def read_day(self, session_id: str, day: Optional[str] = None) -> list[dict[str, Any]]:
        records = self.read_session(session_id)
        if not day:
            return records
        return [item for item in records if str(item.get("created_at") or "").startswith(day)]

    def read_all_sessions(self) -> dict[str, list[dict[str, Any]]]:
        self.flush()
        with self.database.read() as conn:
            sessions = [
                row["session_id"]
                for row in conn.execute(
                    "SELECT DISTINCT session_id FROM raw_turns WHERE status = 'complete'"
                ).fetchall()
            ]
        return {session: self.read_session(session) for session in sessions}

    def flush(self, timeout: float = 5.0) -> bool:
        if self._closed:
            return True
        done = threading.Event()
        self._enqueue({"kind": "barrier", "done": done})
        return done.wait(timeout=max(0.0, float(timeout)))

    def close(self, wait: bool = True) -> None:
        if self._closed:
            return
        if wait:
            self.flush()
        self._closed = True
        done = threading.Event()
        try:
            self._queue.put_nowait({"kind": "stop", "done": done})
        except queue.Full:
            self._queue.put({"kind": "stop", "done": done})
        if wait:
            done.wait(timeout=5.0)
            self._writer.join(timeout=5.0)

    def _enqueue(self, event: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("raw conversation store is closed")
        try:
            self._queue.put_nowait(event)
        except queue.Full as exc:
            raise RawWriteQueueFull("raw write queue is full") from exc

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.flush_interval
            while len(batch) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break

            stop = any(item.get("kind") == "stop" for item in batch)
            signals = [item.get("done") for item in batch if item.get("kind") in {"barrier", "stop"}]
            try:
                while True:
                    try:
                        self._apply_batch(batch)
                        self.last_error = ""
                        break
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        if self._closed:
                            break
                        time.sleep(min(1.0, self.flush_interval * 5))
            finally:
                for signal in signals:
                    if signal is not None:
                        signal.set()
                for _item in batch:
                    self._queue.task_done()
            if stop:
                return

    def _apply_batch(self, batch: list[dict[str, Any]]) -> None:
        writable = [item for item in batch if item.get("kind") in {"begin", "message", "complete"}]
        if not writable:
            return
        with self.database.write() as conn:
            for item in writable:
                kind = item["kind"]
                if kind == "begin":
                    conn.execute(
                        """
                        INSERT INTO raw_turns(
                            session_id, turn_seq, user_id, channel_type, status,
                            started_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
                        ON CONFLICT(session_id, turn_seq) DO NOTHING
                        """,
                        (
                            item["session_id"], item["turn_seq"], item["user_id"],
                            item["channel_type"], item["created_at"], item["created_at"],
                            item["created_at"],
                        ),
                    )
                    continue
                row = conn.execute(
                    "SELECT id FROM raw_turns WHERE session_id = ? AND turn_seq = ?",
                    (item["session_id"], item["turn_seq"]),
                ).fetchone()
                if row is None:
                    continue
                if kind == "message":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO raw_messages(
                            turn_id, message_seq, role, content_json, name,
                            metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"], item["message_seq"], item["role"],
                            json.dumps(item["content"], ensure_ascii=False, default=str),
                            item.get("name"),
                            json.dumps(item.get("metadata") or {}, ensure_ascii=False, default=str),
                            item["created_at"],
                        ),
                    )
                elif kind == "complete":
                    conn.execute(
                        """
                        UPDATE raw_turns SET status = 'complete', completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (item["completed_at"], item["completed_at"], row["id"]),
                    )

    def _migrate_legacy_jsonl_once(self) -> None:
        if self.database.get_metadata("raw_jsonl_migrated_v1"):
            return
        raw_dir = self.memory_dir / "raw"
        if not raw_dir.exists():
            self.database.set_metadata("raw_jsonl_migrated_v1", True)
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(raw_dir.glob("*/*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                session = str(item.get("session_id") or path.parent.name)
                grouped.setdefault(session, []).append(item)
        with self.database.write() as conn:
            for session_id, records in grouped.items():
                records.sort(key=lambda item: str(item.get("created_at") or ""))
                turns: list[list[dict[str, Any]]] = []
                current: list[dict[str, Any]] = []
                for record in records:
                    if record.get("role") == "user":
                        if current:
                            turns.append(current)
                        current = [record]
                    elif current:
                        current.append(record)
                if current:
                    turns.append(current)
                for turn_seq, messages in enumerate(turns, 1):
                    if not any(message.get("role") == "assistant" for message in messages):
                        continue
                    started_at = str(messages[0].get("created_at") or self._now())
                    completed_at = str(messages[-1].get("created_at") or started_at)
                    metadata = messages[0].get("metadata") or {}
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO raw_turns(
                            session_id, turn_seq, user_id, channel_type, status,
                            started_at, completed_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'complete', ?, ?, ?, ?)
                        """,
                        (
                            session_id, turn_seq, str(metadata.get("user_id") or "local"),
                            str(metadata.get("channel_type") or metadata.get("entrypoint") or ""),
                            started_at, completed_at, started_at, completed_at,
                        ),
                    )
                    row = conn.execute(
                        "SELECT id FROM raw_turns WHERE session_id = ? AND turn_seq = ?",
                        (session_id, turn_seq),
                    ).fetchone()
                    if row is None:
                        continue
                    for message_seq, message in enumerate(messages, 1):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO raw_messages(
                                turn_id, message_seq, role, content_json, name,
                                metadata_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"], message_seq, str(message.get("role") or ""),
                                json.dumps(message.get("content"), ensure_ascii=False, default=str),
                                message.get("name"),
                                json.dumps(message.get("metadata") or {}, ensure_ascii=False, default=str),
                                str(message.get("created_at") or started_at),
                            ),
                        )
        self.database.set_metadata("raw_jsonl_migrated_v1", True)

    @staticmethod
    def _rows_to_turns(rows, session_id: str) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        by_seq: dict[int, dict[str, Any]] = {}
        for row in rows:
            seq = int(row["turn_seq"])
            turn = by_seq.get(seq)
            if turn is None:
                turn = {
                    "session_id": session_id,
                    "turn_seq": seq,
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "messages": [],
                }
                by_seq[seq] = turn
                turns.append(turn)
            try:
                content = json.loads(row["content_json"])
            except (TypeError, json.JSONDecodeError):
                content = row["content_json"]
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            turn["messages"].append({
                "created_at": row["created_at"],
                "session_id": session_id,
                "turn_seq": seq,
                "message_seq": int(row["message_seq"]),
                "role": row["role"],
                "content": content,
                "name": row["name"],
                "metadata": metadata,
            })
        return turns

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        text = (session_id or "default").strip()
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")
