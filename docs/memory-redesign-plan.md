# my-agent 记忆系统重构方案

## 背景

当前 `my-agent` 的 memory 实现偏向“结构化语义记忆库”：模型输出 `add/update/delete` action 后直接写入 SQLite，再生成 `MEMORY.md` 视图。

这个设计不符合后续目标。新的目标是参考 CowAgent 的记忆设计思路，将记忆本体和检索索引分开，并明确区分：

```text
语义记忆 semantic memory
情景记忆 episodic memory
原始对话 raw conversation
检索索引 memory index
做梦整理 deep dream
```

本文件先作为重构前的设计确认稿。当前已按本文完成第一版代码重构，后续增强仍以本文原则为准。

## 设计原则

1. 记忆本体优先使用可读文件保存，数据库只做索引和检索加速。
2. 语义记忆和情景记忆必须分离。
3. 原始对话必须保存，作为事实来源和后续重新总结的依据。
4. daily 情景记忆不是逐轮流水账，而是高质量事件摘要。
5. `MEMORY.md` 只保存长期稳定、有复用价值的语义记忆。
6. 做梦机制消费情景记忆，产出更精炼的语义记忆。

## 目标目录结构

```text
data/memory/
  MEMORY.md
  raw/
    default/
      2026-07-06.jsonl
  daily/
    2026-07-06.md
  dreams/
    2026-07-06.md
  index.db
```

说明：

```text
MEMORY.md              语义记忆，本体文件
raw/*.jsonl            原始对话，本体文件
daily/*.md             情景记忆，本体文件
dreams/*.md            做梦整理日志
index.db               检索索引，不是记忆本体
```

## 语义记忆

语义记忆使用 `data/memory/MEMORY.md` 存储。

语义记忆保存长期稳定的信息：

```text
用户偏好
项目长期决策
稳定事实
长期规则
重要经验
反复出现的约束
```

示例：

```markdown
# Semantic Memory

## User Preferences

- 用户要求重大架构设计先写方案，确认后再执行。
- 用户希望 my-agent 的 memory 设计参考 CowAgent，而不是自定义 action 库。

## Project Decisions

- my-agent 的记忆分为语义记忆和情景记忆。
- 语义记忆使用 `MEMORY.md` 存储。
- 情景记忆使用 daily markdown 摘要，并保留 raw conversation 作为事实来源。
```

语义记忆不应该保存：

```text
普通闲聊
一次性工具输出
临时中间状态
重复事实
没有长期价值的措辞
```

## 情景记忆

情景记忆使用 `data/memory/daily/YYYY-MM-DD.md` 存储。

它记录“当天发生过哪些有意义的事件”，不是逐轮聊天记录。

情景记忆保存：

```text
用户提出的重要需求
围绕同一事项的多轮讨论结论
关键设计取舍
执行过的重要修改
发现的问题和修复方式
待办事项
用户明确表达的反馈
```

情景记忆不保存：

```text
问候
重复确认
无意义寒暄
完整工具输出
无结论的短暂试探
模型自己的无价值解释
```

## 为什么不只使用对话原文

对话原文必须保存，但不应该只使用原文作为主要情景记忆。

原文的价值：

```text
信息完整
可追溯
可重新总结
适合 debug
避免第一次总结误伤
```

原文的问题：

```text
token 成本高
噪声多
检索容易命中无价值片段
做梦整理成本高
不适合长期直接注入模型上下文
```

因此采用双层设计：

```text
raw conversation 作为事实来源
daily memory 作为可检索、可做梦的情景摘要
```

## 原始对话

原始对话使用 JSONL 存储：

```text
data/memory/raw/default/YYYY-MM-DD.jsonl
```

每行一条消息或事件：

```json
{"session_id":"default","role":"user","content":"...","created_at":"..."}
{"session_id":"default","role":"assistant","content":"...","created_at":"..."}
{"session_id":"default","role":"tool","name":"read_file","content":"...","created_at":"..."}
```

原始对话用于：

```text
追溯事实
重新生成 daily memory
debug 总结质量
未来做更强压缩或回放
```

默认不直接注入模型上下文。

## 检索索引

SQLite / FTS 只作为索引，不作为记忆本体。

索引对象：

```text
MEMORY.md
daily/*.md
必要时 raw/*.jsonl 的摘要片段
```

索引字段建议：

```text
id
source_type: semantic | episodic | raw
source_path
start_line
end_line
text
hash
created_at
updated_at
```

检索流程：

```text
query
  -> FTS/关键词召回候选片段
  -> 后续可扩展 embedding 召回
  -> 合并候选
  -> 按相关性、来源类型、时间排序
  -> 返回 Top-K 给 ContextManager
```

排序倾向：

```text
语义记忆优先级高，因为它更稳定
近期情景记忆优先级次之
raw 原文默认不直接返回，除非需要追溯细节
```

## daily 情景记忆总结设计

### 触发时机

参考 CowAgent，不每轮都总结。

触发条件：

```text
ContextManager 裁剪历史时
上下文 token 超限时
手动触发 daily summary 时
程序退出前可选 flush 时
```

核心语义：

```text
当一批历史即将从短期上下文中丢失时，
将这批 discarded messages 总结成 daily 情景记忆。
```

不是：

```text
每条 message 都总结
每轮对话都总结
等很多次裁剪后才总结
```

### 输入

daily 总结输入应包含：

```text
被裁剪的完整 turns
当前日期
session_id
可选：当前任务主题
可选：已有当日 daily memory，用于避免重复
```

输入对话应尽量保持完整轮次：

```text
user message
assistant message
tool call summary
tool result summary
```

工具结果不应原样全部塞给总结模型，应该先压缩为：

```text
工具名
操作对象
成功/失败
关键结果
变更文件
```

### 输出

写入：

```text
data/memory/daily/YYYY-MM-DD.md
```

推荐格式：

```markdown
# Daily Memory: 2026-07-06

## Trimmed Context 14:35

- 用户要求重构 my-agent 的记忆设计，明确分为语义记忆和情景记忆。
- 用户指出当前实现没有完全参考 CowAgent，要求先写方案并确认后再执行。
- 设计结论：语义记忆使用 `MEMORY.md`，情景记忆使用 daily markdown，同时保留 raw conversation 作为事实来源。
```

### 高质量总结规则

daily 情景记忆总结必须遵守：

1. 按事件总结，不按对话轮次逐条记录。
2. 合并同一事件的多轮讨论。
3. 保留用户真实需求、反馈、决策、结论和待办事项。
4. 保留关键文件、模块、函数名和设计名。
5. 保留“为什么这样做”的原因，而不只记录“做了什么”。
6. 区分用户要求、模型建议、最终确认结果。
7. 不编造对话中没有的信息。
8. 不保存无意义闲聊、重复确认、纯工具噪声。
9. 如果没有记录价值，输出 `无`。
10. 每条一句话，尽量短，但不能丢关键约束。

## daily 总结 Prompt 草案

该 prompt 参考 CowAgent 的事件级 daily memory 总结要求，并针对代码学习项目增强了工程信息保留。

### System Prompt

```text
你是一个高质量对话记忆整理助手，负责将即将从上下文中裁剪的对话归纳为当天的情景记忆。

你的目标不是复述聊天记录，而是提炼“发生过的有意义事件”。

## 总结原则

1. 按事件总结，不按对话轮次逐条记录。
2. 合并同一事件的多轮讨论，避免重复。
3. 只记录有长期追溯价值的信息，忽略寒暄、重复确认、无意义中间话。
4. 保留用户明确提出的需求、反馈、纠正、偏好和约束。
5. 保留重要决策、设计取舍、结论、待办事项。
6. 如果涉及代码或项目，保留关键文件路径、模块名、函数名、方案名。
7. 如果涉及错误和修复，记录“问题 -> 原因 -> 处理结果”。
8. 严禁编造、推测、扩展对话中不存在的信息。
9. 不要保存完整工具输出，只保留工具执行产生的关键事实。
10. 如果对话没有任何记忆价值，只输出“无”。

## 输出格式

- 每条一行，以 "- " 开头。
- 每条尽量是一句完整的话。
- 不要输出标题、解释、JSON 或 Markdown 表格。
```

### User Prompt

```text
请将以下被裁剪的对话总结为当天的情景记忆：

会话信息：
- date: {date}
- session_id: {session_id}
- reason: {reason}

已有当日情景记忆：
{existing_daily_memory}

被裁剪的对话：
{conversation}

请输出高质量事件摘要。
```

### 示例

输入：

```text
user: 我都说你完全抄 cow-agent 的记忆设计思路，写我们的代码，你为什么独做主张？
assistant: 你说得对，我先写方案，不改代码。
user: 语义记忆使用 memory.md，情景记忆我想问为什么不使用对话原文？
assistant: 原文应该保存，但 daily 摘要更适合检索和做梦。
```

输出：

```text
- 用户纠正 my-agent 的记忆实现方向，要求严格参考 CowAgent 的记忆设计思路，并在执行前先写方案确认。
- 用户要求记忆分为语义记忆和情景记忆，其中语义记忆使用 `MEMORY.md` 存储。
- 设计结论：原始对话需要保存为事实来源，但 daily 情景摘要更适合检索、上下文注入和后续做梦整理。
```

## 做梦机制设计

做梦机制消费：

```text
当前 MEMORY.md
最近 N 天 daily/*.md
```

输出：

```text
更新后的 MEMORY.md
dreams/YYYY-MM-DD.md
```

做梦目标：

```text
从情景记忆中提取长期稳定事实
合并重复语义记忆
解决冲突
删除过期和临时信息
保持 MEMORY.md 精炼
记录这次整理过程
```

做梦不直接消费 raw conversation，除非：

```text
daily 摘要质量不足
需要追溯细节
用户要求重新整理某段对话
```

## 模块拆分方案

```text
memory/raw_store.py
  保存和读取原始对话 JSONL。

memory/episodic_store.py
  写入和读取 daily 情景记忆。

memory/semantic_store.py
  读写 MEMORY.md 语义记忆。

memory/index.py
  维护 SQLite/FTS 检索索引。

memory/curator.py
  负责 daily 总结和 deep dream。

memory/manager.py
  门面层，编排以上模块。
```

## 与当前实现的差异

需要废弃当前这种主存储思路：

```text
LLM 输出 add/update/delete
  -> 直接写 SQLite memories 表
  -> 再生成 MEMORY.md
```

改为：

```text
raw conversation / daily / MEMORY.md 是记忆本体
SQLite/FTS 是索引
daily 是情景记忆
MEMORY.md 是语义记忆
deep dream 负责 daily -> MEMORY.md
```

## 第一版实现决策

1. raw conversation 按 `session_id/date` 分文件，路径为 `data/memory/raw/{session_id}/YYYY-MM-DD.jsonl`。
2. daily 情景记忆按自然日一个文件，路径为 `data/memory/daily/YYYY-MM-DD.md`。
3. daily summary prompt 要求保留关键文件路径、模块名、函数名和方案名。
4. 做梦机制先提供 `MemoryManager.deep_dream()` 手动入口，暂不做自动定时。
5. raw conversation 第一版只作为事实追溯源，不进入索引；索引只覆盖 `MEMORY.md` 和 `daily/*.md`。

后续如果 daily 摘要质量不足，再考虑从 raw conversation 回溯重建 daily。
