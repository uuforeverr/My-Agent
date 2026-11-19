import json
import time
from collections import defaultdict, deque
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from typing import Any, Optional

from memory.curator import MemoryCurator
from memory.embeddings import EmbeddingProvider, create_embedding_provider
from memory.episodic_store import EpisodicMemoryStore
from memory.index import MemoryIndex, MemoryRecord, MemorySearchResult
from memory.raw_store import RawConversationStore
from memory.semantic_store import SemanticMemoryError, SemanticMemoryStore
from memory.sop_store import SopStore
from state.database import StateDatabase


MEMORY_OWNER_ID = "local"


class MemoryManager:
    def __init__(
        self,
        memory_dir: Path,
        llm: Optional[Any] = None,
        state_database: Optional[StateDatabase] = None,
        *,
        embedding_provider: str = "disabled",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        embedding_device: str = "cpu",
        embedding_local_files_only: bool = False,
        vector_backend: str = "auto",
        candidate_limit: int = 30,
        rrf_k: int = 60,
        semantic_max_chars: int = 20000,
        raw_write_batch_size: int = 32,
        raw_write_flush_interval_ms: int = 100,
        raw_write_queue_capacity: int = 4096,
        episodic_batch_turns: int = 20,
        episodic_max_wait_seconds: int = 3600,
        episodic_worker_poll_seconds: int = 60,
        episodic_body_max_chars: int = 60,
        embedder: Optional[EmbeddingProvider] = None,
    ):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.state_database = state_database or StateDatabase(memory_dir / "index.db")

        self.raw_store = RawConversationStore(
            memory_dir,
            self.state_database,
            batch_size=raw_write_batch_size,
            flush_interval_ms=raw_write_flush_interval_ms,
            queue_capacity=raw_write_queue_capacity,
        )
        self.episodic_store = EpisodicMemoryStore(
            memory_dir,
            self.state_database,
            body_max_chars=episodic_body_max_chars,
        )
        self.semantic_store = SemanticMemoryStore(memory_dir)
        self.sop_store = SopStore(memory_dir)
        self.embedder = embedder or create_embedding_provider(
            embedding_provider,
            model_id=embedding_model,
            device=embedding_device,
            local_files_only=embedding_local_files_only,
        )
        self.embedding_status = self.embedder.warmup()
        self.semantic_max_chars = max(1000, int(semantic_max_chars))
        self.index = MemoryIndex(
            self.state_database.path,
            embedder=self.embedder,
            vector_backend=vector_backend,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
        )
        self.curator = MemoryCurator(llm)
        self.episodic_batch_turns = max(1, int(episodic_batch_turns))
        self.episodic_max_wait_seconds = max(1, int(episodic_max_wait_seconds))
        self.episodic_worker_poll_seconds = max(1, int(episodic_worker_poll_seconds))
        self.episodic_body_max_chars = max(40, int(episodic_body_max_chars))

        # Only episodic memory is indexed. Semantic memory is injected in full.
        self._episodic_lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory")
        self._closed = False
        self._pending: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._pending_condition = Condition()
        self._pending_stop = Event()

        self._rebuild_episodic_index()
        self._pending_thread = Thread(
            target=self._pending_worker_loop,
            name="episodic-worker",
            daemon=True,
        )
        self._pending_thread.start()

    def remember(
        self,
        content: str,
        source: str = "manual",
        scope: str = "shared",
        metadata: Optional[dict[str, Any]] = None,
        user_id: str = "local",
    ) -> str:
        return self.semantic_store.remember(
            content,
            source=source,
            user_id=MEMORY_OWNER_ID,
        )

    def mutate_semantic_memory(
        self,
        operation: str,
        content: str,
        *,
        memory_id: str = "",
        source: str = "remember_memory",
    ) -> dict[str, str]:
        return self.semantic_store.apply_operation(
            {
                "op": operation,
                "content": content,
                "memory_id": memory_id,
            },
            user_id=MEMORY_OWNER_ID,
            source=source,
        )

    def record_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        turn_seq: Optional[int] = None,
    ):
        return self.raw_store.append_message(
            session_id=session_id,
            role=role,
            content=content,
            name=name,
            metadata=metadata,
            turn_seq=turn_seq,
        )

    @staticmethod
    def _compaction_state_key(session_id: str) -> str:
        return f"session_compaction:{str(session_id or 'default')}"

    def load_compaction_state(self, session_id: str) -> dict[str, Any]:
        state = self.state_database.get_metadata(
            self._compaction_state_key(session_id),
            {},
        )
        return dict(state) if isinstance(state, dict) else {}

    def save_compaction_state(
        self,
        session_id: str,
        *,
        summary: str,
        compact_count: int,
        summarized_through_turn_seq: int,
        raw_path: str,
    ) -> None:
        self.state_database.set_metadata(
            self._compaction_state_key(session_id),
            {
                "summary": str(summary or "").strip(),
                "compact_count": max(0, int(compact_count)),
                "summarized_through_turn_seq": max(
                    0,
                    int(summarized_through_turn_seq),
                ),
                "raw_path": str(raw_path or ""),
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )

    def raw_transcript_pointer(self, session_id: str) -> str:
        return f"sqlite:{self.state_database.path}#raw-session={str(session_id or 'default')}"

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.05,
        *,
        user_id: str = "local",
        trace: Optional[Any] = None,
    ) -> list[MemorySearchResult]:
        results, _metadata = self._search_with_trace(
            query,
            limit=limit,
            min_score=min_score,
            user_id=MEMORY_OWNER_ID,
            trace=trace,
        )
        return results

    def format_for_prompt(self, query: str, limit: int = 5, user_id: str = "local") -> str:
        prompt, _ = self.build_prompt(query, limit=limit, user_id=MEMORY_OWNER_ID)
        return prompt

    def build_prompt(
        self,
        query: str,
        limit: int = 5,
        *,
        user_id: str = "local",
        trace: Optional[Any] = None,
    ) -> tuple[str, int]:
        prompt, count, _details = self.build_prompt_details(
            query,
            limit=limit,
            user_id=MEMORY_OWNER_ID,
        )
        return prompt, count

    def build_prompt_details(
        self,
        query: str,
        limit: int = 5,
        *,
        user_id: str = "local",
    ) -> tuple[str, int, dict[str, Any]]:
        user_id = MEMORY_OWNER_ID
        semantic_started = time.perf_counter()
        semantic_content = self.semantic_store.read(user_id=user_id)
        semantic_truncated = len(semantic_content) > self.semantic_max_chars
        if semantic_truncated:
            semantic_content = semantic_content[: self.semantic_max_chars].rstrip()
        semantic_ms = round((time.perf_counter() - semantic_started) * 1000, 2)
        episodic_results, _search_metadata = self._search_with_trace(
            query,
            limit=limit,
            min_score=0.05,
            user_id=user_id,
            trace=None,
        )

        details = {
            "semantic.duration_ms": semantic_ms,
            "semantic.content_chars": len(semantic_content),
            "semantic.truncated": semantic_truncated,
            "episodic.duration_ms": _search_metadata.get("duration_ms", 0.0),
            "episodic.bm25_duration_ms": _search_metadata.get("bm25_duration_ms", 0.0),
            "episodic.vector_duration_ms": _search_metadata.get("vector_duration_ms", 0.0),
            "episodic.embedding_duration_ms": _search_metadata.get("embedding_duration_ms", 0.0),
            "episodic.result_count": _search_metadata.get("result_count", len(episodic_results)),
            "episodic.result_ids": _search_metadata.get("result_ids", []),
            "episodic.results": [
                {
                    "id": result.record.id,
                    "score": result.score,
                    "path": result.record.path,
                    "content": self.episodic_store.render_record(result.record),
                }
                for result in episodic_results
            ],
        }

        if not semantic_content and not episodic_results:
            return "", 0, details

        sections = [
            "## Relevant memory",
            "",
            "Semantic memory is loaded in full. Episodic memory is retrieved on demand.",
        ]

        if semantic_content:
            sections.extend(["", "### Semantic memory", "", semantic_content])

        if episodic_results:
            sections.extend(["", "### Episodic memory"])
            for result in episodic_results:
                rendered = self.episodic_store.render_record(result.record)
                sections.append("- " + rendered.replace("\n", "\n  "))

        count = (1 if semantic_content else 0) + len(episodic_results)
        return "\n".join(sections), count, details

    def _search_with_trace(
        self,
        query: str,
        *,
        limit: int,
        min_score: float,
        user_id: str,
        trace: Optional[Any],
    ) -> tuple[list[MemorySearchResult], dict[str, Any]]:
        results, metadata = self.index.search_with_metadata(
            query,
            limit=limit,
            min_score=min_score,
            user_id=user_id,
        )
        canonical = self.episodic_store.get_records(
            [result.record.id for result in results]
        )
        results = [
            MemorySearchResult(
                record=canonical.get(result.record.id, result.record),
                score=result.score,
                bm25_rank=result.bm25_rank,
                vector_rank=result.vector_rank,
                vector_backend=result.vector_backend,
            )
            for result in results
        ]
        span_specs = (
            (
                "memory.query_embedding",
                {
                    "provider": self.embedding_status.provider,
                    "model": self.embedding_status.model_id,
                    "dimensions": self.embedding_status.dimensions,
                    "warm": self.embedding_status.available,
                    "measured_duration_ms": metadata.get("embedding_duration_ms", 0.0),
                    "error": self.embedding_status.error,
                },
            ),
            (
                "memory.bm25_search",
                {
                    "user_id": user_id,
                    "candidate_count": metadata.get("bm25_candidates", 0),
                    "measured_duration_ms": metadata.get("bm25_duration_ms", 0.0),
                },
            ),
            (
                "memory.vector_search",
                {
                    "user_id": user_id,
                    "backend": metadata.get("vector_backend", "disabled"),
                    "candidate_count": metadata.get("vector_candidates", 0),
                    "measured_duration_ms": metadata.get("vector_duration_ms", 0.0),
                    "fallback_reason": self.index.vector_error,
                },
            ),
            (
                "memory.rrf_fusion",
                {
                    "rrf_k": metadata.get("rrf_k", 60),
                    "result_count": metadata.get("result_count", 0),
                    "result_ids": metadata.get("result_ids", []),
                    "total_retrieval_ms": metadata.get("duration_ms", 0.0),
                },
            ),
        )
        for name, attributes in span_specs:
            with self._trace_span(trace, name, span_kind="memory.retrieve", **attributes):
                pass
        return results, metadata

    @staticmethod
    def _trace_span(trace: Optional[Any], name: str, **attributes: Any):
        if trace is None:
            return nullcontext(None)
        return trace.start_span(name, **attributes)

    def read_main_memory(self, user_id: str = "local") -> str:
        return self.semantic_store.read(user_id=MEMORY_OWNER_ID)

    def read_today_episodic_memory(self, user_id: Optional[str] = None) -> str:
        return self.episodic_store.read_day(user_id=MEMORY_OWNER_ID)

    def list_records(self, limit: int = 80, user_id: str = "local") -> list[MemoryRecord]:
        return self.index.list_recent(limit=limit, user_id=MEMORY_OWNER_ID)

    def flush_from_messages(
        self,
        messages: list[dict[str, Any]],
        reason: str = "context_trim",
        session_id: str = "default",
        max_messages: int = 12,
        user_id: str = "local",
    ) -> dict[str, Any]:
        if not messages:
            return {"ok": True, "skipped": True, "reason": "empty_messages", "async": False}

        if self._closed:
            return {"ok": False, "skipped": True, "reason": "manager_closed", "async": False}

        snapshot = list(messages)
        future = self._executor.submit(
            self._flush_worker,
            snapshot,
            reason,
            session_id,
            max_messages,
            MEMORY_OWNER_ID,
        )
        return {
            "ok": True,
            "skipped": False,
            "async": True,
            "reason": reason,
            "future": future,
        }

    def deep_dream(self, lookback_days: int = 1, user_id: str = "local") -> dict[str, Any]:
        user_id = MEMORY_OWNER_ID
        semantic_memory = self.semantic_store.read(user_id=user_id)
        episodic_memory, positions = self.episodic_store.read_after_cursor(user_id)
        if not positions:
            return {"ok": False, "skipped": True, "reason": "no_new_episodic"}
        validation_error = ""
        applied: dict[str, int] | None = None
        dream_diary = ""
        for attempt in range(2):
            try:
                operations, dream_diary = self.curator.deep_dream(
                    semantic_memory=semantic_memory,
                    episodic_memory=episodic_memory,
                    validation_error=validation_error,
                )
                applied = self.semantic_store.apply_operations(
                    operations,
                    user_id=user_id,
                    source="dream",
                )
                break
            except (SemanticMemoryError, ValueError) as exc:
                details = getattr(exc, "details", {})
                validation_error = json.dumps(
                    {
                        "error": str(exc),
                        "code": getattr(exc, "code", "invalid_function_call"),
                        "details": details,
                    },
                    ensure_ascii=False,
                )
                if attempt == 1:
                    return {
                        "ok": False,
                        "skipped": False,
                        "reason": "invalid_dream_function_call",
                        "error": validation_error,
                    }
        if applied is None:
            return {"ok": False, "skipped": False, "reason": "no_dream_output"}
        self.episodic_store.set_dream_cursor(user_id, positions[-1])
        dream_path = self._write_dream_diary(dream_diary)
        return {
            "ok": True,
            "skipped": False,
            "dream_path": str(dream_path) if dream_path else "",
            "operations": applied,
        }

    def distill_workflows(self, lookback_days: int = 3, user_id: str = "local") -> dict[str, Any]:
        user_id = MEMORY_OWNER_ID
        with self._episodic_lock:
            existing_sop = self.sop_store.read()
            episodic = self.episodic_store.read_recent(days=lookback_days, user_id=user_id)

        new_sop, insights = self.curator.distill_workflows(
            existing_sop=existing_sop,
            episodic_recent=episodic,
        )
        if not new_sop:
            return {"ok": False, "skipped": True, "reason": "no_sop_output"}

        with self._episodic_lock:
            self.sop_store.overwrite(new_sop)
            log_path = self.sop_store.append_log(
                new_sop,
                insights,
                {"lookback_days": lookback_days},
            )
        return {
            "ok": True,
            "skipped": False,
            "sop_path": str(self.sop_store.path),
            "log_path": str(log_path),
            "insights": insights,
        }

    def enqueue_pending_turns(
        self,
        session_id: str,
        turns: list[dict[str, Any]],
        *,
        user_id: str = "local",
    ) -> int:
        """Add complete turns that have left the SessionWindow."""
        if self._closed or not turns:
            return 0
        added = 0
        now = time.monotonic()
        with self._pending_condition:
            target = self._pending[str(session_id or "default")]
            known = {int(item.get("turn_seq") or 0) for item in target}
            for turn in turns:
                seq = int(turn.get("turn_seq") or 0)
                if seq <= 0 or seq in known:
                    continue
                target.append({
                    "session_id": str(session_id or "default"),
                    "turn_seq": seq,
                    "started_at": turn.get("started_at") or "",
                    "messages": list(turn.get("messages") or []),
                    "user_id": MEMORY_OWNER_ID,
                    "source_user_id": str(turn.get("user_id") or user_id or "local"),
                    "enqueued_at": now,
                })
                known.add(seq)
                added += 1
            if added:
                self._pending_condition.notify_all()
        return added

    def enqueue_pending_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        user_id: str = "local",
    ) -> int:
        turns: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        for message in messages:
            if message.get("role") == "user":
                if current is not None:
                    turns.append(current)
                current = {
                    "turn_seq": int(message.get("_turn_seq") or 0),
                    "started_at": message.get("_created_at") or "",
                    "messages": [],
                    "user_id": user_id,
                }
            if current is not None:
                clean = {key: value for key, value in message.items() if not key.startswith("_")}
                current["messages"].append(clean)
        if current is not None:
            turns.append(current)
        return self.enqueue_pending_turns(session_id, turns, user_id=user_id)

    def recover_pending(
        self,
        session_id: str,
        session_window_turns: int,
        *,
        summarized_through_turn_seq: int = 0,
        flush_raw: bool = True,
    ) -> int:
        cursor = self._episodic_cursor(session_id)
        turns = self.raw_store.read_pending_turns(
            session_id,
            after_turn_seq=cursor,
            exclude_recent=session_window_turns,
            through_turn_seq=summarized_through_turn_seq,
            flush=flush_raw,
        )
        return self.enqueue_pending_turns(session_id, turns)

    def pending_count(self, session_id: str) -> int:
        with self._pending_condition:
            return len(self._pending.get(str(session_id or "default"), ()))

    def _pending_worker_loop(self) -> None:
        while not self._pending_stop.is_set():
            with self._pending_condition:
                self._pending_condition.wait(timeout=self.episodic_worker_poll_seconds)
                if self._pending_stop.is_set():
                    return
                sessions = list(self._pending)
            for session_id in sessions:
                self._process_pending_session(session_id)

    def _process_pending_session(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        with self._episodic_lock:
            return self._process_pending_session_locked(session_id, force=force)

    def _process_pending_session_locked(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        with self._pending_condition:
            pending = self._pending.get(session_id)
            if not pending:
                return {"ok": True, "skipped": True, "reason": "empty_buffer"}
            oldest_age = time.monotonic() - float(pending[0].get("enqueued_at") or 0.0)
            if len(pending) >= self.episodic_batch_turns:
                take = self.episodic_batch_turns
            elif force or oldest_age >= self.episodic_max_wait_seconds:
                take = len(pending)
            else:
                return {"ok": True, "skipped": True, "reason": "below_threshold"}
            batch = [dict(item) for item in list(pending)[:take]]

        messages = [
            message
            for turn in batch
            for message in turn.get("messages") or []
        ]
        user_id = MEMORY_OWNER_ID
        try:
            items = self.curator.summarize_daily(
                messages=messages,
                existing_daily_memory="",
                session_id=session_id,
                reason="pending_buffer",
                max_messages=0,
                body_max_chars=self.episodic_body_max_chars,
            )
            start_seq = int(batch[0]["turn_seq"])
            end_seq = int(batch[-1]["turn_seq"])
            occurred_at = str(batch[-1].get("started_at") or datetime.now().astimezone().isoformat())
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with self.state_database.write() as conn:
                records = self.episodic_store.insert_batch(
                    conn,
                    items,
                    session_id=session_id,
                    user_id=user_id,
                    start_turn_seq=start_seq,
                    end_turn_seq=end_seq,
                    occurred_at=occurred_at,
                )
                conn.execute(
                    """
                    INSERT INTO episodic_cursors(session_id, last_turn_seq, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        last_turn_seq=excluded.last_turn_seq,
                        updated_at=excluded.updated_at
                    """,
                    (session_id, end_seq, now),
                )
            for record in records:
                self.index.upsert_record(record)
        except Exception as exc:
            return {"ok": False, "skipped": False, "error": f"{type(exc).__name__}: {exc}"}

        with self._pending_condition:
            target = self._pending.get(session_id)
            if target is not None:
                consumed = {int(item["turn_seq"]) for item in batch}
                while target and int(target[0].get("turn_seq") or 0) in consumed:
                    target.popleft()
                if not target:
                    self._pending.pop(session_id, None)
        return {
            "ok": True,
            "skipped": not bool(records),
            "processed_turns": len(batch),
            "episodic_items": len(records),
            "last_turn_seq": end_seq,
        }

    def _episodic_cursor(self, session_id: str) -> int:
        with self.state_database.read() as conn:
            row = conn.execute(
                "SELECT last_turn_seq FROM episodic_cursors WHERE session_id = ?",
                (str(session_id or "default"),),
            ).fetchone()
        return int(row["last_turn_seq"] if row is not None else 0)

    def scan_raw_for_episodic(
        self,
        keep_recent_turns: int = 3,
        min_flush_turns: int = 20,
    ) -> dict[str, Any]:
        if self._closed:
            return {"ok": False, "skipped": True, "reason": "manager_closed", "sessions": []}
        if self.curator.llm is None:
            return {"ok": True, "skipped": True, "reason": "no_llm", "sessions": []}

        keep = max(1, int(keep_recent_turns))
        min_batch = max(0, int(min_flush_turns))
        grouped = self.raw_store.read_all_sessions()
        sessions: list[dict[str, Any]] = []
        with self._episodic_lock:
            watermarks = self._load_watermarks()
            for session_id, records in grouped.items():
                result = self._scan_session_locked(
                    session_id,
                    records,
                    keep,
                    min_batch,
                    watermarks,
                )
                sessions.append(result)
            self._save_watermarks(watermarks)
        return {"ok": True, "skipped": False, "sessions": sessions}

    def close(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_stop.set()
        with self._pending_condition:
            self._pending_condition.notify_all()
        if wait:
            self._pending_thread.join(timeout=5.0)
        self.raw_store.close(wait=wait)
        self._executor.shutdown(wait=wait)
        self.index.close()

    def _scan_session_locked(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        keep: int,
        min_batch: int,
        watermarks: dict[str, Any],
    ) -> dict[str, Any]:
        user_indices = [
            index for index, record in enumerate(records) if record.get("role") == "user"
        ]
        total_turns = len(user_indices)
        flushed = int((watermarks.get(session_id) or {}).get("flushed_user_turns") or 0)
        pending = total_turns - flushed
        threshold = keep + min_batch
        if pending <= threshold:
            return {
                "session_id": session_id,
                "skipped": True,
                "reason": "below_flush_threshold",
                "total_turns": total_turns,
                "flushed_user_turns": flushed,
                "threshold": threshold,
            }

        start_turn = flushed
        end_turn = total_turns - keep
        messages = self._extract_user_assistant_turns(records, user_indices, start_turn, end_turn)
        user_id = self._records_user_id(records)
        flush_result = self._write_episodic_from_messages(
            messages,
            reason="raw_watermark",
            session_id=session_id,
            max_messages=0,
            user_id=user_id,
        )
        if flush_result.get("ok"):
            watermarks[session_id] = {"flushed_user_turns": end_turn}
            flushed = end_turn
        return {
            "session_id": session_id,
            "skipped": False,
            "ok": bool(flush_result.get("ok")),
            "total_turns": total_turns,
            "flushed_user_turns": flushed,
            "extracted_turns": end_turn - start_turn,
            "episodic_items": flush_result.get("episodic_items", 0),
        }

    @staticmethod
    def _extract_user_assistant_turns(
        records: list[dict[str, Any]],
        user_indices: list[int],
        start_turn: int,
        end_turn: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for turn_index in range(start_turn, end_turn):
            start = user_indices[turn_index]
            stop = user_indices[turn_index + 1] if turn_index + 1 < len(user_indices) else len(records)
            for record in records[start:stop]:
                role = record.get("role")
                if role not in {"user", "assistant"}:
                    continue
                messages.append({
                    "role": role,
                    "content": record.get("content") or "",
                })
        return messages

    def _load_watermarks(self) -> dict[str, Any]:
        if self.state_database is not None:
            stored = self.state_database.get_metadata("episodic_watermarks")
            if isinstance(stored, dict):
                return stored
            legacy = self._load_legacy_watermarks()
            if legacy:
                self.state_database.set_metadata("episodic_watermarks", legacy)
            return legacy
        return self._load_legacy_watermarks()

    def _load_legacy_watermarks(self) -> dict[str, Any]:
        path = self.memory_dir / "episodic_watermark.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_watermarks(self, watermarks: dict[str, Any]) -> None:
        if self.state_database is not None:
            self.state_database.set_metadata("episodic_watermarks", watermarks)
            return
        path = self.memory_dir / "episodic_watermark.json"
        path.write_text(
            json.dumps(watermarks, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _flush_worker(
        self,
        messages: list[dict[str, Any]],
        reason: str,
        session_id: str,
        max_messages: int,
        user_id: str,
    ) -> dict[str, Any]:
        with self._episodic_lock:
            return self._write_episodic_from_messages(
                messages,
                reason=reason,
                session_id=session_id,
                max_messages=max_messages,
                user_id=user_id,
            )

    def _write_episodic_from_messages(
        self,
        messages: list[dict[str, Any]],
        reason: str,
        session_id: str,
        max_messages: int,
        user_id: str = "local",
    ) -> dict[str, Any]:
        user_id = MEMORY_OWNER_ID
        try:
            existing_daily = self.episodic_store.read_day(user_id=user_id)
            summary = self.curator.summarize_daily(
                messages=messages,
                existing_daily_memory=existing_daily,
                session_id=session_id,
                reason=reason,
                max_messages=max_messages,
                body_max_chars=self.episodic_body_max_chars,
            )
            records = self.episodic_store.append_summary(
                summary,
                reason=reason,
                session_id=session_id,
                user_id=user_id,
            )
            for record in records:
                self.index.upsert_record(record)
            count = len(records)
            result = {
                "ok": True,
                "skipped": count == 0,
                "episodic_items": count,
                "reason": reason,
            }
            if count == 0:
                result["skip_reason"] = "no_summary_items"
            return result
        except Exception as e:
            return {
                "ok": False,
                "skipped": False,
                "error": f"{type(e).__name__}: {e}",
            }

    def _rebuild_episodic_index(self) -> None:
        with self._episodic_lock:
            self.index.clear()
            for record in self.episodic_store.list_records():
                self.index.upsert_record(record)

    @staticmethod
    def _records_user_id(records: list[dict[str, Any]]) -> str:
        return MEMORY_OWNER_ID

    def _write_dream_diary(self, content: str) -> Optional[Path]:
        text = content.strip()
        if not text:
            return None

        dreams_dir = self.memory_dir / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        path = dreams_dir / f"{today}.md"
        path.write_text(f"# Dream Diary: {today}\n\n{text}\n", encoding="utf-8")
        return path
