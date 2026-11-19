from datetime import datetime, timezone
from pathlib import Path
import re

from memory.semantic_store import SemanticMemoryStore


def _id_for(store: SemanticMemoryStore, content: str) -> str:
    return store._find_content_id(store.read().splitlines(), content)


def test_init_writes_skeleton(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    content = store.read()
    assert "# Semantic Memory" in content
    assert "## User Preferences" in content
    assert "## Project Decisions" in content
    assert "[M-YYYYMMDDTHHMMSSmmm-NNN]" in content


def test_remember_appends_visible_id_to_realtime_updates(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    msg = store.remember("用户偏好简洁回答", source="test")
    assert "saved" in msg.lower()
    content = store.read()
    assert "## Realtime Updates" in content
    assert "用户偏好简洁回答" in content
    memory_id = _id_for(store, "用户偏好简洁回答")
    assert re.fullmatch(r"M-\d{8}T\d{9}-\d{3,}", memory_id)
    assert f"[{memory_id}]" in content
    assert "<!--" not in content


def test_remember_dedup(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("同一条记忆", source="a")
    msg = store.remember("同一条记忆", source="b")
    assert "already exists" in msg.lower()
    assert store.read().count("同一条记忆") == 1


def test_insert_ids_use_time_and_increment_on_same_millisecond(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    fixed = datetime(2026, 10, 28, 10, 35, 22, 481000, tzinfo=timezone.utc)

    first = store._new_memory_id([], now=fixed)
    second = store._new_memory_id(
        [store._format_line(first, "第一条")],
        now=fixed,
    )

    assert first == "M-20261028T103522481-000"
    assert second == "M-20261028T103522481-001"


def test_add_rejects_model_supplied_memory_id(tmp_memory_dir: Path):
    from memory.semantic_store import SemanticMemoryError

    store = SemanticMemoryStore(tmp_memory_dir)
    try:
        store.apply_operations([{
            "op": "ADD",
            "memory_id": "M-0000000000",
            "content": "新事实",
        }])
    except SemanticMemoryError as exc:
        assert exc.code == "generated_field_not_allowed"
        assert exc.details["field"] == "memory_id"
    else:
        raise AssertionError("model-supplied memory_id must be rejected for ADD")


def test_remember_empty_rejected(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    msg = store.remember("   ", source="x")
    assert "empty" in msg.lower()


def test_overwrite_replaces_all(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("旧记忆", source="x")
    store.overwrite("# Semantic Memory\n\n- 新的整理结果")
    content = store.read()
    assert "新的整理结果" in content
    assert "旧记忆" not in content


def test_overwrite_empty_falls_back(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.overwrite("   ")
    assert "# Semantic Memory" in store.read()


def test_channel_user_ids_share_one_semantic_file(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("跨渠道共享事实", user_id="feishu-open-id")

    assert "跨渠道共享事实" in store.read(user_id="local")
    assert store.path_for("feishu-open-id") == store.path


def test_legacy_channel_semantic_files_are_merged_without_deletion(tmp_memory_dir: Path):
    legacy_dir = tmp_memory_dir / "semantic"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / "feishu-open-id.md"
    legacy.write_text("# Semantic Memory\n\n- 渠道里的旧事实\n", encoding="utf-8")

    store = SemanticMemoryStore(tmp_memory_dir)

    assert "渠道里的旧事实" in store.read()
    assert legacy.exists()


def test_supersede_removes_old_fact_from_active_bullets(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("旧事实", source="test")
    old_id = _id_for(store, "旧事实")

    applied = store.apply_operations([
        {"op": "SUPERSEDE", "id": old_id, "content": "新事实"},
    ])

    content = store.read()
    assert applied["SUPERSEDE"] == 1
    assert "旧事实" not in content
    new_id = _id_for(store, "新事实")
    assert re.fullmatch(r"M-\d{8}T\d{9}-\d{3,}", new_id)
    assert f"[{new_id}] 新事实" in content
    assert old_id not in content
    assert "<!--" not in content


def test_supersede_rejects_model_supplied_new_id(tmp_memory_dir: Path):
    from memory.semantic_store import SemanticMemoryError

    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("旧事实", source="test")
    old_id = _id_for(store, "旧事实")

    try:
        store.apply_operations([{
            "op": "SUPERSEDE",
            "id": old_id,
            "content": "新事实",
            "new_id": "M-0000000000",
        }])
    except SemanticMemoryError as exc:
        assert exc.code == "generated_field_not_allowed"
        assert exc.details["field"] == "new_id"
    else:
        raise AssertionError("model-supplied new_id must be rejected")


def test_existing_bullets_migrate_to_visible_ids(tmp_memory_dir: Path):
    path = tmp_memory_dir / "MEMORY.md"
    path.write_text("# Semantic Memory\n\n- 旧格式事实 <!-- source: old -->\n", encoding="utf-8")

    store = SemanticMemoryStore(tmp_memory_dir)
    content = store.read()

    migrated_id = _id_for(store, "旧格式事实")
    assert re.fullmatch(r"M-\d{8}T\d{9}-\d{3,}", migrated_id)
    assert f"- [{migrated_id}] 旧格式事实" in content
    assert "source: old" not in content
    assert "<!--" not in content


def test_existing_visible_ids_keep_id_while_debug_comments_are_removed(tmp_memory_dir: Path):
    path = tmp_memory_dir / "MEMORY.md"
    path.write_text(
        "# Semantic Memory\n\n"
        "- [M-3964DC78AB] 共享同一个 memory owner。 "
        "<!-- source: dream, at: 2026-10-28T09:57:58 -->\n",
        encoding="utf-8",
    )

    content = SemanticMemoryStore(tmp_memory_dir).read()

    assert "- [M-3964DC78AB] 共享同一个 memory owner。" in content
    assert "source: dream" not in content
    assert "2026-10-28" not in content
    assert "<!--" not in content


def test_update_requires_existing_visible_id(tmp_memory_dir: Path):
    from memory.semantic_store import SemanticMemoryError

    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("原事实", source="test")
    memory_id = _id_for(store, "原事实")

    result = store.apply_operation(
        {"op": "UPDATE", "memory_id": memory_id, "content": "修正后的事实"},
        source="test",
    )

    assert result["memory_id"] == memory_id
    assert f"[{memory_id}] 修正后的事实" in store.read()
    try:
        store.apply_operation(
            {"op": "UPDATE", "memory_id": "M-0000000000", "content": "x"},
            source="test",
        )
    except SemanticMemoryError as exc:
        assert exc.code == "memory_not_found"
        assert memory_id in exc.details["available_memory_ids"]
    else:
        raise AssertionError("missing id must fail")


def test_update_only_requires_existing_id_and_does_not_run_insert_dedup(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    first_id = store.apply_operation({"op": "ADD", "content": "第一条"})["memory_id"]
    second_id = store.apply_operation({"op": "ADD", "content": "第二条"})["memory_id"]

    result = store.apply_operation({
        "op": "UPDATE",
        "memory_id": second_id,
        "content": "第一条",
    })

    assert result["memory_id"] == second_id
    content = store.read()
    assert f"[{first_id}] 第一条" in content
    assert f"[{second_id}] 第一条" in content
