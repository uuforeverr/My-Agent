"""Run a real-model, real-scheduler end-to-end smoke test.

The test uses the configured model endpoint, but isolates state, memory and
workspace data under ``data/e2e/<run-id>``.  It asks the model to create a
short one-shot reminder, waits for validation and scheduled delivery, then
writes machine-readable evidence to ``result.json``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.agent import Agent  # noqa: E402
from agent.run_context import RunContext  # noqa: E402
from channel.manager import ChannelManager  # noqa: E402
from config import load_settings  # noqa: E402


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    try:
        return asdict(value)
    except (TypeError, ValueError):
        return str(value)


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=int, default=30, help="Reminder delay in seconds.")
    parser.add_argument("--timeout", type=int, default=120, help="Overall wait after task creation.")
    args = parser.parse_args()
    if args.delay < 10:
        parser.error("--delay must be at least 10 seconds so validation can complete")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_root = PROJECT_ROOT / "data" / "e2e" / run_id
    isolated_root = run_root / "project"
    isolated_workspace = run_root / "workspace"
    isolated_root.mkdir(parents=True, exist_ok=True)
    isolated_workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "prompts", isolated_root / "prompts")
    if (PROJECT_ROOT / "skills").exists():
        shutil.copytree(PROJECT_ROOT / "skills", isolated_root / "skills")

    base = load_settings()
    if not base.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    settings = replace(
        base,
        project_root=isolated_root,
        workspace=isolated_workspace,
        channels=("cli",),
        scheduler_enabled=True,
        scheduler_tasks_path=run_root / "no-legacy-tasks.json",
        daily_dream_enabled=False,
        episodic_scan_interval=0,
        tool_approval_mode="off",
        state_db_path=run_root / "state.db",
        otel_enabled=False,
    )

    marker = f"E2E-SCHEDULER-{uuid.uuid4().hex[:8].upper()}"
    result_path = run_root / "result.json"
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="milliseconds"),
        "model": settings.model,
        "api_base": settings.openai_api_base,
        "marker": marker,
        "delay_seconds": args.delay,
        "state_db": str(settings.state_db_path),
        "deliveries_observed": [],
    }
    _write_result(result_path, evidence)
    print(f"E2E_RUN_ROOT={run_root}", flush=True)
    print(f"E2E_MARKER={marker}", flush=True)
    print(f"REAL_MODEL={settings.model} via {settings.openai_api_base}", flush=True)

    manager: ChannelManager | None = None
    observation_lock = threading.Lock()
    observed: list[dict[str, Any]] = []
    exit_code = 1
    try:
        agent = Agent(settings)
        manager = ChannelManager(settings, agent=agent)
        channels = manager.build_channels()
        if not channels:
            raise RuntimeError("CLI channel was not created")

        cli = channels[0]
        original_send = cli.send

        def recording_send(reply, message):
            observed_at = datetime.now().isoformat(timespec="milliseconds")
            with observation_lock:
                observed.append(
                    {
                        "observed_at": observed_at,
                        "content": reply.content,
                        "channel_type": message.channel_type,
                        "user_id": message.user_id,
                    }
                )
            print(f"DELIVERY_OBSERVED_AT={observed_at}", flush=True)
            return original_send(reply, message)

        cli.send = recording_send
        manager.start_scheduler()

        session_id = f"cli:e2e-local:{run_id}"
        context = RunContext.create(
            session_id=session_id,
            workspace=settings.workspace,
            entrypoint="e2e_scheduler_smoke",
            channel_type="cli",
            user_id="e2e-local",
            is_group=False,
        )
        prompt = (
            "这是一次真实端到端调度测试。请务必调用 scheduler 工具，只创建一个一次性定时任务："
            f"从现在起 {args.delay} 秒后，在当前会话发送固定文本“{marker}”。"
            "参数必须使用 schedule_type=once、"
            f"schedule_value=+{args.delay}s、timing_semantics=notify_at；"
            "message 必须严格等于上述固定文本。不要直接发送该文本，也不要创建其他任务。"
        )
        model_started = datetime.now().isoformat(timespec="milliseconds")
        answer, creation_trace = agent.run(prompt, context=context)
        model_finished = datetime.now().isoformat(timespec="milliseconds")
        evidence.update(
            {
                "model_call_started_at": model_started,
                "model_call_finished_at": model_finished,
                "model_answer": answer,
                "creation_trace_id": creation_trace.trace_id,
            }
        )
        print(f"MODEL_ANSWER={answer}", flush=True)

        repository = agent.scheduler_store
        tasks = repository.list()
        evidence["tasks_after_creation"] = tasks
        if len(tasks) != 1:
            raise AssertionError(f"expected exactly one task, found {len(tasks)}")
        task = tasks[0]
        task_id = task["id"]
        scheduled_for = task.get("next_run_at")
        if (task.get("action") or {}).get("content") != marker:
            raise AssertionError("model-created task content did not match marker")
        if task.get("timing_semantics") != "notify_at":
            raise AssertionError("model-created task did not use notify_at")
        print(f"TASK_CREATED id={task_id} scheduled_for={scheduled_for}", flush=True)

        deadline = time.monotonic() + args.timeout
        last_status = ""
        scheduled_delivery: dict[str, Any] | None = None
        scheduled_run: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            task = repository.get(task_id) or task
            runs = repository.list_runs(task_id=task_id, limit=20)
            validation_runs = [row for row in runs if row.get("run_kind") == "validation"]
            scheduled_runs = [row for row in runs if row.get("run_kind") == "scheduled"]
            status_line = (
                f"task={task.get('status')} "
                f"validation={validation_runs[0].get('status') if validation_runs else '-'} "
                f"scheduled={scheduled_runs[0].get('status') if scheduled_runs else '-'}"
            )
            if status_line != last_status:
                print(f"STATUS {status_line}", flush=True)
                last_status = status_line
            for candidate in scheduled_runs:
                deliveries = repository.list_deliveries(run_id=candidate["id"], limit=20)
                delivered = [row for row in deliveries if row.get("status") == "delivered"]
                if delivered:
                    scheduled_run = candidate
                    scheduled_delivery = delivered[0]
                    break
            if scheduled_delivery is not None:
                break
            time.sleep(0.1)

        all_runs = repository.list_runs(task_id=task_id, limit=20)
        all_deliveries: list[dict[str, Any]] = []
        for row in all_runs:
            all_deliveries.extend(repository.list_deliveries(run_id=row["id"], limit=20))
        with observation_lock:
            evidence["deliveries_observed"] = list(observed)
        evidence.update(
            {
                "final_task": repository.get(task_id),
                "runs": all_runs,
                "outbox": all_deliveries,
                "finished_at": datetime.now().isoformat(timespec="milliseconds"),
            }
        )
        if scheduled_run is None or scheduled_delivery is None:
            raise TimeoutError("scheduled delivery was not confirmed before timeout")

        marker_observations = [item for item in observed if item.get("content") == marker]
        if len(marker_observations) != 1:
            raise AssertionError(f"expected one marker delivery, observed {len(marker_observations)}")
        observed_at = marker_observations[0]["observed_at"]
        drift_seconds = (
            _iso_to_datetime(observed_at) - _iso_to_datetime(str(scheduled_for))
        ).total_seconds()
        validation_run = next((row for row in all_runs if row.get("run_kind") == "validation"), None)
        evidence["assertions"] = {
            "real_model_created_task": True,
            "validation_succeeded": bool(validation_run and validation_run.get("status") == "succeeded"),
            "scheduled_run_succeeded": scheduled_run.get("status") == "succeeded",
            "scheduled_delivery_confirmed": scheduled_delivery.get("status") == "delivered",
            "delivered_exactly_once": len(marker_observations) == 1,
            "delivery_channel": marker_observations[0]["channel_type"],
            "scheduled_for": scheduled_for,
            "observed_delivery_at": observed_at,
            "delivery_drift_seconds": round(drift_seconds, 3),
        }
        evidence["ok"] = all(
            value is True
            for key, value in evidence["assertions"].items()
            if key in {
                "real_model_created_task",
                "validation_succeeded",
                "scheduled_run_succeeded",
                "scheduled_delivery_confirmed",
                "delivered_exactly_once",
            }
        )
        print("ASSERTIONS=" + json.dumps(evidence["assertions"], ensure_ascii=False), flush=True)
        exit_code = 0 if evidence["ok"] else 1
    except Exception as exc:
        evidence["ok"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["finished_at"] = datetime.now().isoformat(timespec="milliseconds")
        print(f"E2E_ERROR={evidence['error']}", flush=True)
    finally:
        if manager is not None:
            manager.shutdown()
        _write_result(result_path, evidence)
        print(f"E2E_RESULT={result_path}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
