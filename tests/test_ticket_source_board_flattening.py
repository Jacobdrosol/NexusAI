"""Tests for the generic_http board-flattening ticket source poller."""

import asyncio
import json

import pytest

from control_plane.tickets.pollers import _flatten_board_items, poll_generic_http


def test_flatten_board_items_basic():
    boards = [
        {
            "id": "b1",
            "title": "Product Board",
            "columns": [
                {
                    "id": "c1",
                    "name": "Backlog",
                    "cards": [
                        {"id": "1", "title": "Fix login", "state": "todo", "priority": "high"},
                        {"id": "2", "title": "Add export", "state": "doing", "priority": "medium"},
                    ],
                },
                {
                    "id": "c2",
                    "name": "Done",
                    "cards": [
                        {"id": "3", "title": "Ship v1", "state": "done", "priority": "low"},
                    ],
                },
            ],
        }
    ]
    items = _flatten_board_items(
        boards,
        column_field="columns",
        card_field="cards",
        column_name_field="name",
        board_title_field="title",
        field_map={},
        max_items=100,
    )
    assert len(items) == 3
    assert items[0]["external_id"] == "1"
    assert items[0]["title"] == "Fix login"
    assert items[0]["state"] == "todo"
    assert items[0]["raw"]["_board_title"] == "Product Board"
    assert items[0]["raw"]["_column_name"] == "Backlog"
    assert items[2]["raw"]["_column_name"] == "Done"


def test_flatten_board_items_field_map():
    boards = [
        {
            "title": "Board",
            "columns": [
                {
                    "name": "Sprint 1",
                    "cards": [
                        {
                            "ticket_id": "T-100",
                            "subject": "Bug in checkout",
                            "description": "Payment fails",
                            "link": "https://example.com/T-100",
                            "status": "open",
                            "tags": ["bug", "payments"],
                        }
                    ],
                }
            ],
        }
    ]
    field_map = {
        "id": "ticket_id",
        "title": "subject",
        "body": "description",
        "url": "link",
        "state": "status",
        "labels": "tags",
    }
    items = _flatten_board_items(
        boards,
        column_field="columns",
        card_field="cards",
        column_name_field="name",
        board_title_field="title",
        field_map=field_map,
        max_items=100,
    )
    assert len(items) == 1
    item = items[0]
    assert item["external_id"] == "T-100"
    assert item["title"] == "Bug in checkout"
    assert item["body"] == "Payment fails"
    assert item["url"] == "https://example.com/T-100"
    assert item["state"] == "open"
    assert item["labels"] == ["bug", "payments"]


def test_flatten_board_items_max_items():
    boards = [
        {
            "title": "B",
            "columns": [
                {"name": "C", "cards": [{"id": str(i), "title": f"Item {i}"} for i in range(10)]}
            ],
        }
    ]
    items = _flatten_board_items(
        boards,
        column_field="columns",
        card_field="cards",
        column_name_field="name",
        board_title_field="title",
        field_map={},
        max_items=3,
    )
    assert len(items) == 3


def test_flatten_board_items_skips_missing_id():
    boards = [
        {
            "title": "B",
            "columns": [
                {"name": "C", "cards": [{"title": "No id"}, {"id": "5", "title": "Has id"}]}
            ],
        }
    ]
    items = _flatten_board_items(
        boards,
        column_field="columns",
        card_field="cards",
        column_name_field="name",
        board_title_field="title",
        field_map={},
        max_items=100,
    )
    assert len(items) == 1
    assert items[0]["external_id"] == "5"


def test_flatten_board_items_nested_field_map():
    boards = [
        {
            "title": "B",
            "columns": [
                {
                    "name": "C",
                    "cards": [
                        {
                            "id": "9",
                            "fields": {"summary": "Nested title", "status": {"name": "In Progress"}},
                        }
                    ],
                }
            ],
        }
    ]
    field_map = {"title": "fields.summary", "state": "fields.status.name"}
    items = _flatten_board_items(
        boards,
        column_field="columns",
        card_field="cards",
        column_name_field="name",
        board_title_field="title",
        field_map=field_map,
        max_items=100,
    )
    assert items[0]["title"] == "Nested title"
    assert items[0]["state"] == "In Progress"


def test_fetch_json_sends_browser_like_user_agent(monkeypatch):
    import urllib.request

    captured = {}

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=30):
        captured["headers"] = {k: v for k, v in req.header_items()}
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    from control_plane.tickets.pollers import _fetch_json

    _fetch_json("https://example.com/api", headers={})
    assert "Python-urllib" not in str(captured["headers"])
    assert "User-agent" in captured["headers"] or "user-agent" in captured["headers"]
    ua = captured["headers"].get("User-agent") or captured["headers"].get("user-agent")
    assert ua and "Mozilla" in ua


def test_fetch_json_respects_custom_user_agent(monkeypatch):
    import urllib.request

    captured = {}

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=30):
        captured["headers"] = {k: v for k, v in req.header_items()}
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    from control_plane.tickets.pollers import _fetch_json

    _fetch_json("https://example.com/api", headers={}, user_agent="CustomBot/2.0")
    ua = captured["headers"].get("User-agent") or captured["headers"].get("user-agent")
    assert ua == "CustomBot/2.0"


@pytest.mark.asyncio
async def test_poll_generic_http_board_mode(monkeypatch):
    payload = {
        "boards": [
            {
                "title": "Scrum",
                "columns": [
                    {"name": "To Do", "cards": [{"id": "1", "title": "Task A", "status": "todo"}]}
                ],
            }
        ]
    }

    def fake_fetch(url, headers, timeout=30, user_agent=None):
        return payload

    monkeypatch.setattr("control_plane.tickets.pollers._fetch_json", fake_fetch)
    items = await poll_generic_http(
        {
            "url": "https://example.com/api/board",
            "board_field": "boards",
            "column_field": "columns",
            "card_field": "cards",
            "column_name_field": "name",
            "board_title_field": "title",
            "max_items": 10,
        },
        credential=None,
    )
    assert len(items) == 1
    assert items[0]["title"] == "Task A"
    assert items[0]["raw"]["_column_name"] == "To Do"