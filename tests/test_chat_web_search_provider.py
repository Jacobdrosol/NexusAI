import pytest

from control_plane.chat.web_search import (
    _context_from_results,
    _filter_generic_results,
    parse_bing_rss,
    resolve_web_queries_context_items,
)

SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel>
  <item>
    <title>Scanning &amp; Capture - Polycam Help</title>
    <link>https://learn.poly.cam/hc/en-us/categories/scanning</link>
    <description>How to capture objects with Polycam.</description>
  </item>
  <item>
    <title>Getting Started with KIRI Engine</title>
    <link>https://www.kiriengine.app/tutorial/getting-started</link>
    <description>Photogrammetry tutorial.</description>
  </item>
</channel></rss>"""


def test_parse_bing_rss_keeps_citations_and_unescapes_text():
    items = parse_bing_rss(SAMPLE_RSS, limit=5)

    assert len(items) == 2
    assert items[0].startswith("[web:learn.poly.cam] Scanning & Capture - Polycam Help")
    assert "URL: https://learn.poly.cam/hc/en-us/categories/scanning" in items[0]
    assert items[1].startswith("[web:www.kiriengine.app] Getting Started with KIRI Engine")


def test_generic_result_domains_are_dropped():
    items = [
        "[web:bestbuy.com] Best Buy\nURL: https://www.bestbuy.com/",
        "[web:learn.poly.cam] Polycam\nURL: https://learn.poly.cam/guide",
        "[web:sketchfab.com] Sketchfab\nURL: https://sketchfab.com/",
    ]

    assert _filter_generic_results(items) == [items[1]]


def test_context_from_results_maps_and_filters_provider_payloads():
    results = [
        {
            "title": "Scanning & Capture - Polycam",
            "url": "https://learn.poly.cam/guide",
            "description": "<b>How</b> to scan an object",
        },
        {
            "title": "Best Buy",
            "url": "https://www.bestbuy.com/",
            "description": "Store locator",
        },
    ]

    items = _context_from_results(
        results,
        limit=5,
        title_key="title",
        url_key="url",
        content_key="description",
    )

    assert len(items) == 1
    assert items[0].startswith("[web:learn.poly.cam] Scanning & Capture - Polycam")
    assert "How to scan an object" in items[0]


@pytest.mark.anyio
async def test_multiple_queries_are_merged_without_duplicates(monkeypatch):
    async def fake_context(query, *, limit=5, require_intent=True):
        return [f"item-{query}"]

    monkeypatch.setattr(
        "control_plane.chat.web_search.resolve_web_context_items",
        fake_context,
    )

    result = await resolve_web_queries_context_items(["a", "b", "a"])

    assert result == ["item-a", "item-b"]


@pytest.mark.anyio
async def test_missing_provider_key_falls_back_to_bing(monkeypatch):
    from control_plane.chat import web_search as module

    monkeypatch.setenv("NEXUSAI_SEARCH_PROVIDER", "brave")
    monkeypatch.delenv("NEXUSAI_BRAVE_SEARCH_API_KEY", raising=False)
    calls: list[str] = []

    async def fake_brave(query, *, limit=5):
        calls.append("brave")
        return []

    async def fake_bing(query, *, limit=5):
        calls.append("bing")
        return ["[web:example.com] Example\nURL: https://example.com"]

    async def fake_searx(query, *, limit=5):
        calls.append("searx")
        return []

    monkeypatch.setattr(module, "_brave_context_items", fake_brave)
    monkeypatch.setattr(module, "_bing_rss_context_items", fake_bing)
    monkeypatch.setattr(module, "_searxng_context_items", fake_searx)

    items = await module.resolve_web_context_items("test query", require_intent=False)

    assert calls == ["brave", "bing"]
    assert items == ["[web:example.com] Example\nURL: https://example.com"]
