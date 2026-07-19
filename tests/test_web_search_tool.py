import io
import json
import os
import urllib.error

import pytest

from config import load_settings
from tools.web_search_tool import WebSearchTool


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_web_search_requires_api_key(tmp_workspace):
    tool = WebSearchTool(tmp_workspace, api_key="")
    result = tool.run({"query": "hello"})
    assert result.ok is False
    assert "QIANFAN_API_KEY" in result.content


def test_web_search_requires_query(tmp_workspace):
    tool = WebSearchTool(tmp_workspace, api_key="key")
    result = tool.run({"query": "  "})
    assert result.ok is False
    assert "query is required" in result.content


def test_web_search_success_parses_qianfan_references(tmp_workspace, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse({
            "references": [
                {
                    "title": "Title 1",
                    "url": "https://example.com/a",
                    "snippet": "Snippet 1",
                    "website": "Example",
                    "date": "2026-01-01",
                }
            ]
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tool = WebSearchTool(
        tmp_workspace,
        api_key="bce-v3/test",
        api_base="https://qianfan.example/v2",
        timeout=9,
    )

    result = tool.run({"query": "北京天气", "count": 3, "freshness": "oneWeek"})

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["backend"] == "qianfan"
    assert payload["query"] == "北京天气"
    assert payload["results"][0]["title"] == "Title 1"
    assert payload["results"][0]["url"] == "https://example.com/a"
    assert captured["timeout"] == 9
    assert captured["url"] == "https://qianfan.example/v2/ai_search/web_search"
    assert captured["headers"]["Authorization"] == "Bearer bce-v3/test"
    assert captured["body"]["messages"][0]["content"] == "北京天气"
    assert captured["body"]["resource_type_filter"][0]["top_k"] == 3
    assert "search_filter" in captured["body"]


def test_web_search_count_out_of_range_falls_back_to_default(tmp_workspace, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse({"references": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tool = WebSearchTool(tmp_workspace, api_key="key")

    result = tool.run({"query": "x", "count": 999})

    assert result.ok is True
    assert captured["body"]["resource_type_filter"][0]["top_k"] == 5


def test_web_search_http_error_is_readable(tmp_workspace, monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad key"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tool = WebSearchTool(tmp_workspace, api_key="bad")

    result = tool.run({"query": "x"})

    assert result.ok is False
    assert "Qianfan HTTP 401" in result.content


def test_web_search_business_error_is_readable(tmp_workspace, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResponse({"code": 17, "message": "quota exceeded"}),
    )
    tool = WebSearchTool(tmp_workspace, api_key="key")

    result = tool.run({"query": "x"})

    assert result.ok is False
    assert "quota exceeded" in result.content


@pytest.mark.live
def test_live_qianfan_web_search_smoke(tmp_workspace):
    if os.environ.get("RUN_LIVE_WEB_SEARCH") != "1":
        pytest.skip("set RUN_LIVE_WEB_SEARCH=1 to run real Qianfan web search smoke test")
    settings = load_settings()
    if not settings.qianfan_api_key:
        pytest.skip("QIANFAN_API_KEY is not set")

    tool = WebSearchTool(
        tmp_workspace,
        api_key=settings.qianfan_api_key,
        api_base=settings.qianfan_api_base,
        timeout=settings.web_search_timeout,
    )
    result = tool.run({"query": "百度千帆 AI 搜索", "count": 1})

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["backend"] == "qianfan"
    assert payload["count"] >= 0
