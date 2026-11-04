"""联网工具：网页检索 + 抓取正文。

- ``web_search``：优先用 Tavily（配置了 AGENT_SEARCH_API_KEY 时），否则抓 Bing /
  DuckDuckGo 的 HTML 结果页，无需任何 Key。
- ``fetch_web_page``：抓取指定 URL 的正文，方便模型读完搜索摘要后展开细节。

两个工具都做了基本防护：只允许 http/https、拒绝本机和内网地址、限制返回长度。
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
import urllib.parse
from typing import Any

import httpx
from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("agent.tools.web")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

_BING_ITEM = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_TITLE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

_DDG_ITEM = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.S | re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_WS_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_RE = re.compile(r"\n{3,}")


def _clean_html(fragment: str) -> str:
    text = _TAG_RE.sub("", fragment)
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _blocked_url(url: str) -> str | None:
    """返回拒绝原因；合法则返回 None。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "只支持 http/https 网址"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "网址缺少主机名"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return "不允许访问本机/局域网地址"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return "不允许访问内网/保留地址"
    return None


def _format_results(query: str, items: list[dict[str, Any]], provider: str) -> str:
    lines = [f"搜索「{query}」（来源：{provider}，共 {len(items)} 条）："]
    for index, item in enumerate(items, start=1):
        title = (item.get("title") or "(无标题)").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        lines.append(f"{index}. {title}\n   {url}")
        if snippet:
            lines.append(f"   {snippet[:300]}")
    return "\n".join(lines)


def _search_tavily(query: str, limit: int, api_key: str) -> list[dict[str, Any]]:
    settings = get_settings()
    response = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=settings.web_timeout,
    )
    response.raise_for_status()
    payload = response.json() or {}
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        }
        for item in payload.get("results", [])
    ][:limit]


def _search_bing(query: str, limit: int) -> list[dict[str, Any]]:
    settings = get_settings()
    url = (
        "https://www.bing.com/search?"
        + urllib.parse.urlencode({"q": query, "count": str(limit), "setlang": "zh-CN"})
    )
    response = httpx.get(url, headers=_HEADERS, timeout=settings.web_timeout, follow_redirects=True)
    response.raise_for_status()
    body = response.text
    items: list[dict[str, Any]] = []
    for block in _BING_ITEM.findall(body)[:limit]:
        title_match = _BING_TITLE.search(block)
        if not title_match:
            continue
        snippet_match = _BING_SNIPPET.search(block)
        items.append(
            {
                "title": _clean_html(title_match.group(2)),
                "url": html_lib.unescape(title_match.group(1)),
                "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
            }
        )
    return items


def _search_duckduckgo(query: str, limit: int) -> list[dict[str, Any]]:
    settings = get_settings()
    response = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers=_HEADERS,
        timeout=settings.web_timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    body = response.text
    snippets = [_clean_html(item) for item in _DDG_SNIPPET.findall(body)]
    items: list[dict[str, Any]] = []
    for index, (href, title) in enumerate(_DDG_ITEM.findall(body)[:limit]):
        link = html_lib.unescape(href)
        # DDG 结果链接是跳转地址，取 uddg 参数里的真实 URL
        if link.startswith("//duckduckgo.com/l/") or "duckduckgo.com/l/" in link:
            query_string = urllib.parse.urlparse("https:" + link if link.startswith("//") else link).query
            real = urllib.parse.parse_qs(query_string).get("uddg", [])
            if real:
                link = urllib.parse.unquote(real[0])
        items.append(
            {
                "title": _clean_html(title),
                "url": link,
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
    return items


@tool
def web_search(query: str, max_results: int = 5, site: str = "") -> str:
    """联网搜索，返回若干条结果（标题 + 链接 + 摘要）。

    当问题涉及最新信息、实时数据、版本变化、官方文档等训练数据里没有或可能过时的内容时使用。
    需要读某个结果的完整内容时，用 fetch_web_page 打开它的链接。

    Args:
        query: 搜索关键词或问题。
        max_results: 返回条数，1~10，默认 5。
        site: 可选，限定站点（例如 "docs.langchain.com"），会拼进查询语句里。
    """
    text = (query or "").strip()
    if not text:
        return "搜索失败：query 不能为空"
    settings = get_settings()
    limit = max(1, min(int(max_results or 5), 10))
    if site.strip():
        text = f"{text} site:{site.strip()}"

    provider = settings.effective_search_provider
    attempts: list[tuple[str, Any]] = []
    api_key = settings.search_api_key_value
    if provider == "tavily":
        if not api_key:
            return "搜索失败：AGENT_SEARCH_PROVIDER=tavily 但未配置 AGENT_SEARCH_API_KEY"
        attempts = [("tavily", lambda: _search_tavily(text, limit, api_key))]
    elif provider == "duckduckgo":
        attempts = [
            ("duckduckgo", lambda: _search_duckduckgo(text, limit)),
            ("bing", lambda: _search_bing(text, limit)),
        ]
    else:  # bing（默认）
        attempts = [
            ("bing", lambda: _search_bing(text, limit)),
            ("duckduckgo", lambda: _search_duckduckgo(text, limit)),
        ]

    errors: list[str] = []
    for name, call in attempts:
        try:
            items = call()
        except Exception as exc:  # noqa: BLE001 - 网络问题不应中断对话
            logger.warning("搜索源 %s 失败：%s: %s", name, type(exc).__name__, exc)
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if items:
            return _format_results(text, items, name)
        errors.append(f"{name}: 无结果")
    return "搜索失败或没有结果（" + "；".join(errors) + "），可以换个关键词再试"


@tool
def fetch_web_page(url: str, max_chars: int = 0) -> str:
    """抓取一个网页的正文文本（已去除 HTML 标签与脚本）。

    Args:
        url: 要抓取的完整网址，必须以 http:// 或 https:// 开头。
        max_chars: 最多返回多少字符，0 表示使用默认上限（默认 4000，最大 12000）。
    """
    target = (url or "").strip()
    if not target:
        return "抓取失败：url 不能为空"
    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme:
        # 只给"example.com/page"这种没有 scheme 的输入补协议头
        target = "https://" + target
    elif parsed.scheme not in {"http", "https"}:
        return f"抓取失败：只支持 http/https 网址（收到 {parsed.scheme}://）"
    blocked = _blocked_url(target)
    if blocked:
        return f"抓取失败：{blocked}"

    settings = get_settings()
    limit = int(max_chars) if max_chars else settings.web_max_chars
    limit = max(500, min(limit, 12000))

    try:
        response = httpx.get(
            target,
            headers=_HEADERS,
            timeout=settings.web_timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return f"抓取失败：{type(exc).__name__}: {exc}"

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return f"抓取失败：不支持的内容类型 {content_type or '未知'}"

    body = response.text
    title_match = _TITLE_RE.search(body)
    title = _clean_html(title_match.group(1)) if title_match else target
    text = _SCRIPT_RE.sub(" ", body)
    text = _TAG_RE.sub("\n", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = _BLANK_RE.sub("\n\n", text).strip()

    truncated = len(text) > limit
    return (
        f"页面：{title}\nURL：{response.url}\n"
        f"{'-' * 40}\n{text[:limit]}"
        + ("\n...（内容已截断，可增大 max_chars）" if truncated else "")
    )


__all__ = ["fetch_web_page", "web_search"]
