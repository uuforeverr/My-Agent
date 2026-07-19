import json
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

STORE_VERSION = 1
_FAR_FUTURE = "9999-12-31T23:59:59"


class TaskStore:
    """JSON-backed persistence for scheduler tasks.

    tasks.json layout::

        {"version": 1, "updated_at": ISO, "tasks": {task_id: task_dict}}

    All reads/writes hold a lock so the SchedulerService thread and the
    SchedulerTool (called from channel worker threads) never race. Each save
    first copies the previous file to tasks.json.bak for recoverability.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def new_task_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def add(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = task.get("id")
        if not task_id:
            raise ValueError("task must have an id")
        with self._lock:
            data = self._load()
            if task_id in data["tasks"]:
                raise ValueError(f"task already exists: {task_id}")
            data["tasks"][task_id] = task
            self._save(data)
        return task

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._load()["tasks"].get(task_id)

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            tasks = list(self._load()["tasks"].values())
        if enabled_only:
            tasks = [t for t in tasks if t.get("enabled", True)]
        tasks.sort(key=self._sort_key)
        return tasks

    def update(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            task.update(updates)
            task["updated_at"] = self._now()
            self._save(data)
            return task

    def delete(self, task_id: str) -> bool:
        with self._lock:
            data = self._load()
            if task_id not in data["tasks"]:
                return False
            del data["tasks"][task_id]
            self._save(data)
            return True

    def set_enabled(self, task_id: str, enabled: bool) -> dict[str, Any]:
        return self.update(task_id, {"enabled": bool(enabled)})

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": STORE_VERSION, "updated_at": None, "tasks": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": STORE_VERSION, "updated_at": None, "tasks": {}}
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
            return {"version": STORE_VERSION, "updated_at": None, "tasks": {}}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.backup_path)
            except OSError:
                pass
        data["version"] = STORE_VERSION
        data["updated_at"] = self._now()
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _sort_key(task: dict[str, Any]) -> tuple[bool, str]:
        enabled_first = not task.get("enabled", True)
        next_run = task.get("next_run_at") or _FAR_FUTURE
        return (enabled_first, next_run)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
