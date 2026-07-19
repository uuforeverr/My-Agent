# Channel 通道模块设计说明书（面向小白的设计师口述版）

> 这份文档假设我是 channel 模块的设计师，正坐在你对面，用大白话把整个设计讲给你听。
> 目标：读完能在脑子里画出一整幅设计图——数据怎么流、为什么要多线程、排队和并发怎么实现、类怎么一层层继承下来。
> 对应代码：`my-agent/channel/` 目录。

---

## 第 0 章：先想清楚，channel 到底解决什么问题

我们的 agent 已经有了"大脑"——记忆、大模型、工具。但大脑装在盒子里，它**听不见外面的人说话，也没法把想法说出去**。

channel 就是我给这个大脑装的**耳朵和嘴巴**：

- 耳朵：从终端、飞书……把用户的话**收**进来。
- 嘴巴：把大脑想出来的回答**发**回终端、飞书……

听起来简单，对吧？但魔鬼在细节里。我在设计时反复被三个问题拷问：

1. 飞书和终端长得完全不一样（一个走网络长连接+JSON，一个走键盘+屏幕），我不想让大脑去操心"这条消息到底从哪来的"。**怎么屏蔽差异？**
2. 同一时间可能好几个人在飞书找机器人聊天，还有人在终端敲字。**怎么让他们互不干扰、又不用排长队干等？**
3. 网络会断、电脑会睡觉。**断了怎么自己爬起来？**

这份文档，就是我一步步回答这三个问题的过程。

---

## 第 1 章：统一信封——先把"消息长什么样"定死

### 我的第一个决定

我不想让大脑知道"飞书"或"终端"这些词。大脑只该看到**一封标准格式的信**。所以我先设计两个"信封"：

- **`IncomingMessage`**：进来的信（用户说的话）
- **`Reply`**：出去的信（大脑的回答）

不管消息从哪个渠道来，进门第一件事，就是被翻译成 `IncomingMessage` 这个统一格式。大脑只跟它打交道。将来加微信、加钉钉，大脑一行都不用改——**这就是"统一信封"的威力**。

### 进来的信封长这样（message.py）

```python
@dataclass
class IncomingMessage:
    channel_type: str      # 哪个渠道："cli" / "feishu"
    user_id: str           # 谁发的
    content: str           # 说了什么
    msg_id: str            # 这条消息的唯一编号（用来防重复）
    is_group: bool = False # 是不是群消息
    group_id: Optional[str] = None  # 群编号
    raw: dict = field(default_factory=dict)  # 原始数据，留底备查
```

这里有一个**全局最重要的字段**，它不是直接存的，而是算出来的：

```python
    @property
    def session_id(self) -> str:
        scope = self.group_id if self.is_group and self.group_id else self.user_id
        return f"{self.channel_type}:{scope}"
```

**`session_id`（会话 id）是整个 channel 模块的灵魂钥匙。** 记住这句话，后面所有的排队、隔离、并发，全都围着它转。

它的规则很朴素：
- 私聊：`session_id = "feishu:某个人"`（一个人 = 一个会话）
- 群聊：`session_id = "feishu:某个群"`（整个群 = 一个会话，群里所有人共享上下文）
- 终端：`session_id = "cli:local"`

你可以把 session_id 理解成**"一条独立的对话线"**。飞书上张三和你的对话是一条线，李四和你的对话是另一条线，终端是第三条线。这些线**彼此独立**——这是我后面做"隔离"和"并发"的基础。

> 🌱 小白记忆点：`session_id` = 一条独立对话线的身份证。

### 出去的信封（reply.py）

```python
@dataclass
class Reply:
    content: str
    type: str = "text"
```

极简。回答就是一段文本。`type` 先留着，将来支持图片语音时再用。

---

## 第 2 章：分层——我不想把同样的逻辑写两遍

### 我遇到的重复

飞书和终端，**收发方式**天差地别：
- 飞书：连 websocket、调 HTTP 接口、要处理去重和 @。
- 终端：`input()` 读键盘、`print()` 打屏幕。

但它们**中间那段逻辑一模一样**：消息来了要排队、要防止同一个人的消息串掉、要交给大脑、大脑回答后要发回去。

如果我给飞书写一遍排队逻辑，再给终端写一遍……以后加微信又写一遍。三份几乎相同的复杂代码，改一个 bug 要改三处。**我绝不接受。**

### 我的解法：切成两层，用"继承"粘起来

我把 channel 切成上下两层：

```
        Channel（抽象基类）—— 定合同：你必须会 startup / send / stop
            │
        ChatChannel（通用调度层）—— 把"排队、并发、交给大脑"写一次，所有渠道共享
          ╱      ╲
  CLIChannel    FeishuChannel —— 只补自己那点"怎么收、怎么发"
```

这就是**类的继承关系**，从上到下三层：

**第一层 `Channel`（base.py）——一纸合同**

```python
class Channel:
    channel_type: str = ""

    def startup(self) -> None:
        raise NotImplementedError   # 子类必须实现，否则一调用就报错

    def send(self, reply, message) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        pass                        # 默认啥都不做，需要的子类再重写
```

它不干活，只规定"**任何渠道都必须会三件事**：启动监听(startup)、发消息(send)、关停(stop)"。`raise NotImplementedError` 是我埋的"强制条款"——子类要是忘了实现 send，一用就报错提醒。

**第二层 `ChatChannel`（chat_channel.py）——通用调度中枢**

这是整个模块的心脏。**排队、并发、把消息交给大脑**——这些所有渠道都一样的逻辑，我只在这里写一次。（第 3、4 章专门讲它。）

**第三层 `CLIChannel` / `FeishuChannel`——只管收发**

它们继承 `ChatChannel`，所以**天生就拥有全套排队并发能力**，自己只需要补两个洞：怎么把消息收进来、怎么把回答发出去。

> 🌱 小白记忆点：合同（Channel）定"必须会什么"→ 中枢（ChatChannel）写"大家共用的复杂逻辑"→ 具体渠道只填"自己独有的收发细节"。越往下越具体。

---

## 第 3 章：排队——用"一把钥匙"锁住每条对话线

### 我最怕的场景

想象飞书上一个人**手速很快，连发三条**："帮我看下 a.py" → "算了看 b.py" → "还是看 a.py 吧"。

如果我三条同时丢给大脑处理，三个处理过程会**同时读写同一条对话线的记忆和上下文**——回答会串味、记忆会写乱。就像三个人同时在一张纸上写字。

所以我定下铁律：**同一条对话线（同一个 session_id）的消息，必须一条处理完再处理下一条（串行）。**

### 怎么实现"串行"？我用了信号量当钥匙

先看数据结构。在 `ChatChannel` 里，我给**每条对话线**都配了一套装备：

```python
self.sessions: dict[str, list] = {}   # key 是 session_id，value 是 [队列, 钥匙]
```

每个 session 对应一个 `[deque(), BoundedSemaphore(1)]`：
- `deque()`：这条线的**消息排队队列**。
- `BoundedSemaphore(1)`：**一把钥匙**（初始只有 1 把）。

**消息进来时（produce，生产者）：**

```python
def produce(self, message: IncomingMessage) -> None:
    session_id = message.session_id
    with self.lock:
        if session_id not in self.sessions:
            # 这条对话线第一次出现，给它配一套：空队列 + 一把钥匙
            self.sessions[session_id] = [deque(), threading.BoundedSemaphore(1)]
        self.sessions[session_id][0].append(message)  # 消息排到队尾
```

生产者只干一件事：把消息**塞进对应对话线的队列尾部**。就这么简单。

**关键在"钥匙"怎么用**（下一章的 consume 里）：谁想处理某条对话线的消息，**必须先拿到那条线的钥匙**。因为只有 1 把：
- 张三的第一条消息处理时，钥匙被拿走了。
- 张三的第二条消息想处理？拿不到钥匙，只能等。
- 第一条处理完，钥匙还回来，第二条才能拿到钥匙开始处理。

**"只有一把钥匙" = 同一条线永远只有一条消息在处理 = 串行。** 这就是我实现串行的全部秘密。

> 🌱 小白记忆点：每条对话线一把钥匙，钥匙只有一把 → 天然排队。

### 那把 `self.lock` 又是什么？别搞混

注意 `produce` 里还有个 `with self.lock:`。这跟"钥匙"是**两码事**，别混淆：

- **钥匙（每 session 一个信号量）**：管"业务层面的排队"——保证同一对话线消息串行。
- **`self.lock`（全局一把锁）**：管"数据结构层面的安全"——因为多个线程会同时读写 `self.sessions` 这个字典，不加锁字典会被改乱。它只保护"改字典"这一瞬间。

一个是"排队叫号"，一个是"改台账时别人别插手"。

---

## 第 4 章：并发——用"线程池"让不同对话线同时干活

### 串行解决了，但又冒出新问题

我规定了"同一条线串行"。可如果我把这规矩用错地方，变成"**所有消息都串行**"，那就惨了：张三在飞书聊，李四得等张三聊完才能被理睬，终端的你还得再等。一个大模型回答要几秒，排队能排到天荒地老。

所以第二条铁律：**不同对话线之间，必须能同时处理（并行）。**

张三、李四、终端的你——三条独立的线，凭什么互相等？该三个大模型请求同时飞出去。

### 怎么实现"并行"？线程池 + 一个不停巡逻的消费者

我请了一队"工人"——**线程池**（`ThreadPoolExecutor`，默认 8 个工人）。谁有活，就分给一个空闲工人去干，工人们各干各的，互不影响。

然后我安排了一个**不知疲倦的巡逻员**——`consume`（消费者循环），它在后台线程里一圈一圈地转：

```python
def consume(self) -> None:
    while self._running:
        with self.lock:
            session_ids = list(self.sessions.keys())   # 拷一份所有对话线的名单
        for session_id in session_ids:                  # 挨个巡视
            with self.lock:
                entry = self.sessions.get(session_id)
            if entry is None:
                continue
            queue, semaphore = entry
            if semaphore.acquire(blocking=False):        # ① 试着拿这条线的钥匙
                message = None
                with self.lock:
                    if queue:
                        message = queue.popleft()        # ② 拿到钥匙，取出一条消息
                if message is not None:
                    future = self.executor.submit(self._handle, message)  # ③ 扔给工人
                    future.add_done_callback(self._make_done_callback(session_id))  # ④ 登记"干完还钥匙"
                else:
                    semaphore.release()                  # 队列空了，钥匙还回去
        time.sleep(self.poll_interval)                   # 睡 0.05 秒，别烧 CPU
```

我带你走一遍巡逻员的心路历程，盯紧带圈的四步：

- **① 试着拿钥匙**：`acquire(blocking=False)` 是"试一下，拿不到立刻走人，绝不傻等"。
  - 拿到了 → 这条线现在空闲，可以处理。
  - 没拿到 → 这条线**正有消息在处理中**（钥匙在别人手里），跳过，去看下一条线。**这一步就实现了"同线串行"。**
- **② 取消息**：拿到钥匙，从队列头取出一条待处理的消息。
- **③ 扔给工人**：`executor.submit(self._handle, message)` 把"处理这条消息"这个活**扔进线程池就立刻返回**，不等它干完！巡逻员马上去巡视下一条线。于是张三的活刚扔出去，李四的活紧接着也能扔出去——**两个工人同时干，这就是"跨线并行"。**
- **④ 登记还钥匙**：给这个活挂一个"干完之后自动执行"的回调，负责把钥匙还回来。

**串行和并行，在这一段里同时成立**，靠的就是：
- 钥匙只有一把 → 同一条线串行。
- 处理动作 `submit` 给线程池、且不等待 → 不同线并行。

### 干完一定要还钥匙——否则死锁

```python
def _make_done_callback(self, session_id: str):
    def _callback(_future) -> None:
        with self.lock:
            entry = self.sessions.get(session_id)
        if entry is not None:
            entry[1].release()   # entry[1] 是钥匙，还回去
    return _callback
```

这是个"**返回函数的函数**"（专业名叫闭包）。为什么要套一层？因为线程池干完活时，只会把结果对象递给回调，**不会告诉你这是哪条线的活**。我用外层函数提前把 `session_id` "记"进去，回调触发时就知道该还哪把钥匙。

**为什么还钥匙要放在"干完的回调"里，而不是处理逻辑的结尾？** 因为回调**无论成功还是报错都会触发**。哪怕大模型处理时崩了，钥匙照样能还回来。要是钥匙没还，这条对话线就永远拿不到钥匙、永远卡死了——这叫死锁，是我最想避免的。

> 🌱 小白记忆点：巡逻员不停转圈，看到"有消息 + 钥匙空着"就派个工人去干、干完自动还钥匙。钥匙管串行，工人管并行。

### 大脑在哪一步被调用？——_handle

工人真正执行的活是 `_handle`，这是 channel 和大脑的**接缝**：

```python
def _handle(self, message: IncomingMessage) -> None:
    context = RunContext.create(               # 打包一次运行的上下文
        session_id=message.session_id,          # 带上会话身份证——大脑靠它区分记忆
        workspace=self.settings.workspace,
        entrypoint=message.channel_type,
        channel_type=message.channel_type,
        user_id=message.user_id,
    )
    try:
        answer, trace = self.agent.run(message.content, context=context)  # 交给大脑思考
        self.last_trace = trace
        self.send(Reply(answer), message)       # 把回答装信封，发回去
    except Exception as e:
        self.send(Reply(f"ERROR: {type(e).__name__}: {e}"), message)  # 出错也别崩，告诉用户
```

三步：**打包上下文 → 交给大脑 → 把回答发出去**。注意最后调的 `self.send` 是抽象方法——具体怎么发，交给 CLI 或飞书子类去实现。通用层只管"该发了"，不管"怎么发"。这就是第 2 章分层设计的回报。

---

## 第 5 章：为什么每个渠道要各开一个线程启动？

### 一个绕不过的坎：startup 是"卡住不返回"的

到这里你可能会问：既然有了并发，为什么 manager 启动渠道时还要**每个渠道单开一个线程**？

因为每个渠道的 `startup()` 都是**阻塞式**的——它一旦开始，就卡在那儿不返回：
- 终端的 `startup`：卡在 `while True: input()`，永远等你敲下一行字。
- 飞书的 `startup`：卡在 websocket 连接上，永远等下一条消息推过来。

如果我在主线程里先启动终端，代码就**永远卡在终端的输入循环里，根本执行不到"启动飞书"那一行**。飞书永远起不来。

### 解法：一个渠道一个线程

```python
# manager.py
def run(self) -> None:
    if not self.channels:
        self.build_channels()
    threads = []
    for channel in self.channels:
        thread = threading.Thread(
            target=channel.startup,   # 每个渠道的 startup 各自占一个线程
            daemon=True,
            name=f"{channel.channel_type or 'channel'}-startup",
        )
        thread.start()
        threads.append(thread)
    try:
        for thread in threads:
            thread.join()             # 主线程在这里等它们（其实会一直跑）
    except KeyboardInterrupt:
        pass
    finally:
        self.shutdown()               # 无论如何退出，都要收尾
```

每个渠道的 `startup` 各占一个线程，各自去"卡"自己的输入循环，互不挡道。终端在它的线程里等键盘，飞书在它的线程里等网络，**两者同时运行**。这就是"不同渠道并行"的实现——**在启动这一层**。

> 🌱 小白记忆点：startup 会"卡住不返回"，所以每个渠道得有自己的线程去卡，否则第一个渠道就把后面的堵死了。

### 注意这里有"两层并行"，别搞混

我特意点出来，很多人会晕：

1. **渠道级并行**（第 5 章，manager 里）：终端 vs 飞书，各一个 startup 线程。
2. **会话级并行**（第 4 章，chat_channel 里）：飞书内部，张三 vs 李四，各一个线程池工人。

一个管"不同渠道同时在线"，一个管"同渠道内不同人的消息同时处理"。两层加起来，才是完整的并发能力。

---

## 第 6 章：健壮性——网络断了，自己爬起来（飞书专属）

### 现实很骨感

飞书走 websocket 长连接。长连接**一定会断**：网络抖一下、你的 Mac 合盖睡觉、飞书服务端定期回收连接……都会断。你会在日志看到 `keepalive ping timeout` 之类的报错。

我的设计哲学是：**不追求"永不断线"（做不到），而是追求"断了能自动重连"。**

### 解法：监督循环 + 指数退避

```python
# feishu_channel.py 的 startup 里
backoff = RECONNECT_MIN_BACKOFF          # 从 1 秒起步
while self._supervise:                    # 只要没被叫停，就一直守着
    try:
        self._install_fresh_event_loop()  # ★ 关键：换一个全新的事件循环
        self._ws_client = lark.ws.Client(...)
        self._ws_client.start()           # 连上并卡在这里收消息
    except Exception:
        pass                              # 断了？没关系，往下走
    if not self._supervise:
        break
    time.sleep(backoff)                   # 等一会再重连
    backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)  # 1→2→4→…→最多30秒
```

- `start()` 正常时会一直卡着收消息。一旦断线，它返回或抛错 → 被 `except` 兜住 → 往下走。
- 等一会（`backoff` 秒）再回到循环顶部**重新连接**。
- **指数退避**：第一次等 1 秒，还连不上就等 2 秒、4 秒……最多 30 秒。为什么要逐渐拉长？因为如果网络还没恢复，疯狂每秒重试只会浪费资源、还可能被服务端拉黑。越等越久，既省力又礼貌。

### 一个血泪教训：那行 `_install_fresh_event_loop`

这行是我踩了坑才加的，特别值得讲给你听：

飞书 SDK 内部把 asyncio 的"事件循环"（异步任务的发动机）存成了一个**全局变量**。连接断掉后，那个发动机也**报废**了。如果我直接拿旧发动机重连，会立刻报 "Event loop is closed"——**看起来在重连，其实每次都秒失败**，机器人永远活不过来。

所以每次重连前，我都**造一个全新的发动机换上去**：

```python
@staticmethod
def _install_fresh_event_loop() -> None:
    import asyncio
    loop = asyncio.new_event_loop()          # 造新发动机
    asyncio.set_event_loop(loop)
    try:
        import lark_oapi.ws.client as _mod
        _mod.loop = loop                     # 塞回 SDK 的全局变量
    except Exception:
        pass
```

> 🌱 小白记忆点：断线重连不难，难的是"重连前必须换个全新的发动机"，否则重连只是假动作。

### 光靠代码还不够：Mac 睡觉了谁都救不了

补一句设计边界：代码能在"断线"后自愈，但如果**整台 Mac 睡着了**，Python 进程被系统挂起，连自愈的循环都停摆。所以真要 7×24，得配合系统层面别让电脑睡（比如用 `caffeinate` 启动），或者干脆把 agent 放在一台常开的机器上跑。这不是代码能解决的，是部署问题——好的设计师要能分清"哪些归代码管，哪些归环境管"。

---

## 第 7 章：飞书收消息的"闯关"设计

飞书把消息推给我们后，不是照单全收，而是要**层层闯关**。我按"越便宜的检查越靠前"排序，能早丢就早丢：

```python
def _handle_message_event(self, event: dict) -> None:
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    if not message or not sender:            # 关0：结构不完整 → 丢
        return
    msg_id = message.get("message_id")
    if not msg_id or self._is_duplicate(msg_id):  # 关1：重复消息 → 丢（查字典，最便宜）
        return
    if self._is_stale(message.get("create_time")): # 关2：太老的消息 → 丢（比个时间）
        return
    if message.get("message_type") != "text":      # 关3：非文本 → 丢
        return
    chat_type = message.get("chat_type")
    is_group = chat_type == "group"
    if is_group and not self._is_mention_bot(message.get("mentions")):  # 关4：群里没@我 → 丢
        return
    content = self._extract_text(message.get("content"))
    if not content:                                # 关5：内容空 → 丢
        return
    open_id = (sender.get("sender_id") or {}).get("open_id") or ""
    if not open_id:                                # 关6：不知道谁发的 → 丢
        return
    self.produce(IncomingMessage(...))             # 全部通过 → 入队
```

每一关都是我踩过或预判的坑：
- **去重**（关1）：飞书偶尔会把同一条消息推两次，不去重就会回答两遍。
- **过期**（关2）：重启后飞书会补推离线期间堆积的老消息，不拦就会被几十条旧消息刷屏。
- **@ 判断**（关4）：开了"读取群里所有消息"权限后，飞书把群里**所有**消息都推来（包括"张三@李四"）。我必须精确判断"@ 的到底是不是机器人本人"，否则机器人会在群里乱插嘴。

其中"@ 判断"的精确实现值得一看：

```python
def _is_mention_bot(self, mentions) -> bool:
    if not mentions:
        return False                            # 没@任何人 → 不理
    if self._bot_open_id:                       # 知道我自己的id
        return any(
            (m.get("id") or {}).get("open_id") == self._bot_open_id  # @里有没有我
            for m in mentions
        )
    return True                                 # 兜底：查不到我的id，就当@我了，别哑火
```

为了做这个判断，我在 `startup` 时先调飞书接口查到"机器人自己的 open_id"存起来（`_fetch_bot_open_id`），这样才能拿群消息里被 @ 的 id 跟"我"比对。

> 🌱 小白记忆点：飞书消息要过七道关，便宜的检查排前面，能早丢早丢。@判断要比对"是不是@我本人"，不能只看"有没有@"。

---

## 第 8 章：怎么发出去 & 怎么优雅关门

### 发消息：各渠道自己实现 send

终端版：

```python
# cli_channel.py
def send(self, reply, message) -> None:
    print(f"\n{reply.content}")     # 打屏幕，就这么朴素
```

飞书版（要调 HTTP 接口）：

```python
# feishu_channel.py
def send(self, reply, message) -> None:
    access_token = self.fetch_access_token()   # 先拿令牌（飞书所有接口都要）
    if not access_token:
        return
    # 群聊发到群，私聊发给人
    if message.is_group and message.group_id:
        receive_id, receive_id_type = message.group_id, "chat_id"
    else:
        receive_id, receive_id_type = message.user_id, "open_id"
    payload = {"receive_id": receive_id, "msg_type": "text",
               "content": json.dumps({"text": reply.content}, ensure_ascii=False)}
    # 用 Python 自带的 urllib 发 POST，不依赖第三方库
    ...
```

同一个 `send`，两个渠道两种实现。通用层 `_handle` 里那句 `self.send(...)` 会自动调到正确的那个——这就是继承 + 抽象方法带来的好处：**调用的人不用关心自己面对的是终端还是飞书**。

### 关门：manager 统一收尾

```python
# manager.py
def shutdown(self) -> None:
    for channel in self.channels:
        try:
            channel.stop()          # 逐个通知渠道关停
        except Exception:
            pass                     # 一个关失败不影响其他
    self.agent.memory.close(wait=True)  # ★ 最后关记忆，wait=True 等没写完的记忆落盘
```

关停的顺序很讲究：**先关渠道，最后关记忆，而且要等记忆写完**。因为记忆是异步在后台写的，如果不等就退出，可能丢掉最后几条还没落盘的记忆。`run()` 里我用 `finally` 保证——**无论是正常退出还是 Ctrl+C，shutdown 一定会执行**。

---

## 第 9 章：把整幅图在脑子里串起来

现在闭上眼，跟我走一遍飞书消息的完整旅程：

```
①用户在飞书发"你好"
        │
        ▼
②飞书服务器通过 websocket 把事件推给 FeishuChannel
   （websocket 连接由"监督循环"维持，断了会自动重连）
        │
        ▼
③_handle_message_event 层层闯关（去重/过期/类型/@/内容/发送者）
        │  通过
        ▼
④包成统一信封 IncomingMessage → produce 塞进"feishu:张三"这条线的队列
        │
        ▼
⑤后台巡逻员 consume 转到这条线：发现有消息 + 钥匙空着
   → 拿钥匙 → 把 _handle 这个活扔给线程池的某个工人
   （与此同时，李四那条线的活也能扔给另一个工人——并行！）
        │
        ▼
⑥工人执行 _handle：打包上下文 → agent.run("你好") 大脑思考出答案
        │
        ▼
⑦_handle 调 FeishuChannel.send → 通过 HTTP 接口把答案发回飞书 → 用户看到回复
        │
        ▼
⑧这个活干完 → 回调自动触发 → 把"feishu:张三"的钥匙还回去
   → 张三的下一条消息现在可以处理了（串行）
```

终端的旅程几乎一样，只有①②（键盘代替 websocket）和⑦（print 代替 HTTP）不同——**中间③到⑧全在共享的 ChatChannel 里，一份代码，两个渠道复用。**

---

## 第 10 章：一句话记住每个设计

| 设计点 | 一句话 |
|--------|--------|
| **统一信封** | 用 IncomingMessage / Reply 屏蔽渠道差异，大脑只认标准信封 |
| **session_id** | 一条独立对话线的身份证，隔离与并发都围着它转 |
| **三层继承** | Channel（定合同）→ ChatChannel（写共用调度）→ CLI/Feishu（只补收发） |
| **排队（钥匙）** | 每条线一把信号量钥匙，只有一把 → 同线串行 |
| **并发（线程池）** | 处理动作 submit 给线程池且不等待 → 跨线并行 |
| **还钥匙用回调** | 挂在"干完回调"上，成功失败都还，杜绝死锁 |
| **多线程启动渠道** | startup 会卡住不返回，每渠道一线程才能同时在线 |
| **两层并行** | 渠道级（manager）+ 会话级（chat_channel），别混 |
| **断线自愈** | 监督循环 + 指数退避；重连前必须换新事件循环 |
| **闯关过滤** | 飞书消息七道关，便宜的排前面，能早丢早丢 |
| **优雅关停** | 先关渠道后关记忆，wait=True 等记忆落盘，finally 保证执行 |

**整个 channel 模块的设计精髓，浓缩成一句话：**

> 用统一信封抹平渠道差异，用继承让"排队+并发"的复杂调度只写一次，用"信号量当钥匙 + 线程池"实现"同一对话线串行、不同对话线并行"，再用"监督重连"扛住网络的不可靠——最终让一个大脑，能同时、稳定、不串味地服务终端和飞书上的每一个人。
