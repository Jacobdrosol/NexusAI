"""Model-driven web research tools for direct chat.

The model decides when to search, chooses its own queries, reads the results,
and can fetch a page for more detail. Nothing here derives search queries from
conversation history.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from lxml import html as lxml_html

from control_plane.chat.web_search import search_web_results

_MAX_PAGE_CHARS = 6000
_MAX_PAGE_BYTES = 1_500_000
_PAGE_TIMEOUT = httpx.Timeout(12.0, connect=4.0)

WEB_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information. Call this when the answer depends on "
                "recent events, current prices, product specs, software versions, or facts you "
                "are not sure about. You may call it multiple times with different queries. "
                "Cite the URLs from the results in your final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A specific search query. Name products, apps, versions, or "
                            "technical terms when relevant."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return, 1 to 8. Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web_page",
            "description": (
                "Fetch a public web page and return its readable text. Use this after web_search "
                "when a result looks promising but the snippet is not enough to judge it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full http or https URL to fetch.",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


async def execute_web_tool(name: str, arguments: dict[str, Any]) -> str:
    if name == "web_search":
        return await _tool_web_search(arguments)
    if name == "fetch_web_page":
        return await _tool_fetch_page(arguments)
    return f"ERROR: Unknown tool '{name}'."


async def _tool_web_search(arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "ERROR: 'query' is required."
    try:
        count = int(arguments.get("count") or 5)
    except (TypeError, ValueError):
        count = 5
    results = await search_web_results(query, limit=max(1, min(count, 8)))
    if not results:
        return json.dumps(
            {"query": query, "results": [], "note": "No results returned."},
            ensure_ascii=False,
        )
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)


async def _tool_fetch_page(arguments: dict[str, Any]) -> str:
    url = str(arguments.get("url") or "").strip()
    if not url:
        return "ERROR: 'url' is required."
    if not await _is_safe_public_url(url):
        return "ERROR: URL is not a permitted public http or https address."
    try:
        async with httpx.AsyncClient(
            timeout=_PAGE_TIMEOUT,
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").lower()
            if "html" not in content_type and "text/" not in content_type:
                return f"ERROR: Unsupported content type: {content_type[:80] or 'unknown'}"
            raw = response.text[:_MAX_PAGE_BYTES]
            final_url = str(response.url)
    except (httpx.HTTPError, ValueError) as exc:
        return f"ERROR: Could not fetch page: {type(exc).__name__}"
    text = _extract_text(raw)
    if not text:
        return "No readable text found on the page."
    return f"URL: {final_url}\n\n{text[:_MAX_PAGE_CHARS]}"


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _extract_text(raw_html: str) -> str:
    try:
        document = lxml_html.fromstring(raw_html)
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(raw_html or ""))).strip()
    for node in document.xpath(
        "//script|//style|//noscript|//svg|//nav|//footer|//header|//form|//aside"
    ):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    bodies = document.xpath("//body")
    text = " ".join(bodies[0].xpath(".//text()")) if bodies else " ".join(document.xpath("//text()"))
    return re.sub(r"\s+", " ", text).strip()


async def _is_safe_public_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if port not in {80, 443}:
        return False
    lower_host = host.strip().lower()
    if lower_host == "localhost" or lower_host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except Exception:
        return False
    for info in infos:
        address = str(info[4][0])
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True
