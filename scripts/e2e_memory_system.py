"""Real-model end-to-end validation for the three-layer memory system.

The run is isolated under data/e2e-memory and never touches production state.db.
It intentionally uses natural Chinese data from the memory-system design discussion.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import Agent
from agent.run_context import RunContext
from config import load_settings


SOURCE_TURNS = [
    (
        "my-agent 只服务一个用户，但我会同时从 CLI 和飞书聊天。长期记忆不能按渠道身份拆开。",
        "确认：CLI 与飞书只是不同入口，Episodic 和 Semantic 必须共享同一个 memory owner。",
    ),
    (
        "生产配置的短期记忆窗口固定取最近 20 个完整 Turn，并允许在配置文件中调整。",
        "确认：恢复历史时按 session_id 和递增 turn_seq 取最近 N 个 complete Turn，而不是直接限制消息条数。",
    ),
    (
        "Raw 对话原文要通过内存队列异步写入 SQLite，Agent 主链路不等待落盘。",
        "确认：由单 Writer 批量提交，强杀时接受最后未提交批次的尾部风险。",
    ),
    (
        "离开短期窗口的 Turn 才进入 PendingBuffer；累计 20 Turn 或最老 Turn 等待超过 1 小时后提炼。",
        "确认：Episodic Worker 异步执行，成功写入情景记忆并推进游标后才能弹出 Buffer。",
    ),
    (
        "情景记忆正文只写关键背景、过程和结果，元数据只保留 stable_facts 与 lessons。",
        "确认：正文只写事件短句，时间单独放元数据，默认设置 60 字符上限。",
    ),
    (
        "语义记忆只有一份 MEMORY.md，Dream 不应整篇重写。",
        "确认：Dream 通过稳定 memory-id 输出 ADD、UPDATE、SUPERSEDE 或 NOOP 操作。",
    ),
    (
        "历史 JSONL 数据需要迁移到 SQLite，但旧文件不能删除，方便追溯。",
        "确认：首次启动幂等导入，原 JSONL 作为历史材料保留。",
    ),
]


def wait_until(predicate, timeout: float = 90.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def record_turn(agent: Agent, context, run_context: RunContext, user: str, assistant: str) -> None:
    receipt = agent.memory.record_message(
        run_context.session_id,
        "user",
        user,
        metadata={
            "channel_type": run_context.get("channel_type"),
            "user_id": run_context.get("user_id"),
            "entrypoint": "memory-e2e",
        },
    )
    context.append_user_message(user, turn_seq=receipt.turn_seq)
    context.build_model_messages(run_context, tool_schemas=[])
    context.append_assistant_message(
        {"role": "assistant", "content": assistant},
        turn_seq=receipt.turn_seq,
    )
    agent.memory.record_message(
        run_context.session_id,
        "assistant",
        assistant,
        turn_seq=receipt.turn_seq,
    )


def main() -> int:
    base = load_settings()
    if not base.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for this real-model E2E run")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    project_root = ROOT / "data" / "e2e-memory" / run_id
    workspace = project_root / "workspace"
    (project_root / "prompts").mkdir(parents=True, exist_ok=True)
    (project_root / "skills").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (project_root / "prompts" / "identity.md").write_text(
        "你是 my-agent 记忆系统的端到端测试助手。必须优先依据注入的长期记忆作答，禁止猜测。",
        encoding="utf-8",
    )

    embedding_model = "BAAI/bge-small-zh-v1.5"
    settings = replace(
        base,
        project_root=project_root,
        workspace=workspace,
        state_db_path=project_root / "data" / "state.db",
        scheduler_tasks_path=project_root / "data" / "scheduler" / "tasks.json",
        scheduler_enabled=False,
        daily_dream_enabled=False,
        otel_enabled=False,
        sandbox_enabled=False,
        session_window_turns=3,
        memory_search_results=5,
        memory_embedding_provider="local",
        memory_embedding_model=embedding_model,
        memory_embedding_local_files_only=True,
        memory_vector_backend="auto",
        raw_write_batch_size=4,
        raw_write_flush_interval_ms=25,
        episodic_batch_turns=2,
        episodic_max_wait_seconds=3600,
        episodic_worker_poll_seconds=1,
        episodic_body_max_chars=60,
    )

    report: dict[str, object] = {
        "run_id": run_id,
        "artifact_dir": str(project_root),
        "model": settings.model,
        "checks": {},
    }
    agent = Agent(settings)
    try:
        session_id = "cli:local:memory-e2e"
        run_context = RunContext.create(
            session_id,
            workspace,
            entrypoint="memory-e2e",
            channel_type="cli",
            user_id="local",
        )
        context = agent._get_context_manager(session_id)
        for user, assistant in SOURCE_TURNS:
            record_turn(agent, context, run_context, user, assistant)

        raw_flushed = agent.memory.raw_store.flush(timeout=10)
        worker_completed = wait_until(lambda: agent.memory._episodic_cursor(session_id) >= 4)
        raw_turns = agent.memory.raw_store.read_recent_turns(session_id, 100)
        recent_turns = agent.memory.raw_store.read_recent_turns(session_id, 3)
        episodes = agent.memory.episodic_store.list_records(user_id="local")
        episode_text = "\n".join(record.content for record in episodes)

        # Retrieval intentionally indexes only the episodic body.  Use terms that
        # occur in the body rather than metadata such as stable_facts.
        query = "多入口 memory owner 短期窗口"
        search_results, search_meta = agent.memory.index.search_with_metadata(
            query,
            limit=5,
            min_score=0.0,
            user_id="local",
        )
        dream = agent.memory.deep_dream(user_id="feishu-open-id")
        semantic = agent.memory.read_main_memory(user_id="another-channel-id")

        checks = {
            "embedding_available": agent.memory.embedding_status.available,
            "embedding_dimensions": agent.memory.embedding_status.dimensions,
            "vector_backend": agent.memory.index.vector_backend,
            "raw_flush_completed": raw_flushed,
            "raw_complete_turn_count": len(raw_turns),
            "raw_recent_turn_seq": [turn["turn_seq"] for turn in recent_turns],
            "episodic_worker_completed": worker_completed,
            "episodic_cursor": agent.memory._episodic_cursor(session_id),
            "pending_count": agent.memory.pending_count(session_id),
            "episodic_count": len(episodes),
            "episodic_body_within_limit": all(
                len(record.search_text) <= 60 for record in episodes
            ),
            "episodic_body_has_complete_ending": all(
                record.search_text.endswith(("。", "！", "？", "!", "?", "…"))
                for record in episodes
            ),
            "episodic_time_is_metadata": all(
                bool(record.metadata.get("occurred_at"))
                and not record.content.startswith(str(record.metadata["occurred_at"])[:10])
                for record in episodes
            ),
            "episodic_render_has_time_and_event": all(
                agent.memory.episodic_store.render_record(record).startswith("时间：")
                and "\n事件：" in agent.memory.episodic_store.render_record(record)
                for record in episodes
            ),
            "episodic_contains_real_fact": any(
                term in episode_text for term in ("多入口", "渠道", "飞书", "memory owner")
            ),
            "bm25_candidates": search_meta.get("bm25_candidates", 0),
            "vector_candidates": search_meta.get("vector_candidates", 0),
            "retrieval_backend": search_meta.get("vector_backend"),
            "retrieval_result_count": len(search_results),
            "top_retrieval": search_results[0].record.content if search_results else "",
            "dream_ok": dream.get("ok", False),
            "dream_operations": dream.get("operations", {}),
            "semantic_contains_cross_channel_fact": (
                ("渠道" in semantic or "飞书" in semantic) and "20" in semantic
            ),
        }
        report["checks"] = checks

        required = {
            "embedding_available": checks["embedding_available"] is True,
            "vector_backend": checks["vector_backend"] in {"sqlite-vec", "python"},
            "raw_turns": checks["raw_complete_turn_count"] == len(SOURCE_TURNS),
            "recent_turns": checks["raw_recent_turn_seq"] == [5, 6, 7],
            "worker": checks["episodic_worker_completed"] is True,
            "cursor": int(checks["episodic_cursor"]) >= 4,
            "buffer_popped": checks["pending_count"] == 0,
            "episodes": int(checks["episodic_count"]) >= 2,
            "episode_quality": checks["episodic_body_within_limit"] is True,
            "episode_complete_ending": checks["episodic_body_has_complete_ending"] is True,
            "episode_time_metadata": checks["episodic_time_is_metadata"] is True,
            "episode_rendering": checks["episodic_render_has_time_and_event"] is True,
            "hybrid_candidates": (
                int(checks["bm25_candidates"]) > 0
                and int(checks["vector_candidates"]) > 0
            ),
            "dream": checks["dream_ok"] is True,
            "semantic": checks["semantic_contains_cross_channel_fact"] is True,
        }
        report["required"] = required
        if not all(required.values()):
            raise AssertionError(f"memory E2E checks failed: {required}")
    finally:
        agent.memory.close(wait=True)

    reader = Agent(settings)
    try:
        restored = reader._get_context_manager("cli:local:memory-e2e").history
        restored_user_messages = [m["content"] for m in restored if m.get("role") == "user"]
        answer, turn_result = reader.run(
            "我从 CLI 换到飞书后，长期记忆是否共用？生产默认的短期窗口保留多少个完整 Turn？只回答结论。",
            context=RunContext.create(
                "feishu:open-id:memory-e2e-recall",
                workspace,
                entrypoint="memory-e2e",
                channel_type="feishu",
                user_id="feishu-open-id",
            ),
        )
        restart_checks = {
            "restored_user_turn_count": len(restored_user_messages),
            "restored_is_recent_three": restored_user_messages == [
                item[0] for item in SOURCE_TURNS[-3:]
            ],
            "recall_outcome": turn_result.outcome,
            "recall_answer": answer,
            "recall_mentions_shared": "共享" in answer or "共用" in answer,
            "recall_mentions_20": "20" in answer,
        }
        report["restart_checks"] = restart_checks
        if not (
            restart_checks["restored_is_recent_three"]
            and restart_checks["recall_outcome"] == "success"
            and restart_checks["recall_mentions_shared"]
            and restart_checks["recall_mentions_20"]
        ):
            raise AssertionError(f"restart/recall checks failed: {restart_checks}")
    finally:
        reader.memory.close(wait=True)

    report_path = project_root / "memory-e2e-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
