"""Web access tools for the dsh agent.

The sandbox in which this code runs may restrict some domains, but on a normal
developer machine these tools let DeepSeek search the web and fetch pages
during a coding session.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus

import httpx

_BING_SEARCH_URL = "https://www.bing.com/search"
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


class WebTools:
    def __init__(self, config):
        self.config = config

    def is_web_tool(self, name: str) -> bool:
        return name in {"web_search", "web_fetch"}

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "web_search",
                "description": (
                    "Search the public web and return titles, URLs and snippets. "
                    "Use it when the user asks about current documentation, "
                    "APIs, engine errors, or anything outside the project files."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "max_results": {
                            "type": "integer",
                            "description": "1-10, default 6.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "web_fetch",
                "description": (
                    "Fetch a public http(s) URL and return its text content. "
                    "HTML is stripped to readable text. Use this after a "
                    "web_search to read the actual page."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {
                            "type": "integer",
                            "description": "Default 50000.",
                        },
                    },
                    "required": ["url"],
                },
            },
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "web_search":
                return await self.search(
                    str(arguments.get("query", "")),
                    int(arguments.get("max_results", 6)),
                )
            if name == "web_fetch":
                return await self.fetch(
                    str(arguments.get("url", "")),
                    int(arguments.get("max_chars", 50_000)),
                )
            return _error("unknown web tool: %s" % name)
        except Exception as exc:  # noqa: BLE001
            return _error("%s: %s" % (type(exc).__name__, exc))

    async def search(self, query: str, max_results: int = 6) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return _error("web_search requires a query")
        max_results = max(1, min(int(max_results), 10))

        results = await self._search_bing(query, max_results)
        if not results:
            results = await self._search_duckduckgo(query, max_results)
        if not results:
            return _error("web_search returned no results")
        text = json.dumps(results, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    async def _search_bing(self, query: str, max_results: int) -> list[dict[str, str]]:
        params = {"q": query, "count": str(max_results), "setlang": "zh-CN"}
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(
                    _BING_SEARCH_URL, params=params, headers={"User-Agent": _USER_AGENT}
                )
                response.raise_for_status()
                page = response.text
        except Exception:
            return []

        blocks = re.findall(r'<li class="b_algo".*?</li>', page, re.S)
        results: list[dict[str, str]] = []
        for block in blocks[:max_results]:
            link_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not link_match:
                continue
            snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            results.append(
                {
                    "title": _clean_html(link_match.group(2)),
                    "url": html.unescape(link_match.group(1)),
                    "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
                }
            )
        return results

    async def _search_duckduckgo(self, query: str, max_results: int) -> list[dict[str, str]]:
        data = {"q": query}
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.post(
                    _DDG_LITE_URL, data=data, headers={"User-Agent": _USER_AGENT}
                )
                response.raise_for_status()
                page = response.text
        except Exception:
            return []

        results: list[dict[str, str]] = []
        blocks = re.findall(r'<tr class="result-sponsored".*?</tr>|<tr.*?</tr>', page, re.S)
        if not blocks:
            links = re.findall(
                r"<a[^>]+class=['\"]result-link['\"][^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
                page,
                re.S,
            )
            snippets = re.findall(
                r"<td[^>]+class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S
            )
            for i, (url, title) in enumerate(links[:max_results]):
                results.append(
                    {
                        "title": _clean_html(title),
                        "url": html.unescape(url),
                        "snippet": _clean_html(snippets[i]) if i < len(snippets) else "",
                    }
                )
        return results

    async def fetch(self, url: str, max_chars: int = 50_000) -> dict[str, Any]:
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            return _error("web_fetch only accepts http(s) URLs")
        max_chars = max(500, min(int(max_chars), 200_000))
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            body = response.text
        if "html" in content_type.lower() or "<html" in body[:1000].lower():
            text = _html_to_text(body)
        else:
            text = body
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated by web_fetch]"
        header = "URL: %s\n\n%s" % (str(response.url), text)
        return {"content": [{"type": "text", "text": header}], "isError": False}


def _clean_html(raw: str) -> str:
    return re.sub(r"\s+", " ", _html_to_text(raw)).strip()


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        return re.sub(r"(?s)<[^>]+>", " ", html.unescape(raw))
    return html.unescape(" ".join(parser.parts))


def _error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "WEB TOOL ERROR:\n" + message}],
        "isError": True,
    }
