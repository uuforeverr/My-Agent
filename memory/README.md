# memory 模块逐行讲解

这份 README 只讲 `memory/` 目录下的代码。目标是让你在读完之后，能对每一行的作用、为什么这样写、以及它和其他文件之间的关系都心里有数。

阅读顺序建议：

```text
1. memory/__init__.py         导出边界
2. memory/raw_store.py        原始对话 raw jsonl
3. memory/episodic_store.py   daily 情景记忆
4. memory/semantic_store.py   MEMORY.md 语义记忆
5. memory/curator.py          LLM 总结与做梦
6. memory/index.py            SQLite/FTS 检索索引
7. memory/manager.py          门面层，编排所有模块
```

一句话概括分层：

```text
raw jsonl        事实来源，完整原文
daily md         情景记忆，事件级摘要
MEMORY.md        语义记忆，长期稳定信息
index.db         检索索引，不是记忆本体
dreams md        做梦整理日志
```

---

## memory/__init__.py

文件职责：定义 `memory` 包对外暴露的三个符号，其他模块只应该用这里导出的名字。

```python
1  from memory.index import MemoryRecord, MemorySearchResult
2  from memory.manager import MemoryManager
3
4  __all__ = ["MemoryManager", "MemoryRecord", "MemorySearchResult"]
```

- 第 1 行：从索引模块导入 `MemoryRecord`（一条记忆的结构）和 `MemorySearchResult`（一次检索命中的结构）。这两个 dataclass 被 tools/agent 使用，所以要从包顶层导出。
- 第 2 行：从门面层导入 `MemoryManager`。外部模块只需要认识 `MemoryManager`，不需要认识底层的 raw/episodic/semantic。
- 第 4 行：`__all__` 明确 `from memory import *` 只会拿到这三个符号。这里既是约束，也是文档。

---

## memory/raw_store.py

文件职责：把每一条 `user/assistant/tool` 消息按 `session_id + 日期` 追加到 JSONL 文件。这是唯一的“原始事实来源”，任何总结、做梦、追溯都可以回到它。

```python
1  import json
2  from datetime import datetime
3  from pathlib import Path
4  from typing import Any, Optional
```

- 第 1 行：`json` 用来把每条消息序列化成一行 JSON。
- 第 2 行：`datetime` 用来生成日期文件名和时间戳。
- 第 3 行：`Path` 用面向对象方式处理路径。
- 第 4 行：`Any` 表示 `content` 可能是字符串，也可能是复杂结构；`Optional` 表示 `name/metadata` 可以为空。

```python
7  class RawConversationStore:
8      def __init__(self, memory_dir: Path):
9          self.raw_dir = memory_dir / "raw"
10         self.raw_dir.mkdir(parents=True, exist_ok=True)
```

- 第 7 行：类名直接表明职责——原始对话仓库。
- 第 8 行：接收 memory 根目录（例如 `data/memory`）。
- 第 9 行：把 `data/memory/raw` 记录成根路径。所有 session 都在这个子目录下继续分文件夹。
- 第 10 行：`parents=True` 保证父目录存在；`exist_ok=True` 避免重启时报错。

```python
12     def append_message(
13         self,
14         session_id: str,
15         role: str,
16         content: Any,
17         name: Optional[str] = None,
18         metadata: Optional[dict[str, Any]] = None,
19     ) -> Path:
```

- 第 12–19 行：向 raw jsonl 追加一条消息。签名和 OpenAI messages 一一对应：`role/content/name`。`metadata` 留给未来记录 `tool_call_id`、`step` 等运行信息，第一版可以传空。

```python
20         path = self._path_for(session_id)
21         path.parent.mkdir(parents=True, exist_ok=True)
```

- 第 20 行：算出这条消息应该落到哪个文件（按 session 和当天日期）。
- 第 21 行：`session_id` 分目录，第一次写入时创建。

```python
22         record = {
23             "created_at": self._now(),
24             "session_id": session_id,
25             "role": role,
26             "content": content,
27             "name": name,
28             "metadata": metadata or {},
29         }
```

- 第 22–29 行：构造一条 JSON 记录。`created_at` 放到最前，方便 `tail -f` 时肉眼阅读；`metadata` 为空时统一存 `{}`，避免下游读取时判断 `None`。

```python
30         with path.open("a", encoding="utf-8") as f:
31             f.write(json.dumps(record, ensure_ascii=False) + "\n")
32         return path
```

- 第 30 行：追加模式打开，UTF-8。
- 第 31 行：`ensure_ascii=False` 保证中文原样保存，末尾加 `\n` 让每条记录独占一行，符合 JSONL 规范。
- 第 32 行：返回写入的文件路径，方便 trace 或调试记录。

```python
34     def read_day(self, session_id: str, day: Optional[str] = None) -> list[dict[str, Any]]:
35         path = self._path_for(session_id, day=day)
36         if not path.exists():
37             return []
```

- 第 34 行：按 session 读取某一天的所有消息。`day=None` 表示读今天。
- 第 35–37 行：文件不存在直接返回空列表，不抛异常。

```python
39         records = []
40         for line in path.read_text(encoding="utf-8").splitlines():
41             if not line.strip():
42                 continue
43             try:
44                 records.append(json.loads(line))
45             except json.JSONDecodeError:
46                 continue
47         return records
```

- 第 39–47 行：逐行解析。空行跳过；解析失败的坏行也跳过，不让一条坏数据把整天记录都读不出来。

```python
49     def _path_for(self, session_id: str, day: Optional[str] = None) -> Path:
50         safe_session_id = self._safe_session_id(session_id)
51         file_day = day or datetime.now().strftime("%Y-%m-%d")
52         return self.raw_dir / safe_session_id / f"{file_day}.jsonl"
```

- 第 49–52 行：路径规则 = `raw/{safe_session_id}/{YYYY-MM-DD}.jsonl`。用户传的 `session_id` 可能带斜杠或空格，所以先转成安全形态；`day=None` 时用今天。

```python
54     @staticmethod
55     def _safe_session_id(session_id: str) -> str:
56         text = (session_id or "default").strip()
57         return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
```

- 第 54–57 行：只保留字母、数字、`-`、`_`。其他字符统一替换为 `_`。既防路径穿越，又让文件名可预测。

```python
59     @staticmethod
60     def _now() -> str:
61         return datetime.now().isoformat(timespec="seconds")
```

- 第 59–61 行：秒级 ISO 时间。集中在一处，方便以后要切成 UTC 或纳秒时只改一个地方。

设计要点：
- raw jsonl 只追加，不修改。这样后续可以随时重跑 daily summary，或者做“重跑历史”的 debug。
- raw 不进入检索索引。它是事实来源，不是记忆运用层，避免污染检索结果。

---

## memory/episodic_store.py

文件职责：写 `data/memory/daily/YYYY-MM-DD.md`，保存事件级情景记忆。它不生产内容，只负责“落盘和读取 daily md”。真正的总结逻辑在 `curator.py`。

```python
1  from datetime import datetime, timedelta
2  from pathlib import Path
3  from typing import Any, Optional
```

- 第 1 行：`timedelta` 用于“往前回溯 N 天”。
- 其余同上。

```python
6  class EpisodicMemoryStore:
7      def __init__(self, memory_dir: Path):
8          self.daily_dir = memory_dir / "daily"
9          self.daily_dir.mkdir(parents=True, exist_ok=True)
```

- 第 6–9 行：daily 记忆都写在 `data/memory/daily/`。构造时确保目录存在。

```python
11     def append_summary(
12         self,
13         summary: Any,
14         reason: str,
15         session_id: str,
16         day: Optional[str] = None,
17     ) -> int:
```

- 第 11–17 行：向当天 daily 文件追加一段摘要。参数含义：
  - `summary`：可能是 LLM 返回的字符串，也可能是解析好的列表。
  - `reason`：这次总结的触发原因，会决定小标题。
  - `session_id`：写在 metadata 一行，方便后续按 session 追溯。
  - `day`：允许显式指定日期，默认写今天。

```python
18         items = self._normalize_items(summary)
19         if not items:
20             return 0
```

- 第 18 行：把 `summary` 归一化成 `list[str]`，一条 bullet 一项。
- 第 19–20 行：如果解析后没有可写内容，直接返回 0，不留下空块。

```python
22         path = self.path_for(day=day, ensure_exists=True)
23         block = [
24             f"## {self._reason_title(reason)} ({datetime.now().strftime('%H:%M')})",
25             "",
26             f"session_id: `{session_id}`",
27             "",
28         ]
29         block.extend(f"- {item}" for item in items)
30         block.append("")
```

- 第 22 行：拿到今天的 daily md，路径不存在时会先写标题。
- 第 23–30 行：拼一段 Markdown 区块：
  - `##` 小标题带原因和时间。
  - `session_id` 显式写出。
  - 每条摘要以 `- ` 开头。
  - 结尾补一个空行，保证下一次追加时块与块之间有空行。

```python
32         with path.open("a", encoding="utf-8") as f:
33             f.write("\n".join(block))
34
35         return len(items)
```

- 第 32–35 行：追加写入 daily md，并返回写入条目数。返回值上游会用来判断“是否需要重建索引”。

```python
37     def read_day(self, day: Optional[str] = None) -> str:
38         path = self.path_for(day=day, ensure_exists=False)
39         if not path.exists():
40             return ""
41         return path.read_text(encoding="utf-8").strip()
```

- 第 37–41 行：读某一天的 daily md。文件不存在返回空字符串，供 curator 用作“已有当日 daily”。

```python
43     def read_recent(self, days: int = 1) -> str:
44         chunks = []
45         for offset in range(max(days, 1)):
46             day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
47             content = self.read_day(day)
48             if content:
49                 chunks.append(content)
50         return "\n\n".join(reversed(chunks))
```

- 第 43–50 行：读最近 N 天的 daily 内容并按“早到晚”拼在一起。做梦时 `deep_dream(lookback_days=7)` 就靠它。
  - `max(days, 1)` 兼容传 0 的错误调用，至少读今天。
  - `reversed(chunks)` 让最早的一天在最上面，模型阅读顺序更自然。

```python
52     def list_files(self) -> list[Path]:
53         return sorted(self.daily_dir.glob("*.md"))
```

- 第 52–53 行：列出所有 daily md 文件，按文件名排序（等于按日期升序）。索引重建会用这个列表。

```python
55     def path_for(self, day: Optional[str] = None, ensure_exists: bool = False) -> Path:
56         file_day = day or datetime.now().strftime("%Y-%m-%d")
57         path = self.daily_dir / f"{file_day}.md"
58         if ensure_exists and not path.exists():
59             path.write_text(f"# Daily Memory: {file_day}\n\n", encoding="utf-8")
60         return path
```

- 第 55–60 行：算 daily 文件路径，可选“文件不存在就写标题”。第一次追加时用 `ensure_exists=True`；纯读的场景用 `False`。

```python
62     def _normalize_items(self, summary: Any) -> list[str]:
63         if summary is None:
64             return []
65         if isinstance(summary, str):
66             raw_items = summary.splitlines()
67         elif isinstance(summary, list):
68             raw_items = [str(item) for item in summary]
69         else:
70             raw_items = [str(summary)]
```

- 第 62–70 行：兼容三种输入：`None`、`str`、`list`。字符串按行拆；其他情况全部先 `str()`。这样上游传什么都能落得下去。

```python
72         items = []
73         for item in raw_items:
74             text = item.strip()
75             if text.startswith("- "):
76                 text = text[2:].strip()
77             if not text or text in {"无", "None"} or text.lower() == "none":
78                 continue
79             items.append(text)
80         return items
```

- 第 72–80 行：清洗每一条：
  - 去掉行首 `- `，避免和后面统一加 `- ` 重复。
  - 空行、`无`、`None`（大小写不敏感）都视为“无内容”跳过。

```python
82     @staticmethod
83     def _reason_title(reason: str) -> str:
84         titles = {
85             "context_trim": "Trimmed Context",
86             "trim": "Trimmed Context",
87             "overflow": "Context Overflow Recovery",
88             "daily_summary": "Daily Summary",
89         }
90         return titles.get(reason, "Session Notes")
```

- 第 82–90 行：不同触发原因对应不同的小标题，方便肉眼阅读 daily md。
  - `context_trim` / `trim`：常规裁剪。
  - `overflow`：因为上下文溢出被迫抢救。
  - `daily_summary`：定时或手动生成日结。
  - 其他统一叫 `Session Notes`。

---

## memory/semantic_store.py

文件职责：读写 `data/memory/MEMORY.md`。这就是语义记忆本体，不是数据库视图。手写 `remember_memory` 和做梦重写都走这里。

```python
1  from datetime import datetime
2  from pathlib import Path
3  from typing import Optional
```

- 三行标准导入。`Optional` 目前没用到，可保留以便扩展。

```python
6  class SemanticMemoryStore:
7      def __init__(self, memory_dir: Path):
8          self.path = memory_dir / "MEMORY.md"
9          self.path.parent.mkdir(parents=True, exist_ok=True)
10         self._ensure_exists()
```

- 第 7–10 行：确定 `MEMORY.md` 位置，确保父目录存在，并调用 `_ensure_exists()` 建骨架。

```python
12     def read(self) -> str:
13         return self.path.read_text(encoding="utf-8").strip()
```

- 第 12–13 行：读全文，去掉首尾空白。因为它每次都会先 `_ensure_exists`，所以直接读不会 `FileNotFoundError`。

```python
15     def remember(self, content: str, source: str = "manual") -> str:
16         text = content.strip()
17         if not text:
18             return "No memory saved: content is empty."
```

- 第 15–18 行：手写一条语义记忆。空内容直接拒绝，返回给工具当结果。

```python
20         current = self.read()
21         if self._contains_memory(current, text):
22             return "Memory already exists."
```

- 第 20–22 行：文本级查重，避免多次点“记住 A”出现重复条目。这里没有走数据库，只在文件里比对，符合“数据库只是索引”的原则。

```python
24         self._ensure_manual_section()
25         line = f"- {text}"
26         if source:
27             line += f"  <!-- source: {source}, at: {self._now()} -->"
28         with self.path.open("a", encoding="utf-8") as f:
29             f.write(line + "\n")
30         return "Semantic memory saved."
```

- 第 24 行：确保存在 `## Manual Notes` 段，所有手写记忆都归到这一段。
- 第 25–27 行：每条一行 bullet。`source` 和时间放在 HTML 注释里，肉眼读不干扰内容，机器可解析。
- 第 28–30 行：追加写入并返回成功信息。

```python
32     def overwrite(self, content: str) -> None:
33         text = content.strip()
34         if not text:
35             text = "# Semantic Memory\n\n"
36         self.path.write_text(text.rstrip() + "\n", encoding="utf-8")
```

- 第 32–36 行：做梦机制会整段重写 `MEMORY.md`，走这里。空内容兜底为初始标题，避免把语义记忆一次性抹空。

```python
38     def _ensure_exists(self) -> None:
39         if self.path.exists():
40             return
41         self.path.write_text(
42             "# Semantic Memory\n\n"
43             "## User Preferences\n\n"
44             "## Project Decisions\n",
45             encoding="utf-8",
46         )
```

- 第 38–46 行：第一次初始化时写入骨架，包含两类最常用的语义记忆分类。让 LLM 一眼就知道该往哪写。

```python
48     def _ensure_manual_section(self) -> None:
49         content = self.read()
50         if "## Manual Notes" in content:
51             if not content.endswith("\n"):
52                 with self.path.open("a", encoding="utf-8") as f:
53                     f.write("\n")
54             return
```

- 第 48–54 行：如果已经有 `## Manual Notes`，只补一个可能缺失的换行。
  - 为什么要补换行？追加写时如果文件不以 `\n` 结尾，下一条 bullet 会紧贴上一行末尾，Markdown 会渲染出错。

```python
56         with self.path.open("a", encoding="utf-8") as f:
57             if content and not content.endswith("\n"):
58                 f.write("\n")
59             f.write("\n## Manual Notes\n\n")
```

- 第 56–59 行：首次追加时先创建 `## Manual Notes` 分组。前面额外一个空行是为了和上一段分开。

```python
61     @staticmethod
62     def _contains_memory(content: str, text: str) -> bool:
63         needle = text.strip().lower()
64         for line in content.splitlines():
65             cleaned = line.strip().lstrip("-").strip()
66             cleaned = cleaned.split("<!--", 1)[0].strip().lower()
67             if cleaned == needle:
68                 return True
69         return False
```

- 第 61–69 行：查重逻辑。
  - 去掉行首 `- `。
  - 去掉 HTML 注释部分（`<!-- source: ... -->`）。
  - 全部转小写比较。
  - 只做“整条相等”的判断，不做模糊匹配，保持谨慎。

```python
71     @staticmethod
72     def _now() -> str:
73         return datetime.now().isoformat(timespec="seconds")
```

- 第 71–73 行：时间格式统一入口。

---

## memory/curator.py

文件职责：这是唯一“调用 LLM”的 memory 模块，负责两件事：
1. `summarize_daily()`：把被裁剪的对话变成 daily 情景记忆的 bullet 列表。
2. `deep_dream()`：读语义记忆和情景记忆，产出新的 `MEMORY.md` 和一段做梦日志。

其他模块（store、index、manager）都不接触 LLM。

### DAILY_SUMMARY_SYSTEM_PROMPT（第 5–26 行）

- 明确“不是复述聊天，是提炼事件”。
- 10 条规则按“做什么/不做什么”组织：
  - 1–7：告诉模型什么值得保留（事件、需求、纠正、决策、路径、错误处理）。
  - 8：禁止编造。
  - 9：工具输出只留关键事实。
  - 10：真的没价值只输出“无”。
- 输出格式统一为 `- ` bullet，行内一句话，方便直接落成 daily md。

### DREAM_SYSTEM_PROMPT（第 29–63 行）

- 把语义记忆和情景记忆作为两份输入。
- 强调 `MEMORY.md` 会被注入到 system prompt，所以必须精炼。
- 输出格式规定 `[MEMORY] ... [DREAM] ...`，`parse_dream_output()` 依赖这个约定。

### class MemoryCurator

```python
66  class MemoryCurator:
67      def __init__(self, llm: Optional[Any] = None):
68          self.llm = llm
```

- 第 66–68 行：保存 LLM 客户端。当传入 `None` 时，`summarize_daily` 和 `deep_dream` 会短路返回空，代表“未接 LLM 时不影响主流程”。

```python
70     def summarize_daily(
71         self,
72         messages: list[dict[str, Any]],
73         existing_daily_memory: str,
74         session_id: str,
75         reason: str,
76         max_messages: int = 0,
77     ) -> list[str]:
78         if not messages or self.llm is None:
79             return []
```

- 第 70–79 行：入口参数
  - `messages`：被裁剪掉的 OpenAI messages。
  - `existing_daily_memory`：今天已经写过什么，传给 LLM 做去重。
  - `session_id/reason`：用于 prompt 里说明背景。
  - `max_messages`：可选截断上限，0 表示全部。
  - 短路条件：空 messages 或未配置 LLM 都直接返回 `[]`。

```python
81         selected = messages[-max_messages:] if max_messages > 0 else messages
82         conversation = self.format_messages(selected)
83         if not conversation.strip():
84             return []
```

- 第 81 行：`max_messages > 0` 时只取最新 N 条，避免 prompt 过大。
- 第 82–84 行：把 messages 格式化成人类可读文本；如果全被过滤（例如全是空 tool 消息）就跳过。

```python
86         user_prompt = (
87             "请将以下被裁剪的对话总结为当天的情景记忆：\n\n"
88             "会话信息：\n"
89             f"- date: {datetime.now().strftime('%Y-%m-%d')}\n"
90             f"- session_id: {session_id}\n"
91             f"- reason: {reason}\n\n"
92             "已有当日情景记忆：\n"
93             f"{existing_daily_memory or '(empty)'}\n\n"
94             "被裁剪的对话：\n"
95             f"{conversation}\n\n"
96             "请输出高质量事件摘要。"
97         )
```

- 第 86–97 行：构造用户 prompt。三部分：
  1. 会话背景（日期/会话/触发原因）。
  2. 已有 daily 内容，供去重。
  3. 待总结的原文。
  4. 明确任务：只要事件摘要。

```python
99         response = self.llm.chat([
100            {"role": "system", "content": DAILY_SUMMARY_SYSTEM_PROMPT},
101            {"role": "user", "content": user_prompt},
102        ])
103        return self.parse_bullets(response.get("content") or "")
```

- 第 99–103 行：只用两条消息（system + user）调用 LLM，然后交给 `parse_bullets()` 解析。

```python
105    def deep_dream(
106        self,
107        semantic_memory: str,
108        episodic_memory: str,
109    ) -> tuple[str, str]:
110        if self.llm is None or not episodic_memory.strip():
111            return "", ""
```

- 第 105–111 行：做梦入口。
  - 无 LLM 或没有近期情景记忆时直接返回空，Manager 会把它当作“skipped”。

```python
113        user_prompt = (
114            "## 当前语义记忆（MEMORY.md）\n\n"
115            f"{semantic_memory or '(empty)'}\n\n"
116            "## 近期情景记忆（daily memory）\n\n"
117            f"{episodic_memory}\n"
118        )
119        response = self.llm.chat([
120            {"role": "system", "content": DREAM_SYSTEM_PROMPT},
121            {"role": "user", "content": user_prompt},
122        ])
123        return self.parse_dream_output(response.get("content") or "")
```

- 第 113–123 行：把两份材料拼给模型，然后按 `[MEMORY]/[DREAM]` 段落解析返回值。

```python
125    def format_messages(self, messages: list[dict[str, Any]]) -> str:
126        lines = []
127        for message in messages:
128            role = message.get("role", "unknown")
129            content = message.get("content")
130            name = message.get("name")
```

- 第 125–130 行：遍历要总结的消息。

```python
132            if role == "tool":
133                summary = self._summarize_tool_message(name, content)
134                if summary:
135                    lines.append(f"tool:{name or 'unknown'}: {summary}")
136                continue
```

- 第 132–136 行：工具输出往往很大，先做一次“取头尾”式截断，再交给 LLM，避免整段日志淹没关键事实。

```python
138            if content:
139                lines.append(f"{role}: {content}")
140            elif message.get("tool_calls"):
141                tool_names = []
142                for call in message.get("tool_calls") or []:
143                    function = call.get("function") or {}
144                    tool_names.append(function.get("name", "unknown"))
145                lines.append(f"{role}: [tool_calls: {', '.join(tool_names)}]")
146        return "\n".join(lines)
```

- 第 138–146 行：
  - 有 `content` 就按 `role: content` 拼；
  - 没有 `content` 但有 `tool_calls`（Assistant 触发工具调用），就记录调用了哪些工具名，让模型知道“这里发生了什么”。
  - 空消息直接跳过。

```python
148    def parse_bullets(self, text: str) -> list[str]:
149        cleaned = text.strip()
150        if not cleaned or cleaned == "无" or cleaned.lower() == "none":
151            return []
```

- 第 148–151 行：LLM 输出解析入口。“无 / None / 空”都视作“没有值得记的事”。

```python
153        items = []
154        for line in cleaned.splitlines():
155            item = line.strip()
156            if not item:
157                continue
158            if item.startswith("- "):
159                item = item[2:].strip()
160            if item and item not in {"无", "None"} and item.lower() != "none":
161                items.append(item)
162        return items
```

- 第 153–162 行：按行拆分，去掉 `- ` 前缀，再次过滤 sentinel。留下真正的 bullet。

```python
164    def parse_dream_output(self, text: str) -> tuple[str, str]:
165        raw = text.strip()
166        if not raw:
167            return "", ""
```

- 第 164–167 行：做梦输出为空时直接返回两个空串。

```python
169        memory = ""
170        dream = ""
171        if "[MEMORY]" in raw:
172            start = raw.index("[MEMORY]") + len("[MEMORY]")
173            end = raw.index("[DREAM]") if "[DREAM]" in raw else len(raw)
174            memory = raw[start:end].strip()
175        if "[DREAM]" in raw:
176            start = raw.index("[DREAM]") + len("[DREAM]")
177            dream = raw[start:].strip()
178        return memory, dream
```

- 第 169–178 行：定位 `[MEMORY]` 和 `[DREAM]` 标记，用切片取内容。若只有 `[MEMORY]` 没有 `[DREAM]`，则 `dream` 为空，避免抛异常。

```python
180    def _summarize_tool_message(self, name: Optional[str], content: Any) -> str:
181        text = str(content or "").strip()
182        if not text:
183            return ""
184
185        lines = text.splitlines()
186        if len(text) <= 500:
187            return text
188
189        head = "\n".join(lines[:6])
190        tail = "\n".join(lines[-4:]) if len(lines) > 10 else ""
191        if tail:
192            return f"{head}\n...\n{tail}"
193        return head
```

- 第 180–193 行：工具消息截断规则。
  - 500 字以内原样保留。
  - 更长时取前 6 行 + 后 4 行，用 `...` 表示中间被省略。
  - 行数不足 10 行时只留头部。
  - 这样既能让 LLM 看到“做了什么/结果如何”，又不会把 20KB 的输出塞进 daily prompt。

---

## memory/index.py

文件职责：SQLite + FTS5 检索索引。它不生产记忆，只把 `MEMORY.md` 和 `daily/*.md` 分块，落到数据库里供查询。

### 数据结构

```python
8  @dataclass(frozen=True)
9  class MemoryRecord:
10     id: str
11     content: str
12     source: str
13     scope: str
14     created_at: str
15     updated_at: str
16     path: str = ""
17     start_line: int = 1
18     end_line: int = 1
```

- 第 8–18 行：一条记忆的通用结构：`id/content` 是必要字段；`source` 记录来自 semantic 或 episodic；`path/start_line/end_line` 让搜索结果能定位到源文件的具体位置。

```python
21 @dataclass(frozen=True)
22 class MemorySearchResult:
23     record: MemoryRecord
24     score: float
```

- 第 21–24 行：一次命中包括记录本体和相关性分数。

### MemoryIndex 主体

```python
27 class MemoryIndex:
28     def __init__(self, db_path: Path):
29         self.db_path = db_path
30         self.db_path.parent.mkdir(parents=True, exist_ok=True)
31         self.conn = sqlite3.connect(str(self.db_path))
32         self.conn.row_factory = sqlite3.Row
33         self.fts_available = self._init_db()
```

- 第 27–33 行：确保数据库文件父目录存在；`sqlite3.Row` 让后面能用列名访问；`fts_available` 表示当前 SQLite 是否支持 FTS5。

```python
35     def rebuild(self, files: list[tuple[Path, str]], root: Path) -> None:
36         self.clear()
37         for path, source_type in files:
38             self.index_file(path, source_type=source_type, root=root)
```

- 第 35–38 行：一次性重建索引。传入 `(path, source_type)` 列表，用 `root` 计算相对路径。之所以每次全量重建，是因为 daily/MEMORY.md 内容不大，全量最简单可靠。

```python
40     def clear(self) -> None:
41         self.conn.execute("DELETE FROM memory_chunks")
42         if self.fts_available:
43             self.conn.execute("DELETE FROM memory_chunks_fts")
44         self.conn.commit()
```

- 第 40–44 行：清空主表和 FTS 表。

```python
46     def index_file(self, path: Path, source_type: str, root: Path) -> None:
47         if not path.exists() or not path.is_file():
48             return
49
50         content = path.read_text(encoding="utf-8").strip()
51         if not content:
52             return
```

- 第 46–52 行：跳过不存在或空的文件。

```python
54         rel_path = str(path.relative_to(root))
55         for start_line, end_line, text in self._chunk_markdown(content):
56             chunk_id = self._make_chunk_id(rel_path, start_line, end_line, text)
57             text_hash = sha1(text.encode("utf-8")).hexdigest()
58             self.conn.execute(
59                 """
60                 INSERT OR REPLACE INTO memory_chunks
61                     (id, source_type, source_path, start_line, end_line, text, hash, updated_at)
62                 VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
63                 """,
64                 (chunk_id, source_type, rel_path, start_line, end_line, text, text_hash),
65             )
66             self._upsert_fts(chunk_id, text)
67         self.conn.commit()
```

- 第 54–67 行：核心逻辑。
  - 用 `relative_to(root)` 存相对路径，让 `MEMORY.md` 或 `daily/2026-07-06.md` 这种路径可复用。
  - `_chunk_markdown` 按 `## ` 段切块。
  - `_make_chunk_id` 生成稳定的短 ID。
  - `INSERT OR REPLACE` 保证同一段每次重建时覆盖旧值。
  - 每一块都写入 FTS 供关键词召回。

```python
69     def search(
70         self,
71         query: str,
72         limit: int = 5,
73         min_score: float = 0.05,
74     ) -> list[MemorySearchResult]:
75         if limit <= 0:
76             return []
77
78         candidates = self._candidate_rows(query, max(limit * 8, 20))
79         results = []
80         for row in candidates:
81             score = self._score(query, row["text"], row["source_type"])
82             if score >= min_score:
83                 results.append(MemorySearchResult(
84                     record=self._record_from_row(row),
85                     score=score,
86                 ))
87         results.sort(key=lambda item: (item.score, item.record.updated_at), reverse=True)
88         return results[:limit]
```

- 第 69–88 行：检索入口。
  - 拿到候选行（数量放大到 `limit*8`，最少 20，给打分留余地）。
  - 每行打分并过滤低分。
  - 排序：先按分数，同分按更新时间。
  - 返回前 `limit` 条。

```python
90     def list_recent(self, limit: int = 80) -> list[MemoryRecord]:
91         rows = self.conn.execute(
92             """
93             SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
94             FROM memory_chunks
95             ORDER BY updated_at DESC
96             LIMIT ?
97             """,
98             (limit,),
99         ).fetchall()
100        return [self._record_from_row(row) for row in rows]
```

- 第 90–100 行：给 `memory_search` 工具和调试用。按更新时间倒序。

```python
102    def close(self) -> None:
103        self.conn.close()
```

- 第 102–103 行：显式关闭连接。当前 CLI 用不到，但服务化时必须。

```python
105    def _init_db(self) -> bool:
106        self.conn.execute("""
107            CREATE TABLE IF NOT EXISTS memory_chunks (
...
118        """)
119        self.conn.execute("""
120            CREATE INDEX IF NOT EXISTS idx_memory_chunks_source
121            ON memory_chunks(source_type, source_path)
122        """)
123        self.conn.commit()
```

- 第 105–123 行：建主表和二级索引。字段说明：
  - `source_type` 区分 semantic/episodic。
  - `source_path` 保存源文件相对路径。
  - `start_line/end_line` 让搜索结果可以“回到源文件的第几行”。
  - `hash` 目前只作为将来去重扩展的占位。

```python
125        try:
126            self.conn.execute("""
127                CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts
128                USING fts5(id UNINDEXED, text)
129            """)
130            self.conn.commit()
131            return True
132        except sqlite3.OperationalError:
133            return False
```

- 第 125–133 行：创建 FTS5 虚拟表。如果本地 SQLite 没有 FTS5 支持，捕获异常并返回 `False`，系统降级为“只按更新时间列表 + 打分”。

```python
135    def _candidate_rows(self, query: str, limit: int) -> list[sqlite3.Row]:
136        ids = self._search_fts_ids(query, limit)
137        rows = []
138        if ids:
139            placeholders = ",".join("?" for _ in ids)
140            rows = self.conn.execute(
141                f"""
142                SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
143                FROM memory_chunks
144                WHERE id IN ({placeholders})
145                """,
146                ids,
147            ).fetchall()
```

- 第 135–147 行：先用 FTS 得到候选 ID，再 `IN (...)` 一次拿元数据。手动拼 `?,?,?` 是因为 SQLite 参数化不支持列表。

```python
149        if len(rows) < limit:
150            seen = {row["id"] for row in rows}
151            fallback = self.conn.execute(
152                """
153                SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
154                FROM memory_chunks
155                ORDER BY updated_at DESC
156                LIMIT ?
157                """,
158                (limit,),
159            ).fetchall()
160            rows.extend(row for row in fallback if row["id"] not in seen)
161
162        return rows[:limit]
```

- 第 149–162 行：候选不足或 FTS 不可用时，补最近更新的行，避免检索结果空空如也。

```python
164    def _search_fts_ids(self, query: str, limit: int) -> list[str]:
165        if not self.fts_available:
166            return []
167
168        tokens = self._tokens(query)
169        if not tokens:
170            return []
171
172        fts_query = " OR ".join(f'"{token}"' for token in tokens[:8])
173        try:
174            rows = self.conn.execute(
175                """
176                SELECT id
177                FROM memory_chunks_fts
178                WHERE memory_chunks_fts MATCH ?
179                LIMIT ?
180                """,
181                (fts_query, limit),
182            ).fetchall()
183            return [row["id"] for row in rows]
184        except sqlite3.OperationalError:
185            return []
```

- 第 164–185 行：把 query 切成 token，拼成 FTS5 的 `OR` 查询表达式。用 `"token"` 包裹是为了兼容中文和特殊字符。
  - 最多用 8 个 token，防止极端长 query 撑爆语法。
  - 查询异常时返回空列表，让上层退化到按时间召回。

```python
187    def _upsert_fts(self, chunk_id: str, text: str) -> None:
188        if not self.fts_available:
189            return
190        self.conn.execute("DELETE FROM memory_chunks_fts WHERE id = ?", (chunk_id,))
191        self.conn.execute(
192            "INSERT INTO memory_chunks_fts (id, text) VALUES (?, ?)",
193            (chunk_id, text),
194        )
```

- 第 187–194 行：FTS 没有原生 upsert，所以先删后插。因为写入频率很低（每次 rebuild），代价可以接受。

```python
196    def _record_from_row(self, row: sqlite3.Row) -> MemoryRecord:
197        return MemoryRecord(
198            id=row["id"],
199            content=row["text"],
200            source=row["source_type"],
201            scope="shared",
202            created_at=row["created_at"],
203            updated_at=row["updated_at"],
204            path=row["source_path"],
205            start_line=row["start_line"],
206            end_line=row["end_line"],
207        )
```

- 第 196–207 行：把 SQLite 行映射为 `MemoryRecord`。集中在一处，避免每个查询函数各自手写字段映射。

```python
209    def _score(self, query: str, text: str, source_type: str) -> float:
210        query_text = query.strip().lower()
211        content_text = text.lower()
212        tokens = set(self._tokens(query_text))
213        if not query_text and not tokens:
214            return 0.1
```

- 第 209–214 行：本地相关性打分。query 完全为空时返回一个很低的常数 0.1，让“空查询”仍然能拿到最近记忆兜底。

```python
216        score = 0.0
217        if query_text and query_text in content_text:
218            score += 0.6
219        if tokens:
220            hits = sum(1 for token in tokens if token in content_text)
221            score += hits / len(tokens)
```

- 第 216–221 行：
  - 完整 query 是子串加 0.6，强奖励整段匹配。
  - token 命中比例最高再加 1.0。
  - 结合起来能覆盖“整句 + 部分词”两种情况。

```python
222        if source_type == "semantic":
223            score += 0.15
224        elif source_type == "episodic":
225            score += 0.05
226        return min(score, 1.0)
```

- 第 222–226 行：source 加分。
  - 语义记忆稳定、经过做梦提炼，天然更值得注入，加 0.15。
  - 情景记忆加 0.05，作为“记得住有事情发生”。
  - 最终不超过 1.0。

```python
228    def _chunk_markdown(self, content: str) -> list[tuple[int, int, str]]:
229        lines = content.splitlines()
230        chunks = []
231        current = []
232        start_line = 1
233
234        for index, line in enumerate(lines, 1):
235            if line.strip().startswith("## ") and current:
236                chunks.append((start_line, index - 1, "\n".join(current).strip()))
237                current = [line]
238                start_line = index
239            else:
240                if not current:
241                    start_line = index
242                current.append(line)
243
244        if current:
245            text = "\n".join(current).strip()
246            if text:
247                chunks.append((start_line, len(lines), text))
248
249        return chunks
```

- 第 228–249 行：按 `## ` 二级标题切块，因为 `MEMORY.md` 和 daily md 都用 `## ` 组织内容。
  - `enumerate(..., 1)`：行号从 1 开始，符合编辑器视角。
  - 每次遇到新的 `## `，把当前累积的段封成 chunk，并开始一段新的。
  - 循环结束再收尾。
  - `strip()` 去掉块内多余的空白。

```python
251    @staticmethod
252    def _tokens(text: str) -> list[str]:
253        lowered = text.lower()
254        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", lowered)
255        return [word for word in words if word.strip()]
```

- 第 251–255 行：英文按连续字母数字下划线切；中文按单字切。这样中英文都能进 FTS。

```python
257    @staticmethod
258    def _make_chunk_id(path: str, start_line: int, end_line: int, text: str) -> str:
259        raw = f"{path}:{start_line}:{end_line}:{sha1(text.encode('utf-8')).hexdigest()}"
260        return "chunk_" + sha1(raw.encode("utf-8")).hexdigest()[:16]
```

- 第 257–260 行：ID = `chunk_` + `sha1(path:start:end:text_sha1)` 的前 16 位。既稳定又几乎不冲突；文本变了 ID 也会变，做 `INSERT OR REPLACE` 时不会误留旧数据。

---

## memory/manager.py

文件职责：门面层。它自己不写数据库，也不调用 LLM，只是把 raw/episodic/semantic/curator/index 编排成对外统一接口 `MemoryManager`。

```python
1  from datetime import datetime
2  from pathlib import Path
3  from typing import Any, Optional
4
5  from memory.curator import MemoryCurator
6  from memory.episodic_store import EpisodicMemoryStore
7  from memory.index import MemoryIndex, MemoryRecord, MemorySearchResult
8  from memory.raw_store import RawConversationStore
9  from memory.semantic_store import SemanticMemoryStore
```

- 第 1–9 行：把五个子模块引入。`MemoryRecord/MemorySearchResult` 在这里 re-export，配合 `memory/__init__.py` 让外部一句 `from memory import ...` 就能用。

```python
12 class MemoryManager:
13     def __init__(self, memory_dir: Path, llm: Optional[Any] = None):
14         self.memory_dir = memory_dir
15         self.memory_dir.mkdir(parents=True, exist_ok=True)
16
17         self.raw_store = RawConversationStore(memory_dir)
18         self.episodic_store = EpisodicMemoryStore(memory_dir)
19         self.semantic_store = SemanticMemoryStore(memory_dir)
20         self.index = MemoryIndex(memory_dir / "index.db")
21         self.curator = MemoryCurator(llm)
22
23         self.rebuild_index()
```

- 第 12–15 行：确保 memory 根目录存在。
- 第 17–21 行：把 5 个组件都实例化。它们各自负责一层。
- 第 23 行：初始化时先把索引重建一次，保证冷启动后 `MEMORY.md` 和 daily md 都能被搜到。

```python
25     def remember(
26         self,
27         content: str,
28         source: str = "manual",
29         scope: str = "shared",
30         metadata: Optional[dict[str, Any]] = None,
31     ) -> str:
32         result = self.semantic_store.remember(content, source=source)
33         self.rebuild_index()
34         return result
```

- 第 25–34 行：语义记忆写入入口。
  - 写完就 `rebuild_index()`，保证下一次 `search()` 能查到。
  - 保留 `scope/metadata` 是为了以后扩展。

```python
36     def record_message(
37         self,
38         session_id: str,
39         role: str,
40         content: Any,
41         name: Optional[str] = None,
42         metadata: Optional[dict[str, Any]] = None,
43     ) -> Path:
44         return self.raw_store.append_message(
45             session_id=session_id,
46             role=role,
47             content=content,
48             name=name,
49             metadata=metadata,
50         )
```

- 第 36–50 行：向 raw jsonl 写一条原始消息。Agent.run() 会在每次 user/assistant/tool 消息进入 history 时调用。

```python
52     def search(
53         self,
54         query: str,
55         limit: int = 5,
56         min_score: float = 0.05,
57     ) -> list[MemorySearchResult]:
58         return self.index.search(query, limit=limit, min_score=min_score)
```

- 第 52–58 行：所有检索都走索引，manager 只透传。它甚至不知道 semantic 还是 episodic，全靠 index 打分排序。

```python
60     def format_for_prompt(self, query: str, limit: int = 5) -> str:
61         prompt, _ = self.build_prompt(query, limit=limit)
62         return prompt
```

- 第 60–62 行：向后兼容旧调用，返回单个字符串。

```python
64     def build_prompt(self, query: str, limit: int = 5) -> tuple[str, int]:
65         results = self.search(query, limit=limit)
66         if not results:
67             return "", 0
```

- 第 64–67 行：找不到相关记忆就返回空字符串和 0，供 ContextManager 决定不注入。

```python
69         semantic = []
70         episodic = []
71         for result in results:
72             line = (
73                 f"- {result.record.content} "
74                 f"(source={result.record.path}:{result.record.start_line}-{result.record.end_line})"
75             )
76             if result.record.source == "semantic":
77                 semantic.append(line)
78             else:
79                 episodic.append(line)
```

- 第 69–79 行：按 semantic 和 episodic 分两组。每条 bullet 后面带 `source=path:start-end`，方便你在检索结果和源文件之间来回定位。

```python
81         sections = [
82             "## Relevant memory",
83             "",
84             "Use these retrieved memories only when they are relevant to the current request.",
85         ]
86         if semantic:
87             sections.extend(["", "### Semantic memory", *semantic])
88         if episodic:
89             sections.extend(["", "### Episodic memory", *episodic])
90         return "\n".join(sections), len(results)
```

- 第 81–90 行：拼出 system prompt 中的 memory 段，并返回条目数写入 trace。

```python
92     def read_main_memory(self) -> str:
93         return self.semantic_store.read()
94
95     def read_today_episodic_memory(self) -> str:
96         return self.episodic_store.read_day()
97
98     def list_records(self, limit: int = 80) -> list[MemoryRecord]:
99         return self.index.list_recent(limit=limit)
```

- 第 92–99 行：暴露给工具/CLI 的只读接口。分别读 `MEMORY.md`、今天的 daily md、最近索引。

```python
101    def flush_from_messages(
102        self,
103        messages: list[dict[str, Any]],
104        reason: str = "context_trim",
105        session_id: str = "default",
106        max_messages: int = 12,
107    ) -> dict[str, Any]:
108        if not messages:
109            return {"ok": True, "skipped": True, "reason": "empty_messages"}
```

- 第 101–109 行：上下文裁剪触发的核心入口。空 messages 直接跳过并返回统计。

```python
111        existing_daily = self.episodic_store.read_day()
112        try:
113            summary = self.curator.summarize_daily(
114                messages=messages,
115                existing_daily_memory=existing_daily,
116                session_id=session_id,
117                reason=reason,
118                max_messages=max_messages,
119            )
120            count = self.episodic_store.append_summary(
121                summary,
122                reason=reason,
123                session_id=session_id,
124            )
125            if count:
126                self.rebuild_index()
```

- 第 111 行：先读今天已有的 daily，作为 LLM 去重依据。
- 第 112–119 行：调用 curator 让 LLM 产出 bullet 列表。
- 第 120–124 行：把 bullets 追加到 daily md。
- 第 125–126 行：只有真的写入了才重建索引，避免空写引发无谓的 IO。

```python
127            return {
128                "ok": True,
129                "skipped": count == 0,
130                "episodic_items": count,
131                "reason": reason,
132            }
133        except Exception as e:
134            return {
135                "ok": False,
136                "skipped": False,
137                "error": f"{type(e).__name__}: {e}",
138            }
```

- 第 127–138 行：把结果结构化返回给 ContextManager，会写入 trace。异常统一兜住，避免 memory flush 出错拖累主流程。

```python
140    def deep_dream(self, lookback_days: int = 1) -> dict[str, Any]:
141        semantic_memory = self.semantic_store.read()
142        episodic_memory = self.episodic_store.read_recent(days=lookback_days)
143        new_memory, dream_diary = self.curator.deep_dream(
144            semantic_memory=semantic_memory,
145            episodic_memory=episodic_memory,
146        )
147        if not new_memory:
148            return {"ok": False, "skipped": True, "reason": "no_dream_output"}
```

- 第 140–148 行：读语义记忆和最近情景记忆，交给 curator。若 LLM 没产出内容就跳过。

```python
150        self.semantic_store.overwrite(new_memory)
151        dream_path = self._write_dream_diary(dream_diary)
152        self.rebuild_index()
153        return {
154            "ok": True,
155            "skipped": False,
156            "dream_path": str(dream_path) if dream_path else "",
157        }
```

- 第 150–157 行：
  - 覆盖写 `MEMORY.md`。
  - 保存做梦日志。
  - 重建索引，让新的语义记忆立刻可搜。

```python
159    def rebuild_index(self) -> None:
160        files = [(self.semantic_store.path, "semantic")]
161        files.extend((path, "episodic") for path in self.episodic_store.list_files())
162        self.index.rebuild(files, root=self.memory_dir)
```

- 第 159–162 行：告诉索引现在有哪些源文件。语义记忆固定一份；情景记忆则动态列出 daily md。

```python
164    def close(self) -> None:
165        self.index.close()
```

- 第 164–165 行：CLI 生命周期短一般不用；如果做长驻服务，退出时应显式关。

```python
167    def _write_dream_diary(self, content: str) -> Optional[Path]:
168        text = content.strip()
169        if not text:
170            return None
171
172        dreams_dir = self.memory_dir / "dreams"
173        dreams_dir.mkdir(parents=True, exist_ok=True)
174        today = datetime.now().strftime("%Y-%m-%d")
175        path = dreams_dir / f"{today}.md"
176        path.write_text(f"# Dream Diary: {today}\n\n{text}\n", encoding="utf-8")
177        return path
```

- 第 167–177 行：做梦日志按天覆盖一份。这个内容不是记忆本体，也不进入索引；它只是给你看看“昨晚模型做了什么整理”。

---

## 各模块协作总览

```text
Agent.run(user_message)
  -> memory.record_message(user)        # raw jsonl
  -> ContextManager.append_user_message
  -> ContextManager.build_model_messages
       -> memory.build_prompt(query)    # 查 index，拼 semantic + episodic
  -> LLM.chat(...)
  -> memory.record_message(assistant)   # raw jsonl
  -> tool_call
       -> memory.record_message(tool)   # raw jsonl
  -> ContextManager._trim_history
       -> memory.flush_from_messages(discarded)
            -> curator.summarize_daily
            -> episodic_store.append_summary
            -> index.rebuild
```

做梦机制走独立入口：

```text
MemoryManager.deep_dream()
  -> semantic_store.read()
  -> episodic_store.read_recent()
  -> curator.deep_dream()
  -> semantic_store.overwrite()
  -> _write_dream_diary()
  -> index.rebuild()
```

工具入口：

```text
remember_memory -> semantic_store.remember + rebuild_index
memory_search   -> index.search
read_memory     -> semantic_store.read
```

---

## 常见问题速查

- **为什么 raw 不进入索引？**
  raw 是事实来源，不是可用记忆。让它进索引会污染检索结果，也让 daily 摘要失去意义。

- **为什么每次都 rebuild 索引，不做增量？**
  当前实现已经是增量：只有情景记忆进入索引，`flush_from_messages` 完成后走 `MemoryIndex.reindex_file()` 只更新当天 daily md；启动时才走 `rebuild()` 一次性覆盖 daily 目录。语义记忆完全不进索引。

- **为什么不用向量检索？**
  第一版只用 FTS + 关键词打分，避免强依赖 embedding 服务。索引层留了 `source_type` 加分位，将来接入 embedding 可以直接扩展 `_score()`。

- **做梦一定要 LLM 吗？**
  是的。做梦的核心是“语义再提炼”。没有 LLM 会直接跳过，只保留手写 `remember_memory` 和 daily 摘要。

- **上下文裁剪时会不会阻塞主循环？**
  不会。`MemoryManager` 内部有一个 `max_workers=1` 的 `ThreadPoolExecutor`，`flush_from_messages` 把 discarded messages 提交给后台线程后立即返回 `future`，主循环继续对话。`_episodic_lock` 保证同一天 daily md 不会被并发追加。CLI 退出时 `MemoryManager.close(wait=True)` 会等 flush 全部完成再退出，避免丢数据。
