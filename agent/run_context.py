from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

@dataclass
class RunContext:
    session_id: str
    workspace: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        session_id: str,
        workspace: Path,
        **metadata: Any,
    ) -> "RunContext":
        return cls(
            session_id=session_id,
            workspace=workspace.resolve(),
            metadata=dict(metadata),
        )

    def add_metadata(self, **data: Any) -> None:
        self.metadata.update(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)


@dataclass
class TurnResult:
    outcome: str = "success"
    trace_id: str = ""
    recovered_error_count: int = 0
    waiting_approval_id: str = ""
