import pytest

from control_plane.api.chat import _parse_planned_queries
from control_plane.chat.web_search import (
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


def test_planned_queries_ignore_intro_lines_and_numbering():
    output = (
        "Here are the queries:\n"
        '1. "how to 3D scan with a phone photogrammetry Polycam KIRI"\n'
        "2. Samsung Galaxy S26 Ultra 3D scanning capability\n"
    )

    assert _parse_planned_queries(output) == [
        "how to 3D scan with a phone photogrammetry Polycam KIRI",
        "Samsung Galaxy S26 Ultra 3D scanning capability",
    ]


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
