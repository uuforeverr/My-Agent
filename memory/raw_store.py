import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class RawConversationStore:
    def __init__(self, memory_dir: Path):
        self.raw_dir = memory_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        path = self._path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "created_at": self._now(),
            "session_id": session_id,
            "role": role,
            "content": content,
            "name": name,
            "metadata": metadata or {},
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def read_day(self, session_id: str, day: Optional[str] = None) -> list[dict[str, Any]]:
        return self._read_jsonl(self._path_for(session_id, day=day))

    def read_session(self, session_id: str) -> list[dict[str, Any]]:
        session_dir = self.raw_dir / self._safe_session_id(session_id)
        records = self._read_session_dir(session_dir)
        records.sort(key=lambda item: item.get("created_at") or "")
        return records

    def read_all_sessions(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        if not self.raw_dir.exists():
            return grouped
        for session_dir in self.raw_dir.iterdir():
            if not session_dir.is_dir():
                continue
            records = self._read_session_dir(session_dir)
            for record in records:
                session_id = record.get("session_id") or session_dir.name
                grouped.setdefault(session_id, []).append(record)
        for records in grouped.values():
            records.sort(key=lambda item: item.get("created_at") or "")
        return grouped

    def _read_session_dir(self, session_dir: Path) -> list[dict[str, Any]]:
        if not session_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(session_dir.glob("*.jsonl")):
            records.extend(self._read_jsonl(path))
        return records

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _path_for(self, session_id: str, day: Optional[str] = None) -> Path:
        safe_session_id = self._safe_session_id(session_id)
        file_day = day or datetime.now().strftime("%Y-%m-%d")
        return self.raw_dir / safe_session_id / f"{file_day}.jsonl"

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        text = (session_id or "default").strip()
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
