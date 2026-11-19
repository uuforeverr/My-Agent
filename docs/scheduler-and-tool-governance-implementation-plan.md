# 定时任务可靠投递与 Risk-based 工具治理实施方案

> 状态：阶段 A～D 与正式效果实验均已完成；正式报告已生成，等待用户 Review 正常任务可用性缺口。
>
> 本文是 `scheduler-and-tool-governance-effect-validation-plan.md` 的代码实施前置方案。只有用户确认本文后，才进入业务代码修改；业务改造和回归测试通过后，还需再次确认才运行正式效果实验。

## 1. 本轮目标与非目标

### 1.1 目标

1. 保留正常投递的低延迟直接提交路径，同时增加低频 Outbox Relay，消除“outbox 已落库但未成功提交 Delivery Worker”造成的永久滞留。
2. 为 `sending` 增加 120 秒租约恢复，使 Delivery Worker 异常退出后可由 Relay 重新接管。
3. 将 delivery retry 统一交给 Relay，不再为每条失败投递创建 APScheduler retry job。
4. 将 misfire 收敛为真正的 `latest`：一次性任务最多补一次，周期任务只补最近一次并直接推进到未来。
5. 保留 `always / never / risk_based` 三种审批模式；`risk_based` 下 R0/R1 直接执行，R2 逐次审批。
6. R2 必须逐次审批，审批通过后仍完整经过 Docker 沙箱、路径校验和硬禁止规则。

### 1.2 非目标

- 不实现系统或用户自定义白名单、参数通配规则或长期 capability grant。
- 不宣称飞书等无幂等键渠道具备端到端 exactly-once；采用 at-least-once，优先避免永久丢失。
- 不在本阶段运行 20 个正式调度实验或完整 Docker 攻击实验。
- 不改造记忆、Dream、SOP 和 OTel 架构。
- 不删除 `always / never`，也不把它们纳入本轮效果实验。

## 2. 关键语义

### 2.1 投递语义

```text
业务线程写入 outbox（durable）
        ├─ 立即 submit Delivery Worker（低延迟）
        └─ Outbox Relay 周期扫描（可靠性兜底）
```

- 正常路径不等待 Relay，因此 Relay 的 15 秒间隔不计入普通到点精度。
- Relay 启动时立即执行第一轮，之后每 15 秒执行一次。
- 每轮最多选择 20 条。
- `claim_delivery` 仍作为唯一执行权原子门；直接提交和 Relay 重复 submit 同一 ID 时只有一个 Worker 能 claim。
- `sending` 超过 120 秒视为租约过期，恢复为可再次 claim 的状态。
- 如果平台已接收但本地在写 `delivered` 前崩溃，租约恢复可能重复发送；无提供方幂等键时无法同时保证不丢和绝不重复。

### 2.2 delivery 状态机

```text
pending
  └─ claim → sending
       ├─ 确认送达 → delivered（终态）
       ├─ 结果不确定 → unknown（终态，不盲目重试）
       ├─ 永久失败/重试耗尽 → failed（终态）
       ├─ 可重试失败 → retry_wait(next_attempt_at)
       └─ Worker 异常且租约过期 → retry_wait(now, lease_expired)

retry_wait 到期
  └─ Relay submit → claim → sending
```

`delivered / unknown / failed` 永远不被 Relay 重新投递。

### 2.3 `latest` 语义

- once：若尚无对应成功 occurrence，无论晚多久只执行一次，随后完成。
- interval：根据持久化的 `next_run_at` 和间隔直接计算 `<= now` 的最近 occurrence，只执行该次，并将下一次设为最近 occurrence + interval。
- cron：根据 cron 表达式求 `<= now` 的最近 occurrence，只执行该次，再求严格晚于当前时间的下一 occurrence。
- 错过多个周期时不得逐个创建 queued run。
- occurrence 唯一键仍为 `(task_id, run_kind, scheduled_for)`，重复启动不能再次补同一 occurrence。

### 2.4 审批模式语义

| 模式 | R0 | R1 | R2 | 未知等级 |
| --- | --- | --- | --- | --- |
| `always` | 审批 | 审批 | 审批 | 拒绝 |
| `never` | 自动执行 | 自动执行 | 自动执行 | 拒绝 |
| `risk_based` | 自动执行 | 自动执行 | 始终逐次审批 | 拒绝 |

所有模式都保留参数 Schema、路径边界、敏感信息硬禁止和 Docker 沙箱；`never` 仅表示不要求人工审批。

`risk_based` 本轮不允许 session scope：R2 只能批准一次，不形成会话或长期授权。

## 3. Outbox Relay 设计

### 3.1 新组件

新增 `scheduler/outbox_relay.py`：

```text
OutboxRelay
├─ start()       启动线程并立即执行首轮
├─ stop(wait)    设置 Event 并等待线程退出
├─ run_once()    回收过期租约、查询 due rows、提交 Delivery Worker
└─ wake()        可选，仅用于测试或未来降低故障恢复等待；正常直接投递不依赖它
```

实现使用独立 daemon Thread + `Event.wait(interval)`，不占用 APScheduler 的定时线程，也不使用阻塞 `sleep`。同一 Relay 进程内不允许并发执行两轮。

构造参数：

```text
repository
submit_delivery(delivery)
interval_seconds = 15
batch_size = 20
lease_seconds = 120
clock（测试注入）
```

### 3.2 Repository 改动

在 `scheduler/repository.py` 增加：

1. `recover_expired_delivery_leases(now, lease_seconds, limit)`
   - 原子地将过期 `sending` 改为 `retry_wait`；
   - `next_attempt_at=now`；
   - `last_error=delivery_lease_expired`；
   - 不触碰终态。
2. `list_relayable_deliveries(now, limit)`
   - 只查询到期的 `pending/retry_wait`；
   - 复用 `idx_delivery_outbox_due`；
   - 排序使用 `COALESCE(next_attempt_at, created_at)`。
3. 保留 `claim_delivery()` 的 compare-and-set：只有 `pending/retry_wait` 能转为 `sending`。

为避免 Relay 查询和 `deliver_by` 冲突：尚未到 `next_attempt_at/deliver_at` 的记录不得返回。

### 3.3 Coordinator 接线

修改 `scheduler/coordinator.py`：

- 构造 `OutboxRelay`，读取配置的 15/20/120。
- `start()`：
  1. 标记中断 run；
  2. 启动 APScheduler paused；
  3. 重建 task jobs；
  4. 启动 Relay（Relay 自己立即首轮）；
  5. resume APScheduler。
- 删除 `start()` 内直接 `repository.list_due_deliveries()` 的恢复逻辑。
- `stop()` 先停止 Relay，再关闭 APScheduler、execution pool 和 delivery pool，避免 Relay 向已关闭线程池 submit。
- `_enqueue_content()` 仍先事务写 outbox，再直接 `_submit_delivery()`。
- `_deliver()` 遇到 retryable failure 时只更新：
  - `status=retry_wait`；
  - `next_attempt_at=now+30s/60s`；
  - `last_error`；
  - 不再 `scheduler.add_job(_retry_delivery)`。
- 删除 `_retry_delivery()` 及 `delivery:<id>` APScheduler job。

### 3.4 Schema 与配置

现有 `started_at` 足以作为租约起点，第一版不新增 lease 列。`updated_at` 只用于审计，不参与租约判断。

在 `config.py` 和 `.env.example` 增加：

```text
OUTBOX_RELAY_INTERVAL_SECONDS=15
OUTBOX_RELAY_BATCH_SIZE=20
OUTBOX_DELIVERY_LEASE_SECONDS=120
```

旧的 `SCHEDULER_SCAN_INTERVAL` 属于 legacy Scheduler，不复用为 Relay 配置，避免语义混淆。

## 4. 真正的 `latest` 设计

### 4.1 Schedule 层

在 `scheduler/schedule.py` 增加纯函数：

```text
calculate_latest_due_and_next(schedule, persisted_next_run, now)
    -> (latest_due, next_future, missed_count)
```

规则：

- `persisted_next_run > now`：没有 misfire，保持原计划。
- once：`latest_due=persisted_next_run`，`next_future=None`，`missed_count=1`。
- interval：用整数除法直接跳至最近周期，不循环追赶。
- cron：用 croniter 计算最近 occurrence 和下一未来 occurrence；不得逐条创建历史 run。
- `missed_count` 只用于观测和测试。cron 长时间停机时采用有上限的计数，超过上限记录为 `>=cap`，不能为统计而执行无界循环。

### 4.2 Coordinator 层

`sync_task()` 发现 `next_run_at <= now` 时，先调用上述函数并持久化折叠结果，再只注册一个立即触发 job。

建议持久化顺序：

1. 计算 `latest_due` 和 `next_future`；
2. 用 `latest_due` 作为本次 `_fire_task(..., scheduled_for=...)`；
3. occurrence 创建后，将任务的 `next_run_at` 一次性更新为 `next_future`；
4. 若 once 没有下一次，则标记 completed；
5. 不再从旧 `scheduled_for` 一步一步调用 `calculate_next_run()`。

彻底删除 `SCHEDULER_CATCHUP_SECONDS` 和 `misfire_policy`：

- `Settings`、环境变量解析、`.env.example`、README 和 ChannelManager 构造参数中删除 `SCHEDULER_CATCHUP_SECONDS`；
- `APSchedulerCoordinator` 删除 `catchup_seconds` 字段和所有 `skip` 分支；
- `scheduled_tasks` 删除 `misfire_policy` 列，Repository 不再读写该字段；
- State DB schema 升级并迁移旧表，迁移时忽略旧值，所有任务统一采用 `latest`；
- legacy JSON 中即使存在 `misfire_policy` 也不再导入；
- 测试和文档不保留旧策略兼容断言。

### 4.3 `deliver_by` 注意点

- 若恢复时 deadline 已过，只补最近一次，完成后立即投递。
- 若恢复时还没到 deadline，但已经进入提前执行窗口，按新的 `latest_due` 注册准备 job。
- Relay 必须尊重 outbox 的 `next_attempt_at=deliver_at`，不能提前释放结果。

## 5. `risk_based` 工具治理设计

### 5.1 Policy API

修改 `tools/policy.py`：

- `VALID_RISK_LEVELS = (R0, R1, R2)`；删除 R3 分支。
- `normalize_approval_mode()` 接受 `always / never / risk_based`，继续兼容 `on → always`、`off → never`。
- `ToolPolicy.decide()`：
  - unknown risk → deny；
  - untrusted-to-sensitive 的现有硬禁止保持在所有模式之前；
  - always → need_approval；
  - never → allow；
  - risk_based：R0/R1 → allow；R2 → need_approval。

### 5.2 风险矩阵

不增加白名单组件或白名单数据。`risk_based` 只依据工具动态计算出的 R0/R1/R2 做决策：

| 工具/操作 | 风险 | risk_based 行为 |
| --- | --- | --- |
| `glob`、`read_file` | R0 | 自动执行 |
| `memory_search` / `read_memory` | R0 | 自动执行 |
| `scheduler.list/get` | R0 | 自动执行 |
| `web_search` | R1 | 自动执行并将结果标为 untrusted |
| `write_file/edit_file` 普通路径 | R1 | 自动执行并记录 diff preview |
| `remember_memory` | R1 | 自动执行 |
| `bash read_only` | R1 | 自动执行，但仍进入 Docker 沙箱 |
| `write_file/edit_file` 受保护路径 | R2 | 始终逐次审批 |
| `bash workspace_write` | R2 | 始终逐次审批 |
| `scheduler.create/delete/enable/disable` | R2 | 始终逐次审批 |

按用户确认，`.env*` 等文件通过 `read_file` 读取时仍为 R0，不增加特殊审批或硬拒绝。Bash 现有针对 `.env`、`api_key` 等命令文本的 hard deny 保持不变；风险矩阵必须在 README 中明确披露二者差异。

本轮不保存任何系统或用户白名单规则，不增加白名单模块、数据库表或配置文件。

### 5.3 ApprovalGate 与 Registry

修改 `tools/registry.py` 和 `tools/approval.py`：

- OTel policy span 增加 `my_agent.policy.mode`。
- `risk_based` 下需要审批的请求只允许 `once`；不读取或写入 session allowance。
- R2 在所有需要审批的模式下强制 `once`，不仅仅是 Bash。
- 审批恢复继续用 `allows_resume_request()` 校验工具名和完整参数，防止批准后替换命令。
- 审批卡的 `session_scope_allowed` 在 `risk_based` 下恒为 false；always 保持现有行为或同样收敛为一次性，实施时以最小改动为准。
- scheduled R2 继续 park，不阻塞调度线程；批准后恢复原始 exact request。

### 5.4 Bash 沙箱不因审批而降级

审批仅把 PolicyDecision 从 `need_approval` 变为 `allow`。后续仍调用同一个 `BashTool.execute()` 和 `DockerSandboxExecutor.execute()`：

```text
批准 exact request
→ BashTool hard denies
→ DockerSandboxExecutor
→ read_only / workspace_write mount
→ network none / read-only root / cap-drop / no-new-privileges / resource limits
```

不增加任何“审批后宿主执行”或“审批后放宽容器参数”的分支。

### 5.5 配置与默认值

`config.py` 的 `_parse_approval_mode()` 接受 `risk_based`；`.env.example` 和 README 列出三种模式。代码默认值和 `.env.example` 默认值均改为 `risk_based`。若用户现有 `.env` 显式设置 `always` 或 `never`，继续尊重其选择，不自动覆盖。

## 6. 文件改动清单

### 新增

- `scheduler/outbox_relay.py`
- `tests/test_outbox_relay.py`
- 后续实验阶段：`scripts/effect_validation/*`、`tests/effect_validation/*`

### 修改

- `scheduler/coordinator.py`：Relay 生命周期、移除启动扫描与 retry jobs、latest 接线。
- `scheduler/repository.py`：租约回收与 relayable 查询。
- `scheduler/schedule.py`：计算最近一次错过 occurrence 和未来下一次。
- `state/database.py`：提升 schema version，迁移并删除 `scheduled_tasks.misfire_policy`。
- `config.py`、`.env.example`：Relay 参数、删除 catchup 配置、加入并默认启用 risk_based。
- `channel/manager.py`：向 Coordinator 传 Relay 配置。
- `tools/policy.py`：三模式与 R0～R2 决策。
- `tools/registry.py`：审批 scope 和 Trace 字段。
- `tools/approval.py`：R2/risk_based 强制 once。
- `README.md` 与相关 scheduler/tool governance 文档：同步真实语义。

## 7. 分阶段实现与验证

### 阶段 A：Outbox Relay

先测试后接线：

1. pending 到期被 Relay submit；
2. retry_wait 未到时间不提交，到时提交；
3. 启动立即首轮恢复；
4. 每批最多 20 条；
5. direct submit 与 Relay 重复 submit，只有一次 claim 成功；
6. 119 秒 sending 不回收，121 秒回收；
7. delivered/failed/unknown 不回收；
8. deliver_by 未到时间不提前投递；
9. stop 后不再向 delivery pool submit。

阶段验收后运行 scheduler/state 相关离线回归，不运行正式实验。

实施结果（2026-11-18）：

- 已新增 `scheduler/outbox_relay.py`；正常路径直接 submit 保持不变，Relay 启动即首轮、随后按 15 秒间隔兜底。
- 已增加 20 条批次限制与 120 秒 `sending` 租约回收。
- Coordinator 启动时不再直接扫描 due deliveries；retryable failure 只写 `retry_wait + next_attempt_at`，不再注册 delivery retry job。
- 已接入三项 Relay 配置和 ChannelManager 参数传递。
- 新增 Relay 与 Coordinator 重试测试；scheduler/state/config/channel 针对性回归结果为 `104 passed`。
- 未运行正式效果实验，未进入阶段 B/C。

### 阶段 B：真正的 `latest`

1. once 过期一天只执行一次；
2. interval 错过 5 次只创建最近一次 run；
3. cron 错过 5 次只创建最近一次 run；
4. next_run_at 一次跳到未来；
5. 重启两次不重复补同一 occurrence；
6. deliver_by 恢复后不提前或重复投递；
7. 不产生历史 queued run 链。

实施结果（2026-11-19）：

- 新增 `calculate_latest_due_and_next()`：once 最多补一次，interval 以整数计算直接折叠，cron 只选取不晚于当前时间的最近 occurrence。
- Coordinator 在恢复同步和实际触发时都会校正为最近 occurrence，只创建一个 run；旧回调再次到达时会因 `next_run_at` 已推进而失效。
- legacy JSON Scheduler 同步采用相同 latest-only 语义，失败重试仍固定在同一个最近 occurrence。
- 已彻底删除 `SCHEDULER_CATCHUP_SECONDS`、Coordinator/legacy catchup 分支及 Repository 中的 `misfire_policy` 读写。
- State DB schema 升至 v5；迁移会删除旧 `scheduled_tasks.misfire_policy` 列并保留任务数据。
- 新增 once、interval、cron 折叠、重复旧回调和 v5 数据迁移测试。
- 针对性回归结果为 `89 passed`；全量离线回归结果为 `426 passed, 4 skipped`。
- 未运行正式效果实验，未进入阶段 C。

### 阶段 C：risk_based

1. always/never 既有行为回归；
2. risk_based R0/R1 自动执行；
3. risk_based R2 始终审批；
4. unknown risk 拒绝；
5. risk_based 审批不能选 session scope；
6. 审批前、拒绝、超时均不执行；
7. 批准后只能执行原工具名和原参数；
8. scheduled R2 park/resume；
9. 已批准的 Bash 仍由 fake sandbox/真实 Docker 边界约束；
10. `read_file .env` 作为 R0 自动执行，而 Bash 读取 `.env` 仍被 hard deny，验证实现与已确认矩阵一致。

实施结果（2026-11-19）：

- 审批模式保留 `always / never / risk_based`，代码和 `.env.example` 默认值已改为 `risk_based`，旧 `on/off` 仍只作为配置别名解析。
- 风险等级收敛为 R0–R2；R3/L3 不再是合法等级，未知等级统一拒绝。
- `risk_based` 下 R0/R1 自动执行，R2 在交互调用中等待审批、在定时任务中 park；无 ApprovalGate 时不会执行。
- 所有 R2 的 session 审批请求都会降级为 once，审批卡和文本提示不再为 R2 展示 session scope。
- 审批恢复继续校验原工具名、完整参数、session 和 user；批准不会更换 Bash 执行器或放宽沙箱 profile。
- 已验证获批的 `bash workspace_write` 仍进入同一 SandboxExecutor，获批后读取 `.env` 仍在执行器调用前被 hard deny。
- `read_file .env` 按确认保持 R0 自动执行；Bash 对 `.env`/API Key 的命令文本硬拒绝保持不变。
- OTel policy span 新增 `my_agent.policy.mode`，README 已同步三种模式、R0–R2 矩阵和审批/沙箱边界。
- 治理相关回归结果为 `122 passed, 2 skipped`；修正与审批无关的测试夹具后，全量离线回归为 `431 passed, 4 skipped`。
- 未运行阶段 D dry-run 或正式效果实验。

### 阶段 D：小样本 dry-run

- 调度：3 个分散任务、2 个同时任务、1 个重启恢复、1 个 Relay 兜底。
- 工具：每个风险等级和审批结果各 1 个；Docker 只做无害读取和工作区外哨兵验证。
- 只验证报告字段、安全边界和统计口径。

dry-run 完成后暂停，提交结果给用户 Review；只有再次确认后才运行 20 个日常任务和完整攻击矩阵。

实施结果（2026-11-19）：

- 新增三个可重复运行的隔离脚本：调度 dry-run、工具治理 dry-run、CSV/JSON 对账与报告生成；实验输出写入被 Git 忽略的 `artifacts/effect-validation/`。
- 调度覆盖 3 个分散 deadline、2 个相同 deadline、1 个 Coordinator 重启恢复和 1 个未直接 submit 的 Relay 恢复，共 7 个样例。
- 调度结果为 7/7 最终送达，漏投递 0、重复投递 0；本轮 P95/最大投递偏差均为 136.751 ms。该数字只用于校验 dry-run 口径，不进入简历。
- Relay 兜底样例选中 1 条并提交 1 条，最终状态为 delivered。
- 工具治理共记录 12 个样例；其中 11 个已执行样例全部通过，覆盖 R0/R1 正常操作、R2 审批前/拒绝/超时/批准、未知等级拒绝、路径逃逸、审批后沙箱调用链及 hard deny。
- 工作区外哨兵哈希保持不变；`read_file .env` 使用实验假数据按 R0 成功，获批 Bash 读取 `.env` 仍在进入 executor 前被拒绝。
- 真实 Docker 边界样例因本机 Docker daemon 或本地镜像不可用标记为 `not_executed_environment`，未自动拉取镜像、未计入通过率；RecordingSandbox 结果明确标记为故障注入，不冒充真实容器证据。
- 报告生成前会从两份 CSV 重算数量并与 summary.json 对账；相关回归结果为 `70 passed`。
- 原始数据和报告位于 `artifacts/effect-validation/dry-run-20261119-stage-d/`。

## 8. 回归测试范围

最低需要运行：

```text
tests/test_state_database.py
tests/test_scheduler_schedule.py
tests/test_scheduler_coordinator.py
tests/test_scheduler_executor.py
tests/test_scheduler_approval.py
tests/test_scheduler_tool.py
tests/test_outbox_relay.py
tests/test_tool_policy.py
tests/test_approval.py
tests/test_registry.py
tests/test_bash_tool.py
tests/test_security_boundaries.py
tests/test_config.py
tests/test_channel_manager.py
```

完成三个业务阶段后再运行全量离线回归。真实模型、真实飞书和正式效果实验不随单元测试自动触发。

## 9. 风险与回滚

1. Relay 重复提交：由数据库原子 claim 防止普通重复执行。
2. sending 租约误回收：120 秒必须大于 Channel 正常发送超时；若渠道可能阻塞更久，应先给 Channel 设置明确超时或提高租约。
3. 平台已收但本地未记账：at-least-once 下可能重复，报告必须披露。
4. cron latest 边界：精确时间点、时区和夏令时必须用确定性用例覆盖；当前系统仍使用本地 naive datetime，本轮不顺带重构完整时区模型。
5. risk_based 默认启用会使 R2 开始审批；显式配置 `always/never` 的用户不受影响，README 必须说明升级行为。
6. R0/R1 自动执行范围较宽：尤其 `read_file .env` 可进入模型上下文，这是用户确认的风险取舍，测试与文档必须如实体现。

所有改动均以小步 `apply_patch` 实施，不重置用户当前 dirty worktree。若某阶段失败，只回退该阶段新增接线，不删除既有任务、run 或 outbox 数据。

## 10. 已确认决策与实施门禁

用户已确认：

1. 新增独立 `OutboxRelay`，采用 15 秒 / 20 条 / 120 秒参数，并统一负责启动恢复、retry_wait 和过期 sending；
2. 删除 `SCHEDULER_CATCHUP_SECONDS` 和 `misfire_policy`，不保留旧行为兼容；
3. risk_based 下 R0/R1 自动执行，R2 逐次审批；
4. `.env*` 的 `read_file` 读取保持 R0，不增加特殊审批；
5. 默认 `TOOL_APPROVAL_MODE=risk_based`；
6. 保留 always/never 作为用户显式选择；
7. 不实现系统或用户白名单。

阶段 D 门禁已通过，正式实验已按用户确认执行；结果见下一节。

## 11. 正式效果实验结果（2026-11-19）

正式运行目录：`artifacts/effect-validation/formal-20261119/`。

### 11.1 调度

- 固定 seed `20261119`，将 20 个个人日常任务分布压缩到 120 秒真实时钟窗口。
- 20/20 最终送达，漏投递 0、普通重复投递 0；P95 投递偏差 93.882 ms，最大偏差 100.471 ms。
- 5 个同 deadline 任务全部送达，首尾投递展开 189.1 ms。
- 重启恢复、一次性/周期任务 latest、Relay 漏提交恢复、两次 retryable 后第三次成功、permanent failure、ambiguous unknown、过期租约和 deliver_by 不提前投递均按预期。
- 5 秒慢任务存在时，三个轻任务 P95 偏差相对日常基线增加 50.371 ms。
- 调度正式矩阵合计 44/44 通过。

### 11.2 工具治理

- 首轮使用 `python:3.11-slim` 时，10 个正常任务仅通过 8 个，定位到镜像缺少 `rg` 和 `pytest`，安全性与可用性无法同时满足。
- 新增项目镜像 `my-agent-sandbox:py311-v1`：基础镜像锁定 digest，并安装 ripgrep 14.1.1 与 pytest 8.4.2；默认配置、README、身份提示和正式实验脚本同步切换到该镜像。
- 使用真实 Docker daemon 复跑路径逃逸、未挂载宿主路径、只读挂载、只读根文件系统、capability、断网、资源限制、hard deny 和审批后沙箱边界。
- 26/26 个可执行攻击/边界样例全部被阻断；工作区逃逸成功 0、工作区外文件修改 0、假凭证泄露 0、网络外传成功 0。
- 10/10 个正常任务通过，可用性从首轮 80% 提升至 100%；45 个可执行治理样例全部通过。
- Windows 当前权限不能创建目录软链接，1 项按 `not_executed_environment` 记录，未计入攻击拦截率；需在 Developer Mode 或 Linux 环境补跑。

### 11.3 数据口径

- 报告生成器从 `occurrences.csv`、`faults.csv` 和 `results.csv` 重新统计并与 summary.json 对账。
- 只将 `daily_20` 用于到点精度结论；故意过期一天的 once latest 样例不混入精度指标。
- Outbox 仍明确为 at-least-once，不声称端到端 exactly-once。

正式实验与镜像修复复跑均已完成；最终简历只引用复跑后的 100% 正常任务可用性和 26/26 攻击/边界拦截结果，并保留软链接样例未执行的实验限制。

### 11.4 真实进程重启与 20 项同时到期补充实验

原正式实验的“重启”只重建了同一进程内的 coordinator，集中到期也只有 5 个通用提醒，不能单独支撑“进程重启后可靠恢复”和“个人助手极端集中到期”的结论。因此新增独立子进程实验：

- 在任务到期前强制终止整个调度子进程，再由新进程连接同一 SQLite。覆盖 once、interval、cron 的 6 个任务全部恢复触发并送达，P95 到点偏差 91.957 ms，最大偏差 91.957 ms。
- 构造执行结果和待投递记录已经写入 SQLite、但即时发送前进程以退出码 73 崩溃的故障。新进程启动后由 Relay 恢复投递，恢复耗时 368.448 ms，结果只发送一次。
- 将 20 个代表性用户任务设为同一 deadline，覆盖 once、interval、cron 三种调度类型，以及提醒、Agent、工具三种动作类型。20/20 最终送达，P95 交付延迟 1704.729 ms，最大 1722.842 ms，全部交付展开 1460.73 ms，漏发和普通重复发送均为 0。
- 实验预先固定验收线为 P95 不超过 2 秒、最大不超过 3 秒；实测通过。外部 LLM 与工具主体使用确定性替身，以排除外部服务耗时；调度、独立进程重启、SQLite、执行线程池、结果落库和投递链路均为真实实现。

原始数据位于 `artifacts/effect-validation/formal-20261119/scheduler-resilience/`。
