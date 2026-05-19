from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup


WEB_SEARCH_TRIGGERS = (
    "latest",
    "current",
    "today",
    "recent",
    "news",
    "search the web",
    "web search",
    "look up",
    "browse",
    "online",
    "this week",
    "this month",
    "as of",
)


@dataclass(slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

    def model_dump(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def should_use_web_search(text: str) -> bool:
    lowered = text.lower()
    return any(trigger in lowered for trigger in WEB_SEARCH_TRIGGERS)


class WebSearchService:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "CustomChat/0.1 local research tool"})
            response.raise_for_status()
        return [result.model_dump() for result in _parse_duckduckgo_html(response.text, max_results)]


def format_web_context(results: list[dict[str, Any]]) -> str:
    if not results:
        return "Web search returned no results."
    lines = ["Web search results:"]
    for index, result in enumerate(results, start=1):
        title = result.get("title") or "Untitled"
        url = result.get("url") or ""
        snippet = result.get("snippet") or ""
        lines.append(f"[WEB{index}] {title}\nURL: {url}\nSnippet: {snippet}")
    return "\n\n".join(lines)


def _parse_duckduckgo_html(html: str, max_results: int) -> list[WebSearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[WebSearchResult] = []
    for result in soup.select(".result"):
        link = result.select_one(".result__a")
        if link is None:
            continue
        href = str(link.get("href") or "")
        parsed = urlparse(href)
        if parsed.netloc.endswith("duckduckgo.com") and "uddg=" in parsed.query:
            from urllib.parse import parse_qs, unquote

            href = unquote(parse_qs(parsed.query).get("uddg", [href])[0])
        snippet_node = result.select_one(".result__snippet")
        results.append(
            WebSearchResult(
                title=link.get_text(" ", strip=True),
                url=href,
                snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
            )
        )
        if len(results) >= max_results:
            break
    return results
