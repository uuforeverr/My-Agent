# my-agent 多通道（channel）设计与测试方案

> 目标：在保留 CLI 的同时接入飞书（本地 websocket 长连接模式），实现"不同渠道并行、同一渠道同一会话串行"的多通道输入输出。
> 本文档先落盘设计与测试用例，经认可后再写实现与测试代码。
> 参考：CowAgent 的 `channel/chat_channel.py` 调度层 + `bridge/agent_bridge.py` 的 per-session 隔离。

---

## 1. 关键架构决策（已与用户确认）

### 1.1 session 隔离：每 session 独立 ContextManager

**CowAgent 的实证机制**（隔离不在 channel 层，而在"谁持有对话历史"这一层）：

1. **每 session 一个 Agent 实例**：`AgentBridge.agents = {}  # session_id -> Agent`，`get_agent(session_id)` 懒创建。每个 Agent 持有自己的 `self.messages` 短期历史（`messages_lock` 保护）。
2. **channel 层只做调度**：`produce` 按 `session_id` 入队；`consume` 用 per-session `BoundedSemaphore(1)` 保证同 session 串行，共享 `handler_pool` 保证跨 session 并行。因为同一份 `agent.messages` 只会被一个线程碰，所以运行期无需再对 messages 加锁。
3. **持久层独立**：SQLite `ConversationStore` 按 session_id 存全量历史，与内存隔离解耦。

**my-agent 的忠实映射**：CowAgent 在 Agent 层隔离，是因为在它那里 Agent 就是 history 持有者。而 my-agent 的 history 持有者是 `ContextManager`；Agent 额外持有 `memory / llm / tools / skill_loader`，这些是**应当共享的重量级单例**（`MemoryManager` 内含单线程后台池 + SQLite 连接 + 锁，每 session 造一个既浪费又语义错误）。

因此隔离边界落在 **ContextManager**，而非 Agent：

- `Agent` 从持有单个 `self.context_manager` 改为持有 `self.context_managers: dict[str, ContextManager]` + 一把 `threading.Lock` + 懒创建工厂。
- `memory / llm / tools / skill_loader` 保持单例共享。
- `ContextManager` 类本身**零改动**；`tests/test_context_manager.py` **零改动**。
- 记忆层已是 session-aware：`raw_store` 按 session_id 分文件、`flush_from_messages` 带 session_id、后台 `ThreadPoolExecutor(max_workers=1)+RLock` 天然串行化多 session 写入。缺的只有短期窗口 `history` 隔离这一块，本次补上。

### 1.2 并发模型（照搬 CowAgent 调度层）

- 每个 channel 一个独立 `consume()` daemon 线程 + 自己的 `sessions` / `lock`（实例级，不共享，避免跨通道串消息）。
- 同 session：per-session 队列 + `BoundedSemaphore(1)` → 串行。
- 跨 session / 跨通道：共享 `ThreadPoolExecutor` → 并行。
- `session_id = f"{channel_type}:{user_id}"`（如 `cli:local`、`feishu:ou_xxx`）。

### 1.3 其他已确认点

- 飞书走 **websocket 长连接模式**（免公网，本地开发友好）；ByteFaaS webhook 截图为误发，不采纳。
- 先只做**纯文本**收发；不做流式卡片、图片、语音、文件（后续再扩展）。
- CLI 改造成一个 channel，与飞书并存。
- `.env` 新增 `CHANNELS=cli,feishu`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`。
- channel 层**仅离线测试**（不跑真实飞书联网、不连真实 websocket、不需安装 lark 才能跑测试）。
- 并发用例用 `threading.Barrier` / `threading.Event` 做确定性同步，配超时兜底，避免 sleep 造成的 flaky。

---

## 2. 拟建目录结构

```
my-agent/
├── channel/
│   ├── __init__.py
│   ├── message.py            # IncomingMessage：统一入站消息
│   ├── reply.py              # Reply：统一出站回复（先只 TEXT）
│   ├── base.py               # Channel 抽象基类
│   ├── chat_channel.py       # ChatChannel：produce/consume/_handle 调度逻辑
│   ├── factory.py            # create_channel(channel_type, agent, settings)
│   ├── manager.py            # ChannelManager：构建/启动/优雅关停各通道
│   ├── cli/
│   │   ├── __init__.py
│   │   └── cli_channel.py    # CLIChannel：stdin 循环 + 命令分发 + stdout 输出
│   └── feishu/
│       ├── __init__.py
│       └── feishu_channel.py # FeishuChannel：lark websocket 长连接（lark 懒加载）
└── main.py                   # 改造为 ChannelManager 启动器
```

---

## 3. 组件契约（测试靶子）

### 3.1 channel/message.py — IncomingMessage

统一入站消息的数据类（`@dataclass`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `channel_type` | str | 来源通道，如 `cli` / `feishu` |
| `user_id` | str | 发送者 id（飞书为 open_id；CLI 为 `local`） |
| `content` | str | 文本内容 |
| `msg_id` | str | 消息唯一 id（幂等用；CLI 可用自增或 uuid） |
| `is_group` | bool | 是否群消息（默认 False） |
| `session_id` | str（property/构造派生） | `f"{channel_type}:{group_id or user_id}"` |
| `raw` | dict | 原始事件（飞书 event dict；CLI 为 `{}`） |

行为契约：
- 提供 `session_id` 派生逻辑（p2p 用 user_id，group 用 group/chat_id）。
- 字段默认值合理（`is_group=False`、`raw={}`）。

### 3.2 channel/reply.py — Reply

先只做文本：`Reply(content: str)`（预留 `type` 字段但当前恒为 TEXT）。

### 3.3 channel/base.py — Channel 抽象基类

```
class Channel:
    channel_type: str = ""
    def startup(self) -> None: ...     # 启动监听（子类实现）；抽象
    def send(self, reply, message) -> None: ...  # 发送回复；抽象
    def stop(self) -> None: ...        # 优雅关停；默认 no-op
```

### 3.4 channel/chat_channel.py — ChatChannel（核心调度）

继承 Channel，实现与具体通道无关的调度：

- `__init__(self, agent, settings, executor=None, poll_interval=0.05)`：持有共享 `agent`；实例级 `self.sessions = {}`、`self.lock`；`executor` 允许注入（测试用），默认共享线程池；`poll_interval` 允许调小加速测试。
- `produce(message: IncomingMessage)`：按 `message.session_id` 入队；无队列则建 `[deque(), BoundedSemaphore(1)]`。
- `consume()`：daemon 循环；对每个 session 用 `semaphore.acquire(blocking=False)` 取信号量后 `executor.submit(self._handle, message)`，done 回调里 `semaphore.release()`。
- `_handle(message)`：构造 `RunContext.create(session_id=message.session_id, workspace=settings.workspace, entrypoint=message.channel_type, channel_type=message.channel_type, user_id=message.user_id)`；调 `answer, trace = agent.run(message.content, context)`；`reply = Reply(answer)`；`self.send(reply, message)`。异常兜底：捕获后 `send(Reply(错误文案))`，且保证 `semaphore` 释放（不死锁）。
- `start_consuming()`：起 daemon 线程跑 `consume()`。

为可测性：`_handle` 是可单独调用的纯函数（不依赖线程）；`consume` 的轮询间隔可注入。

### 3.5 channel/factory.py

`create_channel(channel_type, agent, settings) -> Channel`：
- `"cli"` → `CLIChannel`
- `"feishu"` → `FeishuChannel`
- 未知 → `ValueError`
- 飞书的 lark import 延迟到 `FeishuChannel.startup()`，工厂本身不 import lark。

### 3.6 channel/manager.py — ChannelManager

- `__init__(self, settings)`：构造共享 `Agent(settings)`。
- `build_channels()`：读 `settings.channels`（list），逐个 `create_channel`。
- `run()`：对每个 channel 起线程跑 `startup()`；主线程等待（或让 CLI channel 占用主线程）。
- `shutdown()`：对每个 channel 调 `stop()`；最后 `agent.memory.close(wait=True)`。

### 3.7 channel/cli/cli_channel.py — CLIChannel

- `channel_type = "cli"`。
- `startup()`：`while True: input()` 循环；把每行交给 `_dispatch(line)`。
- `_dispatch(line)`（拆成可单测的纯函数）：处理 `exit/quit`（返回停止信号）、`/trace`、`/dream`、空行；普通输入 → `produce(IncomingMessage(channel_type="cli", user_id="local", content=line, msg_id=<uuid>))`。
- `send(reply, message)`：`print(reply.content)` 到 stdout。

### 3.8 channel/feishu/feishu_channel.py — FeishuChannel

- `channel_type = "feishu"`。
- `startup()`：懒 import lark；用 `EventDispatcherHandler` 注册 `p2_im_message_receive_v1`；`lark.ws.Client(app_id, app_secret, event_handler).start()`（阻塞在本线程，由 manager 的线程承载）。
- `_handle_message_event(event: dict)`（核心解析，可单测）：
  - 幂等：`msg_id` 命中 `received_msgs`（`ExpiredDict` 或有界 set）→ 忽略。
  - 过期：`create_time` 距今 > 60s → 忽略。
  - 群聊未 @ bot → 忽略；@ 了才处理。
  - 解析出文本 → 构造 `IncomingMessage`（p2p: user_id=open_id、session_id=`feishu:open_id`；group: session_id=`feishu:chat_id`）→ `produce`。
- `send(reply, message)`：`fetch_access_token()` → POST `im/v1/messages`（p2p 用 `receive_id_type=open_id`，group 用 `chat_id`），`msg_type="text"`，`content=json({"text": ...})`。
- `fetch_access_token()`：POST `auth/v3/tenant_access_token/internal/`，解析 `tenant_access_token`。

lark 与 requests 在测试中全部 mock，不产生真实网络。

---

## 4. 测试矩阵（全部离线；无网络、无真实 LLM、无真实 lark 连接）

统一放 `tests/`，沿用现有 `conftest.py` 的 `FakeLLM` / `settings_factory` / `tmp_path`。新增一个 `FakeAgent`（带可控 Event/Barrier 的 `run()`）用于并发与路由测试。

| ID | 用例 | 组件 | 手法 | 级别 |
|----|------|------|------|------|
| ISO-1 | 两 session 短期历史互不串：A 说"我叫A"，B 问"我是谁"时 model_messages 不含"我叫A"；反向亦然 | Agent + 多 ContextManager | FakeLLM 捕获 messages | **P0** |
| ISO-2 | 同 session 多轮历史连续累积（cm 复用、非每次新建） | Agent | FakeLLM | P0 |
| MSG-1 | IncomingMessage 字段默认值与 session_id 派生（p2p vs group） | message.py | 纯构造 | P0 |
| PROD-1 | produce 把消息投进正确的 per-session 队列；不同 session 建不同队列 | ChatChannel | 直接查 sessions | P0 |
| CONC-1 | 同 session 串行：先投 A 再投 B，B 的 _handle 必须在 A 结束后才开始 | ChatChannel + FakeAgent | Event 门控 + 超时 | **P0** |
| CONC-2 | 跨 session 并行：两 session 各一条，两个 _handle 能同时到达 Barrier(2) | ChatChannel + FakeAgent | Barrier(2) + 超时 | **P0** |
| CONC-3 | 跨通道并行：两个 ChatChannel 各一 session，能同时到达 Barrier(2) | 两个 ChatChannel | Barrier(2) + 超时 | P1 |
| ROUTE-1 | _handle 用正确的 session_id/channel_type/entrypoint 构造 RunContext，且回复经 send() 发出 | ChatChannel + FakeAgent + spy send | 断言参数 | **P0** |
| ERR-1 | agent.run 抛异常 → send 收到错误文案，且信号量被释放（后续同 session 消息仍能处理，不死锁） | ChatChannel + FakeAgent raise | Event + 超时 | P1 |
| FAC-1 | "cli"→CLIChannel、"feishu"→FeishuChannel、未知→ValueError；工厂不 import lark | factory | — | P0 |
| MGR-1 | CHANNELS="cli,feishu" 构建出两个通道，类型正确 | ChannelManager | mock startup | P0 |
| MGR-2 | shutdown 调用每个 channel.stop() 且 agent.memory.close(wait=True) 被调用一次 | ChannelManager | spy | P0 |
| CFG-1 | load_settings 解析 CHANNELS（逗号切分+去空白）、FEISHU_APP_ID/SECRET，及默认值（CHANNELS 默认 ["cli"]） | config | monkeypatch env | P0 |
| CLI-1 | _dispatch：exit/quit→停止；/trace、/dream、空行分别正确分支；普通输入→produce 一条 IncomingMessage | CLIChannel | 拆出的 _dispatch | P1 |
| CLI-2 | send(reply) 打印 reply.content 到 stdout | CLIChannel | capsys | P1 |
| FS-1 | _handle_message_event 解析 p2p 文本事件 → 正确 IncomingMessage（user_id=open_id、session_id=feishu:open_id、content 正确） | FeishuChannel | 构造 event dict | **P0** |
| FS-2 | 幂等：同 msg_id 第二次调用被忽略（不产生第二条 produce） | FeishuChannel | spy produce | P0 |
| FS-3 | 群聊未 @ 忽略；@ 了才处理；group session_id=feishu:chat_id | FeishuChannel | 构造 event dict | P0 |
| FS-4 | 过期消息（create_time 早于 now-60s）被过滤 | FeishuChannel | 构造 event dict | P1 |
| FS-5 | send() 构造正确的 URL / headers(Bearer) / payload(receive_id, msg_type=text, content json) | FeishuChannel | mock requests.post | **P0** |
| FS-6 | fetch_access_token 从 mock 响应解析出 token；失败返回空 | FeishuChannel | mock requests.post | P0 |

**明确不测**：真实 lark websocket 连接、真实飞书网络往返、流式卡片、图片/语音/文件、多进程压测。

---

## 5. 并发测试手法（确定性，非 sleep）

`FakeAgent.run(content, context)` 内部：
- **门控模式（CONC-1 / ERR-1）**：持有一个 `threading.Event`；`run` 进入后记录时间戳/顺序，然后 `event.wait(timeout=T)`。测试通过控制 `set()` 时机断言串行顺序。
- **会合模式（CONC-2 / CONC-3）**：持有 `threading.Barrier(2)`；`run` 进入后 `barrier.wait(timeout=T)`。若两个 handler 无法同时到达，`BrokenBarrierError`/超时 → 测试失败。这样"并行"被确定性地证明，而不是靠 sleep 猜测。
- 所有等待都带**超时兜底**（如 2s），超时即判失败，避免测试卡死。

---

## 6. 改动 blast radius

| 文件 | 改动 | 风险 |
|------|------|------|
| `agent/agent.py` | 单 cm → dict[session_id, cm] + Lock + 懒创建；run 用局部 cm | 中；有现成 test_agent_loop 回归护栏 |
| `agent/context_manager.py` | **不动** | 无 |
| `config.py` | Settings 加 `channels/feishu_app_id/feishu_app_secret`；load_settings 解析 | 低 |
| `main.py` | 改为 ChannelManager 启动器 | 低（无覆盖，本就是入口） |
| `channel/**` | 新增 | 新代码 |
| `tests/test_context_manager.py` | **不动** | 无 |
| `tests/test_agent_loop.py` | 可能补 ISO-2 断言，原用例不改 | 低 |

---

## 7. 运行方式

```bash
# 全部离线测试（含新增 channel 测试）
python3 -m pytest

# 覆盖率
python3 -m pytest -q --cov=. --cov-report=term-missing --cov-report=html:htmlcov
```

channel 测试不新增 live 标记（本层不联网）。CLI→真实 Agent→DeepSeek 的端到端已由既有 `test_live_agent_e2e.py` 覆盖，不重复。

---

## 8. 通过标准

- 全部 channel 离线用例通过；既有 113 passed 回归不破。
- `agent/agent.py` 改造后 `test_agent_loop.py` 仍绿。
- ISO-1/ISO-2 证明短期历史 session 隔离；CONC-1/2/3 确定性证明串行/并行模型。
- 覆盖率不低于既有水平（channel 层目标 ≥ 85%，排除真实 websocket/网络分支）。
- 不产生真实 `data/`、`workspace/` 写入残留，不发起真实网络请求。

---

## 9. 执行顺序（认可后）

1. `config.py`：加 channels/feishu 配置 + 解析（先让 CFG-1 可测）。
2. `channel/message.py` + `reply.py`（MSG-1）。
3. `agent/agent.py`：ContextManager per-session 隔离（ISO-1/ISO-2）。
4. `channel/base.py` + `chat_channel.py`（PROD-1/CONC-1/2/ROUTE-1/ERR-1）。
5. `channel/cli/cli_channel.py`（CLI-1/2）。
6. `channel/feishu/feishu_channel.py`（FS-1~6）。
7. `channel/factory.py` + `manager.py`（FAC-1/MGR-1/2/CONC-3）。
8. `main.py` 改造为 ChannelManager 启动器。
9. 跑全量测试 + 覆盖率，回归既有用例。

---

## 10. 交付结果（已完成）

- 全部 9 步实现完成，`context_manager.py` 与 `test_context_manager.py` 零改动（如设计承诺）。
- 测试：**158 passed, 2 skipped**（新增 38 个 channel 用例，既有 113 全绿回归不破）。
- 覆盖率：**总计 94%**（较改造前 91% 提升）。channel 层：`chat_channel.py` 97%、`cli_channel.py` 100%、`factory/message/reply` 100%、`feishu_channel.py` 89%、`manager.py` 86%。
- 未覆盖行均为设计明确排除的真实网络/交互入口（websocket `startup`、stdin 循环、网络异常 `except` 分支），已用 `# pragma: no cover` 如实标注。
- 并发用例全部走 `threading.Barrier`/`Event` + 超时，确定性证明"同 session 串行、跨 session/跨通道并行"，无 sleep-based flaky。
- 测试全程使用 `tmp_path`，未对真实 `data/`、`workspace/` 产生写入残留，未发起真实网络请求。

新增测试文件：
- `tests/test_channel_message.py`（MSG-1）
- `tests/test_channel_config.py`（CFG-1）
- `tests/test_channel_isolation.py`（ISO-1/ISO-2）
- `tests/test_channel_chat_channel.py`（PROD-1/CONC-1/2/ROUTE-1/ERR-1）
- `tests/test_channel_cli.py`（CLI-1/2）
- `tests/test_channel_feishu.py`（FS-1~6 + 守卫分支）
- `tests/test_channel_manager.py`（FAC-1/MGR-1/2/CONC-3 + run 生命周期）

> 说明：飞书 websocket 真实连接需安装 `lark-oapi` 且在飞书开放平台配置应用；本轮测试不依赖 lark 安装即可全绿（lark 懒加载 + 全程 mock）。真实联调需另行提供 `FEISHU_APP_ID/SECRET` 并安装 SDK。
