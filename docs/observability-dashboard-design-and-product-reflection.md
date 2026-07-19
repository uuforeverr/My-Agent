# my-agent 观测面板设计说明

本文只描述当前 `my-agent` 观测面板的定位、核心概念、架构、页面结构和使用方式。它不讨论 B 端/C 端产品化方向。

## 结论先行

当前观测面板的准确定位是：**本地 Trace Explorer，用于追踪和调试 Agent 对话链路**。

它暂时不需要扩展成完整生产运维平台。当前阶段最重要的是把单次对话执行链路看清楚，定位 LLM、tool、memory、skill、context、scheduler 和 channel 中的问题。

它主要回答这些问题：

- 这次对话从哪个入口进来？
- Agent 构建了哪些上下文？
- 注入了哪些 memory？
- skill 只是注入了目录，还是实际被激活读取？
- 调用了几次 LLM？
- LLM 是否选择了工具？
- 工具执行是否成功？
- 哪个环节最慢？
- 错误发生在哪个 span 里？
- 这次 trace 能不能导出成 debug bundle 复盘？

换句话说，它不是给最终用户看的报表，而是给开发者和系统维护者看的执行链路调试工具。

## 目录

- [一、核心概念](#一核心概念)
- [二、整体架构](#二整体架构)
- [三、数据存储](#三数据存储)
- [四、API 设计](#四api-设计)
- [五、页面结构](#五页面结构)
- [六、典型使用流程](#六典型使用流程)
- [七、当前设计边界](#七当前设计边界)

## 一、核心概念

### Trace

Trace 表示一次完整执行。

在 `my-agent` 里，一次用户消息、一次调度任务、一次 channel 触发的 Agent run，都应该有一个 trace。它是本次执行的事实源，包含：

- `trace_id`
- `request_id`
- `status`
- `total_ms`
- `events`
- `spans`
- `root_span_id`

面板中的 Trace List 展示的就是这些历史执行记录。

### Span

Span 表示 trace 中一段有明确开始、结束和耗时的操作区间。

例如：

- `agent.invoke`：一次 Agent 调用。
- `context.build`：构建模型上下文。
- `memory.inject`：检索并注入记忆。
- `skill.catalog.load`：扫描并构建 skill 目录 prompt。
- `skill.catalog.inject`：把 skill 目录注入 system prompt。
- `skill.activate`：真正读取某个 `skills/<skill_name>/SKILL.md`。
- `llm.chat`：调用模型。
- `tool.execute`：执行工具。
- `channel.send`：发送回复。

Span Tree 里的缩进表示父子调用关系。例如 `memory.inject` 和 `skill.catalog.inject` 是 `context.build` 的子操作，`skill.activate` 是 `tool.execute/read_file` 的子操作。

### Event

Event 是 trace 或 span 内的瞬时事件，不一定有持续时间。

例如：

- `user_message`
- `context_built`
- `llm_call`
- `tool_call`
- `final_answer`
- `max_steps_reached`

Event 适合记录“某个时刻发生了什么”，Span 适合记录“某个操作从开始到结束用了多久，是否成功”。

### Waterfall

Timeline 采用 waterfall 展示。每个 span 使用 `start_offset_ms` 表示相对 trace 起点的开始偏移，再结合 `duration_ms` 展示真正的执行时序。

这和单纯的 duration bar 不一样。duration bar 只能回答“谁耗时长”，waterfall 可以回答“谁在什么时候开始、是否并发、是否被前面的步骤阻塞”。

### Debug Bundle

Debug bundle 是一次 trace 的可导出调试包，包含：

- trace summary
- span tree
- error spans
- raw trace

它的作用是让一次问题复盘可以离开浏览器页面，沉淀成一个可保存、可分享、可回归分析的证据包。

## 二、整体架构

当前链路可以理解为四层：

```text
Agent 执行层
  -> RunContext / Trace / Span 埋点
  -> JSONL 审计日志 + SQLite 查询索引
  -> Trace API
  -> Dashboard Trace Explorer
```

### 1. Agent 执行层

核心调用路径是：

```text
Channel / CLI
  -> RunContext.create()
  -> Agent.run()
  -> ContextManager.build_model_messages()
  -> MemoryManager.build_prompt()
  -> SkillLoader.build_prompt()
  -> LLM.chat()
  -> ToolRegistry.run()
  -> BaseTool.run()
```

这条路径里发生的关键操作会进入 trace。

### 2. 埋点层

`observability/tracing.py` 提供核心数据结构：

- `Trace`
- `Span`
- `Trace.start_span()`
- `Trace.add()`
- redaction 脱敏逻辑

每个 span 都有：

- `span_id`
- `trace_id`
- `parent_span_id`
- `name`
- `span_kind`
- `start_offset_ms`
- `duration_ms`
- `status`
- `attributes`
- `events`
- `error`

### 3. API 层

`observability/api.py` 提供本地 HTTP API。当前 API 只服务于本地 Trace Explorer，不承担多租户平台能力。

### 4. Dashboard 层

`observability/dashboard.py` 内嵌前端页面，服务于 `/dashboard`。

当前没有引入复杂前端框架，原因是：

- 部署简单。
- 本地调试成本低。
- 不引入额外构建链路。
- 当前目标是追踪调试，而不是做复杂 SaaS 控制台。

## 三、数据存储

当前采用双存储：

```text
data/observability/traces.jsonl
data/observability/traces.db
```

JSONL 是 append-only 审计日志，优点是简单、可靠、容易排查。

SQLite 是查询索引，优点是适合按状态、session、entrypoint、时间范围和 trace id 查询。

这两个角色不同：

- JSONL 是事实存档。
- SQLite 是查询加速层。

因此面板提供 `Repair index`，用于把 JSONL 中存在但 SQLite 缺失的记录补进索引。

## 四、API 设计

当前核心接口是：

| 接口 | 用途 |
| --- | --- |
| `GET /dashboard` | 打开本地 Trace Explorer |
| `GET /api/health` | 健康检查 |
| `GET /api/auth/check` | 校验 token 是否可用 |
| `GET /api/overview` | 获取总览统计 |
| `GET /api/traces` | 查询 trace 列表，支持过滤和分页 |
| `GET /api/traces/<trace_id>` | 查询单条 trace 详情 |
| `GET /api/traces/<trace_id>/bundle` | 导出 debug bundle |
| `POST /api/index/repair` | 增量修复 SQLite 索引 |
| `POST /api/index/rebuild` | 从 JSONL 重建 SQLite 索引 |

API 支持本地 token：

```text
Authorization: Bearer <token>
X-Trace-Api-Token: <token>
```

## 五、页面结构

### 顶部区域

顶部主要处理连接状态和 token。

如果 API 配置了 token，用户需要在页面保存 token 后才能查询数据。`Check token` 会调用 `/api/auth/check`，明确告诉用户当前 token 是否有效。

### Overview

Overview 用来快速判断当前 trace 数据的基本情况。

它不是完整监控指标，目前更接近 trace 数据摘要，例如总数、成功失败分布、最近执行情况等。

### Filters

Trace List 支持按以下条件过滤：

- status
- session_id
- entrypoint
- start time
- end time

这让我们可以快速定位某一类对话，例如：

- 只看失败 trace。
- 只看 CLI 入口。
- 只看某个 session。
- 只看某个时间窗口内的执行。

### Trace List

Trace List 是历史执行入口。

每条记录应该帮助用户快速判断：

- trace 是否成功。
- 总耗时多久。
- 从哪个入口产生。
- 属于哪个 session。
- 创建时间是什么。

点击 trace 后进入详情。

### Span Tree

Span Tree 展示父子调用结构。

它回答的是：

```text
这次执行内部到底嵌套了哪些步骤？
```

一个典型对话可能长这样：

```text
channel.handle
  agent.invoke
    context.build
      memory.inject
      skill.catalog.load
      skill.catalog.inject
    llm.chat
    tool.execute read_file
      skill.activate
    context.build
      memory.inject
      skill.catalog.load
      skill.catalog.inject
    llm.chat
  channel.send
```

这里有一个重要现象：工具调用后再次出现 `context.build`、`memory.inject`、`skill.catalog.load`、`skill.catalog.inject` 是合理的。

原因是 LLM API 本身是无状态的。每次模型调用都需要重新组装 messages，把当前对话历史、工具结果、memory、skill catalog 一起传给模型。

这不代表 skill 每次都真正启用了。真正启用只看 `skill.activate`。

### Timeline / Waterfall

Timeline 展示每个 span 在整个 trace 中的开始偏移和耗时。

它适合排查：

- 是 LLM 慢，还是工具慢？
- memory 注入是否明显拖慢？
- 工具是否在 LLM 后才开始？
- 某个失败是否发生在后半段？
- 多个 span 是否存在嵌套或等待关系？

当前 waterfall 的设计已经足够支持单次对话调试。

### Selected Span

点击 span 后，可以查看该 span 的 JSON 详情。

重点关注：

- `span_kind`
- `status`
- `duration_ms`
- `start_offset_ms`
- `attributes`
- `error`

调试时，不要只看名称，要看 attributes。很多关键事实都在 attributes 里，例如：

- tool name
- argument keys
- result chars
- memory count
- prompt chars
- model
- token usage
- finish reason

### Raw Trace

Raw Trace 是完整 trace JSON。

它不是严格意义上的“用户友好时间线”，而是底层事实数据。适合在 UI 解释不够时做最后核对。

Raw Trace 的价值是：

- 证明面板没有隐藏关键字段。
- 方便复制给测试或回归系统。
- 支持后续 debug bundle 和自动分析。

## 六、典型使用流程

### 场景 1：排查一次回答为什么慢

1. 打开 `/dashboard`。
2. 在 Trace List 中选择最近一次慢请求。
3. 看 Timeline，找到最长的 span。
4. 如果是 `llm.chat` 慢，继续看 model、token usage、message_count。
5. 如果是 `tool.execute` 慢，查看 tool name、参数和结果长度。
6. 如果是 `context.build` 慢，查看 memory 和 skill catalog 相关 span。

### 场景 2：排查为什么模型调用了某个工具

1. 找到对应 trace。
2. 看 `llm.chat` 的 finish_reason 是否是 `tool_calls`。
3. 看后续 `tool.execute` 的 tool name。
4. 查看 tool span 的 arguments 摘要。
5. 结合 Raw Trace 看模型调用前的 messages 结构。

### 场景 3：排查 memory 是否影响了回答

1. 找到 `context.build`。
2. 展开其子 span `memory.inject`。
3. 查看 `memory_count`、`prompt_chars`、`query_chars`。
4. 如果 memory 没有注入，检查是否没有检索结果。
5. 如果 memory 注入过多，后续再考虑优化召回策略，而不是先做缓存。

### 场景 4：排查 skill 是否真正启用

不要只看 `skill.catalog.load` 和 `skill.catalog.inject`。

这两个 span 只表示：

- 系统扫描并构建了 skill 目录。
- 系统把 skill 目录说明注入到了 system prompt。

真正启用 skill 看：

```text
skill.activate
```

当前实现中，只有当模型通过 `read_file` 读取 `skills/<skill_name>/SKILL.md` 时，才会出现 `skill.activate`。

### 场景 5：导出问题现场

1. 选择异常 trace。
2. 点击 `Download bundle`。
3. 保存 bundle JSON。
4. 后续可以把 bundle 用于人工复盘、自动测试或回归样本沉淀。

## 七、当前设计边界

当前面板暂时够我们追踪和调试一些对话，但要清楚它的边界。

### 已经够用的部分

- 单次 trace 查询。
- trace detail。
- Span Tree。
- Waterfall Timeline。
- Raw Trace。
- Debug Bundle。
- Token check。
- SQLite index repair。
- 基础脱敏。
- skill catalog 和 skill activate 语义拆分。

### 暂时不做的部分

这些能力目前可以不急：

- skill / memory 缓存。
- 实时 streaming trace。
- 告警系统。
- 多用户权限。
- 团队协作标注。
- 大规模聚合报表。
- 成本中心。
- OpenTelemetry 导出。
- 复杂前端工程化。

原因是当前目标不是“运营一个大规模 Agent 平台”，而是先把单次对话链路看清楚。过早引入这些能力，会让项目复杂度超过当前收益。

### 后续真正需要时再补的能力

当 trace 数量明显增长，或者开始多人使用时，再补：

- Span Tree 折叠和搜索。
- 按 span kind 过滤。
- 错误优先视图。
- trace 标签和备注。
- 从 trace 一键生成回归测试样本。
- 用户反馈和自动评测分数。
- 更强权限和审计。
