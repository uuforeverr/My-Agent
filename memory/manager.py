from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from memory.curator import MemoryCurator
from memory.episodic_store import EpisodicMemoryStore
from memory.index import MemoryIndex, MemoryRecord, MemorySearchResult
from memory.raw_store import RawConversationStore
from memory.semantic_store import SemanticMemoryStore
from memory.sop_store import SopStore


class MemoryManager:
    def __init__(self, memory_dir: Path, llm: Optional[Any] = None):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.raw_store = RawConversationStore(memory_dir)
        self.episodic_store = EpisodicMemoryStore(memory_dir)
        self.semantic_store = SemanticMemoryStore(memory_dir)
        self.sop_store = SopStore(memory_dir)
        self.index = MemoryIndex(memory_dir / "index.db")
        self.curator = MemoryCurator(llm)

        # Only episodic memory is indexed. Semantic memory is injected in full.
        self._episodic_lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory")
        self._closed = False

        self._rebuild_episodic_index()

    def remember(
        self,
        content: str,
        source: str = "manual",
        scope: str = "shared",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        return self.semantic_store.remember(content, source=source)

    def record_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        return self.raw_store.append_message(
            session_id=session_id,
            role=role,
            content=content,
            name=name,
            metadata=metadata,
        )

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.05,
    ) -> list[MemorySearchResult]:
        return self.index.search(query, limit=limit, min_score=min_score)

    def format_for_prompt(self, query: str, limit: int = 5) -> str:
        prompt, _ = self.build_prompt(query, limit=limit)
        return prompt

    def build_prompt(self, query: str, limit: int = 5) -> tuple[str, int]:
        semantic_content = self.semantic_store.read()
        episodic_results = self.search(query, limit=limit)

        if not semantic_content and not episodic_results:
            return "", 0

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
                sections.append(
                    f"- {result.record.content} "
                    f"(source={result.record.path}:"
                    f"{result.record.start_line}-{result.record.end_line})"
                )

        count = (1 if semantic_content else 0) + len(episodic_results)
        return "\n".join(sections), count

    def read_main_memory(self) -> str:
        return self.semantic_store.read()

    def read_today_episodic_memory(self) -> str:
        return self.episodic_store.read_day()

    def list_records(self, limit: int = 80) -> list[MemoryRecord]:
        return self.index.list_recent(limit=limit)

    def flush_from_messages(
        self,
        messages: list[dict[str, Any]],
        reason: str = "context_trim",
        session_id: str = "default",
        max_messages: int = 12,
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
        )
        return {
            "ok": True,
            "skipped": False,
            "async": True,
            "reason": reason,
            "future": future,
        }

    def deep_dream(self, lookback_days: int = 1) -> dict[str, Any]:
        semantic_memory = self.semantic_store.read()
        episodic_memory = self.episodic_store.read_recent(days=lookback_days)
        new_memory, dream_diary = self.curator.deep_dream(
            semantic_memory=semantic_memory,
            episodic_memory=episodic_memory,
        )
        if not new_memory:
            return {"ok": False, "skipped": True, "reason": "no_dream_output"}

        self.semantic_store.overwrite(new_memory)
        dream_path = self._write_dream_diary(dream_diary)
        return {
            "ok": True,
            "skipped": False,
            "dream_path": str(dream_path) if dream_path else "",
        }

    def distill_workflows(self, lookback_days: int = 3) -> dict[str, Any]:
        with self._episodic_lock:
            existing_sop = self.sop_store.read()
            episodic = self.episodic_store.read_recent(days=lookback_days)

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

    def close(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait)
        self.index.close()

    def _flush_worker(
        self,
        messages: list[dict[str, Any]],
        reason: str,
        session_id: str,
        max_messages: int,
    ) -> dict[str, Any]:
        with self._episodic_lock:
            try:
                existing_daily = self.episodic_store.read_day()
                summary = self.curator.summarize_daily(
                    messages=messages,
                    existing_daily_memory=existing_daily,
                    session_id=session_id,
                    reason=reason,
                    max_messages=max_messages,
                )
                count = self.episodic_store.append_summary(
                    summary,
                    reason=reason,
                    session_id=session_id,
                )
                if count:
                    self._reindex_today_episodic()
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
            files = [(path, "episodic") for path in self.episodic_store.list_files()]
            self.index.rebuild(files, root=self.memory_dir)

    def _reindex_today_episodic(self) -> None:
        today_path = self.episodic_store.path_for(ensure_exists=False)
        if today_path.exists():
            self.index.reindex_file(today_path, source_type="episodic", root=self.memory_dir)

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
