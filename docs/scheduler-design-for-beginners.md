# 定时任务设计文档（小白友好版）

> 这份文档用尽量白话的方式解释：my-agent 为什么需要定时任务、它由哪些模块组成、一次提醒任务是怎样从“用户一句话”变成“到点主动发消息”的。

---

## 1. 一句话理解

定时任务就是让 agent 能兑现“以后再做”的承诺。

比如用户说：

```text
10 分钟后提醒我喝水
每天早上 9 点帮我总结日报
每小时搜索一次 AI 最新新闻
```

普通聊天只能处理“现在这句话”，但定时任务要处理“未来某个时间点”。所以它不能只靠模型记住一句话，还需要一个后台服务一直看时间。

最核心的一句话：

```text
scheduler 工具负责登记任务，SchedulerService 后台线程负责真正到点执行。
```

---

## 2. 为什么不能“只做一个工具”

工具只有在模型推理时才会运行。

也就是说：

```text
用户发消息 -> 模型思考 -> 调工具 -> 回复用户
```

如果用户不再发消息，模型不会自己醒来。

所以“10 分钟后提醒我喝水”不能只靠一个工具完成。工具只能把任务登记下来，真正过 10 分钟后执行，必须靠后台线程。

可以把它想成：

| 角色 | 像什么 | 负责什么 |
| --- | --- | --- |
| `SchedulerTool` | 遥控器 | 让模型创建、查询、删除任务 |
| `TaskStore` | 任务本 | 把任务写进磁盘，重启后还在 |
| `SchedulerService` | 闹钟 | 后台循环检查任务是否到点 |
| `SchedulerExecutor` | 执行员 | 到点后真正发消息、跑 agent、调工具 |
| `ChannelTarget` | 收件地址 | 记录到点后应该发给谁 |

---

## 3. 总体架构

定时任务由 5 层配合完成：

```text
用户自然语言
  例：10 分钟后提醒我喝水
        |
        v
模型调用 scheduler 工具
        |
        v
SchedulerTool 创建结构化任务
        |
        v
TaskStore 写入 data/scheduler/tasks.json
        |
        v
SchedulerService 后台扫描
        |
        v
SchedulerExecutor 到点执行
        |
        v
ChannelManager.send_to 主动发回用户
```

对应代码位置：

| 模块 | 文件 | 说明 |
| --- | --- | --- |
| 调度解析 | `scheduler/schedule.py` | 解析 `once / interval / cron`，计算下次执行时间 |
| 任务存储 | `scheduler/task_store.py` | 读写 `data/scheduler/tasks.json` |
| 后台服务 | `scheduler/service.py` | 循环扫描任务是否到点 |
| 执行器 | `scheduler/executor.py` | 执行 `send_message / agent_task / tool_call` |
| 工具入口 | `tools/scheduler_tool.py` | 暴露给模型调用 |
| 投递目标 | `channel/target.py` | 保存“到点后发给谁” |
| 主动发送 | `channel/manager.py` | `send_to(target, reply)` 主动推送 |

---

## 4. 一次固定提醒是怎么发生的

以“10 分钟后提醒我喝水”为例。

### 第一步：用户发消息

```text
用户：10 分钟后提醒我喝水
```

这条消息来自某个 channel，比如 CLI 或飞书。系统会生成一个 `RunContext`，里面有当前会话的信息：

```text
channel_type = cli / feishu
user_id      = local / ou_xxx
is_group     = 是否群聊
group_id     = 群聊 ID，可为空
session_id   = 当前会话 ID
```

这些信息很重要，因为定时任务到点时已经没有“刚收到的用户消息”了，必须提前把“发给谁”保存下来。

---

### 第二步：模型调用 scheduler 工具

模型会把自然语言转换成结构化参数：

```json
{
  "action": "create",
  "name": "喝水提醒",
  "message": "该喝水了",
  "schedule_type": "once",
  "schedule_value": "+10m"
}
```

这里有几个关键字段：

| 字段 | 含义 |
| --- | --- |
| `action=create` | 创建一个新任务 |
| `message` | 到点后发送的固定文本 |
| `schedule_type=once` | 一次性任务 |
| `schedule_value=+10m` | 从现在开始 10 分钟后执行 |

---

### 第三步：SchedulerTool 创建任务

`SchedulerTool` 会做三件事：

1. 校验参数是否正确。
2. 解析时间，算出 `next_run_at`。
3. 从 `RunContext` 中提取 `ChannelTarget`，保存“到点后发给谁”。

创建后的任务大概长这样：

```json
{
  "id": "abcd1234",
  "name": "喝水提醒",
  "enabled": true,
  "schedule": {
    "type": "once",
    "run_at": "2026-07-13T18:00:00"
  },
  "action": {
    "type": "send_message",
    "content": "该喝水了",
    "target": {
      "channel_type": "cli",
      "user_id": "local",
      "is_group": false,
      "group_id": null,
      "session_id": "cli:local"
    }
  },
  "next_run_at": "2026-07-13T18:00:00"
}
```

然后 `TaskStore` 会把它写进：

```text
data/scheduler/tasks.json
```

这样就算程序重启，任务也不会丢。

---

### 第四步：SchedulerService 后台扫描

后台线程会定期扫描任务，默认每 30 秒扫一次。

它做的事情很简单：

```text
读取所有启用的任务
  |
  |-- 还没到点：跳过
  |
  |-- 到点了：交给 SchedulerExecutor 执行
```

如果任务执行成功：

| 调度类型 | 成功后怎么处理 |
| --- | --- |
| `once` | 执行完删除任务 |
| `interval` | 计算下一次执行时间 |
| `cron` | 计算下一次 cron 命中的时间 |

如果任务执行失败，比如 channel 还没准备好，服务不会推进 `next_run_at`，下次扫描会继续重试。

---

### 第五步：SchedulerExecutor 执行任务

对于固定提醒，执行器只需要拿到文本并发出去：

```text
读取 action.content = "该喝水了"
读取 action.target  = 发给谁
调用 ChannelManager.send_to(target, Reply("该喝水了"))
```

这类任务不需要重新调用 agent，也不会污染用户的上下文。

---

### 第六步：ChannelManager 主动发送

普通聊天回复是这样：

```text
用户消息 IncomingMessage -> channel.send(reply, 原消息)
```

但是定时任务到点时没有原消息。

所以系统使用 `ChannelTarget` 生成一个“合成消息”：

```text
ChannelTarget -> synthetic IncomingMessage
```

然后复用已有发送逻辑：

```text
channel.send(reply, synthetic_message)
```

这样 CLI、飞书等 channel 不需要大改，就能支持主动推送。

---

## 5. 支持的三种调度方式

### 5.1 once：只执行一次

适合：

```text
10 分钟后提醒我喝水
明天下午 3 点提醒我开会
```

支持两种写法：

```text
+10m                 10 分钟后
+1h                  1 小时后
+30s                 30 秒后
2026-07-13T18:00:00  指定 ISO 时间
```

执行成功后，任务会被删除。

---

### 5.2 interval：固定间隔执行

适合：

```text
每小时提醒我站起来活动
每 3600 秒执行一次检查
```

内部保存的是秒数：

```json
{
  "type": "interval",
  "seconds": 3600
}
```

执行成功后，下次执行时间会变成：

```text
当前时间 + seconds
```

---

### 5.3 cron：按日历规则执行

适合：

```text
每天早上 9 点
每周一到周五 8:30
每月 1 号 0 点
```

示例：

```text
0 9 * * *       每天 9:00
30 8 * * 1-5    周一到周五 8:30
0 0 1 * *       每月 1 号 0:00
```

cron 解析使用 `croniter`。如果没有安装，`once` 和 `interval` 仍然能用，只有 `cron` 任务会提示需要安装：

```bash
pip install croniter
```

---

## 6. 支持的三种执行动作

### 6.1 send_message：发送固定消息

例子：

```text
10 分钟后提醒我喝水
```

任务 action：

```json
{
  "type": "send_message",
  "content": "该喝水了"
}
```

特点：

- 最简单。
- 不调用模型。
- 不污染上下文。
- 适合普通提醒。

---

### 6.2 agent_task：到点让 agent 做一件事

例子：

```text
每天早上 9 点帮我总结日报
```

任务 action：

```json
{
  "type": "agent_task",
  "ai_task": "总结今天需要关注的日报事项"
}
```

到点后流程：

```text
构造隔离 session
  |
  v
调用 agent.run(ai_task)
  |
  v
把 agent 的回答主动发送给用户
```

为什么要隔离 session？

因为定时任务不是用户当前聊天的一部分。如果直接用用户当前会话，可能把后台任务的中间推理塞进正常聊天历史里，后续聊天会被干扰。

所以使用类似这样的 session：

```text
scheduler:<原 session_id>:<task_id>
```

---

### 6.3 tool_call：到点直接调用工具

例子：

```text
每小时搜索一次 AI 最新新闻
```

任务 action：

```json
{
  "type": "tool_call",
  "tool_name": "web_search",
  "tool_arguments": {
    "query": "AI 最新新闻"
  }
}
```

特点：

- 不经过模型思考。
- 直接调用工具。
- 适合固定参数的自动任务，比如定时搜索、定时读取记忆。

---

## 7. 防递归：为什么要防止定时任务再创建定时任务

假设不做限制，可能出现这种情况：

```text
定时任务到点
  -> agent_task 执行
  -> 模型又调用 scheduler 创建新任务
  -> 新任务到点后又创建更多任务
```

这会变成失控循环。

所以系统在执行定时任务时会打一个标记：

```text
is_scheduled_task = True
```

然后工具注册表会做两层保护：

1. `schemas(context)` 不把 `scheduler` 暴露给模型。
2. 即使模型意外调用 `scheduler`，`run()` 也会拒绝。

简单理解：

```text
定时任务执行期间，模型看不到 scheduler 工具。
```

---

## 8. 为什么 context 作为参数传递

CowAgent 的做法是把当前上下文挂到 scheduler 工具实例上。

my-agent 没有这样做，而是把 context 作为参数传给工具：

```text
ToolRegistry.schemas(context)
ToolRegistry.run(name, args, context)
```

原因是 my-agent 支持多个会话并行处理。

如果把当前 context 存在工具实例里，可能发生：

```text
用户 A 正在创建提醒
用户 B 同时也在创建提醒
工具实例上的 current_context 被互相覆盖
最后任务可能发错人
```

把 context 作为函数参数传递，就像每次调用都带着自己的“身份证”，不会互相串。

---

## 9. 失败重试与停机恢复

### 9.1 失败为什么不立刻放弃

定时任务到点时，channel 可能还没准备好，比如：

- 程序刚启动，飞书连接还没连上。
- 某个 channel 临时发送失败。

这时如果直接把任务标记完成，就会丢提醒。

所以执行失败时：

```text
不推进 next_run_at
不删除任务
下次扫描继续重试
```

---

### 9.2 停机后为什么只补最近一次

如果程序停了一段时间，周期任务可能已经错过多次。系统统一采用
`latest` 语义，不逐条追赶历史 occurrence：

```text
once：无论迟到多久，最多补执行一次，然后完成
interval / cron：只执行不晚于当前时间的最近一次，然后直接推进到未来
```

例如每小时任务在停机期间错过 9、10、11 点三次，12 点前恢复时只创建
11 点这一条 run，不会连续补三次。数据库中的 occurrence 唯一键还会阻止
重启时重复补同一轮。系统不再配置补偿窗口，也没有 `skip` 分支。

---

## 10. 任务文件长什么样

任务保存在：

```text
data/scheduler/tasks.json
```

外层结构：

```json
{
  "version": 1,
  "updated_at": "2026-07-13T18:00:00",
  "tasks": {
    "abcd1234": {
      "id": "abcd1234",
      "name": "喝水提醒",
      "enabled": true,
      "schedule": {},
      "action": {},
      "next_run_at": "2026-07-13T18:10:00"
    }
  }
}
```

为什么 `tasks` 是字典而不是数组？

因为通过任务 ID 查找、更新、删除更方便：

```text
tasks[task_id]
```

---

## 11. 和普通聊天的区别

普通聊天：

```text
用户发消息
  -> agent 立刻处理
  -> channel 回复这条消息
```

定时任务：

```text
用户发消息
  -> agent 创建任务
  -> 任务保存到磁盘
  -> 过一段时间后台线程发现到点
  -> 主动发送给 ChannelTarget
```

最大的区别：

```text
普通聊天有原始 IncomingMessage。
定时任务到点时没有原始 IncomingMessage，只有提前保存的 ChannelTarget。
```

---

## 12. 常见问题

### Q1：为什么固定提醒不写入长期记忆？

因为“该喝水了”这类提醒没有长期知识价值。写进记忆会让记忆变脏。

### Q2：为什么 agent_task 要用隔离 session？

为了不污染用户当前聊天。定时任务的执行过程和用户正在聊的内容应该分开。

### Q3：为什么 cron 要额外安装 croniter？

cron 表达式规则很多，自己实现容易漏边界。使用成熟库更稳。项目采用延迟导入，所以只有创建或执行 cron 任务时才需要它。

### Q4：任务创建后程序重启会丢吗？

不会。任务已经写入 `data/scheduler/tasks.json`。程序重新启动后，后台服务会继续扫描这个文件。

### Q5：如果任务执行失败会怎样？

失败时不会推进下一次执行时间，也不会删除任务。后台服务会在下一轮扫描继续尝试。

---

## 13. 小白版总结

定时任务不是“模型记住一句话”，而是一个完整的小系统：

```text
SchedulerTool
  负责把用户的承诺登记成任务

TaskStore
  负责把任务写进磁盘

SchedulerService
  负责在后台看时间

SchedulerExecutor
  负责到点后执行任务

ChannelTarget + ChannelManager
  负责把结果主动发回正确的人
```

所以它既像“闹钟”，也像“承诺系统”：

```text
用户说以后要做什么
  -> 工具登记
  -> 存储保存
  -> 后台守时
  -> 到点兑现
```

这就是 my-agent 定时任务的完整设计。
