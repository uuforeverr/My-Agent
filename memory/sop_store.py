from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class SopStore:
    """Rolling SOP knowledge distilled from episodic memory.

    SOP.md is a single file iterated in place (like MEMORY.md): each distillation
    overwrites it with a refined version built on top of the previous one. Every
    iteration is also appended to a timestamped log so the evolution is auditable.
    Not injected into conversation prompts.
    """

    SKELETON = "# Standard Operating Procedures\n\n_(No SOP distilled yet.)_\n"

    def __init__(self, memory_dir: Path):
        self.root = memory_dir / "workflows"
        self.log_dir = self.root / "log"
        self.path = self.root / "SOP.md"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_exists()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8").strip()

    def overwrite(self, content: str) -> None:
        text = content.strip()
        if not text:
            text = self.SKELETON
        self.path.write_text(text.rstrip() + "\n", encoding="utf-8")

    def append_log(
        self,
        sop: str,
        insights: str,
        meta: Optional[dict[str, Any]] = None,
    ) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        path = self.log_dir / f"{stamp}.md"
        meta = meta or {}
        meta_lines = [f"- {key}: {value}" for key, value in meta.items()]
        parts = [
            f"# SOP distillation @ {self._now()}",
            "",
            "## Meta",
            *(meta_lines or ["- (none)"]),
            "",
            "## SOP",
            sop.strip() or "(empty)",
            "",
            "## Insights",
            insights.strip() or "(none)",
            "",
        ]
        path.write_text("\n".join(parts), encoding="utf-8")
        return path

    def _ensure_exists(self) -> None:
        if not self.path.exists():
            self.path.write_text(self.SKELETON, encoding="utf-8")

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
