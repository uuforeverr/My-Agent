# my-agent 从 0 到 1 最小化实现路线

## 原则

my-agent 不是复刻 CowAgent，而是用 CowAgent 反推一个可掌控的最小 Agent。

实现原则：

1. 每个模块先做最小闭环，不提前抽象复杂框架。
2. 能用一个文件表达清楚的模块，不拆成多个文件。
3. 只有当职责明显不同，才单独建目录。
4. 横切能力不塞进 `agent/`，例如 tracing 放在 `observability/`。
5. 后续优化必须建立在可运行的最小功能之上。

## 当前已有模块

```text
CLI 入口
配置读取
OpenAI-compatible LLM
最小 Agent Loop
运行上下文：RunContext
模型上下文管理：ContextManager
Prompt 文件：prompts/identity.md
文件工具：bash/glob/read_file/write_file/edit_file
长期记忆：MemoryManager
记忆工具：remember_memory/memory_search/read_memory
技能加载：SkillLoader
workspace 路径边界
trace 记录与 JSONL 落盘
```

当前结构：

```text
agent/             Agent Loop、RunContext、ContextManager
llm/               模型调用
tools/             工具抽象与工具实现
memory/            长期记忆管理代码
prompts/           identity.md 与 prompt 加载
data/memory/       raw 原文、daily 情景记忆、MEMORY.md 语义记忆、SQLite/FTS 索引
skills/            最小技能加载与 SKILL.md
observability/     trace、日志、指标等横切观测能力
workspace/         工具可操作目录
docs/              学习与设计文档
```

## 已完成模块：运行上下文 RunContext

### 为什么需要

早期 `Agent.run(user_message)` 只接收纯文本，缺少请求运行信息。现在已引入 `RunContext`，后续 memory、skill、web_search 都可以共享：

```text
request_id
session_id
workspace
是否启用 memory
是否启用某些 tools
```

### MVP 目标

新增一个轻量 `RunContext`：

```python
@dataclass
class RunContext:
    session_id: str
    workspace: Path
    trace: Trace
    metadata: dict
```

### 放置位置

```text
agent/run_context.py
```

### 暂不做

```text
多通道 channel_type
群聊/单聊
用户身份聚合
复杂 ContextType/ReplyType
```

### CowAgent 参考

```text
bridge/context.py
channel/chat_channel.py
```

但 my-agent 不需要照搬 CowAgent 的 `Context` 字典式设计。

## 已完成模块：模型上下文管理 ContextManager

### 为什么需要

Agent 最佳实践里的 context 更常指“调用模型前发给 LLM 的上下文”，包括：

```text
system prompt
最近对话历史
assistant tool_call
tool result
未来的 memory/skill/web_search 注入
未来的上下文裁剪与压缩
```

因此 my-agent 将运行上下文和模型上下文管理拆开：

```text
RunContext       一次请求的运行元数据
ContextManager   模型上下文组装、历史维护、裁剪/压缩入口
```

### MVP 目标

当前已实现：

```text
append_user_message()
append_assistant_message()
append_tool_result()
build_model_messages()
_trim_history()
```

### 放置位置

```text
agent/context_manager.py
```

### 暂不做

```text
token 级上下文预算
摘要压缩
工具结果压缩
```

这些会在后续 Context 持久化和上下文压缩阶段逐步实现。

## 已完成模块：Prompt Identity

### 为什么需要

system prompt 不应该长期写死在 `agent.py`。角色、规则、工具边界、记忆使用原则都属于 Agent 的身份定义，应当放在可读、可维护的 prompt 文件中。

### 当前实现

```text
prompts/identity.md      定义模型角色、规则、工具策略、记忆策略
prompts/loader.py        负责读取 identity.md
```

### 与 ContextManager 的关系

```text
identity.md      稳定身份层
memory prompt    当前请求相关的长期记忆
skills prompt    当前可用技能列表
history          最近对话上下文
```

`ContextManager.build_model_messages()` 会把这些部分组装为最终发给模型的 messages。

## 待完成模块一：Context 持久化

### 为什么需要

现在 `ContextManager.history` 仍只存在内存里。程序重启后模型上下文会丢失。

### MVP 目标

先做 JSONL 会话日志：

```text
data/sessions/default.jsonl
```

每轮记录：

```json
{"role": "user", "content": "..."}
{"role": "assistant", "content": "..."}
```

启动时加载最近 N 轮。

### 放置位置

```text
agent/session_store.py
```

### 暂不做

```text
SQLite
按用户分页
消息编辑
工具调用链清洗
跨通道身份聚合
```

### CowAgent 参考

```text
agent/memory/conversation_store.py
bridge/agent_initializer.py
```

## 已完成模块：Memory 分层本地版

### 为什么需要

Agent 只有短期对话历史时，程序重启后偏好、事实和重要过程会丢失。当前 memory 参考 CowAgent 思路，将记忆本体和检索索引分开：

```text
raw conversation  原始对话，事实来源
daily memory      情景记忆，事件级摘要
MEMORY.md         语义记忆，长期稳定事实/偏好/规则
index.db          检索索引，不是记忆本体
```

### 当前实现

当前采用分层本地设计：

```text
data/memory/MEMORY.md         语义记忆
data/memory/daily/*.md        情景记忆
data/memory/raw/*/*.jsonl     原始对话
data/memory/dreams/*.md       做梦整理日志
data/memory/index.db          SQLite/FTS 检索索引
```

能力：

```text
1. Agent.run() 将 user/assistant/tool 消息写入 raw JSONL。
2. ContextManager 裁剪历史时调用 MemoryManager.flush_from_messages()。
3. MemoryCurator 用 LLM 将被裁剪的对话总结为 daily 情景记忆。
4. remember_memory 将长期稳定事实写入 MEMORY.md 语义记忆。
5. MemoryIndex 对 MEMORY.md 和 daily/*.md 建立 SQLite/FTS 索引。
6. build_model_messages() 按当前用户问题检索相关语义/情景记忆。
7. 只把相关 Top-K 记忆注入 system prompt。
8. deep_dream() 预留 daily -> MEMORY.md 的做梦整理入口。
```

### 放置位置

```text
memory/raw_store.py
memory/episodic_store.py
memory/semantic_store.py
memory/index.py
memory/curator.py
memory/manager.py
tools/memory_tools.py
```

### 暂不做

```text
embedding 强依赖
向量检索
Deep Dream 自动定时
按用户隔离记忆
```

### CowAgent 参考

```text
agent/memory/manager.py
agent/memory/storage.py
agent/memory/summarizer.py
agent/tools/memory/*
```

my-agent 没有原样照搬 CowAgent 的向量层和复杂同步层，而是先实现本地可运行的 raw 保存、daily 情景摘要、MEMORY.md 语义记忆、SQLite/FTS 检索闭环。

## 已完成模块：基础工具增强

### 为什么需要

原始工具只有：

```text
list_files
read_file
write_file
```

这组工具足够完成简单文件读写，但不适合作为工程 Agent 的基础工具集，因为它缺少：

```text
按模式发现文件
局部编辑文件
运行测试、搜索、格式化等命令
```

### 当前实现

当前基础工具组：

```text
bash        受限 shell 命令，带超时、输出截断和高危命令拦截
glob        按 glob pattern 查找 workspace 内文件
read_file   读取 UTF-8 文件，支持 offset/limit
write_file  写入 UTF-8 文件
edit_file   精确替换一次文本，或 old_text 为空时追加
```

### 安全边界

```text
所有文件路径必须在 workspace 内。
bash 默认只在 workspace 下执行。
bash 拒绝 .env/API_KEY、绝对路径、.. 路径穿越、sudo、重启/关机、磁盘写入、curl|sh 等高危命令。
```

当前 `bash` 不是系统级沙箱，只是应用层安全约束。后续工具调用安全加固可以继续增强命令审计、用户确认、allowlist/denylist 和执行隔离。

## 已完成模块：Skill 最小化实现

### 为什么需要

工具告诉模型“能做什么”，技能告诉模型“遇到某类任务应该怎么做”。最小 Skill 系统让 Agent 能从 `SKILL.md` 中获得任务方法论。

### MVP 目标

当前已实现：

```text
扫描 skills/*/SKILL.md
提取 name/description
格式化为 <available_skills>
注入 system prompt
```

### 放置位置

```text
skills/loader.py
```

### 暂不做

```text
enabled/disabled
requirements
依赖安装
技能市场
技能创建器
```

### CowAgent 参考

```text
agent/skills/loader.py
agent/skills/formatter.py
agent/skills/manager.py
```

my-agent 第一版只做 loader + prompt formatter。

## 待完成模块二：Web Search 最小化实现

### 为什么需要

Agent 目前只能读本地 workspace，不能获取外部实时信息。

### MVP 目标

先做一个 `web_search` 工具，不直接爬网页：

```text
输入 query
调用搜索 API
返回标题、摘要、URL
```

配置：

```text
SEARCH_API_KEY
SEARCH_API_BASE
```

### 放置位置

```text
tools/web_search.py
```

### 暂不做

```text
浏览器自动化
网页正文抽取
PDF/Word 解析
搜索结果 rerank
```

### CowAgent 参考

```text
agent/tools/web_search/*
agent/tools/web_fetch/*
agent/tools/utils/url_safety.py
```

如果后续加 `web_fetch`，必须同时加 URL 安全校验。

## 模块实现顺序

推荐顺序：

```text
1. RunContext 运行上下文（已完成）
2. ContextManager 模型上下文管理（已完成）
3. Prompt Identity（已完成）
4. Memory 成熟本地版（已完成）
5. 基础工具增强（已完成）
6. Skill 最小化（已完成）
7. Context 持久化
8. Web Search 最小化
```

原因：

```text
Context 是后续模块共享的请求载体。
Prompt Identity 定义 Agent 的稳定角色和规则。
基础工具决定 Agent 能否完成真实工程操作。
模型上下文持久化让 Agent 有连续对话基础。
Memory 建立长期偏好与事实。
Skill 提供任务方法论。
Web Search 扩展外部信息能力。
```

## 不建议现在做的事

```text
Web UI
多通道
插件系统
MCP
浏览器自动化
向量数据库
复杂多模型路由
任务调度
```

这些属于系统搭起来后的增强，不是从 0 到 1 的主线。

## 关于 trace 的结构调整

trace 已经从 `agent/` 移到：

```text
observability/tracing.py
```

原因：

```text
trace 是横切能力，不属于 Agent 核心逻辑。
Agent 只负责创建和写入事件，不负责展示、存储格式和查询方式。
Trace 自己负责 to_record/to_json。
JsonlTraceStore 负责落盘。
```

后续如果要新增 trace 查询，不应该继续改 `Agent`，而应该新增：

```text
observability/query.py
```

或者在 CLI 层新增命令调用 `JsonlTraceStore` 的读取接口。
