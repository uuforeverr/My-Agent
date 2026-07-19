from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class EpisodicMemoryStore:
    def __init__(self, memory_dir: Path):
        self.daily_dir = memory_dir / "daily"
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    def append_summary(
        self,
        summary: Any,
        reason: str,
        session_id: str,
        day: Optional[str] = None,
    ) -> int:
        items = self._normalize_items(summary)
        if not items:
            return 0

        path = self.path_for(day=day, ensure_exists=True)
        block = [
            f"## {self._reason_title(reason)} ({datetime.now().strftime('%H:%M')})",
            "",
            f"session_id: `{session_id}`",
            "",
        ]
        block.extend(f"- {item}" for item in items)
        block.append("")

        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(block))

        return len(items)

    def read_day(self, day: Optional[str] = None) -> str:
        path = self.path_for(day=day, ensure_exists=False)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def read_recent(self, days: int = 1) -> str:
        chunks = []
        for offset in range(max(days, 1)):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            content = self.read_day(day)
            if content:
                chunks.append(content)
        return "\n\n".join(reversed(chunks))

    def list_files(self) -> list[Path]:
        return sorted(self.daily_dir.glob("*.md"))

    def path_for(self, day: Optional[str] = None, ensure_exists: bool = False) -> Path:
        file_day = day or datetime.now().strftime("%Y-%m-%d")
        path = self.daily_dir / f"{file_day}.md"
        if ensure_exists and not path.exists():
            path.write_text(f"# Daily Memory: {file_day}\n\n", encoding="utf-8")
        return path

    def _normalize_items(self, summary: Any) -> list[str]:
        if summary is None:
            return []
        if isinstance(summary, str):
            raw_items = summary.splitlines()
        elif isinstance(summary, list):
            raw_items = [str(item) for item in summary]
        else:
            raw_items = [str(summary)]

        items = []
        for item in raw_items:
            text = item.strip()
            if text.startswith("- "):
                text = text[2:].strip()
            if not text or text in {"无", "None"} or text.lower() == "none":
                continue
            items.append(text)
        return items

    @staticmethod
    def _reason_title(reason: str) -> str:
        titles = {
            "context_trim": "Trimmed Context",
            "trim": "Trimmed Context",
            "overflow": "Context Overflow Recovery",
            "daily_summary": "Daily Summary",
        }
        return titles.get(reason, "Session Notes")
