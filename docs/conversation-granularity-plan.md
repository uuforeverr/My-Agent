# 会话粒度细化设计：channel:user → channel:user:conversation

> 目标：把短期上下文的隔离粒度从"渠道+用户"细化到"渠道+用户+会话线程"，对标 Codex/Cursor 的多会话（multi-thread chat）体验。
> 单用户模型（一个 agent = 一个人）：长期记忆全局共享跟人走，短期上下文按 conversation 隔离。
> 本文档先落盘设计，经认可后再写实现与测试。

---

## 1. 核心概念澄清

| 概念 | 是什么 | 存在哪 | 生命周期 |
|------|--------|--------|----------|
| **conversation_id** | 一个会话线程的**标签**（字符串），不含内容 | 各 channel 内存表 `{scope → current_conversation_id}` | 内存态，重启清空 |
| **短期 history** | 该标签**指向的实际对话内容**（`ContextManager.history` 的消息 list），每轮拼给 LLM | `Agent._context_managers[session_id].history` | 内存态，重启清空 |
| **长期记忆** | raw / episodic / semantic / index，跨轮跨天累积 | 落盘 `data/memory/` | 持久，跨重启保留，**全局共享跟人走** |

关系：`conversation_id` 是 `session_id` 的一部分（key），`history` 是这个 key 映射到的 value。
"新建对话" = 换一个新 conversation_id → `_get_context_manager` 命中新 key → 得到一份新的空 history。**长期记忆不受影响。**

---

## 2. 已确认决策

1. **conversation_id 格式**：`{YYYYMMDD}-{HHMMSS}-{4位随机hex}`，如 `20260706-153012-a3f9`。
   - 可排序（字典序=时间序，天然支持"最近会话"排序）、可读、免中央协调（各 channel 本地生成）、同秒多开不撞。
2. **飞书新建**：仅 `/new` 文本命令。用户不发 `/new` 就一直在当前会话（行为可预测）。
3. **持久化**：不做。conversation 标签与短期 history 均为内存态，重启即清。长期记忆仍跨重启保留。

---

## 3. session_id 结构变更

### 现状
```
session_id = f"{channel_type}:{scope}"          # scope = group_id or user_id
例：feishu:ou_abc   cli:local
```

### 目标
```
session_id = f"{channel_type}:{scope}:{conversation_id}"
例：feishu:ou_abc:20260706-153012-a3f9   cli:local:20260706-090000-1b2c
```

这条 session_id 往下自动流经：
- `ChatChannel.produce` → 队列/信号量 key（**含义升级**：现在"同会话串行"= 同一 conversation 内串行；不同 conversation 之间并行）
- `ChatChannel._handle` → `RunContext.create(session_id=...)`
- `Agent._get_context_manager(session_id)` → 短期窗口 key（**每个 conversation 一份独立 history**）

记忆层调用点（`record_message` / `flush_from_messages`）仍收这条 session_id，只作为 raw 分区与标签使用——**长期记忆全局性不变**（episodic/semantic/index 本就不按 session 过滤）。

---

## 4. conversation 归属逻辑放在哪

**放在 channel 层**，不放 IncomingMessage 自己算。原因：conversation_id 是"当前会话"这一**有状态**的东西（要能被 `/new` 切换），而 IncomingMessage 是无状态的一次性消息。

### 4.1 IncomingMessage 变更

新增可选字段 `conversation_id`，并让 `session_id` 派生把它带上：

```python
@dataclass
class IncomingMessage:
    channel_type: str
    user_id: str
    content: str
    msg_id: str
    is_group: bool = False
    group_id: Optional[str] = None
    conversation_id: Optional[str] = None      # 新增
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scope(self) -> str:                     # 抽出来复用
        return self.group_id if self.is_group and self.group_id else self.user_id

    @property
    def session_id(self) -> str:
        base = f"{self.channel_type}:{self.scope}"
        return f"{base}:{self.conversation_id}" if self.conversation_id else base
```

> 兼容性：`conversation_id=None` 时 session_id 退化为旧格式 `channel:scope`。这样既有测试（ISO/PROD/FS 等断言 `feishu:ou_abc`、`cli:local`）**无需修改**。

### 4.2 新增 conversation_id 生成器（共用工具）

新增 `channel/conversation.py`：

```python
import secrets
from datetime import datetime

def new_conversation_id() -> str:
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{now}-{secrets.token_hex(2)}"   # token_hex(2) = 4 位 hex
```

### 4.3 ChatChannel：维护"当前会话"表

`ChatChannel` 新增按 scope 记忆当前 conversation 的表 + 一个 helper：

```python
self._conversations: dict[str, str] = {}     # scope → current conversation_id
self._conv_lock = threading.Lock()

def current_conversation(self, scope: str) -> str:
    with self._conv_lock:
        cid = self._conversations.get(scope)
        if cid is None:
            cid = new_conversation_id()
            self._conversations[scope] = cid
        return cid

def start_new_conversation(self, scope: str) -> str:
    with self._conv_lock:
        cid = new_conversation_id()
        self._conversations[scope] = cid
        return cid
```

> 说明：`_conversations` 与队列 `self.sessions` 是两张不同的表——前者按 **scope**（用户/群）记"当前 conversation"，后者按 **完整 session_id**（含 conversation）记队列。互不干扰。

---

## 5. 各渠道"新建对话"接入

### 5.1 CLI（`channel/cli/cli_channel.py`）

- 启动时自动开一个会话（首条消息触发 `current_conversation("local")` 惰性生成即可，无需显式）。
- 新增 `/new` 命令：调 `start_new_conversation("local")`，打印新会话短 id。
- 新增 `/conv` 命令（可选）：打印当前会话 id。
- 普通输入构造 IncomingMessage 时带上 `conversation_id=self.current_conversation("local")`。
- 提示符可显示短 id：`[a3f9] > `（取 conversation_id 末 4 位）。

`_dispatch` 新增分支（其余不变）：
```
if line == "/new":
    cid = self.start_new_conversation(self.user_id)
    print(f"🌱 new conversation: {cid}")
    return CONTINUE
```

### 5.2 飞书（`channel/feishu/feishu_channel.py`）

- `_handle_message_event` 解析出文本后，先判断是否 `/new`：
  - 是 → `start_new_conversation(scope)`，通过 `send` 回一句"🌱 已开启新对话"，**不**进 agent。
  - 否 → 带 `conversation_id=self.current_conversation(scope)` 构造 IncomingMessage → produce。
- scope 对私聊是 open_id，对群是 chat_id（沿用现有 is_group 逻辑）。

> 注：`/new` 判断放在幂等/过期过滤**之后**、produce **之前**，避免重复消息重复开会话。

### 5.3 网页端（未实现，仅记设计意图）

- 前端每个 chat tab 持有自己的 conversation_id（前端生成，随每条消息带上）。
- 服务端信任前端传入的 conversation_id，直接塞进 IncomingMessage。
- "新对话"按钮 = 前端生成新 id + 开新 tab。本轮不实现，接口预留（IncomingMessage 已支持传入 conversation_id）。

---

## 6. 并发语义的变化（重要）

- **改动前**："同 channel 同 user 串行"——飞书里你发的所有消息排一条队。
- **改动后**："同 channel 同 user 同 conversation 串行；不同 conversation 并行"。
  - 即：`/new` 开了会话 B 后，若会话 A 还有任务在跑，A、B 会**并行**（各自独立 session_id → 各自队列+信号量）。
  - 这符合 Codex/Cursor 多 tab 可同时干活的直觉。

**长期记忆写入的线程安全不受影响**：flush 仍走 MemoryManager 的单线程后台池 + `_episodic_lock`，多 conversation 并发 flush 依然串行化落盘。

---

## 7. 改动 blast radius

| 文件 | 改动 | 风险 |
|------|------|------|
| `channel/conversation.py` | 新增：`new_conversation_id()` | 无（新文件） |
| `channel/message.py` | 加 `conversation_id` 字段 + `scope` property；session_id 派生兼容旧格式 | 低（None 时退化为旧格式，既有测试不破） |
| `channel/chat_channel.py` | 加 `_conversations` 表 + `current_conversation` / `start_new_conversation` | 低（新增方法，不改调度核心） |
| `channel/cli/cli_channel.py` | `/new`（+可选 `/conv`）；produce 带 conversation_id；提示符显示短 id | 低 |
| `channel/feishu/feishu_channel.py` | `/new` 分支；produce 带 conversation_id | 中（需保证 /new 判断位置正确） |
| `agent/agent.py` | **不动**（session_id 变长它自然按新 key 隔离） | 无 |
| `memory/*` | **不动**（长期记忆全局性不变） | 无 |

---

## 8. 测试矩阵（离线，沿用 conftest）

| ID | 用例 | 组件 | 级别 |
|----|------|------|------|
| CONV-1 | `new_conversation_id()` 格式匹配 `\d{8}-\d{6}-[0-9a-f]{4}`；两次调用不相等 | conversation.py | P0 |
| CONV-2 | IncomingMessage 带 conversation_id → session_id = `channel:scope:cid`；不带 → 退化旧格式 | message.py | P0 |
| CONV-3 | `current_conversation(scope)` 首次惰性生成、二次返回同值；不同 scope 不同值 | ChatChannel | P0 |
| CONV-4 | `start_new_conversation(scope)` 换新值，且与旧值不同 | ChatChannel | P0 |
| CONV-5 | 不同 conversation → 不同 session_id → 短期 history 隔离（换 conversation 后 model_messages 不含旧会话内容） | Agent+ChatChannel | **P0** |
| CONV-6 | 同 scope 不同 conversation 的两条消息路由到不同队列（并行前提） | ChatChannel | P0 |
| CLI-NEW-1 | `/new` 调 start_new_conversation 并打印；返回 CONTINUE 不 produce | CLIChannel | P0 |
| CLI-NEW-2 | 普通输入带上 current conversation_id；`/new` 后新输入用新 cid | CLIChannel | P0 |
| FS-NEW-1 | 飞书 `/new` 文本 → 开新会话、回提示、不进 agent（不 produce 业务消息） | FeishuChannel | P0 |
| FS-NEW-2 | 飞书普通文本带上 current conversation_id；`/new` 后新文本用新 cid | FeishuChannel | P0 |
| REG-1 | 既有 ISO/PROD/FS/CONC 全绿（conversation_id=None 退化路径不破坏旧断言） | 全量 | **P0** |

---

## 9. 执行顺序（认可后）

1. `channel/conversation.py`（CONV-1）
2. `channel/message.py` 加字段 + 兼容派生（CONV-2）
3. `channel/chat_channel.py` 加会话表（CONV-3/4/6）
4. `agent` 隔离验证（CONV-5，代码不动，仅加测试）
5. `channel/cli/cli_channel.py` /new（CLI-NEW-1/2）
6. `channel/feishu/feishu_channel.py` /new（FS-NEW-1/2）
7. 跑全量 + 覆盖率，确认 REG-1 既有用例不破

---

## 10. 明确不做（本轮）

- conversation / history 持久化与 `/resume`（内存态即可）。
- 空闲超时自动分段（仅 `/new` 手动）。
- 网页端实现（仅预留 IncomingMessage 接口）。
- 跨渠道用户身份绑定（单用户模型下长期记忆本就全局共享，无需绑定）。
