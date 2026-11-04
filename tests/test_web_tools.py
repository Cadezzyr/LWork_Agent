"""联网工具测试（不访问真实网络，全部 monkeypatch）。"""

from __future__ import annotations

import httpx
import pytest

from app.agent.tools import web as web_module
from app.agent.tools.web import fetch_web_page, web_search


def test_web_search_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_bing(query: str, limit: int) -> list[dict[str, str]]:
        return [
            {"title": "LangChain 文档", "url": "https://docs.langchain.com/", "snippet": "官方文档"},
            {"title": "LangGraph", "url": "https://langchain-ai.github.io/", "snippet": "图编排"},
        ][:limit]

    monkeypatch.setattr(web_module, "_search_bing", fake_bing)
    result = web_search.invoke({"query": "langchain", "max_results": 2})
    assert "搜索「langchain」" in result
    assert "1. LangChain 文档" in result
    assert "https://docs.langchain.com/" in result
    assert "官方文档" in result
    assert "bing" in result


def test_web_search_appends_site_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_bing(query: str, limit: int) -> list[dict[str, str]]:
        captured["query"] = query
        return [{"title": "t", "url": "https://example.com", "snippet": "s"}]

    monkeypatch.setattr(web_module, "_search_bing", fake_bing)
    web_search.invoke({"query": "create_agent", "site": "docs.langchain.com"})
    assert captured["query"] == "create_agent site:docs.langchain.com"


def test_web_search_falls_back_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, limit: int) -> list[dict[str, str]]:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(web_module, "_search_bing", boom)
    monkeypatch.setattr(web_module, "_search_duckduckgo", boom)

    result = web_search.invoke({"query": "anything"})
    assert "搜索失败" in result
    assert "ConnectError" in result


def test_web_search_rejects_empty_query() -> None:
    assert "query 不能为空" in web_search.invoke({"query": "   "})


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "http/https"),
        ("http://localhost:8000/api", "本机"),
        ("http://127.0.0.1:8000/x", "内网"),
        ("http://192.168.1.10/admin", "内网"),
        ("http://169.254.169.254/latest/meta-data", "内网"),
    ],
)
def test_fetch_web_page_blocks_unsafe_urls(url: str, reason: str) -> None:
    result = fetch_web_page.invoke({"url": url})
    assert "抓取失败" in result
    assert reason in result


def test_fetch_web_page_extracts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    html_body = (
        "<html><head><title>示例页面</title><style>.a{color:red}</style></head>"
        "<body><h1>标题</h1><p>正文内容 &amp; 说明</p><script>alert(1)</script></body></html>"
    )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        text = html_body
        url = "https://example.com/page"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(web_module.httpx, "get", lambda *a, **k: FakeResponse())
    result = fetch_web_page.invoke({"url": "https://example.com/page", "max_chars": 600})
    assert "示例页面" in result
    assert "正文内容 & 说明" in result
    assert "alert(1)" not in result  # script 被剥离
    assert "color:red" not in result  # style 被剥离
