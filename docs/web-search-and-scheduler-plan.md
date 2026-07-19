# Web Search 与定时任务设计方案（对标 CowAgent）

> 目标：为 my-agent 增加联网搜索工具与完整定时任务能力。
> 本文档重点服务于“后续给别人讲述设计”，所以不仅写实现点，也解释为什么这样分层。
> 参考对象：CowAgent 的 `web_search` 工具与 `scheduler` 工具体系。

---

## 1. 总体结论

### 1.1 Web Search

Web Search 应该做成一个普通工具：模型需要实时信息时调用 `web_search`，工具返回结构化搜索结果。

第一版供应商选择：**百度千帆 / 百度 AI 搜索**。

选择理由：

- 国内可用，不依赖代理。
- CowAgent 已经支持 `qianfan` backend，接口形态可参考。
- 公开报道显示，百度智能云推出的“百度搜索”和“智能搜索生成”均提供**每日 100 次免费调用额度**，适合个人 agent 原型与轻量日常使用。资料来源：[腾讯新闻：百度 AI 搜索上线千帆平台](https://news.qq.com/rain/a/20250613A088UB00)。
- Google CSE 也有每天 100 次免费额度，但国内网络稳定性与接入成本不如百度，资料来源：[51CTO：Google CSE 每天前 100 次免费](https://www.51cto.com/article/837158.html)。

因此：**my-agent 第一版接百度千帆 AI Search，保留 provider 扩展点，后续可加 Bocha / Google / Tavily。**

### 1.2 定时任务

定时任务应该“以工具形式出现”，但不能“只有工具”。

更准确地说：

```text
SchedulerTool       让模型创建/查询/管理任务
TaskStore           把任务持久化到磁盘
SchedulerService    后台线程，定期扫描任务是否到点
SchedulerExecutor   到点后执行任务：发固定消息 / 运行 agent / 调工具
ChannelTarget       表示主动发送目标，因为到点时没有原始 IncomingMessage
```

一句话：

> scheduler 工具是“登记任务的遥控器”，后台 SchedulerService 才是真正的闹钟。

---

## 2. CowAgent 调研结论

### 2.1 Web Search 在 CowAgent 中如何设计

CowAgent 的 `agent/tools/web_search/web_search.py` 是一个普通工具：

- `name = "web_search"`
- 参数包括：
  - `query`：搜索词
  - `count`：返回条数
  - `freshness`：时间范围，如 `oneDay / oneWeek / oneMonth`
  - `summary`：是否带摘要
  - `provider`：多 provider 时才暴露

CowAgent 支持多个供应商：

```python
PROVIDER_ORDER = ("bocha", "qianfan", "zhipu", "linkai")
```

其设计重点：

1. **provider 自动选择**：优先使用配置了 API key 的供应商。
2. **条件注册**：没有任何搜索 API key 时，不应该把 `web_search` 暴露给模型。
3. **统一返回格式**：无论底层是 Bocha / Qianfan / Zhipu，统一返回 title/url/snippet/date 等字段。
4. **失败不崩溃**：超时、鉴权失败、连接失败都返回 ToolResult 失败结果，而不是让 agent 崩。

### 2.2 Scheduler 在 CowAgent 中如何设计

CowAgent 的 scheduler 不是一个单文件工具，而是完整子系统：

```text
agent/tools/scheduler/
├── scheduler_tool.py      # 模型可调用的工具
├── task_store.py          # tasks.json 持久化
├── scheduler_service.py   # 后台扫描线程
└── integration.py         # 与 AgentBridge / channel / 工具上下文集成
```

核心职责拆分：

| 模块 | 职责 |
|------|------|
| `SchedulerTool` | 创建、查询、删除、启用、禁用定时任务 |
| `TaskStore` | 读写 `tasks.json`，保存任务状态 |
| `SchedulerService` | 后台线程每隔一段时间扫描任务是否到点 |
| `integration.py` | 到点后执行任务并发回用户；把当前上下文注入 scheduler 工具 |

CowAgent 的 scheduler 支持三种调度：

- `once`：一次性，如 `+10m`、`+1h`、指定 ISO 时间
- `interval`：固定间隔，如每 3600 秒
- `cron`：cron 表达式，如每天 9 点

CowAgent 的任务 action 支持：

- `send_message`：到点发送固定消息
- `agent_task`：到点让 agent 完成一个任务
- `tool_call`：到点直接调用某个工具
- `skill_call`：到点执行某个 skill

关键护栏：

- **隔离 session**：`agent_task` 使用 `scheduler_<receiver>_<task_id>`，避免污染用户当前聊天上下文。
- **防递归**：调度任务执行时标记 `is_scheduled_task=True`，避免 agent 在执行定时任务时再次创建定时任务。
- **结果记忆注入可控**：AI 任务结果可以选择性写回用户会话，固定提醒默认不写入，避免浪费上下文。
- **通道就绪检查**：如果 channel 不可用，任务不应直接消耗掉，而应延后重试。

---

## 3. my-agent 当前现状与差距

### 3.1 工具系统现状

my-agent 当前工具注册在 `tools/registry.py`：

```text
BashTool
GlobTool
ReadFileTool
WriteFileTool
EditFileTool
RememberMemoryTool
SearchMemoryTool
ReadMemoryTool
```

目前没有：

- `web_search`
- `scheduler`
- 后台任务服务
- 主动 channel 发送目标

### 3.2 Channel 发送现状

当前 channel 的发送函数是：

```python
send(reply: Reply, message: IncomingMessage)
```

它的语义是：**回复某条刚收到的消息**。

普通聊天有 `IncomingMessage`，所以能知道回复给谁：

```text
用户发消息 → IncomingMessage → agent.run → send(reply, 原消息)
```

但定时任务到点时没有“刚收到的用户消息”，只有任务里保存的投递目标：

```text
到点了 → 给 feishu 的 ou_xxx / oc_xxx 发消息
```

所以需要新增主动发送目标：

```python
ChannelTarget(
    channel_type="feishu",
    user_id="ou_xxx",
    is_group=False,
    group_id=None,
)
```

并提供：

```python
send_to(target, reply)
```

或者第一版用 synthetic `IncomingMessage` 复用现有 `send()`。

---

## 4. Web Search 设计

### 4.1 第一版能力范围

第一版只做一个 provider：**百度千帆 AI Search**。

工具名：

```text
web_search
```

参数：

```json
{
  "query": "搜索词",
  "count": 5,
  "freshness": "noLimit | oneDay | oneWeek | oneMonth | oneYear"
}
```

返回：

```json
{
  "query": "...",
  "backend": "qianfan",
  "count": 5,
  "results": [
    {
      "title": "...",
      "url": "...",
      "snippet": "...",
      "siteName": "...",
      "datePublished": "..."
    }
  ]
}
```

### 4.2 配置

`.env`：

```bash
QIANFAN_API_KEY=
QIANFAN_API_BASE=https://qianfan.baidubce.com/v2
```

只有 `QIANFAN_API_KEY` 存在时才注册 `web_search` 工具。

### 4.3 为什么不先接 Bocha / Google / Tavily

- Bocha 免费额度公开资料更多是总体验次数，不确定是否“每日免费”。
- Google CSE 有每日 100 次免费，但国内网络环境不稳定。
- Tavily 有 1000 credits/月，不是日免费额度。
- 百度 AI 搜索有公开资料显示每日 100 次免费，且国内可用，最符合当前需求。

---

## 5. Scheduler 总体设计

### 5.1 组件图

```text
用户自然语言：每天9点提醒我写日报
        │
        ▼
模型调用 scheduler 工具
        │
        ▼
SchedulerTool.create
        │  解析 once / interval / cron
        ▼
TaskStore 写入 data/scheduler/tasks.json
        │
        ▼
SchedulerService 后台线程循环扫描
        │
        ├── 未到点：跳过
        └── 到点：SchedulerExecutor 执行
                │
                ├── send_message → channel.send_to(...)
                ├── agent_task   → agent.run(... scheduler session ...) → send_to(...)
                └── tool_call    → tools.run(...) → send_to(...)
```

### 5.2 为什么 scheduler 要作为工具

用户会自然表达：

```text
10 分钟后提醒我喝水
每天早上 9 点帮我总结日报
每周五下午提醒我做周报
```

这些话需要模型理解并转换成结构化任务：

```json
{
  "action": "create",
  "name": "写日报提醒",
  "message": "该写日报了",
  "schedule_type": "cron",
  "schedule_value": "0 9 * * *"
}
```

因此“创建任务”天然应该是工具调用。

### 5.3 为什么不能只有工具

工具只在模型推理时运行。用户不说话时，模型不会自己醒来。

所以到点执行必须靠后台服务：

```text
SchedulerService = 常驻闹钟
SchedulerTool = 创建/管理闹钟的遥控器
```

---

## 6. Scheduler 数据模型

任务文件：

```text
data/scheduler/tasks.json
```

结构：

```json
{
  "version": 1,
  "updated_at": "2026-07-12T10:00:00",
  "tasks": {
    "abcd1234": {
      "id": "abcd1234",
      "name": "写日报提醒",
      "enabled": true,
      "created_at": "...",
      "updated_at": "...",
      "schedule": {
        "type": "cron",
        "expression": "0 9 * * *"
      },
      "action": {
        "type": "send_message",
        "content": "该写日报了",
        "target": {
          "channel_type": "feishu",
          "user_id": "ou_xxx",
          "is_group": false,
          "group_id": null,
          "session_id": "feishu:ou_xxx"
        }
      },
      "next_run_at": "..."
    }
  }
}
```

---

## 7. SchedulerTool 设计

工具名：

```text
scheduler
```

操作：

```text
create / list / get / delete / enable / disable
```

创建参数：

```json
{
  "action": "create",
  "name": "任务名称",
  "message": "固定提醒文本",
  "ai_task": "让 agent 执行的任务",
  "tool_name": "可选，直接调用工具",
  "tool_arguments": {},
  "schedule_type": "once | interval | cron",
  "schedule_value": "+10m | +1h | 0 9 * * * | ISO时间"
}
```

约束：

- `message / ai_task / tool_call` 三选一。
- `schedule_type / schedule_value` 必填。
- 创建任务时必须能从当前 `RunContext` 推导出 channel target。

---

## 8. 到点执行设计

### 8.1 固定提醒：send_message

```text
到点 → 取 content → channel 主动发送给 target
```

这是最简单的类型，不调用 agent，不污染上下文。

### 8.2 AI 任务：agent_task

```text
到点
  ↓
构造 RunContext(
  session_id="scheduler:<target>:<task_id>",
  entrypoint="scheduler",
  channel_type=target.channel_type,
  is_scheduled_task=True
)
  ↓
agent.run(ai_task, context=scheduler_context)
  ↓
把 answer 主动发送给 target
```

关键设计：

- 使用隔离 session，避免污染用户当前短期上下文。
- `is_scheduled_task=True`，用于禁止 scheduler 工具递归调用。
- AI 任务输出可记录 raw，后续是否进入 episodic 可单独配置。

### 8.3 工具任务：tool_call

```text
到点 → tools.run(tool_name, tool_arguments) → 把结果发送给 target
```

适合：

- 定时 web_search
- 定时 read_memory
- 定时 SOP 扫描

---

## 9. 与 channel 的集成

新增主动发送目标：

```python
@dataclass
class ChannelTarget:
    channel_type: str
    user_id: str
    is_group: bool = False
    group_id: Optional[str] = None
    session_id: str = ""
```

新增能力：

```python
ChannelManager.send_to(target: ChannelTarget, reply: Reply)
```

第一版实现策略：

1. 从 `target` 构造 synthetic `IncomingMessage`。
2. 找到对应 channel 实例。
3. 调用该 channel 的 `send(reply, synthetic_message)`。

这样最小改动复用现有 CLI / Feishu 发送逻辑。

---

## 10. 防递归设计

定时任务执行时，不能允许模型再次创建定时任务，否则容易套娃：

```text
定时任务 → agent_task → 模型调用 scheduler → 新定时任务 → ...
```

实现方式：

1. `RunContext.metadata["is_scheduled_task"] = True`
2. `ToolRegistry.schemas(context)` 在该标记存在时不暴露 `scheduler`
3. 或 `ToolRegistry.run("scheduler")` 检测该标记并拒绝

推荐第一种：**从 schema 层隐藏 scheduler**，让模型根本看不到这个工具。

---

## 11. 记忆策略

### send_message

固定提醒默认不进入长期记忆：

```text
“该喝水了”
```

这类文本价值低，写入会污染记忆。

### agent_task

AI 任务结果默认记录 raw，但不强行写入 semantic。

可选策略：

- raw：记录，便于追溯。
- episodic：允许未来被总结。
- semantic：只通过后续 dream/SOP 机制自然沉淀，不直接写。

### tool_call

工具结果是否记忆取决于工具类型：

- web_search 结果：默认不写长期记忆，避免把实时新闻永久化。
- SOP / memory 相关工具：可写。

---

## 12. 实现阶段

### 第一阶段：Web Search

1. 新增 `tools/web_search_tool.py`
2. 配置 `QIANFAN_API_KEY`
3. ToolRegistry 条件注册
4. 离线 mock 测试

### 第二阶段：Scheduler 固定提醒

1. 新增 `scheduler/task_store.py`
2. 新增 `scheduler/service.py`
3. 新增 `tools/scheduler_tool.py`
4. 新增 `channel/target.py`
5. ChannelManager 增加 `send_to`
6. Agent 初始化或 ChannelManager 启动时启动 SchedulerService
7. 支持 `send_message`

### 第三阶段：Scheduler AI 任务

1. 支持 `ai_task`
2. 隔离 scheduler session
3. 防递归隐藏 scheduler 工具
4. 发送 agent 执行结果
5. 设计 raw/episodic 记录策略

### 第四阶段：Scheduler tool_call

1. 支持定时直接调用工具
2. 允许定时 web_search / sop / memory read
3. 结果发送到目标 channel

---

## 13. 测试矩阵

### Web Search

| ID | 用例 |
|----|------|
| WS-1 | 无 API key 时不注册 web_search |
| WS-2 | query 为空返回错误 |
| WS-3 | count 越界回退默认 |
| WS-4 | mock 千帆成功响应，解析 title/url/snippet |
| WS-5 | mock HTTP 401 / 500 / 超时，返回 ToolResult(ok=False) |

### Scheduler Store

| ID | 用例 |
|----|------|
| SCH-STORE-1 | tasks.json 不存在时返回空 |
| SCH-STORE-2 | add/get/list/delete |
| SCH-STORE-3 | enable/disable |
| SCH-STORE-4 | 写文件时生成 backup |

### Scheduler Tool

| ID | 用例 |
|----|------|
| SCH-TOOL-1 | create 缺 name/message/schedule 报错 |
| SCH-TOOL-2 | create once +10m 生成 run_at |
| SCH-TOOL-3 | create cron 校验表达式 |
| SCH-TOOL-4 | list/get/delete/enable/disable |
| SCH-TOOL-5 | current context 注入 target |

### Scheduler Service

| ID | 用例 |
|----|------|
| SCH-SVC-1 | 未到点不执行 |
| SCH-SVC-2 | 到点执行 send_message |
| SCH-SVC-3 | once 执行后删除 |
| SCH-SVC-4 | cron 执行后计算下一次 |
| SCH-SVC-5 | 执行失败不推进 next_run_at，等待重试 |

### Channel Target

| ID | 用例 |
|----|------|
| CH-TGT-1 | target → synthetic IncomingMessage |
| CH-TGT-2 | ChannelManager.send_to 找到正确 channel 并 send |
| CH-TGT-3 | 未知 channel 返回错误 |

### Agent Task

| ID | 用例 |
|----|------|
| SCH-AI-1 | agent_task 使用 scheduler session |
| SCH-AI-2 | is_scheduled_task 下隐藏 scheduler 工具 |
| SCH-AI-3 | 执行结果 send_to 用户 |
| SCH-AI-4 | raw/trace 记录正确 |

---

## 14. 讲述版总结

Web search 是 agent 的“眼睛”：当它不知道最新信息时，通过搜索 API 看一眼真实世界。

Scheduler 是 agent 的“闹钟”和“长期承诺系统”：用户说“明天提醒我”，模型不是把这句话记在脑子里，而是调用 scheduler 工具把承诺写进任务表；后台服务像闹钟一样持续巡逻，到点后再把任务送回 channel。

所以 scheduler 不是一个简单工具，而是四件东西合作：

```text
工具负责登记承诺
存储负责保存承诺
后台服务负责守时
channel 负责把承诺兑现给用户
```

这就是完整对标 CowAgent 的设计。

