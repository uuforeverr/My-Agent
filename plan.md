# my-agent Responses API 与 OpenTelemetry 重建计划

> 状态：第一阶段实施完成并通过验收（2026-10-25）。
>
> 执行约束：Responses API 与 OpenTelemetry 两部分设计均已确认。实施期间按阶段修改和验证，最终运行路径不长期保留双写。

## 1. 建设目标

本次重建分为两个相关但相互独立的部分：

1. 将核心 LLM 客户端从 Chat Completions API 迁移到 OpenAI Responses API，并支持模型输出和飞书回复的流式处理。
2. 删除自研 Trace/Span 与 Phoenix 旁路导出，使用 OpenTelemetry 原生 API/SDK 观测一次 Agent Turn 的核心执行链路。

第一阶段聚焦单次 Agent Turn 的诊断，不建设大而全的基础设施监控平台。

## 2. 已确认的范围定义

### 2.1 Turn 与 Session

- 一个 `Turn`：用户发送一条消息，直到 Agent 给出最终答复；对应一条完整 OTel Trace。
- 一个 `Session`：同一飞书聊天中的多轮 Turn；第一阶段仅保留稳定的 `session.id` 作为可聚合字段。
- 每个 Turn 汇总总耗时、各阶段耗时、LLM 次数、工具次数和 Token 消耗。
- 暂不建设长期 Session 累计 Token/耗时 Dashboard，但数据模型必须支持后续聚合。

### 2.2 主要交互通道

- 主要通道是飞书。
- 飞书接受批量刷新，不按每个 Token 请求一次更新。
- CLI 直接消费允许展示的 Responses 类型化 delta 并即时输出；完成事件只负责换行和完整性兜底，不能重复打印最终答案。工具审批请求仍需即时展示。

## 3. Responses API 重建设计（已定稿）

### 3.1 客户端和状态管理

- 使用官方 OpenAI Python SDK，不自行使用 `urllib` 解析 SSE 字节流。
- 核心 LLM API 使用 Responses API。
- 使用 `stream=true` 获取类型化流式事件。
- 使用 `store=false`，不依赖 OpenAI 托管长期会话状态。
- 由 my-agent 保存并重放完整 Responses Items。
- 每次请求重新发送稳定的 `instructions`。
- `ContextManager` 继续负责 system prompt、历史记录、memory 注入、上下文压缩、Token 预算和工具定义。
- 上下文数据模型从 Chat Completions 的 `messages` 升级为 Responses API 的 input/output items。
- 手动重放时必须保留完整可重放 items，包括 message、reasoning、function call、function call output 等；不能只保留 assistant 文本。

### 3.2 内部流式事件模型

供应商原始事件不能直接交给 Channel。LLM Client 先归一化为内部事件，再由 Agent Runtime 和展示策略消费：

```text
ReasoningSummaryDelta  面向用户的推理摘要或进度信息
OutputTextDelta        最终回答文本增量
ToolCallDelta          工具名称或参数增量，只进入内部缓冲
ToolCallCompleted      完整工具调用候选
UsageCompleted         本次响应的 Token 使用量
ResponseCompleted      本次模型响应完成
StreamFailed           流读取或模型响应失败
```

禁止将原始 chunk 直接显示给用户或直接作为工具调用执行。

### 3.3 用户展示策略

- 展示简短的进度说明和最终回答。
- 不展示原始隐藏思维链。
- 不直接展示工具名称、工具参数、工具输出和参数增量。
- 工具运行时间较长时，可展示“正在读取文件”“正在搜索资料”等语义化状态。
- 工具失败时，向用户展示经过处理的自然语言说明；完整参数、输出和异常保留在 Trace 中。
- 飞书使用本地缓冲并批量刷新同一条消息或卡片。
- 批量刷新触发条件包括：达到时间间隔、累计一定字符、阶段完成或发生错误。

### 3.4 流式工具调用组装

- 按 `tool_call_id` 或 output index 分别维护工具调用缓冲区。
- 支持多个工具调用的增量交错到达，但第一阶段顺序执行完整工具调用。
- `ToolCallDelta` 永远不可执行。
- 只有完整 output item/response 已完成后，才将组装结果提升为 `ToolCallCompleted`。
- 工具执行前必须同时满足：
  - call ID 完整；
  - 工具名完整；
  - 参数流已完成；
  - 参数是合法 JSON；
  - 参数通过工具 JSON Schema 校验；
  - 工具已注册且当前上下文可用；
  - 权限策略允许，或所需审批已经完成。
- 流中断或参数不完整时禁止执行工具，也禁止猜测或修补半段参数后执行。

### 3.5 工具执行方式

- 多个工具调用采用顺序执行，与当前 Agent 行为保持一致。
- 暂不实现并行工具执行，避免引入并发副作用、输出排序和审批竞态。
- 每个完整工具调用形成独立执行尝试和独立观测 Span。

### 3.6 结构化工具结果

所有工具结果统一为可序列化结构，再通过 `function_call_output` 和原始 `call_id` 回传模型。

成功示例：

```json
{
  "ok": true,
  "tool": "read_file",
  "call_id": "call_123",
  "data": {
    "content": "..."
  }
}
```

失败示例：

```json
{
  "ok": false,
  "tool": "read_file",
  "call_id": "call_123",
  "error": {
    "code": "invalid_arguments",
    "type": "validation_error",
    "message": "Required property 'path' is missing",
    "retryable": true,
    "details": {
      "missing_fields": ["path"]
    },
    "suggested_action": "Generate a corrected tool call"
  }
}
```

### 3.7 错误分类与恢复

#### 调用生成或参数校验错误

- 包括非法 JSON、缺少参数、类型错误、未知参数和 schema 不匹配。
- 若已形成完整 function call，则返回结构化、`retryable=true` 的 `function_call_output`，引导模型修正调用。
- 若模型流在 function call 完成前中断，则不构造工具结果，重试整个模型请求。

#### 可纠正执行错误

- 文件不存在、网络超时、搜索无结果默认都允许模型自我修正。
- 文件不存在：模型可修改路径、先列目录或向用户确认。
- 搜索无结果：模型可改写查询或切换工具。
- 网络超时：工具运行时先用原参数进行有限的短退避重试；仍失败后再返回结构化错误给模型。

#### 权限和策略错误

- `permission_denied`、`approval_denied`、`approval_timeout`、`policy_denied` 标记为 `retryable=false`。
- 模型不得以相同方式自动重试，应向用户解释未执行原因。
- 如果审批通道可用且请求仍在等待审批，Agent 状态机暂停，不将其作为普通失败立即回传模型。
- 获得审批后恢复原调用；拒绝或超时后才生成结构化工具结果。

#### 重试边界

- 同一个工具调用最多允许模型自动纠正两次。
- 网络瞬时错误由工具运行时先进行最多两次有限重试。
- 保留整个 Agent 的 `max_steps` 上限。
- 使用 `tool_name + normalized_arguments` 生成调用指纹。
- 相同调用指纹和相同错误码重复出现时提前终止纠错。
- 超限后返回 `retry_limit_exceeded`、`retryable=false`，要求模型向用户解释。

### 3.8 单次 Turn 的 Responses 循环

```text
构建 input items
    ↓
responses.create(stream=true, store=false)
    ↓
归一化 typed events
    ↓
保存完整 response.output items
    ↓
若只有最终文本：完成 Turn
    ↓
若存在 function call：组装、验证、顺序执行
    ↓
追加 function_call_output
    ↓
再次调用 Responses API
```

### 3.9 Responses API 迁移不包含的内容

- 第一阶段不实现并行工具执行。
- 不将工具参数增量展示给用户。
- 不将供应商事件类型泄露到 Agent 和 Channel 公共接口。
- 不使用 OpenAI Conversations API 或 `previous_response_id` 托管长期上下文。
- 不在 Responses API 迁移阶段同时建设 Metrics、Logs、Dashboard 或告警。

## 4. OpenTelemetry 观测平台重建（已定稿）

### 4.1 第一阶段范围

- 只建设 OpenTelemetry Traces，不建设 OTel Metrics 和 OTel Logs。
- 只覆盖飞书交互式 Turn；scheduler、跨长时间审批恢复和主动投递的完整 Trace 留到后续阶段。
- 保留普通 Python 日志，但不导出到 Loki 或 OTel Logs。
- 不部署 Grafana、Prometheus、Loki、业务 Dashboard、CPU/RSS 监控和长期趋势告警。
- Jaeger 是第一阶段唯一 Trace 查询界面。

### 4.2 组件与数据流

```text
OTel API 埋点
    ↓
进程级 TracerProvider
    ↓
BatchSpanProcessor（进程内有界内存队列）
    ↓ OTLP/HTTP
OpenTelemetry Collector
    ↓ OTLP
Jaeger
    ↓
Badger 持久化卷
```

- `TracerProvider` 只在进程入口初始化一次，不能在每个 Agent 或 Turn 中重复初始化。
- Collector 仅启用 traces pipeline：OTLP receiver、memory limiter、batch processor、OTLP exporter。
- 不引入 Kafka、RabbitMQ 或 Collector 磁盘队列。
- 遥测链路必须 fail-open：Collector 或 Jaeger 故障不得阻塞、拖慢或弄失败 Agent Turn。
- Agent SDK 和 Collector 均使用有界内存队列；队列满时允许丢 Trace。
- 正常退出时只进行有超时上限的短暂 flush，超时后退出。

### 4.3 Trace 边界与父子关系

一个飞书用户消息形成一条 Trace。根 Span 从平台消息创建或本机接收时刻开始，在最终回复刷新成功或失败后结束：

```text
turn my-agent
├─ receive_request <channel>
├─ invoke_agent
│  ├─ agent.step 1
│  │  ├─ assemble_context
│  │  │  └─ retrieval memory
│  │  ├─ chat <model>
│  │  └─ execute_tool <tool_name>
│  └─ agent.step 2
│     ├─ assemble_context
│     └─ chat <model>
└─ send_response <channel>
```

- `my_agent.turn` 在 Turn Worker 中创建，优先使用可信的飞书消息创建时间，否则使用本机接收时间作为显式 `start_time`；最终投递完成后结束。
- `receive_request` 使用显式时间 Event 记录平台创建、本机接收、入队、Worker 启动和 Agent 分发。
- `invoke_agent` 覆盖一次 Agent 循环。
- 每次推理循环由 `agent.step N` 分组；该 Step 的模型请求与其触发的工具调用是兄弟节点。
- `assemble_context` 覆盖 system instructions、历史、memory 和 Token 预算组装。
- memory 第一阶段保留一个 `retrieval memory` Span，不按存储来源继续拆子 Span；记录各来源耗时、数量、ID、分数、完整正文以及是否被注入或截断。
- 每次模型请求和每次工具执行分别形成独立 Span，并按实际时间线排列。
- `send_response` 是根 Span 的直接子 Span，从首个可展示 Chunk 进入缓冲区开始；每次批量刷新记录为 Event，并在最终投递成功或失败后结束。
- 同线程使用 `start_as_current_span` 隐式继承父级；跨 `ThreadPoolExecutor` 必须显式 capture、attach、detach OTel Context。
- 所有 Context 均用上下文管理器或 `try/finally` 清理，不能泄漏到下一个 Turn。

### 4.4 关联与可检索字段

根 Span 至少记录：

- `my_agent.turn.id`
- `my_agent.session.id`
- `messaging.message.id`
- `messaging.conversation.id`
- `my_agent.user.id_hash`
- `gen_ai.request.model`
- `my_agent.run.outcome`
- `my_agent.run.recovered_error_count`

OTel 自动生成 `trace_id`。飞书 message ID、conversation ID 保留原值便于反查，用户 ID 以稳定哈希记录。Trace ID 不展示给飞书用户。

### 4.5 模型流式时延与 Token

每次 `chat <model>` Span 至少记录：

- `time_to_first_event_ms`：请求发出到第一个有效 Responses 流事件；
- `time_to_first_output_text_ms`：请求发出到第一段可展示文本；
- `stream_duration_ms`：第一个事件到响应完整结束；
- 输入、输出及推理 Token；
- request model、response model、Response ID 和结束状态；
- 完整模型输入、输出 items、重试次数、工具调用数量和异常。

不为每个流式 chunk 创建 Span 或 Event。保存拼接后的完整内容以及首事件、首文本、流结束三个时间点；断流时额外记录最后成功事件类型、事件序号和异常。

### 4.6 工具 Span 与恢复语义

每个 `execute_tool <tool_name>` Span 记录：

- call ID、调用顺序、工具名；
- 完整参数和参数校验结果；
- 开始、结束和耗时；
- 完整结构化结果；
- 错误类型、错误码、异常堆栈；
- 是否可由模型纠正、运行时重试次数和模型纠正次数；
- 命令工具的工作目录、退出码、stdout 和 stderr。

工具内部有限重试不继续创建子 Span，每次尝试使用带时间戳的 `tool.attempt` Event；模型修正参数后的新调用形成新的工具 Span。

- 子工具失败 Span 始终保持 ERROR。
- 如果模型成功恢复并给出有效最终答案，根 Turn 为成功，并增加 `my_agent.run.recovered_error_count`。
- OTel 技术状态与 `my_agent.run.outcome` 分离。
- 权限或策略拒绝可产生 `outcome=denied`，但根 Span 不必标为技术 ERROR。
- 未处理异常、LLM 重试耗尽、永久断流、`max_steps`、最终飞书发送失败或没有可交付回答时，根 Span 为 ERROR。

### 4.7 内容采集、检索与脱敏

第一阶段本机模式采集完整用户消息、system instructions、Responses 输入输出、memory 正文、工具参数结果、飞书最终回复和异常堆栈。

- 短小且用于检索、过滤的元数据保存为 Span Attributes；大段结构化正文保存为 Span Events。
- API Key、Authorization、Cookie、密码、access token、refresh token 等必须在进入 OTel 前统一脱敏。
- 对可疑敏感字段采取失败关闭策略：无法安全脱敏时不记录原文。
- 未触发工具 spill 时，Trace 保存完整原始结果和模型可见结果。
- 触发 spill 时，Trace 只保存模型可见预览、原始大小、Token 估算、哈希和 spill 相对路径；完整原始内容只留在 spill 文件中。
- 所有大内容设置可配置安全上限；超过上限记录大小、哈希和本地引用，不能因观测内容耗尽内存。

### 4.8 飞书批量流式刷新

OpenAI 流事件先归一化，再分别进入 Agent 状态机和只包含允许展示内容的 `StreamBuffer`。

- 使用独立 Feishu Flush Worker，避免飞书 I/O 阻塞 OpenAI 流读取。
- 约每 500～1000 ms、累计字符达到阈值、阶段完成或最终完成时批量刷新。
- 不显示工具名、工具参数、工具结果或底层错误。
- 每次刷新在 `send_response` 记录批次号、字符数、耗时和结果 Event。
- 完整飞书回复只在最终 Event 保存一次，避免每次刷新重复正文。
- 中间刷新失败后重试成功，Turn 可以成功。最终刷新成功或失败后才结束根 Turn；最终送达失败同时把 `send_response` 和根 Turn 标为 ERROR。

### 4.9 本地部署、保留和采样

- Jaeger 和独立 Collector 以两个本地容器运行。
- Collector OTLP 端口和 Jaeger UI 仅绑定 `127.0.0.1`。
- Jaeger 使用 Badger 二进制本地存储及 Docker 持久化卷，不使用自定义 SQLite/JSONL 作为在线 Trace 存储。
- Trace 保存 30 天并滚动清理。
- 采样策略为 `parentbased_always_on`，第一阶段采集 100% 的交互式 Turn。
- 旧 JSONL/SQLite 数据不在线迁移，仅保留为历史归档。

建议配置：

```text
MY_AGENT_OTEL_ENABLED=true
OTEL_SERVICE_NAME=my-agent
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
MY_AGENT_OTEL_CAPTURE_CONTENT=true
MY_AGENT_OTEL_CONTENT_MAX_BYTES=...
```

Resource 至少记录 `service.name`、`service.version`、Git commit、本机实例 ID 和 `deployment.environment=local`。

### 4.10 旧观测体系退出

新链路验证通过后，最终运行路径移除自研 `Trace/Span`、Phoenix 导出、自研 Trace SQLite/JSONL 在线写入、自研 Trace API 和 Dashboard，以及对应配置、CLI 入口和依赖。最终状态不长期双写，历史数据文件不在本次改造中删除。

## 5. 实施与验收计划

### 5.1 实施顺序

1. 引入官方 OpenAI SDK 和 Responses API 内部事件模型。
2. 实现完整工具调用组装、顺序执行和结构化错误。
3. 实现飞书 `StreamBuffer` 与独立 Flush Worker。
4. 初始化 OTel SDK 并按第 4 章埋设 Span。
5. 增加 Collector、Jaeger、Badger 本地部署配置。
6. 通过新链路测试后删除自研观测和 Phoenix 运行路径。
7. 更新启动说明、配置样例和架构文档。

### 5.2 验收场景

- 普通 Turn 能看到完整父子链路，并可区分排队、首事件、首文本、工具和飞书发送耗时。
- 多工具严格顺序执行。
- 工具失败后模型纠正：工具 Span 为 ERROR，根 Turn 成功。
- 权限拒绝：业务结果为 `denied`，不误判成系统崩溃。
- 工具调用尚未完整时断流：绝不执行残缺命令。
- Collector 和 Jaeger 停止后，Agent 仍能正常回复。
- Flush Worker Span 与原 Turn 使用同一个 Trace ID。
- 敏感字段不会出现在 Jaeger。
- Jaeger 重启后历史 Trace 仍存在，超过 30 天的数据可以滚动清理。
- 最终运行路径不再写入旧 Trace JSONL/SQLite，也不再发送 Phoenix。

### 5.3 实施门禁状态

- Responses API 设计：已确认。
- OpenTelemetry Traces 设计：已确认。
- 用户最终审阅与代码实施授权：已获得。

## 6. 第一阶段实施结果（2026-10-25）

### 6.1 已完成

- 核心 LLM 客户端已迁移到官方 OpenAI Python SDK 与 Responses API，使用类型化流事件；只有完整的 `function_call` 输出项才会进入工具执行。
- 多工具按顺序执行；参数校验、执行失败、权限拒绝和可恢复错误均以结构化 `function_call_output` 返回模型。
- 飞书已接入独立批量刷新 Worker；只投递允许用户看到的文本，工具参数和结果不会进入用户流。
- CLI 已接入直接流式输出：文本与 reasoning summary delta 即时打印，工具参数 delta 不展示，完成时避免重复最终答案。
- 自研 Span/Trace 与 Phoenix 在线运行路径已移除，交互式飞书 Turn 使用 OpenTelemetry 原生 Context 和 Traces。
- 本地 Collector、Jaeger 与 Badger 已配置完成，仅监听 `127.0.0.1`；Trace 保留期为 30 天，Docker volume 持久化。
- Collector 使用有界内存队列和异步批处理；观测后端短暂不可用时不会阻塞或弄失败 Agent Turn。
- README、环境变量样例、测试与部署说明已同步更新；旧 JSONL/SQLite 文件仅作为历史数据保留，不再在线写入。

### 6.2 验收结果

- 自动化测试：`395 passed, 4 skipped`。
- Docker Compose 配置可加载，Collector 与 Jaeger 均正常启动。
- 真实 OTLP 冒烟 Trace 已经 Collector 写入 Jaeger，Trace ID 为 `1bd9c6a5665e93f95666e87b7644c4ad`。
- 冒烟 Trace 的父子关系验证为 `my_agent.turn -> invoke_agent my-agent -> chat smoke-model`。
- 单独重启 Jaeger 后，同一 Trace 的 3 个 Span 仍可查询，确认 Badger volume 持久化有效。
- Jaeger 重启窗口内 Collector 仅产生瞬时连接告警，Agent 侧不依赖后端响应，符合 fail-open 设计。

### 6.3 按范围暂缓

- Scheduler 等非交互式入口的完整 Turn 观测。
- 独立 Metrics、Logs、业务 Dashboard、长期趋势与告警。
- 长期 Session 聚合展示；当前只保留可聚合的稳定字段。
- 使用真实 OpenAI 与飞书凭据的线上联调；代码路径已由自动化测试覆盖，但需要可用凭据和实际飞书应用环境完成最终业务冒烟。

## 7. 官方参考

- OpenAI Responses API：<https://developers.openai.com/api/reference/responses/overview>
- Responses streaming events：<https://developers.openai.com/api/reference/resources/responses/streaming-events>
- Function calling：<https://developers.openai.com/api/docs/guides/function-calling>
- Conversation state：<https://developers.openai.com/api/docs/guides/conversation-state>
- Migrate to Responses API：<https://developers.openai.com/api/docs/guides/migrate-to-responses>
