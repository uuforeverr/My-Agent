# my-agent Python 代码逐行讲解

> 历史说明：本文记录旧版实现路径。当前 Raw / Episodic / Semantic 设计以
> `docs/memory-architecture.md` 与实际代码为准，本文中的 JSONL 扫描、同步压缩等
> 记忆细节不再代表现状。

这份文档按执行顺序阅读：

```text
main.py
  -> config.py
  -> prompts/loader.py
  -> agent/run_context.py
  -> memory/manager.py
  -> memory/raw_store.py
  -> memory/episodic_store.py
  -> memory/semantic_store.py
  -> memory/index.py
  -> memory/curator.py
  -> skills/loader.py
  -> agent/context_manager.py
  -> agent/agent.py
  -> observability/tracing.py
  -> llm/openai_client.py
  -> tools/registry.py
  -> tools/base.py
  -> tools/file_tools.py
  -> tools/bash_tool.py
  -> tools/memory_tools.py
```

核心链路：

```text
用户在 CLI 输入
  -> 创建 RunContext
  -> Agent.run()
  -> ContextManager.build_model_messages()
  -> MemoryManager.build_prompt()
  -> LLM.chat()
  -> 模型决定是否 tool_call
  -> ToolRegistry.run()
  -> 具体工具 execute()
  -> 工具结果回填给 LLM
  -> 得到最终回答
```

## main.py

文件职责：命令行入口。负责启动程序、循环读取用户输入、调用 Agent、打印结果。

```python
1  from agent.agent import Agent
2  from agent.run_context import RunContext
3  from config import load_settings
4  from observability import JsonlTraceStore
```

导入我们自己写的 `Agent` 类。真正的 Agent Loop 在 `agent/agent.py`。

同时导入：

- `RunContext`：一次请求的上下文。
- `load_settings`：配置加载函数。
- `JsonlTraceStore`：trace 落盘组件。

```python
5  def main() -> None:
```

定义程序主函数。`-> None` 表示这个函数不返回有意义的值。

```python
6      settings = load_settings()
```

读取配置，例如 API Key、模型名、workspace 路径。

```python
7      agent = Agent(settings)
8      trace_store = JsonlTraceStore(settings.workspace.parent / "logs" / "traces.jsonl")
```

创建 Agent 实例。此时 Agent 会初始化 LLM 客户端、工具注册表、系统提示词和消息历史。

第 8 行创建 trace 落盘对象，日志文件位于项目的 `logs/traces.jsonl`。

```python
9      print("my-agent CLI")
10     print(f"workspace: {settings.workspace}")
11     print(f"trace log: {trace_store.path}")
12     print("Type 'exit' to quit. Type '/trace' after a request to see the latest trace.")
```

打印启动提示。第 10 行的 `f"..."` 是 f-string，可以把变量嵌进字符串。

```python
13     last_trace = None
```

保存最近一次请求的 trace。初始还没有请求，所以是 `None`。

```python
14     while True:
```

开启无限循环。CLI 程序会一直等待用户输入，直到用户输入 `exit` 或按 Ctrl-C。

```python
15         try:
16             user_input = input("\n> ").strip()
```

读取用户输入。`input("\n> ")` 会显示提示符，`.strip()` 去掉首尾空格和换行。

```python
17         except (EOFError, KeyboardInterrupt):
18             print()
19             break
```

处理退出信号：

- `EOFError`：输入流结束。
- `KeyboardInterrupt`：用户按 Ctrl-C。
- `break`：跳出 while 循环，程序结束。

```python
21         if not user_input:
22             continue
```

如果用户只按了回车，输入为空，就跳过本轮循环。

```python
23         if user_input.lower() in {"exit", "quit"}:
24             break
```

如果输入 `exit` 或 `quit`，退出程序。`.lower()` 用来兼容 `EXIT`、`Quit`。

```python
25         if user_input == "/trace":
```

特殊命令：查看最近一次请求的链路追踪。

```python
26             if last_trace is None:
27                 print("No trace yet.")
```

如果还没有请求，就提示没有 trace。

```python
28             else:
29                 print(last_trace.to_json(pretty=True))
```

如果有 trace，就调用 `Trace.to_json(pretty=True)` 把 trace 格式化成 JSON 字符串并打印。

```python
30             continue
```

处理完 `/trace` 后跳过本轮，不再把 `/trace` 交给 LLM。

```python
32         try:
33             context = RunContext.create(
34                 session_id="default",
35                 workspace=settings.workspace,
36                 entrypoint="cli",
37             )
38             answer, last_trace = agent.run(user_input, context=context)
```

第 33-37 行先创建本次请求的 `RunContext`：

- `session_id="default"`：当前 CLI 只有一个默认会话。
- `workspace=settings.workspace`：本次请求可操作的工作区。
- `entrypoint="cli"`：写入 metadata，表示请求来自 CLI。

第 38 行把用户输入和 context 一起交给 Agent 执行。返回两个值：

- `answer`：最终回答。
- `last_trace`：本次请求的追踪信息。

```python
34             print(f"\n{answer}")
```

打印 Agent 回答。

```python
35             print(f"\n[request_id={last_trace.request_id} total_ms={last_trace.total_ms()}]")
```

打印本次请求的 request_id 和总耗时。用于最小可观测性。

```python
36         except Exception as e:
37             print(f"ERROR: {type(e).__name__}: {e}")
```

兜底错误处理。避免程序因为一次请求失败就崩溃。

```python
40 if __name__ == "__main__":
41     main()
```

Python 入口惯用写法。只有直接运行 `python3 main.py` 时才调用 `main()`。

## config.py

文件职责：读取 `.env` 和环境变量，生成统一配置对象。

```python
1  import os
```

导入系统环境变量相关模块。后面用 `os.environ` 读取 API Key。

```python
2  from dataclasses import dataclass
```

导入 `dataclass`。它可以快速定义只存数据的类。

```python
3  from pathlib import Path
```

导入 `Path`，用面向对象方式处理文件路径，比字符串拼接安全。

```python
6  def _load_dotenv(path: Path) -> None:
```

定义一个简单 `.env` 加载函数。函数名前面的 `_` 表示内部函数，不希望外部直接调用。

```python
7      if not path.exists():
8          return
```

如果 `.env` 文件不存在，就直接返回，不报错。

```python
9      for raw_line in path.read_text(encoding="utf-8").splitlines():
```

读取 `.env` 文件内容，按行遍历。

```python
10         line = raw_line.strip()
```

去掉每行首尾空格。

```python
11         if not line or line.startswith("#") or "=" not in line:
12             continue
```

跳过三类行：

- 空行。
- 注释行。
- 不包含 `=` 的非法行。

```python
13         key, value = line.split("=", 1)
```

按第一个 `=` 拆成 key 和 value。`1` 表示只拆一次，防止 value 里也有 `=`。

```python
14         key = key.strip()
15         value = value.strip().strip('"').strip("'")
```

清理 key/value 两边空格，并去掉 value 外层可能的引号。

```python
16         os.environ.setdefault(key, value)
```

写入环境变量，但如果系统环境里已经有同名变量，就不覆盖。这样外部环境变量优先级更高。

```python
19 @dataclass(frozen=True)
```

把下面的 `Settings` 类变成 dataclass。`frozen=True` 表示创建后不可修改，避免运行时配置被乱改。

```python
20 class Settings:
21     openai_api_key: str
22     openai_api_base: str
23     model: str
24     workspace: Path
25     max_steps: int = 6
```

定义配置结构：

- `openai_api_key`：模型 API Key。
- `openai_api_base`：OpenAI-compatible API 地址。
- `model`：模型名。
- `workspace`：工具可访问的工作目录。
- `max_steps`：Agent Loop 最多跑几轮工具调用。

```python
28 def load_settings() -> Settings:
```

对外暴露的配置加载函数。

```python
29     root = Path(__file__).resolve().parent
```

拿到当前项目根目录。`config.py` 在项目根目录，所以 parent 就是 `my-agent/`。

```python
30     _load_dotenv(root / ".env")
```

加载项目根目录下的 `.env`。

```python
32     workspace = Path(os.environ.get("WORKSPACE", root / "workspace")).expanduser().resolve()
```

读取 `WORKSPACE` 配置。如果没配，默认使用 `my-agent/workspace`。

- `expanduser()`：展开 `~`。
- `resolve()`：转成绝对路径。

```python
33     workspace.mkdir(parents=True, exist_ok=True)
```

确保 workspace 目录存在。不存在就创建。

```python
35     return Settings(
```

创建并返回 `Settings` 配置对象。

```python
36         openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
37         openai_api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/"),
38         model=os.environ.get("MODEL", "gpt-4o-mini"),
39         workspace=workspace,
40         max_steps=int(os.environ.get("MAX_STEPS", "6")),
41     )
```

逐项读取环境变量。

第 37 行 `.rstrip("/")` 是为了去掉末尾 `/`，避免拼 URL 时出现 `//chat/completions`。

## agent/run_context.py

文件职责：定义一次 Agent 运行的请求上下文。它不是模型上下文，也不是对话历史，而是“这一轮请求运行时需要携带的信息”。

后续 memory、skill、web_search 都可以从它读取运行时信息，而不是不断给 `Agent.run()` 增加参数。

```python
1  from dataclasses import dataclass, field
2  from pathlib import Path
3  from typing import Any, Optional
```

导入依赖：

- `dataclass`：快速定义只存数据的类。
- `field`：给可变字段或动态字段提供默认值。
- `Path`：表示 workspace 路径。
- `Any`：metadata 里允许放任意类型的值。
- `Optional`：表示某个参数可以是具体值，也可以是 `None`。

```python
5  from observability import Trace
```

导入 trace。每个 context 都带一个 trace，这样所有模块都能把事件记录到同一条链路上。

```python
8  @dataclass
9  class RunContext:
```

定义请求上下文类。它是一次 `Agent.run()` 的运行时载体。

```python
10     session_id: str
11     workspace: Path
12     trace: Trace = field(default_factory=Trace)
13     metadata: dict[str, Any] = field(default_factory=dict)
```

字段说明：

- `session_id`：会话 ID。当前 CLI 只有 `"default"`，后续 Web 或多会话会用不同 ID。
- `workspace`：本次请求允许操作的工作目录。
- `trace`：本次请求的链路追踪对象。
- `metadata`：扩展字段容器，后续可放 `entrypoint`、`enabled_skills`、`memory_enabled` 等。

`trace` 和 `metadata` 都用 `default_factory`，因为它们每次创建 context 都应该是新的对象，不能被多个请求共享。

```python
15     @classmethod
16     def create(
17         cls,
18         session_id: str,
19         workspace: Path,
20         trace: Optional[Trace] = None,
21         **metadata: Any,
22     ) -> "RunContext":
```

`create()` 是工厂方法，用来统一创建 `RunContext`。

为什么不用直接写 `RunContext(...)`？

因为后续可能要在创建 context 时统一做更多事情，例如：

```text
生成 session_id
校验 workspace
注入 user_id
注入 memory 开关
注入 skill 过滤条件
```

用工厂方法可以把这些规则集中在一个地方。

`trace: Optional[Trace] = None` 表示调用方可以传入已有 trace；如果不传，函数内部会新建。

`**metadata` 表示可以传入任意扩展字段，例如：

```python
RunContext.create(..., entrypoint="cli")
```

```python
23         return cls(
24             session_id=session_id,
25             workspace=workspace.resolve(),
26             trace=trace or Trace(),
27             metadata=dict(metadata),
28         )
```

真正创建 `RunContext`：

- `workspace.resolve()`：转成绝对路径，避免后续工具拿到相对路径。
- `trace or Trace()`：有传入 trace 就复用，否则新建。
- `dict(metadata)`：把动态参数复制成普通字典，避免外部对象被意外共享。

```python
30     def add_metadata(self, **data: Any) -> None:
31         self.metadata.update(data)
```

`add_metadata()` 用来给上下文追加扩展信息。

例如后续 skill 模块可以写：

```python
context.add_metadata(enabled_skills=["code-review"])
```

这个函数不返回值，只修改 `self.metadata`。

```python
33     def get(self, key: str, default: Any = None) -> Any:
34         return self.metadata.get(key, default)
```

`get()` 用来安全读取 metadata。

如果 key 不存在，就返回 default，避免抛 `KeyError`。

例如：

```python
entrypoint = context.get("entrypoint", "unknown")
```

这个设计让后续模块可以通过 context 共享信息，但不用彼此强依赖。

## skills/loader.py

文件职责：扫描 `skills/*/SKILL.md`，提取技能名称和描述，并格式化成可以注入 system prompt 的 `<available_skills>`。

这个文件同时承担 loader 和 formatter，避免第一版过早拆成多个模块。

```python
1  from dataclasses import dataclass
2  from pathlib import Path
3  from typing import Optional
```

导入依赖：

- `dataclass`：定义 Skill 数据结构。
- `Path`：处理技能文件路径。
- `Optional`：表示 `_load_one()` 可能返回一个 Skill，也可能返回 None。

```python
6  @dataclass(frozen=True)
7  class Skill:
8      name: str
9      description: str
10     file_path: Path
11     content: str
```

定义技能数据结构。

字段说明：

- `name`：技能名。
- `description`：技能简短描述，给模型判断何时使用。
- `file_path`：`SKILL.md` 绝对路径。
- `content`：完整文件内容，后续如果要读取详细步骤可以使用。

`frozen=True` 表示 Skill 创建后不可修改，避免运行时被误改。

```python
13     def prompt_entry(self) -> str:
14         return (
15             "  <skill>\n"
16             f"    <name>{_escape_xml(self.name)}</name>\n"
17             f"    <description>{_escape_xml(self.description)}</description>\n"
18             f"    <location>{_escape_xml(str(self.file_path))}</location>\n"
19             "  </skill>"
20         )
```

`prompt_entry()` 把一个 Skill 格式化成 XML 片段。

为什么要包含 `location`？

因为模型看到技能后，如果需要详细步骤，可以调用 `read_file` 读取这个 `SKILL.md`。

为什么要 `_escape_xml()`？

如果 name/description 里有 `<`、`&` 等字符，不转义会破坏 XML 结构。

```python
24 class SkillLoader:
```

定义技能加载器。

```python
25     def __init__(self, skills_dir: Path):
26         self.skills_dir = skills_dir
```

初始化技能目录。

当前默认会传入项目根目录下的：

```text
skills/
```

```python
28     def load(self) -> list[Skill]:
29         if not self.skills_dir.exists():
30             return []
```

加载所有技能。

如果 `skills/` 目录不存在，直接返回空列表，不报错。

```python
32         skills = []
33         for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
34             skill = self._load_one(skill_file)
35             if skill:
36                 skills.append(skill)
37         return skills
```

扫描一级子目录下的 `SKILL.md`：

```text
skills/file-summary/SKILL.md
skills/code-review/SKILL.md
```

每个文件交给 `_load_one()` 解析。解析失败或缺少必要字段时返回 None，跳过该技能。

```python
39     def build_prompt(self) -> tuple[str, int]:
40         skills = self.load()
41         if not skills:
42             return "", 0
```

构建 skills prompt。

如果没有技能，返回空字符串和数量 0。

```python
44         entries = "\n".join(skill.prompt_entry() for skill in skills)
45         prompt = (
46             "<available_skills>\n"
47             "Use these skills as task-specific operating instructions when relevant. "
48             "Read the skill file before following detailed steps.\n"
49             f"{entries}\n"
50             "</available_skills>"
51         )
52         return prompt, len(skills)
```

把多个技能拼成一个 `<available_skills>` 段落。

返回两个值：

- prompt 文本。
- 技能数量。

技能数量会写入 trace，方便排查“本次请求到底注入了几个技能”。

```python
54     def _load_one(self, skill_file: Path) -> Optional[Skill]:
55         content = skill_file.read_text(encoding="utf-8")
56         metadata, body = _split_frontmatter(content)
```

读取单个 `SKILL.md`，并拆出 frontmatter 和正文。

```python
58         name = metadata.get("name") or skill_file.parent.name
59         description = metadata.get("description") or _first_paragraph(body)
```

技能名优先取 frontmatter 的 `name`，否则用目录名。

技能描述优先取 frontmatter 的 `description`，否则取正文第一段。

```python
61         name = name.strip()
62         description = description.strip()
63         if not name or not description:
64             return None
```

清理空白。如果 name 或 description 为空，就跳过这个技能。

```python
66         return Skill(
67             name=name,
68             description=description,
69             file_path=skill_file.resolve(),
70             content=content,
71         )
```

返回 Skill 对象。

`file_path.resolve()` 转成绝对路径，方便模型后续直接读取。

```python
75 def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
```

拆分 Markdown frontmatter。

支持这种格式：

```markdown
---
name: file-summary
description: Summarize files.
---

# Body
```

```python
76     lines = content.splitlines()
77     if not lines or lines[0].strip() != "---":
78         return {}, content
```

如果第一行不是 `---`，说明没有 frontmatter，返回空 metadata 和完整正文。

```python
80     metadata = {}
81     for index, line in enumerate(lines[1:], start=1):
82         if line.strip() == "---":
83             body = "\n".join(lines[index + 1:])
84             return metadata, body
```

从第二行开始扫描，遇到第二个 `---` 表示 frontmatter 结束。

结束后返回 metadata 和正文 body。

```python
85         if ":" in line:
86             key, value = line.split(":", 1)
87             metadata[key.strip()] = value.strip().strip('"').strip("'")
```

解析简单的 `key: value`。

这里只支持最小格式，不支持复杂 YAML 数组或嵌套对象。

```python
89     return {}, content
```

如果一直没找到结束的 `---`，认为 frontmatter 无效，返回空 metadata 和完整内容。

```python
92 def _first_paragraph(content: str) -> str:
```

从正文里提取第一段非标题文本，作为 description 的 fallback。

```python
93     lines = []
94     for raw_line in content.splitlines():
95         line = raw_line.strip()
96         if not line:
97             if lines:
98                 break
99             continue
100        if line.startswith("#"):
101            continue
102        lines.append(line)
103    return " ".join(lines)
```

逻辑：

- 跳过开头空行。
- 跳过 Markdown 标题。
- 收集第一段普通文本。
- 遇到空行后停止。
- 用空格拼成一句 description。

```python
106 def _escape_xml(text: str) -> str:
107     return (
108         text.replace("&", "&amp;")
109         .replace("<", "&lt;")
110         .replace(">", "&gt;")
111         .replace('"', "&quot;")
112         .replace("'", "&apos;")
113     )
```

转义 XML 特殊字符。

这个函数确保技能内容不会破坏 `<available_skills>` 结构。

## agent/context_manager.py

文件职责：管理“发给模型的上下文”。它负责保存对话历史、在调用模型前组装 messages，并预留上下文裁剪/压缩入口。

这才是 Agent 语境下更常说的 context management。

```python
1  from copy import deepcopy
2  from typing import Any, Optional
4  from agent.run_context import RunContext
5  from memory import MemoryManager
6  from skills import SkillLoader
```

导入依赖：

- `deepcopy`：构造模型输入时复制历史消息，避免 LLM 调用过程意外修改原始历史。
- `Any`：历史消息里可能包含字符串、tool_calls 等不同结构。
- `Optional`：表示 memory 可以存在，也可以不传。
- `RunContext`：构建上下文时需要把事件写入同一条 trace。
- `MemoryManager`：raw 原文保存、daily 情景记忆生成、语义记忆检索和 prompt 注入能力。
- `SkillLoader`：技能加载与 prompt 格式化能力。

```python
7  class ContextManager:
```

定义模型上下文管理器。

```python
8      def __init__(
9          self,
10         system_prompt: str,
11         max_history_messages: int = 40,
12         memory: Optional[MemoryManager] = None,
13         skill_loader: Optional[SkillLoader] = None,
14         memory_search_results: int = 5,
15         memory_flush_messages: int = 12,
16     ):
17         self.system_prompt = system_prompt
18         self.max_history_messages = max_history_messages
19         self.memory = memory
20         self.skill_loader = skill_loader
21         self.memory_search_results = memory_search_results
22         self.memory_flush_messages = memory_flush_messages
23         self.history: list[dict[str, Any]] = []
```

初始化上下文管理器：

- `system_prompt`：每次发给模型的系统提示词。
- `max_history_messages`：最多保留多少条历史消息，当前是按条数裁剪的最小实现。
- `memory`：可选长期记忆管理器。传入后会按当前用户问题检索相关记忆，并在裁剪上下文时生成 daily 情景记忆。
- `skill_loader`：可选技能加载器。传入后会在模型调用前注入 `<available_skills>`。
- `memory_search_results`：每轮最多注入多少条相关长期记忆。
- `memory_flush_messages`：裁剪上下文时最多拿多少条旧消息生成记忆。
- `history`：不包含 system prompt 的对话历史。

为什么 `history` 不放 system prompt？

因为 system prompt 是稳定规则，每次调用模型时重新放到第一条即可；历史只保存用户、assistant、tool 消息。

```python
13     def append_user_message(self, content: str) -> None:
14         self.history.append({"role": "user", "content": content})
```

追加用户消息。用户输入进入 Agent 后，先写入历史。

```python
16     def append_assistant_message(self, message: dict[str, Any]) -> None:
17         self.history.append(message)
```

追加模型返回的 assistant 消息。

这里保存的是完整 assistant message，而不是只保存 `content`。原因是 assistant message 里可能包含 `tool_calls`，后续 OpenAI 协议要求 tool result 必须能对应前面的 tool_call。

```python
19     def append_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
20         self.history.append({
21             "role": "tool",
22             "tool_call_id": tool_call_id,
23             "name": name,
24             "content": content,
25         })
```

追加工具执行结果。

字段说明：

- `role="tool"`：告诉模型这是工具结果。
- `tool_call_id`：对应 assistant message 里的某个 tool_call。
- `name`：工具名。
- `content`：工具输出。

这一步是 tool loop 的关键：模型先请求工具，代码执行工具，然后把结果以 `role=tool` 回填给模型。

```python
28     def build_model_messages(self, run_context: RunContext) -> list[dict[str, Any]]:
29         history = self._trim_history(self.history)
30         system_prompt, prompt_metadata = self._build_system_prompt()
31         messages = [{"role": "system", "content": system_prompt}]
32         messages.extend(deepcopy(history))
```

构造发给模型的 messages：

1. 先裁剪历史。
2. 调用 `_build_system_prompt()` 组装 system prompt、长期记忆和技能列表。
3. 再拼接历史消息副本。

这里是后续“上下文压缩”的主入口。现在只是按条数裁剪、注入 memory 和注入 skills，未来可以在这里升级成 token 估算、摘要压缩、按需技能筛选。

```python
33         run_context.trace.add(
34             "context_built",
35             session_id=run_context.session_id,
36             message_count=len(messages),
37             history_count=len(history),
38             **prompt_metadata,
39         )
40         return messages
```

记录一次上下文构造事件，并返回模型输入。

记录这些字段是为了后续排查：

- `message_count`：本次实际发给模型多少条消息。
- `history_count`：裁剪后保留多少条历史。
- `memory_injected`：本次 system prompt 是否注入了长期记忆。
- `skill_count`：本次 system prompt 注入了几个技能。

```python
42     def _build_system_prompt(self) -> tuple[str, dict[str, Any]]:
43         sections = [self.system_prompt]
44         metadata = {
45             "memory_injected": False,
46             "skill_count": 0,
47         }
48
49         if self.memory:
50             memory_prompt = self.memory.format_for_prompt()
51             if memory_prompt:
52                 sections.append(memory_prompt)
53                 metadata["memory_injected"] = True
54
55         if self.skill_loader:
56             skills_prompt, skill_count = self.skill_loader.build_prompt()
57             if skills_prompt:
58                 sections.append(skills_prompt)
59                 metadata["skill_count"] = skill_count
60
61         return "\n\n".join(sections), metadata
```

`_build_system_prompt()` 负责组装真正发给模型的 system prompt。

当前包含三部分：

1. `prompts/identity.md` 中定义的身份和规则。
2. 如果检索到相关长期记忆，则追加 memory section。
3. 如果存在技能，则追加 `<available_skills>` section。

返回两个值：

- 拼接后的 system prompt。
- metadata 字典，当前包含 `memory_injected` 和 `skill_count`。

为什么返回 metadata？

因为 trace 里需要知道本次模型调用是否看到了长期记忆、看到了几个技能，方便后续排查“为什么模型没按技能做”。

```python
52     def _trim_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
53         if len(history) <= self.max_history_messages:
54             return list(history)
```

如果历史长度没有超过限制，返回一个浅拷贝。

```python
56         trimmed = list(history[-self.max_history_messages:])
```

如果历史太长，只保留最后 `max_history_messages` 条。

这是最小化实现，不是最终最佳实现。后续应该升级成 token 级裁剪和摘要压缩。

```python
60         while trimmed and trimmed[0].get("role") == "tool":
61             trimmed.pop(0)
```

避免裁剪后第一条是孤立的 tool 消息。

OpenAI tool calling 要求 `role=tool` 必须跟在发起它的 assistant tool_call 后面。如果裁剪把 assistant tool_call 删了，却留下 tool result，模型 API 可能报格式错误。

```python
63         return trimmed
```

返回裁剪后的历史。

## observability/tracing.py

文件职责：记录一次请求的 request_id 和各阶段耗时。

```python
1  import json
2  import time
3  import uuid
4  from dataclasses import dataclass, field
5  from datetime import datetime
6  from pathlib import Path
```

导入依赖：

- `json`：把 trace 转成 JSON。
- `time`：用 `time.monotonic()` 计算耗时。
- `uuid`：生成唯一 request_id。
- `dataclass/field`：定义数据类和动态默认值。
- `datetime`：生成 `created_at`。
- `Path`：表示 trace 日志路径。

```python
9  @dataclass
10 class Trace:
```

定义 Trace 数据类。每次用户请求都会创建一个 Trace。

```python
11     request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
```

创建唯一请求 ID。必须用 `default_factory`，否则所有 Trace 可能共享同一个默认值。

```python
12     started_at: float = field(default_factory=time.monotonic)
```

记录请求开始时间。`monotonic()` 专门适合算耗时，不受系统时间调整影响。

```python
13     events: list[dict] = field(default_factory=list)
```

保存事件列表。每个事件是一个 dict，比如 `llm_call`、`tool_call`。

```python
12     def add(self, name: str, **data) -> None:
```

添加一个 trace 事件。`**data` 表示可以接收任意附加字段。

```python
13         self.events.append({
14             "name": name,
15             "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 2),
16             **data,
17         })
```

追加事件：

- `name`：事件名。
- `elapsed_ms`：从请求开始到现在经过多少毫秒。
- `**data`：调用方传入的额外信息，比如 `tool_name`、`duration_ms`。

```python
19     def total_ms(self) -> float:
20         return round((time.monotonic() - self.started_at) * 1000, 2)
```

返回请求总耗时，单位毫秒。

```python
22     def to_record(self) -> dict:
23         return {
24             "request_id": self.request_id,
25             "created_at": datetime.now().isoformat(timespec="seconds"),
26             "total_ms": self.total_ms(),
27             "events": self.events,
28         }
```

`to_record()` 把 Trace 转成普通 dict，方便后续 JSON 序列化或查询。

字段说明：

- `request_id`：请求唯一 ID。
- `created_at`：落盘或展示时的当前时间。
- `total_ms`：请求总耗时。
- `events`：详细事件列表。

这个函数的意义是把“内存对象”转换成“可存储的数据结构”。

```python
30     def to_json(self, *, pretty: bool = False) -> str:
31         return json.dumps(
32             self.to_record(),
33             ensure_ascii=False,
34             indent=2 if pretty else None,
35         )
```

`to_json()` 把 trace 转成 JSON 字符串。

- `pretty=False`：默认输出单行 JSON，适合写入 JSONL 文件。
- `pretty=True`：输出带缩进的 JSON，适合 CLI `/trace` 查看。
- `ensure_ascii=False`：中文不转义，方便阅读。

`*` 表示 `pretty` 必须用关键字传参：

```python
trace.to_json(pretty=True)
```

这样比 `trace.to_json(True)` 更清晰。

```python
39 class JsonlTraceStore:
```

定义 JSONL trace 存储类。JSONL 是一行一个 JSON 对象，适合持续追加日志。

```python
40     def __init__(self, path: Path):
41         self.path = path
42         self.path.parent.mkdir(parents=True, exist_ok=True)
```

初始化 trace 存储：

- 保存日志路径。
- 自动创建父目录，比如 `logs/`。
- `parents=True` 表示多级目录不存在也一起创建。
- `exist_ok=True` 表示目录已存在时不报错。

```python
44     def append(self, trace: Trace) -> None:
45         with self.path.open("a", encoding="utf-8") as f:
46             f.write(trace.to_json() + "\n")
```

追加写入一条 trace：

- `"a"` 表示 append 模式，只追加不覆盖。
- `trace.to_json()` 默认是单行 JSON。
- 最后加 `\n`，保证每条记录独占一行。

这个类只负责落盘，不负责采集事件。事件采集由 `Trace.add()` 完成。

## agent/agent.py

文件职责：最小 Agent Loop。它决定何时调用 LLM、何时执行工具、何时返回最终答案。

```python
1  import time
2  from typing import Optional
```

导入依赖：

- `time`：统计 LLM 和工具耗时。
- `Optional`：表示 `context` 可以传，也可以不传。

```python
4  from agent.context_manager import ContextManager
5  from agent.run_context import RunContext
6  from config import Settings
7  from llm.openai_client import OpenAICompatibleClient
8  from memory import MemoryManager
9  from observability import Trace
10 from prompts import PromptLoader
11 from skills import SkillLoader
12 from tools.registry import ToolRegistry
```

导入自己项目里的模块：

- ContextManager：模型上下文管理器。
- RunContext：请求上下文。
- Trace：请求追踪。
- Settings：配置对象。
- OpenAICompatibleClient：模型客户端。
- MemoryManager：raw 原文保存、daily 情景记忆、语义记忆和检索索引的门面。
- PromptLoader：从 `prompts/identity.md` 读取 system prompt。
- SkillLoader：技能加载器。
- ToolRegistry：工具注册表。

```python
14 class Agent:
```

定义 Agent 类。

```python
20     def __init__(self, settings: Settings):
```

Agent 初始化函数。传入配置。

```python
21         self.settings = settings
```

保存配置。

```python
22         self.llm = OpenAICompatibleClient(settings)
```

创建 LLM 客户端。

```python
23         self.prompt_loader = PromptLoader(settings.project_root / "prompts" / "identity.md")
```

创建 prompt 加载器。system prompt 从 `prompts/identity.md` 读取，不再写死在代码里。

```python
24         self.memory = MemoryManager(settings.project_root / "data" / "memory", llm=self.llm)
```

创建长期记忆管理器。记忆数据放在 `data/memory/` 下：

```text
data/memory/index.db
data/memory/MEMORY.md
data/memory/daily/*.md
```

```python
25         self.skill_loader = SkillLoader(settings.project_root / "skills")
```

创建技能加载器。

技能目录放在项目根目录的：

```text
skills/
```

```python
26         self.tools = ToolRegistry(
27             settings.workspace,
28             self.memory,
29             bash_timeout=settings.bash_timeout,
30         )
```

创建工具注册表，并把 workspace 和 memory 传给工具。

文件工具需要 workspace，memory 工具需要 memory。

```python
31         self.context_manager = ContextManager(
32             self.prompt_loader.load_identity(),
33             max_history_messages=settings.max_history_messages,
34             memory=self.memory,
35             skill_loader=self.skill_loader,
36             memory_search_results=settings.memory_search_results,
37             memory_flush_messages=settings.memory_flush_messages,
38         )
```

创建模型上下文管理器，并把 memory 和 skill_loader 传进去。

这样 `ContextManager.build_model_messages()` 在调用模型前可以自动检索相关长期记忆、注入技能列表，并在上下文裁剪时触发记忆整理。

```python
28     def run(self, user_message: str, context: Optional[RunContext] = None) -> tuple[str, Trace]:
```

执行一次用户请求。`context` 是可选的：

- CLI 会显式传入 `RunContext`。
- 如果外部测试代码没传，Agent 内部会创建默认 context，保持旧调用方式可用。

```python
29         context = context or RunContext.create(
30             session_id="default",
31             workspace=self.settings.workspace,
32         )
33         trace = context.trace
```

如果调用方没有传 context，就创建一个默认上下文。

第 33 行从 context 中取 trace。这样 trace 不再是 Agent 自己独占的对象，后续 memory、skill、web_search 都可以通过同一个 context 写入同一条链路。

```python
34         self.context_manager.append_user_message(user_message)
```

把用户输入交给上下文管理器保存。

```python
35         trace.add(
36             "user_message",
37             chars=len(user_message),
38             session_id=context.session_id,
39         )
```

记录用户消息事件，同时记录字符数和 session_id。

```python
33         for step in range(1, self.settings.max_steps + 1):
```

Agent Loop。最多跑 `max_steps` 轮，防止模型无限调用工具。

```python
34             llm_start = time.monotonic()
```

记录 LLM 调用开始时间。

```python
41             model_messages = self.context_manager.build_model_messages(context)
42             message = self.llm.chat(model_messages, tools=self.tools.schemas())
```

调用模型前，先通过 `ContextManager.build_model_messages()` 组装真正发给 LLM 的上下文。

然后把模型上下文和工具 schema 一起发给模型。

```python
40             trace.add(
41                 "llm_call",
42                 step=step,
43                 session_id=context.session_id,
44                 duration_ms=...
45             )
```

记录本轮 LLM 调用耗时，并带上 session_id。后续如果有多个会话，就可以按 session 过滤 trace。

```python
51             self.context_manager.append_assistant_message(message)
```

把模型返回的 assistant message 交给上下文管理器保存。

```python
39             tool_calls = message.get("tool_calls") or []
```

从模型回复中取工具调用。如果没有，就设为空列表。

```python
41             if not tool_calls:
```

如果模型没有要求调用工具，说明它已经给出最终回答。

```python
42                 answer = message.get("content") or ""
```

取最终回答文本。

```python
43                 trace.add("final_answer", total_ms=trace.total_ms())
44                 return answer, trace
```

记录最终回答事件，并返回。

```python
46             for call in tool_calls:
```

如果有工具调用，逐个执行。

```python
47                 function = call.get("function") or {}
48                 tool_name = function.get("name", "")
49                 arguments = function.get("arguments", "{}")
```

解析 OpenAI tool_call 格式：

- 工具名在 `function.name`。
- 参数在 `function.arguments`，通常是 JSON 字符串。

```python
51                 tool_start = time.monotonic()
```

记录工具调用开始时间。

```python
52                 result = self.tools.run(tool_name, arguments)
```

通过工具注册表执行工具。

```python
53-59              trace.add(...)
```

记录工具调用事件，包括：

- step。
- session_id。
- tool_name。
- success/error。
- duration_ms。

```python
75                 self.context_manager.append_tool_result(
76                     tool_call_id=call.get("id"),
77                     name=tool_name,
78                     content=result.content,
79                 )
```

把工具结果回填给 LLM。下一轮循环时，模型会看到工具执行结果，然后继续推理或给最终答案。

```python
68         fallback = f"Reached max_steps={...}"
```

如果跑满 `max_steps` 还没结束，说明模型可能陷入工具循环，强制停止。

```python
69         trace.add("max_steps_reached", total_ms=trace.total_ms())
70         self.context_manager.append_assistant_message({"role": "assistant", "content": fallback})
71         return fallback, trace
```

记录超步事件，保存 fallback 回复，并返回。

`Agent` 到这里结束。它不负责 trace 的 JSON 格式化或落盘；这些属于 `observability/tracing.py` 的职责。

## llm/openai_client.py

文件职责：调用 OpenAI-compatible `/chat/completions` API。

```python
1  import json
```

用于把 Python dict 转成 JSON 字符串，也用于解析返回 JSON。

```python
2  import urllib.error
3  import urllib.request
```

使用 Python 标准库发 HTTP 请求，不额外依赖 `requests`。

```python
4  from typing import Any
6  from config import Settings
```

导入类型标注和配置对象。

```python
9  class LLMError(RuntimeError):
10     pass
```

定义 LLM 专用异常。调用模型失败时抛这个错误，方便上层识别。

```python
13 class OpenAICompatibleClient:
```

定义 OpenAI-compatible 客户端。

```python
14     def __init__(self, settings: Settings):
15         self.settings = settings
```

保存配置。

```python
17     def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
```

模型调用方法。输入：

- `messages`：对话历史。
- `tools`：工具 schema，可选。

返回：assistant message。

```python
18         if not self.settings.openai_api_key:
19             raise LLMError(...)
```

如果没配置 API Key，直接报错。

```python
21         payload: dict[str, Any] = {
22             "model": self.settings.model,
23             "messages": messages,
24         }
```

构造 HTTP 请求体。

```python
25         if tools:
26             payload["tools"] = tools
27             payload["tool_choice"] = "auto"
```

如果有工具，把工具 schema 发给模型。`tool_choice="auto"` 表示由模型决定是否调用工具。

```python
29         req = urllib.request.Request(
30             url=f"{self.settings.openai_api_base}/chat/completions",
31             data=json.dumps(payload).encode("utf-8"),
32             headers={...},
36             method="POST",
37         )
```

创建 HTTP POST 请求：

- URL 是 `/chat/completions`。
- body 是 JSON。
- headers 里带 Bearer Token。

```python
39         try:
40             with urllib.request.urlopen(req, timeout=120) as resp:
41                 data = json.loads(resp.read().decode("utf-8"))
```

真正发起 HTTP 请求，最长等 120 秒。返回后解析 JSON。

```python
42         except urllib.error.HTTPError as e:
43             body = e.read().decode("utf-8", errors="replace")
44             raise LLMError(f"LLM HTTP {e.code}: {body}") from e
```

处理 HTTP 错误，比如 401、429、500。把响应体也带出来，方便排查。

```python
45         except Exception as e:
46             raise LLMError(f"LLM request failed: {e}") from e
```

处理其他错误，比如网络断开、DNS 失败。

```python
48         try:
49             return data["choices"][0]["message"]
```

从 OpenAI 标准响应里取 assistant message。

```python
50         except (KeyError, IndexError, TypeError) as e:
51             raise LLMError(f"Unexpected LLM response: {data}") from e
```

如果响应结构不符合预期，抛出更清晰的错误。

## tools/base.py

文件职责：定义所有工具共同遵守的接口和安全路径检查。

```python
1  from dataclasses import dataclass
2  from pathlib import Path
3  from typing import Any
```

导入数据类、路径对象和类型标注。

```python
6  @dataclass
7  class ToolResult:
8      ok: bool
9      content: str
```

工具结果结构：

- `ok`：是否成功。
- `content`：返回给模型看的文本。

```python
12 class ToolError(RuntimeError):
13     pass
```

定义工具专用错误。

```python
16 class BaseTool:
```

所有工具的父类。

```python
17     name = "base"
18     description = "Base tool"
19     parameters: dict[str, Any] = {"type": "object", "properties": {}}
```

工具必须提供：

- name：工具名。
- description：工具描述。
- parameters：JSON Schema 参数描述。

```python
21     def __init__(self, workspace: Path):
22         self.workspace = workspace.resolve()
```

每个工具都绑定一个 workspace，并转成绝对路径。

```python
24     def schema(self) -> dict[str, Any]:
25         return {
26             "type": "function",
27             "function": {
28                 "name": self.name,
29                 "description": self.description,
30                 "parameters": self.parameters,
31             },
32         }
```

把工具描述转换成 OpenAI tools 格式。

```python
34     def run(self, arguments: dict[str, Any]) -> ToolResult:
```

工具统一执行入口。

```python
35         try:
36             return ToolResult(ok=True, content=self.execute(arguments))
```

调用具体工具的 `execute()`，成功就包装成 `ToolResult(ok=True)`。

```python
37         except Exception as e:
38             return ToolResult(ok=False, content=f"{type(e).__name__}: {e}")
```

失败不抛出到 Agent，而是转成工具结果。这样 LLM 可以看到错误并尝试修正。

```python
40     def execute(self, arguments: dict[str, Any]) -> str:
41         raise NotImplementedError
```

抽象方法。子类必须实现。

```python
43     def resolve_workspace_path(self, raw_path: str) -> Path:
```

安全路径解析函数。所有文件工具都应该用它。

```python
44         if not raw_path:
45             raise ToolError("path is required")
```

空路径直接报错。

```python
46         path = (self.workspace / raw_path).resolve()
```

把用户传入的相对路径拼到 workspace 下，并解析成真实绝对路径。

```python
47         if path != self.workspace and self.workspace not in path.parents:
48             raise ToolError(f"path escapes workspace: {raw_path}")
```

安全边界：禁止访问 workspace 外部路径。比如 `../../.ssh/id_rsa` 会被拦截。

```python
49         return path
```

返回安全路径。

## tools/file_tools.py

文件职责：实现三个最小文件工具。

```python
1  from typing import Any
3  from tools.base import BaseTool, ToolError
```

导入类型标注和工具基类。

### ListFilesTool

```python
6  class ListFilesTool(BaseTool):
7      name = "list_files"
8      description = "List files under a directory inside the workspace."
```

定义列目录工具。模型调用工具时用名字 `list_files`。

```python
9-18 parameters = {...}
```

定义参数 schema。它要求模型传一个 `path` 字符串。

```python
20     def execute(self, arguments: dict[str, Any]) -> str:
```

具体执行逻辑。

```python
21         path = self.resolve_workspace_path(arguments.get("path", "."))
```

取参数里的 path，默认 `.` 表示 workspace 根目录，并做安全解析。

```python
22         if not path.exists():
23             raise ToolError(...)
24         if not path.is_dir():
25             raise ToolError(...)
```

检查路径存在且是目录。

```python
26         entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
```

读取目录内容并排序。目录排前面，文件排后面，同类按名字排序。

```python
27         if not entries:
28             return "(empty)"
```

空目录返回 `(empty)`。

```python
29         lines = []
30         for entry in entries:
31             suffix = "/" if entry.is_dir() else ""
32             rel = entry.relative_to(self.workspace)
33             lines.append(f"{rel}{suffix}")
34         return "\n".join(lines)
```

把每个条目转成相对 workspace 的路径。目录后面加 `/`，最后用换行拼接。

### ReadFileTool

```python
37 class ReadFileTool(BaseTool):
38     name = "read_file"
39     description = "Read a UTF-8 text file inside the workspace."
```

定义读文件工具。

```python
40-49 parameters = {...}
```

要求传入 `path`。

```python
51     def execute(self, arguments: dict[str, Any]) -> str:
52         path = self.resolve_workspace_path(arguments.get("path", ""))
```

解析并校验路径。

```python
53         if not path.exists():
54             raise ToolError(...)
55         if not path.is_file():
56             raise ToolError(...)
```

检查文件存在且是普通文件。

```python
57         return path.read_text(encoding="utf-8")
```

读取 UTF-8 文本并返回给模型。

### WriteFileTool

```python
60 class WriteFileTool(BaseTool):
61     name = "write_file"
62     description = "Write UTF-8 text to a file inside the workspace..."
```

定义写文件工具。

```python
63-76 parameters = {...}
```

要求传入：

- `path`：写到哪里。
- `content`：写什么内容。

```python
78     def execute(self, arguments: dict[str, Any]) -> str:
79         path = self.resolve_workspace_path(arguments.get("path", ""))
```

解析写入路径，并确认不会越过 workspace。

```python
80         content = arguments.get("content")
81         if content is None:
82             raise ToolError("content is required")
```

读取内容参数，缺失则报错。

```python
83         path.parent.mkdir(parents=True, exist_ok=True)
```

确保父目录存在。

```python
84         path.write_text(str(content), encoding="utf-8")
```

写入文本。

```python
85         return f"written: {path.relative_to(self.workspace)}"
```

返回写入成功的信息。

## tools/memory_tools.py

文件职责：把 `MemoryManager` 暴露成模型可调用的工具。

当前有三个工具：

```text
remember_memory  写入长期记忆
memory_search    搜索相关长期记忆
read_memory      读取长期记忆
```

```python
1  from pathlib import Path
2  from typing import Any
4  from memory import MemoryManager
5  from tools.base import BaseTool
```

导入依赖：

- `Path`：工具初始化时仍然接收 workspace。
- `Any`：工具参数类型。
- `MemoryManager`：实际负责记忆保存、检索、读取。
- `BaseTool`：所有工具的父类。

### RememberMemoryTool

```python
8  class RememberMemoryTool(BaseTool):
9      name = "remember_memory"
10     description = "Save a durable user preference or fact into long-term memory."
```

定义写记忆工具。

模型会根据 `name` 调用它，根据 `description` 判断什么时候应该用它。

```python
11-20 parameters = {...}
```

定义参数 schema。它要求模型传入：

```json
{"content": "需要长期记住的事实或偏好"}
```

```python
22     def __init__(self, workspace: Path, memory: MemoryManager):
23         super().__init__(workspace)
24         self.memory = memory
```

初始化工具。

虽然 memory 工具不直接操作 workspace，但仍调用 `BaseTool.__init__()` 保持所有工具构造方式一致。

`self.memory` 保存同一个 `MemoryManager` 实例，确保工具写入的语义记忆、ContextManager 检索注入的内容、自动 flush 生成的 daily 情景记忆都能进入同一套检索索引。

```python
26     def execute(self, arguments: dict[str, Any]) -> str:
27         return self.memory.remember(arguments.get("content", ""), source=self.name)
```

执行写记忆：

- 从参数中取 `content`。
- 调用 `MemoryManager.remember()` 写入 `MEMORY.md` 语义记忆，并重建检索索引。
- `source=self.name` 表示这条记忆来自 `remember_memory` 工具。

### SearchMemoryTool

```python
30 class SearchMemoryTool(BaseTool):
31     name = "memory_search"
32     description = "Search relevant long-term memories by natural language query."
```

定义记忆搜索工具。

模型可以用它主动查找长期记忆，尤其适合用户问“你还记得我之前说过什么吗”这类问题。

```python
51     def execute(self, arguments: dict[str, Any]) -> str:
52         query = arguments.get("query", "")
53         max_results = int(arguments.get("max_results", 5))
54         results = self.memory.search(query, limit=max_results)
```

执行搜索：

- `query` 是自然语言问题或关键词。
- `max_results` 控制返回条数。
- `self.memory.search()` 返回带 score 的结果。

如果没有结果，返回 `(no relevant memories found)`。

如果有结果，格式化为：

```text
1. 记忆内容 (id=mem_xxx, score=0.72)
```

### ReadMemoryTool

```python
30 class ReadMemoryTool(BaseTool):
31     name = "read_memory"
32     description = "Read the current long-term memory file."
```

定义读记忆工具。

```python
33-36 parameters = {...}
```

这个工具不需要参数，所以 `properties` 是空字典。

```python
38     def __init__(self, workspace: Path, memory: MemoryManager):
39         super().__init__(workspace)
40         self.memory = memory
```

初始化读记忆工具，同样保存 `MemoryManager` 实例。

```python
42     def execute(self, arguments: dict[str, Any]) -> str:
43         content = self.memory.read_main_memory()
44         return content or "(memory is empty)"
```

读取长期记忆。

这里读取的是 `data/memory/MEMORY.md` 人类可读视图，而不是旧的单文件追加记忆。

如果 memory 为空，返回明确提示 `(memory is empty)`，避免模型误以为空字符串是工具异常。

## tools/registry.py

文件职责：管理工具集合，并根据模型输出的工具名执行对应工具。

```python
1  import json
2  from pathlib import Path
3  from typing import Any, Union
```

导入 JSON、路径和类型标注。

```python
5  from memory import MemoryManager
6  from tools.base import BaseTool, ToolResult
7  from tools.bash_tool import BashTool
8  from tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
9  from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool
```

导入：

- `MemoryManager`：memory 工具依赖的记忆对象。
- `BaseTool` 和 `ToolResult`：工具抽象和工具结果。
- `BashTool`：受限 shell 命令工具。
- 文件工具：`glob/read_file/write_file/edit_file`。
- 记忆工具：`remember_memory/memory_search/read_memory`。

```python
9  class ToolRegistry:
```

定义工具注册表。

```python
10     def __init__(
11         self,
12         workspace: Path,
13         memory: MemoryManager,
14         bash_timeout: int = 20,
15     ):
16         self.tools: dict[str, BaseTool] = {}
```

初始化工具注册表。

参数说明：

- `workspace`：文件工具的安全工作目录。
- `memory`：memory 工具读写的长期记忆管理器。
- `bash_timeout`：受限 bash 的默认超时时间。

`self.tools` 是工具字典，key 是工具名，value 是工具实例。

```python
17         for tool in (
18             BashTool(workspace, default_timeout=bash_timeout),
19             GlobTool(workspace),
20             ReadFileTool(workspace),
21             WriteFileTool(workspace),
22             EditFileTool(workspace),
23             RememberMemoryTool(workspace, memory),
24             SearchMemoryTool(workspace, memory),
25             ReadMemoryTool(workspace, memory),
26         ):
27             self.tools[tool.name] = tool
```

创建工具实例，并按名字注册。

注册完成后，模型能看到八个工具：

```text
bash
glob
read_file
write_file
edit_file
remember_memory
memory_search
read_memory
```

```python
19     def schemas(self) -> list[dict[str, Any]]:
20         return [tool.schema() for tool in self.tools.values()]
```

返回所有工具的 OpenAI schema。这个结果会发给 LLM，让模型知道有哪些工具能用。

```python
22     def run(self, name: str, arguments: Union[str, dict[str, Any]]) -> ToolResult:
```

按工具名执行工具。`arguments` 可能是 JSON 字符串，也可能已经是 dict。

```python
23         tool = self.tools.get(name)
24         if tool is None:
25             return ToolResult(ok=False, content=f"Unknown tool: {name}")
```

如果模型请求了不存在的工具，返回错误结果。

```python
27         if isinstance(arguments, str):
28             try:
29                 parsed = json.loads(arguments or "{}")
```

如果参数是字符串，就按 JSON 解析。

```python
30             except json.JSONDecodeError as e:
31                 return ToolResult(ok=False, content=f"Invalid JSON arguments: {e}")
```

如果 JSON 参数非法，返回错误结果。

```python
32         else:
33             parsed = arguments
```

如果参数已经是 dict，直接用。

```python
35         return tool.run(parsed)
```

调用工具统一入口 `run()`。

## 你现在应该理解的最小闭环

```text
main.py 读取用户输入
  -> Agent.run(user_input)
  -> 把 user_input 加入 messages
  -> LLM.chat(messages, tools=schema)
  -> 如果 LLM 返回 content：直接输出
  -> 如果 LLM 返回 tool_calls：
       ToolRegistry.run(tool_name, arguments)
       工具结果作为 role=tool 写回 messages
       再调用 LLM
  -> 直到得到最终 content 或达到 max_steps
```

## 先掌握这 5 个关键词

```text
messages     对话历史，LLM 的输入上下文
tool schema  工具说明，告诉 LLM 工具有哪些、参数怎么传
tool_call    LLM 决定调用工具时返回的结构
ToolResult   工具执行结果，回填给 LLM
Trace        一次请求的 request_id 和耗时记录
```

## 当前架构说明：Prompt、成熟 Memory、增强 Tools

当前主链路是：

```text
prompts/identity.md
MemoryManager
bash/glob/read_file/write_file/edit_file
remember_memory/memory_search/read_memory
```

## prompts/loader.py

文件职责：从 `prompts/identity.md` 读取 system prompt，让 Agent 的角色、规则和工具策略从代码里拆出来。

### PromptLoader.__init__(identity_path)

```python
def __init__(self, identity_path: Path):
    self.identity_path = identity_path
```

这个函数只保存 identity 文件路径，不立即读取文件。这样做的好处是初始化对象很轻，真正需要 prompt 时再调用 `load_identity()`。

### PromptLoader.load_identity()

```python
def load_identity(self) -> str:
```

这个函数负责读取 `identity.md`：

1. 如果文件不存在，抛出 `FileNotFoundError`，让启动阶段直接暴露配置问题。
2. 读取 UTF-8 文本并 `.strip()`，去掉首尾空白。
3. 如果内容为空，抛出 `ValueError`，避免给模型发送空 system prompt。
4. 返回最终 prompt 字符串。

## memory 分层实现

文件职责：当前 memory 不再把数据库作为记忆本体，而是分为：

```text
raw_store.py       原始对话 JSONL
episodic_store.py  daily 情景记忆
semantic_store.py  MEMORY.md 语义记忆
index.py           SQLite/FTS 检索索引
curator.py         LLM 总结与做梦整理
manager.py         门面编排层
```

## memory/raw_store.py

### RawConversationStore.__init__(memory_dir)

创建 `data/memory/raw/` 目录。这个目录保存原始对话，是后续追溯和重新总结的事实来源。

### RawConversationStore.append_message(session_id, role, content, name, metadata)

把一条消息追加到：

```text
data/memory/raw/{session_id}/YYYY-MM-DD.jsonl
```

每行是一个 JSON 对象，包含时间、会话、角色、内容、工具名和元数据。`Agent.run()` 会用它记录 user、assistant、tool 消息。

### RawConversationStore.read_day(session_id, day)

读取某个会话某一天的 JSONL，解析成 dict 列表。解析失败的坏行会跳过，避免单行损坏影响整个文件。

### RawConversationStore._path_for(session_id, day)

根据 session 和日期生成 JSONL 文件路径。

### RawConversationStore._safe_session_id(session_id)

把 session_id 中不适合做路径的字符替换成 `_`，避免路径穿越或非法文件名。

### RawConversationStore._now()

返回秒级 ISO 时间字符串，用作 raw 记录的 `created_at`。

## memory/episodic_store.py

### EpisodicMemoryStore.__init__(memory_dir)

创建 `data/memory/daily/` 目录。这里保存 daily 情景记忆。

### EpisodicMemoryStore.append_summary(summary, reason, session_id, day)

把 LLM 生成的事件摘要追加到当天 daily 文件。它会：

1. 调用 `_normalize_items()` 清洗摘要条目。
2. 没有有效条目时返回 0。
3. 确保当天文件存在。
4. 写入标题、session_id 和 bullet 列表。
5. 返回写入条数。

### EpisodicMemoryStore.read_day(day)

读取某一天的 daily 情景记忆。没有文件时返回空字符串。

### EpisodicMemoryStore.read_recent(days)

读取最近 N 天 daily 文件，用于做梦机制从情景记忆沉淀语义记忆。

### EpisodicMemoryStore.list_files()

返回所有 daily markdown 文件，用于 MemoryIndex 重建索引。

### EpisodicMemoryStore.path_for(day, ensure_exists)

生成 daily 文件路径；`ensure_exists=True` 时会创建文件并写入标题。

### EpisodicMemoryStore._normalize_items(summary)

把 LLM 输出清洗成条目列表。支持字符串和 list，会去掉 `- ` 前缀，过滤空内容和 `无`。

### EpisodicMemoryStore._reason_title(reason)

把 `context_trim`、`overflow`、`daily_summary` 等原因映射成人类可读标题。

## memory/semantic_store.py

### SemanticMemoryStore.__init__(memory_dir)

设置 `data/memory/MEMORY.md` 路径，并调用 `_ensure_exists()` 确保语义记忆文件存在。

### SemanticMemoryStore.read()

读取 `MEMORY.md` 内容。语义记忆是长期稳定事实、偏好、规则和项目决策。

### SemanticMemoryStore.remember(content, source)

显式保存一条语义记忆。流程：

1. 清洗空白。
2. 用 `_contains_memory()` 避免重复写入。
3. 确保存在 `## Manual Notes` 区块。
4. 追加 bullet，并用 HTML 注释记录 source 和时间。

### SemanticMemoryStore.overwrite(content)

整体重写 `MEMORY.md`。做梦机制会用它把 daily 情景记忆沉淀后的语义记忆写回文件。

### SemanticMemoryStore._ensure_exists()

如果 `MEMORY.md` 不存在，就创建基础模板。

### SemanticMemoryStore._ensure_manual_section()

确保 `MEMORY.md` 里有 `## Manual Notes`，供 `remember_memory` 工具追加人工语义记忆。

### SemanticMemoryStore._contains_memory(content, text)

按 bullet 文本查重。它会忽略 `<!-- source... -->` 注释。

### SemanticMemoryStore._now()

返回秒级 ISO 时间字符串，用于记录手动语义记忆来源。

## memory/index.py

### MemoryRecord

索引命中的记忆片段。字段包括：

```text
id
content
source: semantic | episodic
path
start_line
end_line
created_at
updated_at
```

这里的 record 是“索引片段”，不是记忆本体。

### MemorySearchResult

保存检索结果和相关性分数。

### MemoryIndex.__init__(db_path)

打开 SQLite 数据库，创建 `memory_chunks` 表和可选 FTS5 表。

### MemoryIndex.rebuild(files, root)

重建索引：

1. 清空旧索引。
2. 遍历 `(path, source_type)` 文件列表。
3. 调用 `index_file()` 把 `MEMORY.md` 和 daily 文件分块写入索引。

### MemoryIndex.clear()

清空 `memory_chunks` 和 FTS 表。只清索引，不删除记忆本体文件。

### MemoryIndex.index_file(path, source_type, root)

读取 markdown 文件，调用 `_chunk_markdown()` 分块，然后写入 SQLite 和 FTS。

### MemoryIndex.search(query, limit, min_score)

检索流程：

1. `_candidate_rows()` 先用 FTS 找候选，候选不足时按更新时间补齐。
2. `_score()` 对候选重新打分。
3. 过滤低分结果。
4. 按 `(score, updated_at)` 倒序排序。
5. 返回 Top-K。

### MemoryIndex.list_recent(limit)

返回最近更新的索引片段，主要用于调试和工具展示。

### MemoryIndex.close()

关闭 SQLite 连接。

### MemoryIndex._init_db()

创建 `memory_chunks` 表、source 索引和 FTS5 虚拟表。FTS 不可用时降级为普通候选检索。

### MemoryIndex._candidate_rows(query, limit)

优先通过 `_search_fts_ids()` 召回候选；如果候选不足，再从最近更新的 chunks 补齐。

### MemoryIndex._search_fts_ids(query, limit)

把 query 拆 token 后拼成 FTS 查询表达式，返回命中的 chunk id。

### MemoryIndex._upsert_fts(chunk_id, text)

同步 FTS 表：先删旧记录，再插入新文本。

### MemoryIndex._record_from_row(row)

把 SQLite row 转成 `MemoryRecord`。

### MemoryIndex._score(query, text, source_type)

本地相关性打分：

```text
完整 query 命中加分
token 命中比例加分
semantic 来源额外加分
episodic 来源少量加分
```

### MemoryIndex._chunk_markdown(content)

按 markdown 二级标题切块。每个 chunk 保留起止行号，方便搜索结果回溯来源。

### MemoryIndex._tokens(text)

提取英文数字 token 和中文单字 token，用于 FTS 查询和打分。

### MemoryIndex._make_chunk_id(path, start_line, end_line, text)

根据路径、行号和文本 hash 生成稳定 chunk id。

## memory/curator.py

### DAILY_SUMMARY_SYSTEM_PROMPT

daily 情景记忆总结 prompt。要求按事件总结，合并多轮讨论，保留用户需求、反馈、决策、文件路径和待办事项，忽略寒暄和工具噪声。

### DREAM_SYSTEM_PROMPT

做梦整理 prompt。输入当前 `MEMORY.md` 和近期 daily 情景记忆，输出 `[MEMORY]` 和 `[DREAM]` 两段。

### MemoryCurator.__init__(llm)

保存 LLM 客户端。没有 LLM 时，daily 总结和做梦都会跳过。

### MemoryCurator.summarize_daily(messages, existing_daily_memory, session_id, reason, max_messages)

把被裁剪的 messages 总结成 daily 情景记忆：

1. 没有 messages 或 LLM 时返回空列表。
2. 按 `max_messages` 截取输入。
3. 用 `format_messages()` 压缩消息。
4. 构造包含 date/session/reason/已有 daily 的 prompt。
5. 调用 LLM。
6. 用 `parse_bullets()` 解析 bullet 输出。

### MemoryCurator.deep_dream(semantic_memory, episodic_memory)

把近期情景记忆沉淀成语义记忆。返回 `(new_memory, dream_diary)`。

### MemoryCurator.format_messages(messages)

把 OpenAI messages 转成适合总结的文本。普通消息保留 `role: content`，工具消息调用 `_summarize_tool_message()` 压缩。

### MemoryCurator.parse_bullets(text)

把 LLM 输出的 bullet list 转成字符串列表。会过滤空行和 `无`。

### MemoryCurator.parse_dream_output(text)

从 LLM 输出中解析 `[MEMORY]` 和 `[DREAM]` 两段。

### MemoryCurator._summarize_tool_message(name, content)

压缩工具结果。短结果原样保留，长结果只保留头部和尾部，避免 daily 总结 prompt 被工具输出撑爆。

## memory/manager.py

### MemoryManager.__init__(memory_dir, llm)

初始化并组合五个子模块：

```text
RawConversationStore
EpisodicMemoryStore
SemanticMemoryStore
MemoryIndex
MemoryCurator
```

最后调用 `rebuild_index()`，让索引和当前 `MEMORY.md`、daily 文件同步。

### MemoryManager.remember(content, source, scope, metadata)

显式保存语义记忆：

1. 调用 `SemanticMemoryStore.remember()` 写 `MEMORY.md`。
2. 调用 `rebuild_index()` 更新检索索引。
3. 返回保存结果。

### MemoryManager.record_message(session_id, role, content, name, metadata)

把原始对话写入 raw JSONL。`Agent.run()` 会在 user、assistant、tool 三类消息产生时调用。

### MemoryManager.search(query, limit, min_score)

委托 `MemoryIndex.search()` 搜索 semantic 和 episodic 记忆。

### MemoryManager.format_for_prompt(query, limit)

兼容接口。只返回 `build_prompt()` 生成的 prompt。

### MemoryManager.build_prompt(query, limit)

检索相关记忆，并按 semantic / episodic 分组生成 system prompt 片段。

### MemoryManager.read_main_memory()

读取 `MEMORY.md` 语义记忆。

### MemoryManager.read_today_episodic_memory()

读取当天 daily 情景记忆。

### MemoryManager.list_records(limit)

返回最近索引片段，主要用于调试和 memory_search 工具展示。

### MemoryManager.flush_from_messages(messages, reason, session_id, max_messages)

上下文裁剪时触发：

1. 读取已有当天 daily。
2. 调用 `MemoryCurator.summarize_daily()` 生成高质量情景摘要。
3. 调用 `EpisodicMemoryStore.append_summary()` 写入 daily 文件。
4. 如果写入了新条目，调用 `rebuild_index()`。
5. 返回 trace 可记录的统计信息。

### MemoryManager.deep_dream(lookback_days)

读取当前 `MEMORY.md` 和最近 N 天 daily，调用 `MemoryCurator.deep_dream()`，将结果写回 `MEMORY.md`，并写入 `dreams/YYYY-MM-DD.md`。

### MemoryManager.rebuild_index()

把 `MEMORY.md` 作为 semantic 文件，把 `daily/*.md` 作为 episodic 文件，重建 SQLite/FTS 索引。

### MemoryManager.close()

关闭索引数据库连接。

### MemoryManager._write_dream_diary(content)

把做梦日志写入 `data/memory/dreams/YYYY-MM-DD.md`。空内容不写。

## agent/context_manager.py 本轮变更

### ContextManager.__init__()

新增参数：

```text
memory_search_results  每次注入多少条相关记忆
memory_flush_messages  裁剪时最多拿多少条旧消息生成记忆
```

`memory` 类型是 `MemoryManager`，负责 raw 原文保存、daily 情景记忆生成、语义记忆检索和注入。

### ContextManager.build_model_messages(run_context)

当前流程：

1. `_trim_history()` 必要时裁剪历史。
2. `_latest_user_message()` 取当前用户问题作为 memory query。
3. `_build_system_prompt(query)` 拼 identity、memory、skills。
4. system message 放第一条。
5. 追加裁剪后的 history。
6. 写 trace：消息数、history 数、memory 注入数量、skill 数量。

### ContextManager._build_system_prompt(query)

组装 system prompt：

```text
identity.md 内容
相关长期记忆
可用 skills
```

memory 不再全量注入，而是调用 `MemoryManager.build_prompt(query)` 注入相关 Top-K。

### ContextManager._trim_history(run_context)

当 `history` 超过 `max_history_messages`：

1. 计算要丢弃的消息。
2. 保留最后 N 条。
3. 如果保留片段开头是 tool message，就继续丢弃，避免 OpenAI messages 结构非法。
4. 用裁剪后的内容替换 `self.history`，避免下次重复 flush。
5. 记录 `context_trimmed` trace。
6. 调用 `MemoryManager.flush_from_messages()` 生成 daily 情景记忆。
7. 记录 `memory_flush` trace。

### ContextManager._latest_user_message(history)

从后往前找最近一条 user 消息，作为 memory 检索 query。找不到就返回空字符串。

## tools/file_tools.py 本轮变更

### GlobTool.execute()

读取 `pattern` 和 `max_results`，校验 pattern 后调用 `workspace.glob(pattern)`。结果只返回 workspace 内路径，目录加 `/` 后缀。

### GlobTool._validate_pattern()

拒绝三类 pattern：

```text
空 pattern
绝对路径或 ~
包含 ..
```

### ReadFileTool.execute()

读取 UTF-8 文件，新增：

```text
offset  起始行，支持负数从文件末尾读
limit   最大行数
```

返回内容前会加文件路径和行号信息，并限制最大输出字符数。

### WriteFileTool.execute()

写入 UTF-8 文件。如果父目录不存在会自动创建。返回相对路径和写入字节数。

### EditFileTool.execute()

精确编辑文件：

1. 读取 path、old_text、new_text。
2. 支持 `oldText/newText` 作为兼容别名。
3. old_text 为空时追加到文件末尾。
4. old_text 非空时必须出现且只出现一次。
5. 写回文件并返回第一处变更行号。

### _select_lines(lines, offset, limit)

根据 offset/limit 选取行范围。offset 从 1 开始；负数表示从末尾倒数。

### _truncate_text(text, max_chars)

超过最大字符数时截断，并返回是否截断。

### _append_text(content, new_text)

追加文本时处理换行：

```text
空文件：直接写 new_text
已有内容以换行结尾：直接拼接
已有内容不以换行结尾：先补一个换行
```

## tools/bash_tool.py

文件职责：提供受限 shell 能力，用于测试、搜索、格式化和短诊断。

### BashTool.__init__(workspace, default_timeout)

保存 workspace，并把默认超时限制在 1 到 30 秒之间。

### BashTool.execute(arguments)

执行流程：

1. 读取 command。
2. 用 `_resolve_timeout()` 得到最终超时。
3. 调用 `_check_safety()` 做静态安全检查。
4. 用 `subprocess.run()` 在 workspace 下执行。
5. 合并 stdout/stderr。
6. 用 `_truncate_output()` 截断输出。
7. 返回 exit_code 和命令输出。

### BashTool._resolve_timeout(raw_timeout)

没有传 timeout 时用默认值；传了就限制到 1 到 30 秒。

### BashTool._safe_env()

只传少量环境变量给子进程：

```text
HOME
LANG
LC_ALL
PATH
PYTHONPATH
VIRTUAL_ENV
```

这样可以降低 API Key 等敏感变量泄露风险。

### BashTool._check_safety(command)

拒绝明显危险命令：

```text
.env / API_KEY
sudo / su / shutdown / reboot / mkfs / diskutil
dd 磁盘写入
curl|sh / wget|bash
绝对路径
~ 路径
.. 路径穿越
rm -rf . 或 rm -rf *
```

这不是系统级沙箱，只是当前阶段的应用层安全边界。

### BashTool._tokens(command)

优先用 `shlex.split()` 按 shell 规则拆命令。拆失败时退化为普通空格分割。

### BashTool._deletes_workspace_root(command)

专门识别 `rm -rf .`、`rm -rf *` 等删除 workspace 根目录的情况。

### BashTool._truncate_output(output)

输出超过 20000 字符时保留尾部，避免工具结果撑爆模型上下文。

## tools/memory_tools.py 本轮变更

### RememberMemoryTool.execute()

调用 `MemoryManager.remember()` 显式保存一条语义记忆。

### SearchMemoryTool.execute()

调用 `MemoryManager.search()` 搜索相关记忆，并把结果格式化为带 ID 和 score 的列表。

### ReadMemoryTool.execute()

读取 `data/memory/MEMORY.md` 语义记忆。

## tools/registry.py 本轮变更

### ToolRegistry.__init__()

当前注册工具：

```text
bash
glob
read_file
write_file
edit_file
remember_memory
memory_search
read_memory
```

当前注册表包含受限 bash、glob、edit_file、memory_search 等工程 Agent 基础能力。

### ToolRegistry.schemas()

逻辑不变，仍然返回所有工具的 OpenAI function schema。

### ToolRegistry.run(name, arguments)

逻辑不变，仍然负责：

```text
按 name 找工具
把 JSON 字符串参数解析成 dict
调用 tool.run()
把错误包装成 ToolResult
```

## agent/agent.py 本轮变更

### Agent.__init__()

初始化顺序现在是：

1. 创建 LLM 客户端。
2. 用 `PromptLoader` 读取 `prompts/identity.md`。
3. 创建 `MemoryManager(data/memory, llm)`。
4. 创建 `SkillLoader`。
5. 创建增强后的 `ToolRegistry`。
6. 创建 `ContextManager`，传入 identity、memory、skills 和裁剪参数。

### Agent.run()

主循环结构不变。关键变化是：`Agent.run()` 会把 user/assistant/tool 消息写入 raw JSONL，`ContextManager.build_model_messages()` 会处理 memory 检索和上下文裁剪触发的 daily 情景记忆 flush。

## config.py 本轮变更

### Settings 新字段

```text
project_root             项目根目录
max_history_messages     history 最大保留条数
memory_search_results    每次注入多少条相关记忆
memory_flush_messages    裁剪时最多用多少条旧消息生成记忆
bash_timeout             bash 默认超时
```

这些字段让模块不再用 `workspace.parent` 猜路径。

### load_settings()

新增读取：

```text
MAX_HISTORY_MESSAGES
MEMORY_SEARCH_RESULTS
MEMORY_FLUSH_MESSAGES
BASH_TIMEOUT
```

没有配置时使用保守默认值。
