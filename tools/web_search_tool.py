import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from tools.base import BaseTool, ToolError, ToolResult
from observability.otel import add_event


DEFAULT_QIANFAN_API_BASE = "https://qianfan.baidubce.com/v2"
MAX_WEB_SEARCH_RESULTS = 20


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the web for fresh real-time information using Baidu Qianfan AI Search. "
        "Use when the answer depends on current events, recent information, or external web facts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string.",
            },
            "count": {
                "type": "integer",
                "description": f"Number of web results to return, 1-{MAX_WEB_SEARCH_RESULTS}. Default is 5.",
            },
            "freshness": {
                "type": "string",
                "description": (
                    "Optional time filter: noLimit (default), oneDay, oneWeek, "
                    "oneMonth, or oneYear."
                ),
            },
        },
        "required": ["query"],
    }
    risk_level = "R1"
    requires_approval = False
    idempotent = True

    def run(self, arguments: dict[str, Any], context: Any = None) -> ToolResult:
        result = super().run(arguments, context)
        if result.ok:
            result.metadata.update({"trust_level": "untrusted", "external_input": True})
        return result

    def __init__(
        self,
        workspace: Path,
        api_key: str,
        api_base: str = DEFAULT_QIANFAN_API_BASE,
        timeout: int = 15,
    ):
        super().__init__(workspace)
        self.api_key = (api_key or "").strip()
        self.api_base = (api_base or DEFAULT_QIANFAN_API_BASE).rstrip("/")
        self.timeout = max(int(timeout or 15), 1)

    @staticmethod
    def is_available(api_key: str) -> bool:
        return bool((api_key or "").strip())

    def execute(self, arguments: dict[str, Any]) -> str:
        if not self.api_key:
            raise ToolError("QIANFAN_API_KEY is not configured")

        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ToolError("query is required")

        count = self._resolve_count(arguments.get("count"))
        freshness = str(arguments.get("freshness", "noLimit") or "noLimit")
        payload = self._build_payload(query, count, freshness)
        data = self._post_json(f"{self.api_base}/ai_search/web_search", payload)
        normalized = self._normalize_response(query, data)
        if normalized["count"] == 0:
            raise ToolError(
                "search returned no results",
                error_code="search_no_results",
                error_type="empty_result",
                retryable=True,
                suggested_action="Rewrite the search query and try again",
            )
        return json.dumps(
            normalized,
            ensure_ascii=False,
            indent=2,
        )

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                add_event(None, "tool.attempt", {
                    "attempt": attempt,
                    "status": "success",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                })
                return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                transient = e.code == 429 or e.code >= 500
                add_event(None, "tool.attempt", {
                    "attempt": attempt,
                    "status": "error",
                    "http.status_code": e.code,
                    "retryable": transient,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                })
                if transient and attempt < 3:
                    time.sleep(0.1 * (2 ** (attempt - 1)))
                    continue
                raise ToolError(
                    f"Qianfan HTTP {e.code}: {body[:300]}",
                    error_code="network_timeout" if transient else (
                        "permission_denied" if e.code in {401, 403} else "http_error"
                    ),
                    error_type="transient_error" if transient else "http_error",
                    retryable=transient,
                ) from e
            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
                add_event(None, "tool.attempt", {
                    "attempt": attempt,
                    "status": "error",
                    "retryable": True,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                })
                if attempt < 3:
                    time.sleep(0.1 * (2 ** (attempt - 1)))
                    continue
                raise ToolError(
                    f"Qianfan request failed after {attempt} attempts: {e}",
                    error_code="network_timeout",
                    error_type="transient_error",
                    retryable=True,
                ) from e
            except json.JSONDecodeError as e:
                raise ToolError(
                    f"Qianfan returned invalid JSON: {e}",
                    error_code="invalid_response",
                    error_type="provider_error",
                    retryable=True,
                ) from e
        raise ToolError(
            "Qianfan request failed",
            error_code="network_timeout",
            error_type="transient_error",
        )

    def _build_payload(self, query: str, count: int, freshness: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": count}],
        }
        freshness_filter = self._build_freshness_filter(freshness)
        if freshness_filter:
            payload["search_filter"] = freshness_filter
        return payload

    @staticmethod
    def _resolve_count(raw_count: Any) -> int:
        try:
            count = int(raw_count if raw_count is not None else 5)
        except (TypeError, ValueError):
            return 5
        if count < 1 or count > MAX_WEB_SEARCH_RESULTS:
            return 5
        return count

    @staticmethod
    def _build_freshness_filter(freshness: str) -> Optional[dict[str, Any]]:
        if not freshness or freshness == "noLimit":
            return None
        delta_days = {
            "oneDay": 1,
            "oneWeek": 7,
            "oneMonth": 30,
            "oneYear": 365,
        }.get(freshness)
        if not delta_days:
            return None

        now = datetime.now()
        start_date = (now - timedelta(days=delta_days)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return {"range": {"page_time": {"gte": start_date, "lt": end_date}}}

    @staticmethod
    def _normalize_response(query: str, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict) and data.get("code"):
            raise ToolError(f"Qianfan returned {data.get('code')}: {data.get('message', '')}")

        references = data.get("references")
        if references is None:
            references = (data.get("data") or {}).get("references") or []

        results = []
        for item in references or []:
            content = item.get("snippet") or item.get("content") or ""
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": str(content)[:500],
                "siteName": item.get("siteName") or item.get("website") or item.get("web_anchor") or "",
                "datePublished": item.get("datePublished") or item.get("date") or "",
            })

        return {
            "query": query,
            "backend": "qianfan",
            "count": len(results),
            "results": results,
        }
