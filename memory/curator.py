from datetime import datetime
from typing import Any, Optional


DAILY_SUMMARY_SYSTEM_PROMPT = """你是一个高质量对话记忆整理助手，负责将即将从上下文中裁剪的对话归纳为当天的情景记忆。

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
- 不要输出标题、解释、JSON 或 Markdown 表格。"""


DREAM_SYSTEM_PROMPT = """你是一个记忆整理助手，负责定期整理用户的长期记忆。

你将收到两份材料：
1. 当前语义记忆：MEMORY.md 的内容
2. 近期情景记忆：daily memory 的内容

MEMORY.md 会作为长期语义记忆被检索和注入，因此必须保持精炼，只存放有价值和值得长期保留的信息。

严禁编造、推测或添加材料中不存在的信息。

## 任务

### Part 1: 更新后的语义记忆（[MEMORY]）

- 合并语义相近的条目。
- 从情景记忆中提取长期稳定事实、偏好、项目决策、规则和经验。
- 新信息与旧信息冲突时，以近期情景记忆为准。
- 删除临时性、重复、过时、无长期价值的信息。
- 每条一行，用 "- " 开头。
- 可使用 "## 标题" 分组。

### Part 2: 做梦日志（[DREAM]）

用简短文字记录本次整理：
- 提取了哪些长期信息。
- 合并或删除了哪些重复/过时信息。
- 是否发现冲突。

## 输出格式

[MEMORY]
...

[DREAM]
..."""


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
    ) -> list[str]:
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
            "请输出高质量事件摘要。"
        )

        response = self.llm.chat([
            {"role": "system", "content": DAILY_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
        return self.parse_bullets(response.get("content") or "")

    def deep_dream(
        self,
        semantic_memory: str,
        episodic_memory: str,
    ) -> tuple[str, str]:
        if self.llm is None or not episodic_memory.strip():
            return "", ""

        user_prompt = (
            "## 当前语义记忆（MEMORY.md）\n\n"
            f"{semantic_memory or '(empty)'}\n\n"
            "## 近期情景记忆（daily memory）\n\n"
            f"{episodic_memory}\n"
        )
        response = self.llm.chat([
            {"role": "system", "content": DREAM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
        return self.parse_dream_output(response.get("content") or "")

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
