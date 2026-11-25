"""Bounded, privacy-aware web research context for direct chat."""
from __future__ import annotations

import html as html_lib
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
    r"model\s+number|registry|recall|"
    r"best|recommend(?:ation|ed)?|worth\s+it|budget|buy|buying|purchase|purchasing|"
    r"compare|comparison|versus|vs\.?|alternatives?|"
    r"which\s+.*(?:buy|get|choose|pick)|should\s+i\s+(?:buy|get)|"
    r"research|look\s+into|find\s+out|learn\s+about|investigate|"
    r"how\s+to\s+(?:test|use|check|scan|set\s*up|configure|measure|compare|evaluate)|"
    r"capable(?:\s+of)?|does\s+(?:it|this|that)\s+support|compatible\s+with|works\s+with|"
    r"can\s+(?:it|this|that)\s+[\w\s]{0,24}(?:scan|test|handle|support|work)"
    r")\b",
    re.IGNORECASE,
)

_PRIVATE_CHAT_RE = re.compile(
    r"("
    r"private\s+message|personal\s+message|confidential|\bpassword\b|"
    r"email\s+to|message\s+to\s+my|"
    r"my\s+(?:boss|coworker|colleague|wife|husband|girlfriend|boyfriend|therapist|doctor)|"
    r"medical\s+record"
    r")",
    re.IGNORECASE,
)


def should_search_web(query: str) -> bool:
    """Avoid sending ordinary or private chat prompts to an external search engine."""
    text = str(query or "")
    if _PRIVATE_CHAT_RE.search(text):
        return False
    return bool(_WEB_LOOKUP_RE.search(text))


_META_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:using|based\s+on|according\s+to|from|with|use)\s+"
    r"(?:only\s+)?(?:the\s+)?(?:supplied|provided|above|current)?\s*"
    r"(?:web|search|web-search)\s+(?:results?|context)[,:\s-]*",
    re.IGNORECASE,
)
_META_SUFFIX_RE = re.compile(
    r"[,;]?\s*(?:and\s+)?(?:include|cite|provide|give|list)\s+"
    r"(?:their\s+|the\s+|a\s+|an\s+)?(?:exact\s+|source\s+|cited\s+)?"
    r"(?:urls?|links?|sources?)[^.]*$",
    re.IGNORECASE,
)
_INSTRUCTION_RE = re.compile(
    r"\b(?:answer|reply|respond)\s+(?:in|with)\s+(?:one|a|two|three)?\s*"
    r"(?:short\s+|brief\s+)?(?:sentence|paragraph|words?)\b[,.]?",
    re.IGNORECASE,
)
_LEADING_VERB_RE = re.compile(
    r"^(?:please\s+)?(?:name|list|find|show|give|tell\s+me|what\s+are|which\s+are)\s+"
    r"(?:me\s+)?(?:two|three|a\s+few|\d+)?\s*",
    re.IGNORECASE,
)
_RESEARCH_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:it\s+should\s+be\s+doing\s+)?research\s+(?:on|into|about)|"
    r"(?:do\s+)?(?:some\s+)?research\s+(?:on|into|about)|"
    r"(?:look\s+into|investigate|find\s+out\s+about)"
    r")\s+",
    re.IGNORECASE,
)


def _search_query(query: str) -> str:
    """Convert common conversational lookup wording into a useful search query."""
    normalized = re.sub(r"\s+", " ", str(query or "").strip())
    lowered = normalized.lower()
    if not normalized:
        return ""

    model_terms: list[str] = []
    display_names = {
        "deepseek": "DeepSeek",
        "qwen": "Qwen",
        "kimi": "Kimi",
        "llama": "Llama",
        "gemini": "Gemini",
        "claude": "Claude",
        "gpt": "GPT",
        "mistral": "Mistral",
    }
    for name, display_name in display_names.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            model_terms.append(display_name)
    if model_terms and re.search(r"\b(?:model|models|coding|release|released|newest|latest|best)\b", lowered):
        intent = "latest model release"
        if re.search(r"\b(?:best|coding|code)\b", lowered):
            intent += " best coding models"
        return f"{' '.join(model_terms[:3])} {intent} {datetime.now(timezone.utc).year}"

    # Remove leading conversational filler that otherwise becomes the dominant
    # search term in metasearch engines.
    normalized = re.sub(
        r"^(?:i\s+)?(?:want|need|would\s+like)\s+(?:to\s+)?(?:know|find|learn)\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    for _ in range(3):
        updated = _META_PREFIX_RE.sub("", normalized)
        updated = _META_SUFFIX_RE.sub("", updated)
        updated = _INSTRUCTION_RE.sub("", updated)
        updated = _LEADING_VERB_RE.sub("", updated)
        updated = _RESEARCH_PREFIX_RE.sub("", updated)
        updated = re.sub(r"\s*Follow-up request:\s*", " ", updated, flags=re.IGNORECASE)
        updated = re.sub(r"\s+", " ", updated).strip(" ,.;:-")
        if updated == normalized:
            break
        normalized = updated
    return normalized


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


async def resolve_web_context_items(
    query: str,
    *,
    limit: int = 5,
    require_intent: bool = True,
) -> list[str]:
    """Return compact cited context for a query, using Bing or SearXNG."""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    if require_intent and not should_search_web(normalized_query):
        return []
    search_query = _search_query(normalized_query)
    if not search_query:
        return []
    provider = os.environ.get("NEXUSAI_SEARCH_PROVIDER", "bing").strip().lower()
    items: list[str] = []
    if provider == "brave":
        items = await _brave_context_items(search_query, limit=limit)
    elif provider == "tavily":
        items = await _tavily_context_items(search_query, limit=limit)
    elif provider == "serper":
        items = await _serper_context_items(search_query, limit=limit)
    elif provider == "searxng":
        return await _searxng_context_items(search_query, limit=limit)
    else:
        items = await _bing_rss_context_items(search_query, limit=limit)
    if not items:
        items = await _bing_rss_context_items(search_query, limit=limit)
    if items:
        return items
    return await _searxng_context_items(search_query, limit=limit)


async def resolve_web_queries_context_items(queries: list[str], *, limit: int = 8) -> list[str]:
    """Run several planned queries and return one de-duplicated context list."""
    context: list[str] = []
    cap = max(1, min(int(limit), 12))
    for query in queries:
        for item in await resolve_web_context_items(query, limit=5, require_intent=False):
            if item not in context:
                context.append(item)
            if len(context) >= cap:
                return context
    return context


_CONTEXT_ITEM_RE = re.compile(
    r"\[web:([^\]]+)\]\s*(.*?)\nURL:\s*(\S+)(?:\nSnippet:\s*(.*))?",
    re.DOTALL,
)


def _parse_context_item(item: str) -> dict[str, str] | None:
    match = _CONTEXT_ITEM_RE.match(str(item or ""))
    if not match:
        return None
    return {
        "title": match.group(2).strip(),
        "url": match.group(3).strip(),
        "snippet": (match.group(4) or "").strip(),
    }


async def search_web_results(query: str, *, limit: int = 5) -> list[dict[str, str]]:
    """Return structured results for a model-issued web_search tool call."""
    results: list[dict[str, str]] = []
    for item in await resolve_web_context_items(query, limit=limit, require_intent=False):
        parsed = _parse_context_item(item)
        if parsed:
            results.append(parsed)
    return results


_BING_RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

_GENERIC_RESULT_DOMAINS = {
    "bestbuy.com",
    "merriam-webster.com",
    "dictionary.cambridge.org",
    "thefreedictionary.com",
    "sketchfab.com",
    "tinkercad.com",
    "printables.com",
    "3dwarehouse.sketchup.com",
    "etsy.com",
    "espn.com",
    "espn.in",
    "speedtest.net",
    "fast.com",
    "walmart.com",
}


def _strip_markup(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_bing_rss(xml_text: str, *, limit: int = 5) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    context: list[str] = []
    for item in root.iter("item"):
        title = _strip_markup(item.findtext("title") or "")
        url = str(item.findtext("link") or "").strip()
        snippet = _strip_markup(item.findtext("description") or "")
        rendered = _safe_result_text({"title": title, "url": url, "content": snippet})
        if rendered and rendered not in context:
            context.append(rendered)
        if len(context) >= max(1, min(int(limit), 8)):
            break
    return context


def _result_domain(item: str) -> str:
    match = re.search(r"\[web:([^\]]+)\]", str(item or ""))
    return match.group(1).strip().lower() if match else ""


def _filter_generic_results(items: list[str]) -> list[str]:
    kept: list[str] = []
    for item in items:
        domain = _result_domain(item)
        if any(
            domain == blocked or domain.endswith("." + blocked)
            for blocked in _GENERIC_RESULT_DOMAINS
        ):
            continue
        kept.append(item)
    return kept


def _context_from_results(
    results: Any,
    *,
    limit: int,
    title_key: str,
    url_key: str,
    content_key: str,
) -> list[str]:
    context: list[str] = []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        rendered = _safe_result_text(
            {
                "title": _strip_markup(str(result.get(title_key) or result.get("title") or "")),
                "url": str(result.get(url_key) or result.get("link") or "").strip(),
                "content": _strip_markup(
                    str(
                        result.get(content_key)
                        or result.get("snippet")
                        or result.get("content")
                        or ""
                    )
                ),
            }
        )
        if rendered and rendered not in context:
            context.append(rendered)
        if len(context) >= max(1, min(int(limit), 8)):
            break
    return _filter_generic_results(context)


async def _brave_context_items(search_query: str, *, limit: int = 5) -> list[str]:
    key = os.environ.get("NEXUSAI_BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": search_query[:400], "count": max(1, min(int(limit), 10))},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    web = payload.get("web") if isinstance(payload, dict) else {}
    results = web.get("results") if isinstance(web, dict) else []
    return _context_from_results(
        results,
        limit=limit,
        title_key="title",
        url_key="url",
        content_key="description",
    )


async def _tavily_context_items(search_query: str, *, limit: int = 5) -> list[str]:
    key = os.environ.get("NEXUSAI_TAVILY_API_KEY", "").strip()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": search_query[:400],
                    "max_results": max(1, min(int(limit), 10)),
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = payload.get("results") if isinstance(payload, dict) else []
    return _context_from_results(
        results,
        limit=limit,
        title_key="title",
        url_key="url",
        content_key="content",
    )


async def _serper_context_items(search_query: str, *, limit: int = 5) -> list[str]:
    key = os.environ.get("NEXUSAI_SERPER_API_KEY", "").strip()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": search_query[:400], "num": max(1, min(int(limit), 10))},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = payload.get("organic") if isinstance(payload, dict) else []
    return _context_from_results(
        results,
        limit=limit,
        title_key="title",
        url_key="link",
        content_key="snippet",
    )


async def _bing_rss_context_items(search_query: str, *, limit: int = 5) -> list[str]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(
                "https://www.bing.com/search",
                params={
                    "q": search_query[:600],
                    "format": "rss",
                    "count": max(10, min(int(limit) * 2, 20)),
                },
                headers=_BING_RSS_HEADERS,
            )
            response.raise_for_status()
            xml_text = response.text
    except (httpx.HTTPError, ValueError):
        return []
    return _filter_generic_results(parse_bing_rss(xml_text, limit=limit))


async def _searxng_context_items(search_query: str, *, limit: int = 5) -> list[str]:
    endpoint = os.environ.get("NEXUSAI_SEARXNG_URL", "http://searxng:8080").rstrip("/")  # sec-ok: http-url - internal Docker network service, not public
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False) as client:
            response = await client.get(
                f"{endpoint}/search",
                params={"q": search_query[:600], "format": "json", "language": "en-US"},
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
