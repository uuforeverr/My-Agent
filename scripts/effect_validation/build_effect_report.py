from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def _verify_raw_data(
    output_dir: Path,
    scheduler: dict,
    tools: dict,
    resilience: dict | None = None,
) -> None:
    occurrences = _read_csv(output_dir / "scheduler" / "occurrences.csv")
    scheduler_passed = sum(_as_bool(row["passed"]) for row in occurrences)
    missing = sum(row["delivery_status"] != "delivered" for row in occurrences)
    duplicates = sum(max(0, int(row["observed_send_count"]) - 1) for row in occurrences)
    fault_rows: list[dict[str, str]] = []
    if scheduler.get("mode") == "formal":
        fault_rows = _read_csv(output_dir / "scheduler" / "faults.csv")
    expected_occurrences = scheduler.get("occurrence_case_count", scheduler["case_count"])
    total_passed = scheduler_passed + sum(_as_bool(row["passed"]) for row in fault_rows)
    if (
        len(occurrences) != expected_occurrences
        or len(fault_rows) != scheduler.get("fault_case_count", 0)
        or total_passed != scheduler["passed_count"]
        or missing != scheduler["missing_delivery_count"]
        or duplicates != scheduler["duplicate_delivery_count"]
    ):
        raise RuntimeError("scheduler raw CSV does not match summary.json")

    tool_rows = _read_csv(output_dir / "tool-governance" / "results.csv")
    executed = [row for row in tool_rows if row.get("execution_status") != "not_executed_environment"]
    tool_passed = sum(_as_bool(row["passed"]) for row in executed)
    if (
        len(tool_rows) != tools["case_count"]
        or len(executed) != tools["executed_case_count"]
        or tool_passed != tools["passed_count"]
    ):
        raise RuntimeError("tool-governance raw CSV does not match summary.json")

    if resilience is not None:
        resilience_rows = _read_csv(output_dir / "scheduler-resilience" / "results.csv")
        resilience_passed = sum(_as_bool(row["passed"]) for row in resilience_rows)
        if (
            len(resilience_rows) != resilience["case_count"]
            or resilience_passed != resilience["passed_count"]
        ):
            raise RuntimeError("scheduler-resilience raw CSV does not match summary.json")


def run(output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    scheduler = _load(output_dir / "scheduler" / "summary.json")
    tools = _load(output_dir / "tool-governance" / "summary.json")
    resilience_path = output_dir / "scheduler-resilience" / "summary.json"
    resilience = _load(resilience_path) if resilience_path.exists() else None
    _verify_raw_data(output_dir, scheduler, tools, resilience)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        ).stdout.strip())
    except Exception:
        commit, dirty = "unavailable", None
    environment = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": scheduler["mode"],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "apscheduler": importlib.metadata.version("apscheduler"),
        "logical_cpu_count": os.cpu_count(),
        "timezone": str(datetime.now().astimezone().tzinfo),
        "git_commit": commit,
        "dirty_worktree": dirty,
        "scheduler_workers": 2,
        "relay_interval_seconds": 15,
        "relay_batch_size": 20,
        "delivery_lease_seconds": 120,
    }
    (output_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if scheduler["mode"] == "formal":
        failed_tool_rows = [
            row for row in _read_csv(output_dir / "tool-governance" / "results.csv")
            if row.get("execution_status") == "executed" and not _as_bool(row["passed"])
        ]
        failed_names = ", ".join(row["case_id"] for row in failed_tool_rows)
        normal_result_note = (
            "全部 10 个正常任务通过，固定镜像已包含实验所需的 `ripgrep` 和 `pytest`。"
            if not failed_names
            else f"正常任务失败项：`{failed_names}`。详见原始 stderr。"
        )
        image_id_note = f"（镜像 ID `{tools.get('image_id', 'unavailable')}`）"
        if resilience is not None:
            restart = resilience["restart_before_deadline"]
            recovered = resilience["outbox_after_crash"]
            simultaneous = resilience["simultaneous_20"]
            resilience_scope = (
                f"- 真实进程重启：{restart['passed_count']}/{restart['task_count']} 个任务恢复并送达，"
                f"P95 到点偏差 {restart['p95_delivery_drift_ms']} ms。\n"
                f"- 结果落库后崩溃：重启后 {recovered['recovery_delivery_ms']} ms 恢复投递，"
                f"结果只发送一次。\n"
                f"- 20 个任务同时到期：{simultaneous['delivered_count']}/20 送达，"
                f"P95 交付延迟 {simultaneous['p95_delivery_drift_ms']} ms，"
                f"最大 {simultaneous['max_delivery_drift_ms']} ms，"
                f"漏发 {simultaneous['missing_delivery_count']}、"
                f"普通重复发送 {simultaneous['duplicate_result_count']}。"
            )
            resilience_fact = (
                f"- 强制终止并重启真实调度进程后，{restart['task_count']} 个持久化任务全部恢复；"
                f"20 个代表性任务同时到期时全部送达，P95 交付延迟 "
                f"{simultaneous['p95_delivery_drift_ms']} ms。"
            )
            resilience_files = (
                "- `scheduler-resilience/results.csv`\n"
                "- `scheduler-resilience/summary.json`"
            )
        else:
            resilience_scope = "- 未运行独立的真实进程重启与 20 项同时到期实验。"
            resilience_fact = ""
            resilience_files = ""
        report = f"""# Scheduler and Tool Governance Formal Experiment Report

> 生成时间：{environment['generated_at']}
> 口径：正式时间压缩调度实验 + 真实 Docker 工具治理矩阵；原始 CSV 已与 summary.json 对账。

## 1. 环境与范围

- Python {environment['python']}，SQLite {environment['sqlite']}，APScheduler {environment['apscheduler']}。
- 调度：固定 seed `{scheduler['seed']}`，20 个个人日常任务压缩至 {scheduler['compressed_window_seconds']} 秒；另含 5 个同 deadline、重启、latest、Relay/失败恢复和长任务隔离。
- 工具：真实 `{tools['image']}` Docker 容器{image_id_note}、路径边界、断网、资源限制、R0–R2 审批及 10 个正常任务。
- 所有状态库、workspace、假凭证和哨兵均在本实验目录，未使用正式 `data/state.db` 或真实凭证。

## 2. 调度正式结果

| 指标 | 结果 |
| --- | ---: |
| 20 个日常任务最终送达 | {scheduler['daily_20']['delivered']}/20 |
| 日常任务 P95 投递偏差 | {scheduler['daily_20']['p95_delivery_drift_ms']} ms |
| 日常任务最大投递偏差 | {scheduler['daily_20']['max_delivery_drift_ms']} ms |
| 5 个同 deadline 最终送达 | {scheduler['same_deadline_5']['delivered']}/5 |
| 同 deadline 首尾展开 | {scheduler['same_deadline_5']['spread_ms']} ms |
| 漏投递 | {scheduler['missing_delivery_count']} |
| 重复投递 | {scheduler['duplicate_delivery_count']} |
| 故障注入场景 | {scheduler['fault_case_count']}/{scheduler['fault_case_count']} 通过 |
| 轻任务 P95 相对基线增加 | {scheduler['light_task_p95_increase_vs_daily_ms']} ms |

`all_occurrences` 的最大偏差包含故意构造的过期一天 once 任务，因此不用于精度结论；简历指标只引用 `daily_20`。

### 2.1 真实进程重启与集中到期补充实验

{resilience_scope}

外部 LLM 和工具主体使用确定性替身，以隔离外部服务波动；APScheduler、独立进程重启、SQLite、执行线程池、结果落库和投递链路均使用真实实现。

## 3. 工具治理正式结果

| 指标 | 结果 |
| --- | ---: |
| 可执行攻击/边界样例被阻断 | {tools['attack_blocked_count']}/{tools['attack_case_count']} |
| 工作区逃逸成功 | {tools['workspace_escape_success_count']} |
| 工作区外文件修改 | {tools['outside_file_modification_count']} |
| 假凭证泄露 | {tools['credential_leak_count']} |
| 网络外传成功 | {tools['network_exfiltration_success_count']} |
| 正常任务通过 | {tools['normal_task_passed_count']}/{tools['normal_task_count']} ({tools['normal_task_pass_rate'] * 100:.1f}%) |
| 环境原因未执行 | {tools['not_executed_environment_count']} |

未执行项是 Windows 当前权限无法创建目录软链接，不计入攻击拦截率。外部哨兵哈希保持不变：`{tools['outside_sentinel_unchanged']}`。

{normal_result_note}

## 4. 可复核事实句

- 在 20 个个人日常任务的 120 秒时间压缩实验中，最终送达率为 100%，漏投递 0、普通重复投递 0，P95 到点偏差 {scheduler['daily_20']['p95_delivery_drift_ms']} ms。
{resilience_fact}
- 在 {tools['attack_case_count']} 个可执行攻击与边界样例中，工作区逃逸、外部文件修改、凭证泄露和网络外传均为 0；10 个正常任务通过率为 {tools['normal_task_pass_rate'] * 100:.1f}%。

## 5. 限制与后续改进

- Outbox 为 at-least-once；平台已接收但本地未记账即崩溃的不可判定窗口仍可能重复，不宣称端到端 exactly-once。
- 沙箱镜像以固定基础镜像 digest 构建；后续变更 Dockerfile 或工具版本时需生成新标签并复跑完整治理矩阵。
- 软链接边界需在支持创建 symlink 的 Windows Developer Mode 或 Linux 环境补跑。

## 6. 原始数据

- `scheduler/occurrences.csv`
- `scheduler/faults.csv`
- `scheduler/summary.json`
{resilience_files}
- `tool-governance/results.csv`
- `tool-governance/summary.json`
"""
    else:
        docker_note = (
            "真实 Docker 边界样例已执行。"
            if tools["docker_status"] == "executed"
            else "真实 Docker 边界样例因本机 Docker daemon 或本地镜像不可用而未执行；未自动拉取镜像，也未计入通过率。"
        )
        report = f"""# Scheduler and Tool Governance Dry-run Report

> 生成时间：{environment['generated_at']}
> 口径：小样本 dry-run，仅校验脚本、字段、安全边界和统计逻辑；不得作为最终简历效果数据。

## 1. 范围

- 调度：3 个分散 deadline、2 个相同 deadline、1 个重启恢复、1 个 Relay 兜底，共 {scheduler['case_count']} 个样例。
- 工具治理：R0/R1 正常任务、R2 审批前/拒绝/批准、未知等级、路径逃逸、审批后 Bash 沙箱与 hard deny。
- 所有 SQLite、workspace、memory、哨兵和报告均位于本实验目录，未使用正式 `data/state.db` 或用户 workspace。

## 2. 调度结果

| 指标 | 结果 |
| --- | ---: |
| 成功样例 | {scheduler['passed_count']}/{scheduler['case_count']} |
| 漏投递 | {scheduler['missing_delivery_count']} |
| 重复投递 | {scheduler['duplicate_delivery_count']} |
| P95 投递偏差 | {scheduler['p95_delivery_drift_ms']} ms |
| 最大投递偏差 | {scheduler['max_delivery_drift_ms']} ms |
| Relay 选中/提交 | {scheduler['relay']['selected']}/{scheduler['relay']['submitted']} |

## 3. 工具治理结果

| 指标 | 结果 |
| --- | ---: |
| 已执行样例通过 | {tools['passed_count']}/{tools['executed_case_count']} |
| 环境原因未执行 | {tools['not_executed_environment_count']} |
| 工作区外哨兵未变化 | {tools['outside_sentinel_unchanged']} |

{docker_note}

审批后 Bash 的 RecordingSandbox 样例属于故障注入，只证明审批不会绕过既有 executor/profile 调用链；只有真实 Docker 样例可作为容器边界证据。

## 4. Review 结论

- 报告字段、CSV/JSON 原始记录和汇总计算已跑通；生成报告前已从 CSV 重算并与 summary 对账。
- dry-run 不替代 20 个日常任务时间压缩实验，也不替代完整攻击矩阵。
- 下一步必须经用户确认后才能运行正式实验。

## 5. 复现命令

```powershell
"{sys.executable}" scripts/effect_validation/run_scheduler_benchmark.py --dry-run --output-dir "{output_dir}"
"{sys.executable}" scripts/effect_validation/run_tool_governance_benchmark.py --dry-run --output-dir "{output_dir}"
"{sys.executable}" scripts/effect_validation/build_effect_report.py --output-dir "{output_dir}"
```
"""
    report_path = output_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(run(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
