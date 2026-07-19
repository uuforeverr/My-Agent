from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from observability import Trace


@dataclass
class RunContext:
    session_id: str
    workspace: Path
    trace: Trace = field(default_factory=Trace)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        session_id: str,
        workspace: Path,
        trace: Optional[Trace] = None,
        **metadata: Any,
    ) -> "RunContext":
        return cls(
            session_id=session_id,
            workspace=workspace.resolve(),
            trace=trace or Trace(),
            metadata=dict(metadata),
        )

    def add_metadata(self, **data: Any) -> None:
        self.metadata.update(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)
