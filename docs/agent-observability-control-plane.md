# Agent 监控控制面板设计方案

本文档面向 `my-agent` 从学习型 demo 走向可运营 Agent 系统后的观测、调试和持续改进需求。目标不是做一个展示型 dashboard，而是建立一个可以回答生产问题的控制面板：

- 这次执行到底走了哪些步骤？
- 为什么模型选择了这个工具、注入了这些记忆、输出了这个答案？
- 延迟、成本、错误、循环、调度失败分别由哪个环节贡献？
- 哪些真实生产 trace 应该进入回归评测集？
- 哪些 prompt、工具、记忆或调度任务需要被回滚、暂停或人工介入？

## 结论先行

建议采用“本地自有控制面板 + OpenTelemetry 兼容导出”的路线。

`my-agent` 有自己的 channel、scheduler、memory、tool、workspace 安全边界，这些不是通用 LLM 观测产品天然理解的对象。因此控制面板应由我们自有实现，保留生产运维能力；同时事件和 span 命名尽量兼容 OpenTelemetry GenAI / OpenInference 语义，后续可以导出到 Phoenix、Langfuse 或其他后端，避免锁死在某个 UI 或存储方案上。

第一阶段优先级：

1. 把现有扁平 `Trace.events` 升级为可嵌套 span 树，并真正落盘。
2. 为 LLM、tool、memory、context、scheduler、channel 补齐结构化观测字段。
3. 先做 Trace Explorer 和 Live Runs，保证单次执行可追踪、可复盘。
4. 再做质量评估、告警、prompt 版本、数据集沉淀和控制操作。

## 外部产品调研

### LangSmith

[LangSmith](https://www.langchain.com/langsmith-platform) 把 agent 工程拆成 observability、evaluation、deployment、fleet。它强调 “Traces - not code - provide the only record of what your agent did and why”，并把完整对话、agent run、工具调用、在线评测、人工标注队列、失败模式洞察串成闭环。

可借鉴点：

- Trace 是 agent 改进的事实源，不只是日志。
- 线上 trace 要能进入人工标注队列和离线 eval dataset。
- 面板要追踪成本、延迟、错误和质量指标，并支持按失败模式聚类。
- Agent-native 概念应是一等对象，例如 conversation thread、tool、sub-agent、memory。

### Langfuse

[Langfuse](https://langfuse.com/docs) 是开源、可自托管的 AI engineering platform，观测能力覆盖 LLM 和非 LLM 调用、retrieval、embedding、API call、多轮 session、user tracking、agent graph、timeline、dashboard，并且基于 OpenTelemetry 以降低 vendor lock-in。

可借鉴点：

- Trace、session、user、timeline、agent graph 要同时存在。
- Prompt 需要版本化，并且和 trace、指标、评测结果关联。
- 评测方法应覆盖 LLM-as-a-judge、代码 evaluator、用户反馈、人工标注。
- API-first 和导出能力很重要，dashboard 不能成为数据孤岛。

### Arize Phoenix

[Phoenix](https://arize.com/docs/phoenix/llms.txt) 是开源 AI observability 平台，基于 OpenTelemetry / OpenInference，覆盖 tracing、evaluation、prompt engineering、experimentation。它将 LLM calls、tool execution、retrieval 建模为 spans，并支持 sessions、annotations、feedback、cost tracking、masking、sampling、datasets、experiments、CI eval。

可借鉴点：

- OpenTelemetry / OpenInference 是未来兼容层。
- Trace 上要能附加 annotation、score、feedback，而不是另建孤立表。
- 对 memory/search/RAG 这类检索动作，应记录 retrieved documents、scores、query、top_k、注入结果。
- 需要支持内容脱敏、采样、保留策略，不能默认永久保存所有 prompt/result。

### W&B Weave

[Weave](https://docs.wandb.ai/weave/concepts/what-is-weave) 强调 agentic tracing、application tracing、systematic evaluation、version tracking、feedback collection、production monitoring。它把 sessions、LLM calls、tool executions、prompt/model/data 版本和线上 scorer 连接起来。

可借鉴点：

- 每次执行必须知道它使用了哪个 prompt、模型、工具定义和数据版本。
- 质量监控不能只靠人工看 trace，需要自动 scorer 和趋势。
- 线上反馈要能反哺离线评测集，形成数据飞轮。

### Helicone

[Helicone](https://docs.helicone.ai/getting-started/platform-overview) 更偏 AI Gateway + observability，核心是统一 LLM 入口、自动记录成本/延迟/错误、provider fallback、成本归因和 prompt 管理。

可借鉴点：

- LLM 调用层要天然记录 provider、model、token、cost、latency、error。
- 成本面板应能按 user、channel、feature、model、tool path 归因。
- 多 provider 或 OpenAI-compatible endpoint 需要统一 gateway 观测视角。
- 代理式观测容易接入，但看不到应用内部工具和记忆链路，所以不能作为唯一方案。

### OpenTelemetry GenAI

[OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) 正在独立维护 GenAI spans、metrics、events，覆盖 GenAI clients、MCP 和 provider-specific conventions。

可借鉴点：

- 我们的数据模型应保留 `trace_id`、`span_id`、`parent_span_id`、`span_kind`、attributes、events、status。
- LLM span 建议使用 `gen_ai.*` 风格字段，例如 model、provider、input_tokens、output_tokens、finish_reason。
- Tool、agent invoke、MCP、scheduler 这类非 LLM 操作也应该是 span，而不只是日志行。

## 当前 `my-agent` 可观测性现状

已有基础：

- `observability/tracing.py` 提供 `Trace` 和 `JsonlTraceStore`。
- `RunContext` 持有 `Trace`，贯穿一次 `Agent.run()`。
- `Agent.run()` 已记录 `user_message`、`llm_call`、`tool_call`、`final_answer`、`max_steps_reached`。
- `ContextManager` 已记录 `context_built`、`context_trimmed`、`memory_flush_dispatched`、`memory_flush_completed`。
- `MemoryManager` 有 raw/semantic/episodic/index/SOP 等实体，适合和 trace 建关联。
- `SchedulerService`、`SchedulerExecutor`、`ChannelManager` 已有清晰的后台任务和主动推送链路。

关键缺口：

- 当前 `JsonlTraceStore` 存在，但运行时没有在 `ChatChannel._handle()` 或 `ChannelManager` 中统一 append，trace 大多只留在 `last_trace`。
- `Trace.events` 是扁平数组，没有 `span_id`、`parent_span_id`、开始/结束时间、状态、异常栈，难以画执行树。
- `llm_call` 只记录 duration，没有 model/provider、request size、token、cost、finish_reason、HTTP error。
- `tool_call` 只记录 name/status/duration，没有参数摘要、结果摘要、工具错误类型、workspace 拒绝、危险命令审计。
- scheduler 执行、channel 收发、Feishu reconnect、主动推送失败没有进入统一 trace。
- 记忆检索、注入、flush、deep_dream、SOP distillation 只有局部事件，没有统一 memory span 和 record id。
- 没有 prompt 版本、tools schema 版本、settings snapshot，因此很难解释“同样输入为何今天行为不同”。
- 没有 redaction、retention、sampling，直接保存完整 prompt/tool result 有安全风险。

## 设计原则

1. Trace 是事实源

   每次用户输入、调度任务、后台记忆任务都生成 trace。面板所有 drill-down 都从 trace 出发。

2. Span 是最小调试单元

   LLM 调用、工具调用、记忆检索、上下文组装、调度执行、channel 发送都必须是 span，支持树状关系和 timeline。

3. 数据先于 UI

   没有结构化字段的 UI 只是静态报表。先定义数据合同，再实现页面。

4. 本地优先，开放导出

   默认使用本地 SQLite/JSONL，便于学习和离线调试；同时预留 OTLP / JSONL export。

5. 可调试也要可治理

   控制面板允许 pause scheduler、retry task、标注 trace、加入 dataset，但涉及写文件、执行工具、删除数据的操作必须有权限和审计。

6. 默认保护敏感信息

   面板默认展示摘要，完整 prompt、tool args、tool result 需要显式展开，并经过 redaction。

## 观测数据模型

### 核心实体

| 实体 | 含义 | 关键字段 |
| --- | --- | --- |
| `trace` | 一次完整执行 | `trace_id`, `request_id`, `session_id`, `entrypoint`, `channel_type`, `user_id_hash`, `status`, `started_at`, `ended_at`, `total_ms`, `root_span_id` |
| `span` | 一段可定位操作 | `span_id`, `trace_id`, `parent_span_id`, `name`, `span_kind`, `status`, `started_at`, `ended_at`, `duration_ms`, `attributes` |
| `event` | span 内细粒度事件 | `event_id`, `span_id`, `name`, `timestamp`, `attributes` |
| `session` | 多轮对话或自动任务会话 | `session_id`, `channel_type`, `user_id_hash`, `first_seen_at`, `last_seen_at`, `message_count`, `trace_count` |
| `feedback` | 用户或人工审查反馈 | `trace_id`, `span_id`, `score`, `label`, `comment`, `created_by`, `created_at` |
| `eval_score` | 自动评测结果 | `trace_id`, `span_id`, `evaluator`, `score`, `reason`, `version`, `created_at` |
| `prompt_version` | prompt 和工具 schema 版本 | `prompt_id`, `version`, `content_hash`, `tools_hash`, `created_at`, `source_path` |
| `scheduler_execution` | 定时任务执行历史 | `task_id`, `trace_id`, `scheduled_at`, `started_at`, `status`, `attempt`, `last_error` |

### Span kinds

| `span_kind` | 用途 | `my-agent` 埋点位置 |
| --- | --- | --- |
| `agent.invoke` | 一次 Agent run | `Agent.run()` |
| `channel.receive` | 收到 CLI/Feishu 消息 | `ChatChannel.produce()` 或 channel adapter |
| `channel.send` | 发送回复 | `Channel.send()` 实现 |
| `context.build` | 构建模型上下文 | `ContextManager.build_model_messages()` |
| `llm.chat` | 调用 OpenAI-compatible API | `OpenAICompatibleClient.chat()` |
| `tool.execute` | 执行工具 | `ToolRegistry.run()` / `BaseTool.run()` |
| `memory.search` | 检索 episodic memory | `MemoryManager.search()` |
| `memory.inject` | 注入 semantic/episodic memory | `MemoryManager.build_prompt()` |
| `memory.flush` | 上下文裁剪写入 episodic memory | `MemoryManager.flush_from_messages()` |
| `memory.dream` | deep_dream 长期记忆整理 | `MemoryManager.deep_dream()` |
| `scheduler.scan` | 扫描到期任务 | `SchedulerService.check_and_execute()` |
| `scheduler.execute` | 执行某个任务 | `SchedulerExecutor.execute()` |
| `skill.catalog.load` | 扫描并构建可用 skill 目录 prompt | `SkillLoader.build_prompt()` |
| `skill.catalog.inject` | 将 skill 目录注入 system prompt | `ContextManager._build_system_prompt()` |
| `skill.activate` | 真正读取并启用某个 skill 的详细说明 | `ReadFileTool.run()` 读取 `skills/<skill_name>/SKILL.md` 时记录 |

### LLM span 必备字段

```json
{
  "span_kind": "llm.chat",
  "name": "llm.chat openai-compatible",
  "attributes": {
    "gen_ai.provider.name": "openai-compatible",
    "gen_ai.request.model": "gpt-4o-mini",
    "gen_ai.usage.input_tokens": 1834,
    "gen_ai.usage.output_tokens": 392,
    "gen_ai.response.finish_reason": "tool_calls",
    "my_agent.prompt.identity_hash": "sha256:...",
    "my_agent.prompt.tools_hash": "sha256:...",
    "my_agent.context.message_count": 12,
    "my_agent.context.memory_count": 3,
    "my_agent.cost.usd": 0.0042
  }
}
```

说明：

- 如果 provider 不返回 token usage，先记录 request/response 字符数和估算 token，并标记 `token_source=estimated`。
- prompt 全文不默认入库，存储 redacted content、hash 和可选 raw pointer。

### Tool span 必备字段

```json
{
  "span_kind": "tool.execute",
  "name": "tool.execute read_file",
  "attributes": {
    "tool.name": "read_file",
    "tool.call_id": "call_xxx",
    "tool.args_redacted": {"path": "a.txt"},
    "tool.args_hash": "sha256:...",
    "tool.result_chars": 1208,
    "tool.result_preview": "前 300 字摘要...",
    "tool.ok": true,
    "security.workspace_escape_denied": false
  }
}
```

对 `bash`、`write_file`、`edit_file`、`scheduler` 这类有副作用工具，额外记录：

- command 或 patch 的 redacted 摘要。
- workspace 路径解析结果。
- 是否需要人工确认。
- 是否触发安全边界。

### Memory span 必备字段

```json
{
  "span_kind": "memory.search",
  "name": "memory.search episodic",
  "attributes": {
    "query_hash": "sha256:...",
    "query_preview": "用户问题摘要...",
    "top_k": 5,
    "result_count": 3,
    "results": [
      {
        "record_id": "daily/2026-07-13.md:12-18",
        "score": 0.42,
        "source_type": "episodic"
      }
    ]
  }
}
```

这能支撑两个关键问题：

- 模型为什么知道这件事？
- 模型为什么没想起那件事？

## 控制面板信息架构

### 1. Overview 指挥台

用于 30 秒内判断系统是否健康。

核心卡片：

- 当前活跃 runs、队列长度、平均等待时间。
- 成功率、错误率、max_steps 命中率。
- P50/P95/P99 总耗时、LLM 耗时、tool 耗时。
- 今日 token、成本、按 model/provider 分布。
- scheduler 到期任务数、延迟任务数、失败重试数。
- memory flush backlog、deep_dream/SOP 最近执行状态。
- Feishu/CLI channel 在线状态、重连次数、发送失败数。

关键图表：

- Latency 趋势，按 trace complexity 分层：无工具、1-2 个工具、3+ 工具、scheduler。
- Cost 趋势，按 channel/user/model 聚合。
- Error heatmap，按 span_kind 和 tool_name 聚合。
- Quality trend，按 evaluator score、用户反馈、人工标注聚合。

### 2. Live Runs

用于观察正在执行的任务。

表格列：

- `trace_id`
- `started_at`
- `entrypoint`
- `session_id`
- `user/channel`
- 当前 step
- 当前 span
- elapsed
- tool_count
- token_so_far
- status

动作：

- 打开 trace。
- 标记关注。
- 对长时间运行的 trace 加 note。
- 对 scheduler task 做 pause/retry，但需要审计。

### 3. Trace Explorer

这是整个面板最重要的页面。

布局：

- 左侧：span tree，按 agent -> context -> llm -> tool -> llm -> final answer 展开。
- 中间：timeline waterfall，显示每个 span 的相对耗时、重试、异常。
- 右侧：选中 span 详情，包括 input/output 摘要、attributes、events、errors、raw JSON。
- 底部：final answer、user feedback、eval scores、notes。

必须支持：

- 按 `trace_id`、`session_id`、`tool_name`、`error_type`、`span_kind`、`prompt_hash`、`task_id` 搜索。
- Diff 两条 trace：比较 prompt、tool schema、memory 注入、工具序列、耗时、答案。
- 一键生成 “debug bundle”：trace JSON + redacted prompts + tool summaries + settings snapshot。
- 一键加入 regression dataset。

### 4. Sessions & Users

用于跨多轮对话追踪体验，而不是只看单次 request。

能力：

- session 时间线：用户消息、agent 回复、工具调用、调度任务回调。
- 用户维度成本、错误、满意度、最近失败 trace。
- 群聊和单聊分离，避免把 Feishu group context 和个人 context 混淆。
- session-level feedback，例如“本轮对话是否解决问题”。

隐私：

- 默认展示 `user_id_hash`，原始 user id 仅在本地开发或高权限下可见。

### 5. Tools & Safety

用于判断工具是否可靠、是否危险。

工具矩阵：

- 调用次数。
- success/error rate。
- P95 duration。
- 平均 result chars。
- workspace escape denied 次数。
- 最常见错误。
- 关联的 prompt/tool schema 版本。

重点工具：

- `bash`：命令分类、超时、退出码、输出长度、被拒绝命令。
- `read_file/write_file/edit_file`：路径、文件类型、diff 摘要、越权拒绝。
- `web_search`：provider、query 摘要、超时、结果数、失败率。
- `scheduler`：创建/更新/删除任务审计。

### 6. Memory Observatory

用于监控记忆系统是否有效。

页面分区：

- Memory injection：每次 trace 注入了多少 semantic/episodic memory。
- Retrieval quality：query、top_k、score 分布、空结果率。
- Raw conversation volume：按 session/channel/day。
- Episodic flush：异步 flush 成功率、跳过原因、写入条数。
- Deep dream / SOP：最近执行、输出变更、失败原因。
- Memory drift：长期语义记忆增长、重复项、冲突项、过期项。

调试入口：

- 从某条答案跳转到 “它注入了哪些记忆”。
- 从某条 memory record 反查 “哪些 trace 用过它”。
- 标记 memory 为 stale、wrong、sensitive，进入治理队列。

### 7. Scheduler Operations

用于把后台任务当成生产任务运维。

核心视图：

- Task list：enabled、next_run_at、last_run_at、last_error、action type、target。
- Execution history：每次扫描和执行都对应 trace。
- Late tasks：超过 catchup window 的任务。
- Retry queue：发送失败或 channel 不可用导致的重试。
- System tasks：如 `system_daily_dream`。

动作：

- pause/resume task。
- retry last failed execution。
- inspect generated context。
- 查看主动发送结果。

所有动作都必须写入 audit log。

### 8. Quality & Evaluation

用于让观测数据变成改进闭环。

能力：

- 用户反馈：thumbs up/down、标签、评论。
- 人工标注队列：错答、慢、工具误用、记忆污染、拒答不当。
- Online eval：按采样规则对生产 trace 异步评分，不阻塞用户请求。
- Offline dataset：从 trace 创建回归用例。
- Experiment compare：比较 prompt/model/tool 改动前后的得分、成本、耗时。

建议内置 evaluator：

- `answer_helpfulness`
- `tool_selection_correctness`
- `tool_argument_validity`
- `memory_groundedness`
- `retrieval_relevance`
- `scheduler_delivery_success`
- `safety_boundary_compliance`

### 9. Prompt & Config Versions

用于解释行为变化。

记录对象：

- `prompts/identity.md` content hash。
- runtime prompt 模板版本。
- skills prompt hash。
- tools schema hash。
- model name、base url、temperature 等模型参数。
- settings snapshot，例如 `MAX_STEPS`、`MAX_HISTORY_MESSAGES`、`MEMORY_SEARCH_RESULTS`。

页面能力：

- 按 prompt hash 查看 traces。
- 对比两个 prompt 版本的指标。
- 找出某次改动后的错误率/成本变化。

### 10. Alerts & SLO

第一批告警规则：

- `trace.error_rate` 15 分钟内超过 5%。
- `trace.p95_total_ms` 超过 30 秒。
- `llm.cost.daily_usd` 超过预算。
- `agent.max_steps_reached` 连续增长。
- `tool.bash.timeout_rate` 超过阈值。
- `scheduler.late_task_count` 大于 0 且持续 5 分钟。
- `channel.feishu.reconnect_count` 异常增长。
- `memory.flush_error_rate` 大于 0。
- `workspace_escape_denied` 激增。

告警目标：

- 本地开发：控制台 + dashboard banner。
- 团队使用：Feishu/webhook。

## 推荐技术架构

### 采集层

新增 `observability/span.py`：

- `TraceContext`
- `Span`
- `SpanRecorder`
- context manager: `start_span(name, kind, **attrs)`
- redaction helpers
- error capture helpers

示例接口：

```python
with context.trace.start_span("llm.chat", kind="llm.chat", model=self.settings.model) as span:
    try:
        response = self._request(payload)
        span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        span.set_status("ok")
        return response
    except Exception as e:
        span.record_exception(e)
        span.set_status("error")
        raise
```

### 存储层

第一阶段建议 SQLite + JSONL 双写：

- SQLite 用于 dashboard 查询。
- JSONL 作为 append-only 审计和故障恢复。
- raw payload 可以按需单独落文件，并通过 hash/pointer 引用。

建议路径：

```text
data/observability/observability.db
data/observability/traces.jsonl
data/observability/raw/<trace_id>/<span_id>.json
```

核心表：

```text
traces(trace_id, session_id, request_id, entrypoint, channel_type, user_id_hash,
       status, started_at, ended_at, total_ms, root_span_id, tags, metadata_json)

spans(span_id, trace_id, parent_span_id, name, span_kind, status,
      started_at, ended_at, duration_ms, attributes_json, input_ref, output_ref)

events(event_id, trace_id, span_id, name, timestamp, attributes_json)

feedback(id, trace_id, span_id, score, label, comment, created_by, created_at)

eval_scores(id, trace_id, span_id, evaluator, version, score, label, reason, created_at)

scheduler_executions(id, task_id, trace_id, scheduled_at, started_at, ended_at,
                     status, attempt, error)

prompt_versions(prompt_id, version, content_hash, source_path, created_at, metadata_json)
```

### 查询/API 层

新增只读为主的本地 API：

```text
GET /api/overview
GET /api/traces?time_range=&status=&session_id=&tool_name=&q=
GET /api/traces/{trace_id}
GET /api/sessions/{session_id}
GET /api/tools
GET /api/memory
GET /api/scheduler/tasks
GET /api/scheduler/executions
POST /api/feedback
POST /api/datasets/from-trace
POST /api/scheduler/tasks/{task_id}/pause
POST /api/scheduler/tasks/{task_id}/retry
```

写操作需要：

- 本地 token 或开发模式白名单。
- audit log。
- 明确的 action result。

### 前端层

第一版不需要复杂框架，可以做一个本地 Web UI：

- Overview
- Live Runs
- Trace Explorer
- Tools
- Memory
- Scheduler
- Quality

如果后续要做桌面集成或独立 app，再考虑 React/Vite。

## 分阶段落地路线

### Phase 0: 修正观测底座

目标：所有执行都能可靠落盘。

任务：

- 在 `ChatChannel._handle()` 的成功和异常路径都 append trace。
- 为 scheduler 主动执行创建独立 trace，并 append。
- 给 `Trace` 增加 wall-clock `started_at`、`ended_at`、status、error。
- 增加 redaction 方法，默认隐藏 API key、token、secret、authorization、Feishu raw sensitive 字段。
- 保留兼容：旧 `events` 仍可输出，避免测试和 `/trace` 立即失效。

验收：

- CLI、Feishu、scheduler、memory_dream 都能在 `data/observability/traces.jsonl` 查到 trace。
- 异常也能落 trace，而不是只返回 `ERROR:`。

### Phase 1: Span tree

目标：单条 trace 可解释。

任务：

- 引入 `span_id`、`parent_span_id`。
- 包装 `Agent.run()`、`ContextManager`、`OpenAICompatibleClient.chat()`、`ToolRegistry.run()`。
- 工具参数和结果只存 redacted 摘要。
- LLM 响应记录 model、duration、finish_reason、token/cost 或估算值。
- `/trace` 支持 pretty tree 输出。

验收：

- 一次 `read_file -> summarize` 可以看到 agent -> context -> llm -> tool -> llm -> final 的树。
- max_steps、tool error、LLM error 都能定位到具体 span。

### Phase 2: 本地 Trace Explorer

目标：不用 grep JSONL 也能排查问题。

任务：

- SQLite 查询层。
- `/dashboard` 本地服务。
- Trace 列表、搜索、详情、timeline、span tree。
- Debug bundle 导出。

验收：

- 可以按 session、tool、status、时间范围过滤。
- 可以打开任意 trace 并看到每个 span 的输入输出摘要。

### Phase 3: 生产运维视图

目标：从单次 debug 走向系统健康监控。

任务：

- Overview KPI。
- Tools & Safety。
- Memory Observatory。
- Scheduler Operations。
- Cost/Latency/Error 趋势。
- 基础告警规则。

验收：

- 能回答“今天最慢的是模型还是工具”。
- 能回答“哪个工具错误率最高”。
- 能回答“哪些 scheduler 任务没兑现”。
- 能回答“哪些 memory record 影响了某条答案”。

### Phase 4: 质量闭环

目标：从观测走向改进。

任务：

- trace feedback。
- 人工标注队列。
- 从 trace 生成 eval dataset。
- 在线异步 scorer。
- prompt/model/config 版本对比。

验收：

- 用户反馈错答后，可以从 trace 一键加入回归集。
- prompt 改动后，可以跑数据集比较质量、成本、耗时。
- dashboard 能展示质量趋势，而不只是系统指标。

### Phase 5: 开放集成

目标：兼容外部生态。

任务：

- OTLP exporter。
- Phoenix/Langfuse JSONL export。
- 可选接入自托管 Phoenix 或 Langfuse。
- 采样和 retention 策略。

验收：

- 同一份 trace 可在本地面板和外部观测平台查看。
- 关闭外部平台不影响本地调试能力。

## 生产级最低要求

这个面板上线前至少满足：

- 可追踪：任意回复都能定位 trace、session、prompt hash、tool sequence、memory injection。
- 可复盘：异常 trace 包含错误类型、异常栈、失败 span、上下文摘要。
- 可归因：延迟和成本可拆到 model/tool/memory/channel/scheduler。
- 可治理：敏感内容默认脱敏，保留策略明确，操作有审计。
- 可演进：trace 可进入 dataset，feedback/eval 能推动 prompt 和工具迭代。
- 可集成：数据模型兼容 OpenTelemetry 思路，不依赖单一外部 vendor。

## 推荐第一批实现清单

按收益排序：

1. 将 `JsonlTraceStore` 接入当前运行链路，保证 trace 真正落盘。
2. 新增 `Span` 模型并兼容旧 `Trace.add()`。
3. 包装 `OpenAICompatibleClient.chat()`，采集 model、duration、finish_reason、usage、error。
4. 包装 `ToolRegistry.run()`，采集 tool args/result 摘要和安全边界结果。
5. 包装 scheduler execution，使每个后台任务都有 trace。
6. 将 trace 存储迁到 `data/observability/`，避免和临时 `logs/` 混淆。
7. 增加 `/trace <id>` 和 `/traces` CLI 命令，先解决本地可查。
8. 再实现 Web Trace Explorer。

## 不建议的路线

- 只做几个 KPI 卡片，不做 trace tree。这样无法解决真实 debug。
- 只接 Helicone 这类 gateway。它能看 LLM 请求，但看不到工具、记忆、调度、channel。
- 一开始就重前端。没有数据合同，页面会快速返工。
- 默认保存完整 prompt 和工具结果。对 Feishu、文件内容、命令输出都存在隐私和安全风险。
- 只做离线报表，不做 feedback/eval/dataset。没有改进闭环，面板价值会停留在“看见问题”。
