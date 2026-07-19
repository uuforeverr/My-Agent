import json
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

from memory import MemoryManager
from tools.base import BaseTool, ToolResult
from tools.bash_tool import BashTool
from tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool
from tools.scheduler_tool import SchedulerTool
from tools.web_search_tool import WebSearchTool

if TYPE_CHECKING:
    from agent.run_context import RunContext
    from scheduler.task_store import TaskStore

SCHEDULED_TASK_HIDDEN_TOOLS = {"scheduler"}


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        memory: MemoryManager,
        bash_timeout: int = 20,
        qianfan_api_key: str = "",
        qianfan_api_base: str = "",
        web_search_timeout: int = 15,
        scheduler_store: "Optional[TaskStore]" = None,
    ):
        self.tools: dict[str, BaseTool] = {}
        for tool in (
            BashTool(workspace, default_timeout=bash_timeout),
            GlobTool(workspace),
            ReadFileTool(workspace),
            WriteFileTool(workspace),
            EditFileTool(workspace),
            RememberMemoryTool(workspace, memory),
            SearchMemoryTool(workspace, memory),
            ReadMemoryTool(workspace, memory),
        ):
            self.tools[tool.name] = tool

        if WebSearchTool.is_available(qianfan_api_key):
            tool = WebSearchTool(
                workspace,
                api_key=qianfan_api_key,
                api_base=qianfan_api_base,
                timeout=web_search_timeout,
            )
            self.tools[tool.name] = tool

        if scheduler_store is not None:
            scheduler_tool = SchedulerTool(workspace, scheduler_store)
            self.tools[scheduler_tool.name] = scheduler_tool

    def schemas(self, context: "Optional[RunContext]" = None) -> list[dict[str, Any]]:
        hidden = self._hidden_tools(context)
        return [
            tool.schema()
            for name, tool in self.tools.items()
            if name not in hidden
        ]

    def run(
        self,
        name: str,
        arguments: Union[str, dict[str, Any]],
        context: "Optional[RunContext]" = None,
    ) -> ToolResult:
        trace = getattr(context, "trace", None)
        span_context = (
            trace.start_span(
                "tool.execute",
                span_kind="tool.execute",
                tool_name=name,
                argument_chars=self._argument_chars(arguments),
            )
            if trace is not None
            else nullcontext(None)
        )
        with span_context as span:
            if name in self._hidden_tools(context):
                result = ToolResult(ok=False, content=f"Tool not available in this context: {name}")
                self._finish_tool_span(span, result, error_type="hidden_tool")
                return result

            tool = self.tools.get(name)
            if tool is None:
                result = ToolResult(ok=False, content=f"Unknown tool: {name}")
                self._finish_tool_span(span, result, error_type="unknown_tool")
                return result

            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments or "{}")
                except json.JSONDecodeError as e:
                    result = ToolResult(ok=False, content=f"Invalid JSON arguments: {e}")
                    self._finish_tool_span(span, result, error_type="invalid_json")
                    return result
            else:
                parsed = arguments

            if span is not None:
                span.set_attributes(
                    argument_keys=sorted(parsed.keys()) if isinstance(parsed, dict) else [],
                )

            result = tool.run(parsed, context)
            self._finish_tool_span(span, result)
            return result

    @staticmethod
    def _hidden_tools(context: "Optional[RunContext]") -> set[str]:
        if context is not None and context.get("is_scheduled_task"):
            return SCHEDULED_TASK_HIDDEN_TOOLS
        return set()

    @staticmethod
    def _argument_chars(arguments: Union[str, dict[str, Any]]) -> int:
        if isinstance(arguments, str):
            return len(arguments)
        return len(json.dumps(arguments, ensure_ascii=False, default=str))

    @staticmethod
    def _finish_tool_span(span: Any, result: ToolResult, error_type: str = "") -> None:
        if span is None:
            return
        span.set_attributes(
            ok=result.ok,
            result_chars=len(result.content or ""),
        )
        if error_type:
            span.set_attribute("error_type", error_type)
        if not result.ok:
            span.set_status("error")
