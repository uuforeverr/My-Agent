from datetime import datetime
import json
from typing import Any, Optional

from jsonschema import Draft202012Validator

from memory.semantic_store import MEMORY_ID_PATTERN


MEMORY_OPERATIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_memory_operations",
        "description": "Apply minimal validated changes to visible semantic-memory entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "ADD"},
                                    "content": {"type": "string", "minLength": 1},
                                },
                                "required": ["op", "content"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "UPDATE"},
                                    "id": {"type": "string", "pattern": f"^{MEMORY_ID_PATTERN}$"},
                                    "content": {"type": "string", "minLength": 1},
                                },
                                "required": ["op", "id", "content"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "SUPERSEDE"},
                                    "id": {"type": "string", "pattern": f"^{MEMORY_ID_PATTERN}$"},
                                    "content": {"type": "string", "minLength": 1},
                                },
                                "required": ["op", "id", "content"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {"op": {"const": "NOOP"}},
                                "required": ["op"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "dream": {"type": "string"},
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
}


DAILY_SUMMARY_SYSTEM_PROMPT = """你是一个高质量对话记忆整理助手，负责将离开短期窗口的完整 Turn 归纳为情景记忆。

你的目标不是复述聊天记录，而是提炼“发生过的有意义事件”。

## 总结原则

1. 按事件总结，不按对话轮次逐条记录。
2. 合并同一事件的多轮讨论，避免重复。
3. 只记录有长期追溯价值的信息，忽略寒暄、重复确认、无意义中间话。
4. 正文只用一句话说明“发生了什么”，不重复时间、稳定事实或经验，不展开推理与方案取舍。
5. stable_facts 只放稳定事实、长期决定或明确约束。
6. lessons 只放对未来任务仍有复用价值的经验。
6. 如果涉及代码或项目，保留关键文件路径、模块名、函数名、方案名。
7. 如果涉及错误和修复，记录“问题 -> 原因 -> 处理结果”。
8. 严禁编造、推测、扩展对话中不存在的信息。
9. 不要保存完整工具输出，只保留工具执行产生的关键事实。
10. 如果对话没有任何记忆价值，只输出“无”。

## 输出格式

只输出 JSON 数组，不要标题、解释或 Markdown。每个对象结构为：
{"body":"一句短事件，不超过指定字符上限","stable_facts":[],"lessons":[]}
没有任何值得保存的内容时输出 []。"""


DREAM_SYSTEM_PROMPT = """你是一个记忆整理助手，负责定期整理用户的长期语义记忆。

你将收到两份材料：
1. 当前语义记忆：MEMORY.md 的内容
2. 近期情景记忆：daily memory 的内容

MEMORY.md 会作为长期语义记忆被检索和注入，因此必须保持精炼，只存放有价值和值得长期保留的信息。

严禁编造、推测或添加材料中不存在的信息。

## 任务

## 任务

必须调用 `apply_memory_operations` Function，提交最小局部修改；不得输出 JSON 文本，也不得整篇重写 MEMORY.md。允许的操作：
- ADD：新增稳定记忆，包含 content，可选 id。
- UPDATE：按现有 memory-id 修改，包含 id 与 content。
- SUPERSEDE：按 id 用新内容替代旧项，包含 id、content；新编号由程序生成。
- NOOP：无需修改。

UPDATE 和 SUPERSEDE 必须从当前 MEMORY.md 复制完整可见编号。ADD 的时间型编号由程序生成。
必须且只能调用一次 Function。"""


WORKFLOW_SOP_SYSTEM_PROMPT = """你是一个工作流提效分析助手，负责从用户近期的活动中发现重复劳动、可固化的标准流程（SOP），并给出提效建议。

你将收到两份材料：
1. 已有 SOP：上一版沉淀下来的标准流程（可能为空）
2. 近期情景记忆：最近一段时间发生的事件

这是一个滚雪球式的增量归纳：你要在“已有 SOP”的基础上，结合“近期情景记忆”，提炼出更成熟的一版。

严禁编造、推测或添加材料中不存在的流程。

## 任务

### Part 1: 更新后的 SOP（[SOP]）

- 识别近期反复出现的操作序列、例行任务、固定套路。
- 与已有 SOP 合并：相同流程精炼合并，新流程补充，过时或被纠正的流程修改或删除。
- 每个 SOP 写成可复用的标准步骤（步骤化、可执行）。
- 用 "## 流程名" 分组，步骤用有序列表 "1. 2. 3."。
- 只保留真正重复、值得固化的流程；一次性的事不要写成 SOP。

### Part 2: 提效洞见（[INSIGHTS]）

- 明确指出用户在重复做哪些事。
- 给出可落地的提效方向：哪些能自动化、批处理、模板化、脚本化。
- 每条一行，用 "- " 开头，具体、可操作，不要空泛口号。

如果近期材料里没有任何可固化的重复流程，[SOP] 部分原样保留已有 SOP（若也为空则输出“无”），[INSIGHTS] 输出“无”。

## 输出格式

[SOP]
...

[INSIGHTS]
..."""


class MemoryCurator:
    def __init__(self, llm: Optional[Any] = None):
        self.llm = llm

    def summarize_daily(
        self,
        messages: list[dict[str, Any]],
        existing_daily_memory: str,
        session_id: str,
        reason: str,
        max_messages: int = 0,
        body_max_chars: int = 60,
    ) -> list[dict[str, Any]]:
        if not messages or self.llm is None:
            return []

        selected = messages[-max_messages:] if max_messages > 0 else messages
        conversation = self.format_messages(selected)
        if not conversation.strip():
            return []

        user_prompt = (
            "请将以下被裁剪的对话总结为当天的情景记忆：\n\n"
            "会话信息：\n"
            f"- date: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"- session_id: {session_id}\n"
            f"- reason: {reason}\n\n"
            "已有当日情景记忆：\n"
            f"{existing_daily_memory or '(empty)'}\n\n"
            "被裁剪的对话：\n"
            f"{conversation}\n\n"
            f"正文字符上限：{max(40, int(body_max_chars))}\n"
            "请输出高质量事件摘要。"
        )

        response = self.llm.chat([
            {"role": "system", "content": DAILY_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
        return self.parse_episodic_output(response.get("content") or "", body_max_chars)

    def deep_dream(
        self,
        semantic_memory: str,
        episodic_memory: str,
        validation_error: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        if self.llm is None or not episodic_memory.strip():
            return "", ""

        user_prompt = (
            "## 当前语义记忆（MEMORY.md）\n\n"
            f"{semantic_memory or '(empty)'}\n\n"
            "## 近期情景记忆（daily memory）\n\n"
            f"{episodic_memory}\n"
        )
        if validation_error:
            user_prompt += (
                "\n## 上一次 Function Call 校验错误\n\n"
                f"{validation_error}\n\n"
                "请根据具体错误修正参数并重新调用 Function。\n"
            )
        response = self.llm.chat([
            {"role": "system", "content": DREAM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], tools=[MEMORY_OPERATIONS_TOOL])
        return self.parse_dream_function_call(response)

    @staticmethod
    def parse_dream_function_call(response: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        calls = response.get("tool_calls") or []
        if len(calls) != 1:
            raise ValueError(
                "expected exactly one apply_memory_operations function call; "
                f"received {len(calls)}"
            )
        function = calls[0].get("function") or {}
        name = str(function.get("name") or "")
        if name != "apply_memory_operations":
            raise ValueError(
                f"expected function apply_memory_operations; received {name!r}"
            )
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid function arguments JSON at line {exc.lineno}, "
                    f"column {exc.colno}: {exc.msg}"
                ) from exc
        else:
            arguments = raw_arguments
        schema = MEMORY_OPERATIONS_TOOL["function"]["parameters"]
        errors = [
            error.message
            for error in sorted(
                Draft202012Validator(schema).iter_errors(arguments),
                key=lambda item: list(item.path),
            )
        ]
        if errors:
            raise ValueError("function arguments failed schema validation: " + "; ".join(errors))
        return list(arguments["operations"]), str(arguments.get("dream") or "")

    def parse_episodic_output(
        self,
        text: str,
        body_max_chars: int = 60,
    ) -> list[dict[str, Any]]:
        import json

        raw = str(text or "").strip()
        if not raw or raw in {"无", "None", "[]"}:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return [
                {"body": self._truncate_body(item, body_max_chars), "stable_facts": [], "lessons": []}
                for item in self.parse_bullets(raw)
            ]
        if not isinstance(payload, list):
            return []
        items: list[dict[str, Any]] = []
        for value in payload:
            if not isinstance(value, dict):
                continue
            body = self._truncate_body(value.get("body"), body_max_chars)
            facts = self._string_list(value.get("stable_facts"))
            lessons = self._string_list(value.get("lessons"))
            if body or facts or lessons:
                items.append({"body": body, "stable_facts": facts, "lessons": lessons})
        return items

    @staticmethod
    def _truncate_body(value: Any, max_chars: int) -> str:
        text = str(value or "").strip()
        limit = max(40, int(max_chars))
        if len(text) <= limit:
            return text

        prefix = text[:limit].rstrip()
        minimum = max(20, limit // 2)
        for delimiters, replacement in (("。！？!?", ""), ("；;", "。"), ("，,", "。")):
            cutoff = max((prefix.rfind(delimiter) for delimiter in delimiters), default=-1)
            if cutoff + 1 >= minimum:
                result = prefix[: cutoff + 1].rstrip()
                if replacement:
                    result = result[:-1].rstrip() + replacement
                return result
        return prefix[: limit - 1].rstrip("，,、：:；; ") + "…"

    def parse_dream_operations(self, text: str) -> tuple[list[dict[str, Any]], str]:
        """Legacy parser retained only for old migration/tests; Dream does not use it."""
        raw = str(text or "").strip()
        if not raw:
            return [], ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return [], ""
        if not isinstance(payload, dict):
            return [], ""
        operations = payload.get("operations")
        if not isinstance(operations, list):
            operations = []
        return [item for item in operations if isinstance(item, dict)], str(payload.get("dream") or "")

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item).strip()]

    def distill_workflows(
        self,
        existing_sop: str,
        episodic_recent: str,
    ) -> tuple[str, str]:
        if self.llm is None or not episodic_recent.strip():
            return "", ""

        user_prompt = (
            "## 已有 SOP\n\n"
            f"{existing_sop or '(empty)'}\n\n"
            "## 近期情景记忆\n\n"
            f"{episodic_recent}\n"
        )
        response = self.llm.chat([
            {"role": "system", "content": WORKFLOW_SOP_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
        return self.parse_sop_output(response.get("content") or "")

    def format_messages(self, messages: list[dict[str, Any]]) -> str:
        lines = []
        for message in messages:
            role = message.get("role", "unknown")
            content = message.get("content")
            name = message.get("name")

            if role == "tool":
                summary = self._summarize_tool_message(name, content)
                if summary:
                    lines.append(f"tool:{name or 'unknown'}: {summary}")
                continue

            if content:
                lines.append(f"{role}: {content}")
            elif message.get("tool_calls"):
                tool_names = []
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    tool_names.append(function.get("name", "unknown"))
                lines.append(f"{role}: [tool_calls: {', '.join(tool_names)}]")
        return "\n".join(lines)

    def parse_bullets(self, text: str) -> list[str]:
        cleaned = text.strip()
        if not cleaned or cleaned == "无" or cleaned.lower() == "none":
            return []

        items = []
        for line in cleaned.splitlines():
            item = line.strip()
            if not item:
                continue
            if item.startswith("- "):
                item = item[2:].strip()
            if item and item not in {"无", "None"} and item.lower() != "none":
                items.append(item)
        return items

    def parse_dream_output(self, text: str) -> tuple[str, str]:
        raw = text.strip()
        if not raw:
            return "", ""

        memory = ""
        dream = ""
        if "[MEMORY]" in raw:
            start = raw.index("[MEMORY]") + len("[MEMORY]")
            end = raw.index("[DREAM]") if "[DREAM]" in raw else len(raw)
            memory = raw[start:end].strip()
        if "[DREAM]" in raw:
            start = raw.index("[DREAM]") + len("[DREAM]")
            dream = raw[start:].strip()
        return memory, dream

    def parse_sop_output(self, text: str) -> tuple[str, str]:
        raw = text.strip()
        if not raw:
            return "", ""

        sop = ""
        insights = ""
        if "[SOP]" in raw:
            start = raw.index("[SOP]") + len("[SOP]")
            end = raw.index("[INSIGHTS]") if "[INSIGHTS]" in raw else len(raw)
            sop = raw[start:end].strip()
        elif "[INSIGHTS]" not in raw:
            # No tags at all: treat the whole payload as the SOP body.
            sop = raw
        if "[INSIGHTS]" in raw:
            start = raw.index("[INSIGHTS]") + len("[INSIGHTS]")
            insights = raw[start:].strip()

        if sop in {"无", "None"} or sop.lower() == "none":
            sop = ""
        if insights in {"无", "None"} or insights.lower() == "none":
            insights = ""
        return sop, insights

    def _summarize_tool_message(self, name: Optional[str], content: Any) -> str:
        text = str(content or "").strip()
        if not text:
            return ""

        lines = text.splitlines()
        if len(text) <= 500:
            return text

        head = "\n".join(lines[:6])
        tail = "\n".join(lines[-4:]) if len(lines) > 10 else ""
        if tail:
            return f"{head}\n...\n{tail}"
        return head
