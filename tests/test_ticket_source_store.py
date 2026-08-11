"""Tests for the ticket source store and API."""

import asyncio
import json
import os
import tempfile

import pytest

from control_plane.tickets.ticket_source_store import TicketSourceStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    return TicketSourceStore(db_path=str(db))


@pytest.mark.asyncio
async def test_create_and_get_source(store):
    source = await store.create_source(
        project_id="proj-1",
        name="Test GitHub",
        source_type="github_issues",
        config={"repo_full_name": "owner/repo"},
        credential_key_ref="github_pat::proj-1",
    )
    assert source["id"]
    assert source["project_id"] == "proj-1"
    assert source["name"] == "Test GitHub"
    assert source["source_type"] == "github_issues"
    assert source["config"]["repo_full_name"] == "owner/repo"
    assert source["enabled"] is True

    fetched = await store.get_source(source["id"])
    assert fetched is not None
    assert fetched["name"] == "Test GitHub"


@pytest.mark.asyncio
async def test_list_sources_by_project(store):
    await store.create_source(
        project_id="proj-1", name="A", source_type="github_issues",
    )
    await store.create_source(
        project_id="proj-1", name="B", source_type="generic_http",
    )
    await store.create_source(
        project_id="proj-2", name="C", source_type="jira",
    )

    proj1 = await store.list_sources(project_id="proj-1")
    assert len(proj1) == 2
    assert {s["name"] for s in proj1} == {"A", "B"}

    proj2 = await store.list_sources(project_id="proj-2")
    assert len(proj2) == 1


@pytest.mark.asyncio
async def test_update_source(store):
    source = await store.create_source(
        project_id="proj-1", name="Old", source_type="github_issues",
    )
    updated = await store.update_source(source["id"], name="New", enabled=False)
    assert updated["name"] == "New"
    assert updated["enabled"] is False


@pytest.mark.asyncio
async def test_delete_source(store):
    source = await store.create_source(
        project_id="proj-1", name="Delete Me", source_type="github_issues",
    )
    ok = await store.delete_source(source["id"])
    assert ok is True
    assert await store.get_source(source["id"]) is None


@pytest.mark.asyncio
async def test_upsert_and_get_item(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    item = await store.upsert_item(
        source_id=source["id"],
        external_id="42",
        title="Bug in auth",
        body="Login fails",
        url="https://github.com/owner/repo/issues/42",
        state="open",
        labels=["bug", "auth"],
        author="jacob",
    )
    assert item["external_id"] == "42"
    assert item["title"] == "Bug in auth"
    assert item["labels"] == ["bug", "auth"]

    existing = await store.get_item_by_external_id(source["id"], "42")
    assert existing is not None
    assert existing["id"] == item["id"]


@pytest.mark.asyncio
async def test_upsert_item_idempotent(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    item1 = await store.upsert_item(
        source_id=source["id"], external_id="10", title="First",
    )
    item2 = await store.upsert_item(
        source_id=source["id"], external_id="10", title="Updated",
    )
    assert item1["id"] == item2["id"]
    assert item2["title"] == "Updated"


@pytest.mark.asyncio
async def test_list_items(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    for i in range(5):
        await store.upsert_item(
            source_id=source["id"], external_id=str(i), title=f"Issue {i}",
        )
    items = await store.list_items(source["id"], limit=10)
    assert len(items) == 5


@pytest.mark.asyncio
async def test_link_item_to_task(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.upsert_item(
        source_id=source["id"], external_id="99", title="Task issue",
    )
    ok = await store.link_item_to_task(source["id"], "99", "task-abc-123")
    assert ok is True

    item = await store.get_item_by_external_id(source["id"], "99")
    assert item["task_id"] == "task-abc-123"


@pytest.mark.asyncio
async def test_record_poll(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.record_poll(source["id"], status="ok", item_count=5)
    fetched = await store.get_source(source["id"])
    assert fetched["last_poll_status"] == "ok"
    assert fetched["last_poll_count"] == 5
    assert fetched["last_polled_at"] is not None


@pytest.mark.asyncio
async def test_count_items(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    for i in range(3):
        await store.upsert_item(
            source_id=source["id"], external_id=str(i), title=f"Issue {i}",
        )
    count = await store.count_items(source["id"])
    assert count == 3


@pytest.mark.asyncio
async def test_list_items_unlinked_only(store):
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.upsert_item(source_id=source["id"], external_id="1", title="A")
    await store.upsert_item(source_id=source["id"], external_id="2", title="B")
    await store.link_item_to_task(source["id"], "1", "task-1")

    unlinked = await store.list_items(source["id"], unlinked_only=True)
    assert len(unlinked) == 1
    assert unlinked[0]["external_id"] == "2"