from datetime import datetime
from pathlib import Path
from typing import Optional


class SemanticMemoryStore:
    def __init__(self, memory_dir: Path):
        self.path = memory_dir / "MEMORY.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_exists()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8").strip()

    def remember(self, content: str, source: str = "manual") -> str:
        text = content.strip()
        if not text:
            return "No memory saved: content is empty."

        current = self.read()
        if self._contains_memory(current, text):
            return "Memory already exists."

        self._ensure_manual_section()
        line = f"- {text}"
        if source:
            line += f"  <!-- source: {source}, at: {self._now()} -->"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return "Semantic memory saved."

    def overwrite(self, content: str) -> None:
        text = content.strip()
        if not text:
            text = "# Semantic Memory\n\n"
        self.path.write_text(text.rstrip() + "\n", encoding="utf-8")

    def _ensure_exists(self) -> None:
        if self.path.exists():
            return
        self.path.write_text(
            "# Semantic Memory\n\n"
            "## User Preferences\n\n"
            "## Project Decisions\n",
            encoding="utf-8",
        )

    def _ensure_manual_section(self) -> None:
        content = self.read()
        if "## Manual Notes" in content:
            if not content.endswith("\n"):
                with self.path.open("a", encoding="utf-8") as f:
                    f.write("\n")
            return

        with self.path.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write("\n## Manual Notes\n\n")

    @staticmethod
    def _contains_memory(content: str, text: str) -> bool:
        needle = text.strip().lower()
        for line in content.splitlines():
            cleaned = line.strip().lstrip("-").strip()
            cleaned = cleaned.split("<!--", 1)[0].strip().lower()
            if cleaned == needle:
                return True
        return False

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
