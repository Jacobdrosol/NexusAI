"""Bounded, privacy-aware web research context for direct chat."""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx


_WEB_LOOKUP_RE = re.compile(
    r"\b("
    r"current|currently|today|latest|recent|newest|price|pricing|cost|quote|market|"
    r"stock|weather|forecast|news|score|schedule|availability|lookup|look\s+up|"
    r"up[-\s]?to[-\s]?date|knowledge\s+cutoff|training\s+cutoff|"
    r"(?:web|internet|online)\s+search|search\s+(?:the\s+)?(?:web|internet|online)|"
    r"new\s+model|(?:new\s+)?release(?:d)?|serial(?:\s+number)?|part\s+number|"
    r"model\s+number|registry|recall"
    r")\b",
    re.IGNORECASE,
)


def should_search_web(query: str) -> bool:
    """Avoid sending ordinary or private chat prompts to an external search engine."""
    return bool(_WEB_LOOKUP_RE.search(str(query or "")))


def _safe_result_text(result: dict[str, Any]) -> str:
    title = str(result.get("title") or "Untitled result").strip()
    url = str(result.get("url") or "").strip()
    content = str(result.get("content") or result.get("snippet") or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    text = f"[web:{parsed.netloc.lower()}] {title}\nURL: {url}"
    if content:
        text += f"\nSnippet: {content[:900]}"
    return text[:1400]


async def resolve_web_context_items(query: str, *, limit: int = 5) -> list[str]:
    """Query a self-hosted SearXNG endpoint and return compact cited context."""
    normalized_query = str(query or "").strip()
    if not normalized_query or not should_search_web(normalized_query):
        return []
    endpoint = os.environ.get("NEXUSAI_SEARXNG_URL", "http://searxng:8080").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False) as client:
            response = await client.get(
                f"{endpoint}/search",
                params={"q": normalized_query[:600], "format": "json", "language": "en-US"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list):
        return []
    context: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        rendered = _safe_result_text(result)
        if rendered and rendered not in context:
            context.append(rendered)
        if len(context) >= max(1, min(int(limit), 8)):
            break
    return context
