import json

import pytest

from scheduler.task_store import TaskStore


def _make_task(store: TaskStore, name: str, enabled: bool = True, next_run_at=None) -> dict:
    return {
        "id": store.new_task_id(),
        "name": name,
        "enabled": enabled,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "schedule": {"type": "interval", "seconds": 60},
        "action": {"type": "send_message", "content": name, "target": {}},
        "next_run_at": next_run_at,
    }


def test_store_empty_when_file_missing(tmp_path):
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    assert store.list() == []
    assert store.get("nope") is None


def test_store_add_get_list_delete(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = _make_task(store, "reminder")
    store.add(task)

    assert store.get(task["id"]) == task
    assert [t["id"] for t in store.list()] == [task["id"]]

    assert store.delete(task["id"]) is True
    assert store.get(task["id"]) is None
    assert store.delete(task["id"]) is False


def test_store_add_duplicate_raises(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = _make_task(store, "dup")
    store.add(task)
    with pytest.raises(ValueError):
        store.add(task)


def test_store_add_requires_id(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    with pytest.raises(ValueError):
        store.add({"name": "no id"})


def test_store_update_refreshes_updated_at(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = _make_task(store, "x")
    store.add(task)
    updated = store.update(task["id"], {"next_run_at": "2026-02-02T02:02:02"})
    assert updated["next_run_at"] == "2026-02-02T02:02:02"
    assert updated["updated_at"] != "2026-01-01T00:00:00"


def test_store_update_unknown_raises(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    with pytest.raises(KeyError):
        store.update("ghost", {"enabled": False})


def test_store_enable_disable(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = _make_task(store, "toggle")
    store.add(task)

    store.set_enabled(task["id"], False)
    assert store.get(task["id"])["enabled"] is False
    assert store.list(enabled_only=True) == []

    store.set_enabled(task["id"], True)
    assert store.get(task["id"])["enabled"] is True
    assert len(store.list(enabled_only=True)) == 1


def test_store_list_sorts_enabled_first_then_next_run(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    a = _make_task(store, "a-late", next_run_at="2026-05-01T00:00:00")
    b = _make_task(store, "b-early", next_run_at="2026-03-01T00:00:00")
    c = _make_task(store, "c-disabled", enabled=False, next_run_at="2026-01-01T00:00:00")
    for task in (a, b, c):
        store.add(task)

    ordered = [t["name"] for t in store.list()]
    assert ordered == ["b-early", "a-late", "c-disabled"]


def test_store_backup_created_on_second_save(tmp_path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    first = _make_task(store, "first")
    store.add(first)
    assert not store.backup_path.exists()

    second = _make_task(store, "second")
    store.add(second)
    assert store.backup_path.exists()
    backup = json.loads(store.backup_path.read_text(encoding="utf-8"))
    assert first["id"] in backup["tasks"]
    assert second["id"] not in backup["tasks"]


def test_store_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = TaskStore(path)
    assert store.list() == []
