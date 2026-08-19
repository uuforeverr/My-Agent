# 工具审核与上下文压缩方案

> 目标：给 my-agent 补上「执行前统一审批」和「条数 + token 双触发的会话压缩」，对标 Cursor 的工程层做法，而不是复刻 Composer 自总结。
> 本文档先落盘设计，**经认可后再按步骤实现**。每步只做当前步骤，Review 通过后才进入下一步。
> 讨论结论见第 2 节；实现顺序见第 7 节。

---

## 1. 问题与边界

### 1.1 现状

短上下文：`ContextManager._trim_history()` 只按 `max_history_messages`（默认 40）**硬丢弃**旧消息。被丢掉的内容异步 flush 成 daily 情景记忆，窗口里不留工作摘要。没有 token 预算，也没有 `/summarize`。

工具输出：`bash` 截 2 万字、`read_file` 截 6 万字，截掉的部分不可恢复。

工具安全：workspace 路径沙箱和 `bash` 正则黑名单都在工具内部。`ToolRegistry.run` 解析参数后直接执行。调度任务可以在无人值守时跑 `bash`。`bash` 使用 `shell=True`。

### 1.2 明确不做

- 训练式 self-summarization、KV cache 复用（HTTP Chat Completions 做不到）
- MCP / 工具 schema 按需加载（当前约 9 个工具，收益低）
- 飞书交互卡片 / `card.action` 回调（本轮用文本命令）
- 把 daily curator 的 prompt 拿来做会话压缩（职责不同）
- 会话 history 常规持久化（与已有会话粒度约定一致；调度挂起也不落盘 history）
- 每次 `edit_file` / `write_file` 把 diff 推到飞书
- 用户终端会话全量同步成文件（没有集成终端）

### 1.3 两套摘要不要混

| 机制 | 目的 | 结果放哪 |
|------|------|----------|
| **会话压缩**（本方案新增） | 当前对话还能继续 | 一条 `conversation_summary` 留在短期 history |
| **memory flush**（已有） | 跨天回忆 | daily 情景记忆，不进短期窗口 |

压缩替换掉的旧消息，仍然走现有 `flush_from_messages`。两套 prompt 分开，不复用 `MemoryCurator.summarize_daily`。

---

## 2. 已确认决策

1. 压缩触发：**条数 OR token**，任一超限就压。不是 AND。
2. 压缩方式：用摘要**替换**旧消息，不再硬丢；被替换的仍 flush 到 daily。
3. token 计数：用 **tiktoken**，按模型选 encoding。
4. 超长 tool 输出：跟 **Cursor**（全量落盘 + 预览 + 路径），不跟 Codex 主路径的纯截断。预览阈值 = 落盘阈值。实现放在步骤 5，不提前到压缩步骤。
5. `web_search`：**不审批**。标 L2（有网络），`requires_approval=False`，自动放行并记 Trace。
6. L1 写文件预览：只写 Trace，不发渠道。
7. 调度遇到 L2/L3：任务进入 `waiting_approval`（最小挂起，见 4.4）。
8. 审批展示：操作 ID + 工具名 + 风险等级 + 参数（脱敏/截断）+ 作用域说明。
9. 作用域：`once`（仅本步）+ `session`（本会话同工具放行）。
10. 群聊：仅原请求的 `user_id` 可批。
11. `bash`：本轮去掉 `shell=True`，改为 **严格 argv + 命令白名单**；禁止管道和重定向；不兼容 `echo` 等 shell 内建。
12. 实现顺序：策略骨架（默认 off）→ 压缩双触发 → 打开审批通道 → 调度 waiting_approval → 溢写文件 → bash argv/白名单。
13. token 触发阈值 = **模型最大上下文 × 80%**（可被环境变量覆盖）。

### 2.1 文档锁定的默认值（讨论中未逐条投票，实现前可改）

| 项 | 默认 | 说明 |
|----|------|------|
| 未知模型 context window | `32768` | 避免把 8k/32k 模型当成 128k |
| 覆盖项 | `MODEL_CONTEXT_WINDOW` | 显式数字，优先于内置表 |
| 压缩保留窗口 | 最近 **3 个完整 turn** | turn = 一次 user，加上其后 assistant / tool，直到下一条 user |
| 交互审批超时 | **120s** | 超时 = 拒绝 |
| 调度审批超时 | **24h** | 超时 = 本轮失败，cron 前进到下次，once 任务 disable |
| 溢写阈值 | **2000 tokens**（tiktoken） | 与预览同一条线 |
| 审批 ID | 8 位 hex | 与现有 `task_id` 风格一致 |
| 测试/CI | `TOOL_APPROVAL_MODE=off` | 现有工具测试不卡住 |

若这些默认值要改，在认可方案时一并指出。

---

## 3. 上下文压缩

### 3.1 触发

在 `ContextManager.build_model_messages()` 里，组装 **system + history + tool schemas** 之后、调用模型之前：

```text
need_compact = (history 条数 > max_history_messages)
            OR (tiktoken(input_payload) > max_input_tokens)
```

`max_input_tokens = floor(model_context_window * 0.8)`

`model_context_window` 解析顺序：

1. 环境变量 `MODEL_CONTEXT_WINDOW`（正整数）
2. 内置前缀表（见 3.2）
3. 回退 `32768`

含义（以默认 `gpt-4o-mini` 128k 为例）：token 触发约在 **102400**。日常多轮对话通常会先撞 **40 条**；token 触发是大工具输出的安全网。步骤 5 溢写上线后，单次工具结果不再轻易把窗口打满。

手动 `/summarize`：CLI / 飞书在 channel 层拦截（与 `/trace` 同类，**不**进 `Agent.run`），对当前 session 的 `ContextManager` 强制压一轮，回「已压缩」或「无需压缩」。

### 3.2 模型窗口与 encoding

内置表（前缀匹配，更具体的放前面）：

| 前缀 | context window | tiktoken encoding |
|------|----------------|-------------------|
| `gpt-4o` / `gpt-4.1` / `o1` / `o3` / `o4` | 128000；o 系列 200000 | `o200k_base` |
| `gpt-4-turbo` / `gpt-4` | 128000 | `cl100k_base` |
| `gpt-3.5` | 16385 | `cl100k_base` |
| `deepseek` | 128000 | `cl100k_base`（近似） |
| `qwen` | 131072 | `cl100k_base`（近似） |
| `glm` | 128000 | `cl100k_base`（近似） |
| 其他 | 32768 | `cl100k_base` |

`tiktoken.encoding_for_model(model)` 成功则用它的 encoding；失败再按上表。非 OpenAI 模型的 token 数是近似值，只用于触发，不用于计费。

计数范围：identity + runtime + memory + skills + history（含 `tool_calls` JSON）+ 当前 `tools.schemas()`。每条消息加固定开销（OpenAI cookbook：约 +3～4 tokens/message），避免只数正文。

新增依赖：`tiktoken` 写入 `requirements.txt`。

### 3.3 压缩算法

1. 从 history 尾部切出「最近 3 个完整 turn」，保证不拆开 `assistant.tool_calls` 与后续 `tool`。
2. 其余旧消息作为 `to_compact`。若 `to_compact` 为空（例如 40 条全在最近 3 turn 里、只是 token 超了），则先对超长 tool 消息做就地预览化（步骤 5 上线前：对超长 tool content 做头尾截断并打标；步骤 5 后走溢写）。**不允许**把最近 turn 压进摘要。
3. 调用一次无 tools 的 LLM（`prompts/compaction.md`），产出短摘要，必须包含：
   - 用户目标与已做决策
   - 改过的文件路径
   - 剩余任务 / 失败过的路
   - `compact_count`（已压缩次数 + 1）
   - 原文位置：`data/memory/raw/{session}/YYYY-MM-DD.jsonl`
4. history 变为：`[summary_message] + recent_turns`。`summary_message` 用 `role=user` 包在标签里，避免破坏 tool_call 配对：

```text
<conversation_summary compact_count="N">
...结构化短摘要...
原文：data/memory/raw/...
若摘要缺细节，用 memory_search 或按路径阅读原文。
</conversation_summary>
```

5. `to_compact` 仍调用现有 `memory.flush_from_messages(reason="context_compact")`。
6. Trace：`context.compact` span，记录 `reason=count|tokens|manual`、压缩前/后条数与 token、`compact_count`。
7. 压缩 LLM 失败：不丢 history，本轮跳过压缩并打 error span；若已经超过硬顶，只对超长 tool 内容做预览化，避免死循环。压缩调用本身不计 `max_steps`。

`ContextManager` 增加内存态：`compact_count`、`last_compact_reason`。随进程结束而消失，可接受。

### 3.4 `/summarize` 与 `/new`

`/summarize` 压缩当前会话，继续用同一 `session_id`。
`/new`（若已实现）换会话，history 本来就是空的，与压缩无关。

---

## 4. 工具审核

### 4.1 风险分级（按调用，不按工具名一刀切）

`BaseTool` 增加：

```python
risk_level = "L0"            # L0 / L1 / L2 / L3
requires_approval = None     # None = 由 policy 按等级决定；False 强制不批
idempotent = True
```

`scheduler` 必须实现 `risk_for(arguments) -> str`，因为一个工具混了只读和写。

| 等级 | 默认策略 | 调用 |
|------|----------|------|
| L0 | 放行 | `glob` `read_file` `memory_search` `read_memory` `scheduler.list/get` |
| L1 | 放行；`write_file`/`edit_file` 在 Trace 记 path + diff/预览 | `write_file` `edit_file` `remember_memory` |
| L2 | 交互要批；调度挂起 | `bash`；`scheduler.create/delete/enable/disable` |
| L2 网络只读 | **放行**（`requires_approval=False`） | `web_search` |
| L3 | 要批 / 调度挂起 | 本轮无独立 L3 工具；预留 |

L1 diff：`edit_file` 在真正写入前算 unified diff 的前 N 行写入 span；`write_file` 记 path + 字节数 + 是否新建。不推渠道。

### 4.2 执行前统一 policy

`ToolRegistry.run` 顺序：

```text
hidden tool? → 解析 JSON → 构造 ToolRequest → ToolPolicy.decide
  allow           → tool.run（现有沙箱/黑名单仍在工具内）
  deny            → ToolResult(ok=False)，不抛异常
  need_approval   → 见 4.3 / 4.4
```

`ToolRequest` 字段：`name`、`arguments`、`risk_level`、`session_id`、`channel_type`、`user_id`、`is_scheduled_task`、`task_id`。

`TOOL_APPROVAL_MODE`：

- `off`：全部当 allow（测试默认；步骤 1 上线时的默认，避免半成品打断日常使用）
- `on`：按上表执行（步骤 3 打开审批通道时切到 on，或由配置打开）

Trace：`tool.policy` span 作为 `tool.execute` 的兄/父节点，记录 `decision`、`risk_level`、`approval_id`、`scope`、`approver_user_id`。

### 4.3 交互渠道审批（步骤 3）

**死锁约束：** 同一 `session_id` 的 `BoundedSemaphore(1)` 会被正在跑的 `Agent.run` 持有。`/approve` 绝不能 `produce()` 进 Agent。

CLI：在 `_dispatch` 里处理 `/approve` `/deny`，与 `/trace` 一样直接返回。
飞书：在 `_handle_message_event` 里处理，回执后 return，不 `produce`。

交互路径：**阻塞等待**（ApprovalGate 的 `threading.Event`，超时 120s）。`/approve` 在 channel 线程 set event，agent 线程被唤醒后继续 `tool.run`。群聊校验 `sender.user_id == request.user_id`。

发给用户的文本至少包含：

```text
待审批 [a3f9c1d2]
工具: bash    等级: L2
参数:
  command: pytest tests/test_bash_tool.py
  timeout: 20

/approve a3f9c1d2          仅本次
/approve a3f9c1d2 session  本会话允许同类工具
/deny a3f9c1d2             拒绝
超时 120s 将自动拒绝。
```

参数展示规则：

- 列出全部顶层 key
- `content` / `new_text` / `old_text` 超过 200 字截断并附字数
- key 名或值匹配 `api_key|token|password|secret|authorization` 则显示 `[redacted]`
- 禁止把 `write_file` 全文塞进审批消息

`session` 作用域记在内存：`(session_id, tool_name) → allow`，进程结束即失效。

### 4.4 调度 waiting_approval（步骤 4，最小挂起）

调度线程**不能** `Event.wait`。

当 `is_scheduled_task` 且 decision 为 need_approval：

1. 登记内存态 `PendingApproval`（含 tool 名/参数、`tool_call_id`、session_id、ChannelTarget、task_id）。
2. **不**把 tool 结果写入 history（assistant 的 `tool_calls` 已在 history 里，resume 时补 `tool` 消息）。
3. `Agent.run` 结束并返回待审批文本；由 executor 投递到原渠道。
4. executor 返回第三种结果 `waiting_approval`（不是 True/False）。
5. `SchedulerService`：**不** `_advance`，**不**当投递失败重试；把 task 标为：

```text
run_state: waiting_approval
pending_approval_id: a3f9c1d2
parked_session_id: scheduler:...
```

`_is_due` 对 `run_state=waiting_approval` 的任务直接跳过，避免每 30s 再跑一遍。

用户 `/approve`：channel 拦截 → ApprovalGate → `Agent.resume_after_approval(approval_id)`（在线程池执行，不阻塞 websocket / CLI input）：

1. 找到仍在内存的 `ContextManager`
2. 执行已批准的 tool，`append_tool_result`
3. 从当前 step 继续 agent loop，直到最终回答或再次挂起
4. 把最终回答 `send_to` 原 ChannelTarget
5. 清除 `run_state`，再 `_advance` 日程

`/deny` 或超时：tool 侧视为失败结果写回 history（这样模型若仍在 resume 路径上能解释拒绝）；调度任务 `last_error=approval_denied|approval_timeout`。once 任务 disable；cron/interval `_advance` 跳过本轮。

**进程重启：** history 与 PendingApproval 都在内存，无法续跑。启动时扫描 `run_state=waiting_approval` 的任务：清状态、通知原渠道「进程重启，待审批已失效」、once disable / 周期任务 `_advance`。不把 parked history 落盘。

**24h 超时：** 跟在 `SchedulerService.check_and_execute` 里扫 PendingApproval 的 `created_at`，不要另起线程。

`memory_dream` / `send_message` 不走 ToolPolicy。调度里的直接 `tool_call` 动作同样过 `ToolRegistry.run`；L2 时挂起，resume 只执行那一个 tool 再投递结果（不必进 agent loop）。

---

## 5. 超长 tool 输出（步骤 5，跟 Cursor）

Cursor：shell / MCP 等不可控大输出不截断丢弃，而是写入文件，模型用 `tail` / `read` / `grep` 按需取。Codex 主路径仍是 history 里头尾截断（不可恢复）；他们后来只在 hook 上做了 spill。

my-agent 跟 Cursor，并避开 Cursor CLI 曾出现的「20k 截断、40k 才落盘」空洞：**预览阈值 = 落盘阈值**。

流程（`ToolRegistry.run` 在 `tool.run` 成功返回之后）：

1. 用 tiktoken 数 `result.content`
2. `<= 2000 tokens`：原样进 history
3. `> 2000`：全文写入 `workspace/.agent/tool_outputs/{session_sanitized}/{timestamp}_{tool}_{shortid}.txt`
4. 返回给模型的 content = 头尾预览 + 路径 + 原 token/字节/行数 +「用 read_file 的 offset/limit 继续读」
5. 写文件失败：才退回纯截断，并明确告知 truncated（Cursor 也是如此）

例外：`read_file` 已经返回「路径 + 行范围」，超长时不重复落盘，保留现有 `offset/limit` + `MAX_READ_CHARS`（这是 Cursor 说的 first-party 紧凑格式）。

步骤 5 删除 `BashTool._truncate_output` 的静默截断。`.agent/` 加入 `.gitignore`。

步骤 5 之前（步骤 2 的 token 安全网）：压缩若因单条超长 tool 触发、且溢写尚未上线，允许对该条 content 做头尾预览化，避免窗口爆掉。步骤 5 上线后删掉这条临时逻辑。

---

## 6. bash 严格 argv（步骤 6）

去掉 `subprocess.run(..., shell=True)`。

1. `shlex.split(command)` 失败则拒绝（引号不配对）。
2. 任一 token 含元字符或整条命令匹配管道/链则拒绝：`|` `&` `;` `>` `<` `` ` `` `&&` `||` `$()` 换行。
3. `argv[0]` 必须是白名单中的**短名**（不能带路径）。
4. `shutil.which(argv[0])` 解析可执行文件；用户给的 token 仍禁止绝对路径、`~`、`..`。which 得到的系统绝对路径允许。
5. `subprocess.run(argv, shell=False, cwd=workspace, env=_safe_env(), timeout=...)`。
6. 现有凭证/特权/删根黑名单保留，作为第二道闸。

白名单（短名）：

```text
python  python3  py  pytest  pip
git
rg
node  npm  npx
ruff  mypy
```

不包含：`echo` `dir` `ls` `cat` `type` `bash` `sh` `cmd` `powershell` `pwsh` `curl` `wget`。Windows 上 `echo` 不是可执行文件；模型改用 `python -c`。

工具 description 写明：禁止管道与重定向；不要用 echo；长输出会被步骤 5 落盘。

现有测试里的 `echo hello`、`sh -c 'exit 3'` 改为等价的 `python -c`。

---

## 7. 实现顺序与文件

每步可独立 Review。步骤 1 默认 `TOOL_APPROVAL_MODE=off`，不改变日常行为。

### 步骤 1 — 策略骨架（默认 off）

新增/修改：

- `tools/base.py` — `risk_level` / `requires_approval` / `idempotent` / 可选 `risk_for`
- `tools/policy.py` — `ToolRequest`、`ToolDecision`、`ToolPolicy`
- `tools/registry.py` — run 前过 policy；mode=off 短路为 allow
- 各工具类补静态等级；`tools/scheduler_tool.py` 实现 `risk_for`
- `config.py` / `.env.example` — `TOOL_APPROVAL_MODE`
- `observability` 使用现有 span API，不改存储格式

测试要点：off 时行为与现在一致；on 时 L2 在无 ApprovalGate 下应 deny 而不是真执行（步骤 1 尚无审批通道，on 模式对 L2/L3 先 deny）；`scheduler.list` 为 L0、`create` 为 L2；`web_search` 即使 on 也不批。

### 步骤 2 — 条数 + token 双触发压缩

新增/修改：

- `agent/token_counter.py` — encoding、窗口表、`count_messages` / `count_tools`
- `agent/compactor.py` — 调 LLM、解析摘要
- `prompts/compaction.md`
- `agent/context_manager.py` — 用压缩替换硬 trim；失败回退；仍 flush
- `agent/agent.py` — `build_model_messages` 时传入 tool schemas 以便计数
- `config.py` / `.env.example` — `MODEL_CONTEXT_WINDOW`、`KEEP_RECENT_TURNS`（默认 3）
- `channel/cli/cli_channel.py`、`channel/feishu/feishu_channel.py` — `/summarize`
- `requirements.txt` — `tiktoken`

测试要点：只超条数、只超 token、都超、都不超；turn 对齐不拆 tool 对；压缩后 history 以 summary 开头；flush 仍发生；`/summarize` 不进 agent；tiktoken 对未知模型不抛；压缩 LLM 失败不丢 history；`max_input_tokens` 随窗口 80% 变化，可被 `MODEL_CONTEXT_WINDOW` 覆盖。

### 步骤 3 — 打开审批通道

新增/修改：

- `tools/approval.py` — `ApprovalGate`（内存）、操作 ID、session 作用域、超时
- `tools/registry.py` / `tools/policy.py` — 接 Gate；L1 Trace 预览
- `agent/agent.py` — 必要时把 `on_event` 用于「待审批」即时推送（也可在 registry 里通过 context 发）
- `channel/cli/cli_channel.py` — `/approve` `/deny`，不 produce
- `channel/feishu/feishu_channel.py` — 同上；群聊校验 user_id
- `channel/reply.py` — 无需改类型，文本即可
- 默认配置仍可先 off，测试显式 on；文档说明生产打开 `TOOL_APPROVAL_MODE=on`

测试要点：审批文案含名字和参数；敏感字段 redacted；超时拒绝且工具未执行；`/approve` 不进 Agent.run；session 作用域第二次同工具不询问；群聊其他人 `/approve` 无效；L0/L1/web_search 不问。

### 步骤 4 — 调度 waiting_approval

修改：

- `scheduler/task_store.py` / 任务 dict — `run_state`、`pending_approval_id`、`parked_session_id`
- `scheduler/service.py` — 三态返回值；跳过 waiting；扫 24h 超时；启动时失效重启残留
- `scheduler/executor.py` — 把 parked 与 delivered 分开；resume 后投递
- `agent/agent.py` — `run` 遇到 park 时保留未完成的 tool_calls；新增 `resume_after_approval`
- `channel/manager.py` — 把 resume 回调接到 Gate；启动 sweep 通知
- `tools/approval.py` — 区分 interactive wait vs scheduler park

测试要点：agent_task 中途 L2 不阻塞扫描线程；waiting 期间不重复触发；approve 后续跑完并 `_advance`；deny/超时/重启的 once vs cron 行为；直接 `tool_call` 动作的挂起与恢复；交互路径仍是 120s 阻塞，互不干扰。

### 步骤 5 — 溢写文件

修改：

- `tools/registry.py` — 统一 spill
- `tools/bash_tool.py` — 删除静默截断
- `.gitignore` — `.agent/`
- 步骤 2 里「超长 tool 临时预览化」删除或改为只走 spill

测试要点：2010 tokens 落盘且 history 含路径；1990 tokens 不落盘；落盘失败才截断并标明；`read_file` 不重复落盘；session_id 含冒号时目录名合法。

### 步骤 6 — bash argv + 白名单

修改：

- `tools/bash_tool.py` — split / 元字符 / which / `shell=False`
- `tests/test_bash_tool.py`、`tests/test_security_boundaries.py` — 命令改 `python -c`；新增管道拒绝、非白名单拒绝
- `prompts/identity.md` Tool Policy — 同步「无管道、无 echo」

测试要点：`python -c` 成功；`echo` 拒绝；`pytest | cat` 拒绝；`python ../../../x` 拒绝；绝对路径 token 拒绝；白名单外 `curl` 拒绝；Windows 上 `py`/`python` 至少一条可用（CI 若无 Python 短名则 skip）。

---

## 8. 关键实现思路（便于对照代码）

### 8.1 压缩与 Agent loop

`build_model_messages` 需要 tool schemas 才能把 schema token 算进去。由 `Agent.run` 传入 `tool_schemas`，避免 ContextManager 反向依赖 Registry。

压缩是同步 LLM 调用，放在 `context.compact` span 内。不要用 memory 的后台线程：压缩结果必须立刻写回 history。

### 8.2 审批与会话锁

```text
交互: Agent 线程 wait(Event) ← channel 线程 /approve 不进队列
调度: Agent.run 直接返回 parked ← 稍后 resume_after_approval 在线程池跑
```

`ApprovalGate` 用一把锁保护 `pending` dict。approve 时校验：id 存在、status=pending、user_id 匹配、未超时。

### 8.3 resume 的 history 不变量

park 时 history 末尾必须是带 `tool_calls` 的 assistant 消息，且还没有对应 `tool` 消息。resume 只 `append_tool_result`，再进入现有 `for step` 逻辑。不要再 append 一次同样的 assistant 消息。

若 resume 时 ContextManager 已不在（重启），走 4.4 的失效通知，不要凭空 new 一个空 history 续跑。

### 8.4 可复用的现有能力

- Trace / span：照 `tool.execute`、`context.build` 的写法加 `tool.policy`、`context.compact`
- `RunContext.metadata["is_scheduled_task"]`、`SCHEDULER_TASK_HIDDEN_TOOLS`
- `ChannelTarget.from_context` / `send_to`：审批通知和 resume 投递
- CLI `_dispatch`、飞书 `_handle_message_event`：slash 命令拦截
- `read_file` 的 offset/limit：溢写后的回读
- `MemoryManager.flush_from_messages`：压缩后的 daily 摘要
- `edit_file` 已有 old/new 文本，L1 preview 可在 execute 前算 diff，不必新工具

---

## 9. 测试要点汇总

按步骤写自动化，不在本方案阶段加测试文件。每步 Review 时用本节核对。

- Policy 短路 off、等级表、scheduler 按 action 分级、web_search 不批
- 双触发 OR、turn 对齐、摘要替换而非硬删、flush 仍触发、手动 /summarize
- tiktoken 未知模型、80% 预算、`MODEL_CONTEXT_WINDOW` 覆盖
- 审批文案、脱敏、超时、session 作用域、群聊身份、slash 不进 Agent
- 调度三态、挂起不重入、resume 续跑、deny/超时/重启
- 溢写同一阈值、路径回读、bash 不再静默截断
- argv 白名单、元字符、现有安全用例仍失败

---

## 10. 认可后怎么走

请确认本文第 2 节决策和第 2.1 节默认值。认可后**只开始步骤 1**（策略骨架，默认 off），做完汇报再进入步骤 2。
