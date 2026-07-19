# my-agent

一个用于学习 Agent 架构的最小实现。

第一版包含：

- CLI 入口
- OpenAI-compatible LLM 调用
- 最小 Agent Loop
- `RunContext` 运行上下文
- `ContextManager` 模型上下文组装与简单裁剪
- `prompts/identity.md` 文件化 system prompt
- `bash` / `glob` / `read_file` / `write_file` / `edit_file` 基础工具
- `remember_memory` / `memory_search` / `read_memory` 长期记忆工具
- `web_search` 联网搜索工具（百度千帆 AI Search，配置 `QIANFAN_API_KEY` 后自动注册）
- `scheduler` 定时任务工具 + 后台调度服务（提醒 / 周期任务，到点主动推送回对应 channel）
- `data/memory/MEMORY.md` 语义记忆
- `data/memory/daily/*.md` 情景记忆
- `data/memory/raw/*/*.jsonl` 原始对话
- `data/memory/index.db` 本地检索索引
- 上下文裁剪时自动生成 daily 情景记忆，检索时按 Top-K 注入相关记忆
- `skills/*/SKILL.md` 最小技能加载
- `request_id`、`trace_id`、`span tree`、`llm_call`、`tool_call`、`total_ms` 基础可观测性
- `observability/` 模块负责 trace 记录、JSONL 审计落盘、SQLite 查询索引与历史 trace 查询
- workspace 路径边界，文件工具不能越权访问工作区外文件

## 运行

```bash
cd /Users/bytedance/项目学习/my-agent
cp .env.example .env
```

编辑 `.env`，填入模型配置：

```text
OPENAI_API_KEY=...
OPENAI_API_BASE=https://api.openai.com/v1
MODEL=gpt-4o-mini
```

可选配置：

```text
MAX_HISTORY_MESSAGES=40
MEMORY_SEARCH_RESULTS=5
MEMORY_FLUSH_MESSAGES=12
BASH_TIMEOUT=20

# 联网搜索（留空则对模型隐藏 web_search 工具）
QIANFAN_API_KEY=
QIANFAN_API_BASE=https://qianfan.baidubce.com/v2

# 定时任务
SCHEDULER_ENABLED=true
SCHEDULER_SCAN_INTERVAL=30
SCHEDULER_CATCHUP_SECONDS=600
SCHEDULER_TASKS_PATH=./data/scheduler/tasks.json
```

> `cron` 类型的定时任务依赖可选包 `croniter`：`pip install croniter`。
> `once`（如 `+10m`）与 `interval`（固定秒数）不需要该依赖。

启动：

```bash
python3 main.py
```

示例输入：

```text
请读取 a.txt 并总结
```

查看最近一次请求链路：

```text
/trace
```

查看历史 trace 列表：

```text
/traces
/traces 20
/traces 20 status=error
/traces 20 status=error session_id=cli:local entrypoint=cli
/traces 20 before=<trace_id>
```

按 ID 查看历史 trace：

```text
/trace <trace_id>
```

直接查看 JSONL：

```bash
tail -n 5 data/observability/traces.jsonl
```

本地查询索引位于：

```text
data/observability/traces.db
```

启动只读 Trace API（默认仅建议绑定本机）：

```bash
python3 -m observability.api --host 127.0.0.1 --port 8765
```

打开本地 Trace Explorer：

```text
http://127.0.0.1:8765/dashboard
```

如需给 API 加本地访问 token：

```bash
TRACE_API_TOKEN=dev-token python3 -m observability.api --host 127.0.0.1 --port 8765
# 或
python3 -m observability.api --host 127.0.0.1 --port 8765 --token dev-token
```

常用端点：

```text
GET http://127.0.0.1:8765/api/health
GET http://127.0.0.1:8765/api/auth/check
GET http://127.0.0.1:8765/api/overview
GET http://127.0.0.1:8765/api/traces?limit=20
GET http://127.0.0.1:8765/api/traces?limit=20&status=error
GET http://127.0.0.1:8765/api/traces?limit=20&before=<trace_id>
GET http://127.0.0.1:8765/api/traces?session_id=cli:local&entrypoint=cli
GET http://127.0.0.1:8765/api/traces?start=2026-07-15T10:00:00&end=2026-07-15T11:00:00
GET http://127.0.0.1:8765/api/traces/<trace_id>
GET http://127.0.0.1:8765/api/traces/<trace_id>/bundle
POST http://127.0.0.1:8765/api/index/repair
POST http://127.0.0.1:8765/api/index/rebuild
```

trace 记录包含 `schema_version`，序列化时会对 `api_key`、`token`、`secret`、`authorization`、`password`、`cookie` 等敏感字段做脱敏。配置 token 后，请求需携带：

```text
Authorization: Bearer <token>
# 或
X-Trace-Api-Token: <token>
```

Trace API 默认使用 SQLite 查询索引；启动时会从 JSONL 增量修复缺失索引。若查询详情时 SQLite miss，会回查 JSONL 并补回 SQLite。JSONL 仍保留为 append-only 审计记录。

## 定时任务

用自然语言让 agent 登记提醒，后台服务到点主动推送回你所在的 channel：

```text
30 秒后提醒我喝水
每天早上 9 点帮我总结日报
每小时搜索一次 AI 最新新闻
```

模型会调用 `scheduler` 工具把承诺写进 `data/scheduler/tasks.json`。任务分三种动作：

- `send_message`：到点发送固定文本（最轻，不经过 agent）。
- `agent_task`：到点让 agent 执行一个任务，把结果发回（隔离 session，禁止再创建定时任务）。
- `tool_call`：到点直接调用某个工具（如 `web_search`），把结果发回。

调度方式支持 `once`（`+10m` / `+1h` / ISO 时间）、`interval`（固定秒数）、`cron`（如 `0 9 * * *`，需 `croniter`）。

四件东西协作：

```text
scheduler 工具   登记 / 查询 / 管理承诺
TaskStore        持久化到 data/scheduler/tasks.json
SchedulerService 后台线程守时（默认每 30s 扫描，10 分钟补偿窗口）
ChannelManager   到点通过 send_to() 把结果兑现给用户
```

## 当前架构

```text
main.py
  -> load_settings()
  -> ChannelManager.run()
  -> ChannelManager.start_scheduler()   # 后台 SchedulerService 守时
  -> RunContext.create()
  -> Agent.run()
  -> MemoryManager.record_message()
  -> ContextManager.build_model_messages()
  -> MemoryManager.build_prompt()
  -> SkillLoader.build_prompt()
  -> OpenAICompatibleClient.chat()
  -> ToolRegistry.run(name, arguments, context)
  -> BaseTool.run()
  -> JsonlTraceStore.append()
```

到点推送链路：

```text
SchedulerService 扫描到点
  -> SchedulerExecutor.execute(task)
  -> (send_message / agent_task / tool_call)
  -> ChannelManager.send_to(target, reply)
  -> Channel.send(reply, synthetic IncomingMessage)
```

## 下一步

推荐增量：

1. 增加 `agent/session_store.py`，持久化最近 N 轮上下文。
2. 定时任务结果的记忆策略（AI 任务结果选择性写回情景记忆）。
3. 给受限 `bash` 增加更严格的命令审计和用户确认机制。

详细路线见：

```text
docs/minimal-implementation-roadmap.md
```
