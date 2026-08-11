"""Tests for the plan approval gate, ticket_source_v1 payload source, and workflow config."""

import asyncio
import json

import pytest

from control_plane.orchestration.graph_completeness import ORCH_STATES
from control_plane.tickets.ticket_source_store import TicketSourceStore
from control_plane.schedule_payload_sources import (
    TICKET_SOURCE,
    _ticket_source_config,
    SystemPayloadSourceError,
)


def test_orch_states_include_plan_pending_approval():
    assert "plan_pending_approval" in ORCH_STATES


def test_ticket_source_config_valid():
    cfg = _ticket_source_config(
        {"type": TICKET_SOURCE, "source_id": "src-123", "max_items": 10, "unlinked_only": True},
        field_name="system_payload_source",
    )
    assert cfg["source_id"] == "src-123"
    assert cfg["max_items"] == 10
    assert cfg["unlinked_only"] is True
    assert cfg["type"] == TICKET_SOURCE


def test_ticket_source_config_requires_source_id():
    with pytest.raises(SystemPayloadSourceError):
        _ticket_source_config(
            {"type": TICKET_SOURCE, "max_items": 10}, field_name="system_payload_source"
        )


def test_ticket_source_config_validates_max_items():
    with pytest.raises(SystemPayloadSourceError):
        _ticket_source_config(
            {"type": TICKET_SOURCE, "source_id": "s", "max_items": 0},
            field_name="system_payload_source",
        )
    with pytest.raises(SystemPayloadSourceError):
        _ticket_source_config(
            {"type": TICKET_SOURCE, "source_id": "s", "max_items": 101},
            field_name="system_payload_source",
        )


@pytest.mark.asyncio
async def test_ticket_source_payload_returns_unlinked_items(tmp_path):
    store = TicketSourceStore(db_path=str(tmp_path / "t.db"))
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.upsert_item(source_id=source["id"], external_id="1", title="A")
    await store.upsert_item(source_id=source["id"], external_id="2", title="B")
    # link one so it's excluded from unlinked_only
    await store.link_item_to_task(source["id"], "1", "task-1")

    from control_plane.schedule_payload_sources import ticket_source_payload

    schedule = {"project_id": "proj-1"}
    config = {
        "type": TICKET_SOURCE,
        "source_id": source["id"],
        "max_items": 10,
        "unlinked_only": True,
        "target_field": "ticket_items",
    }
    payload = await ticket_source_payload(config, schedule, None, store)
    assert payload["item_count"] == 1
    assert payload["items"][0]["external_id"] == "2"


@pytest.mark.asyncio
async def test_ticket_source_payload_rejects_wrong_project(tmp_path):
    store = TicketSourceStore(db_path=str(tmp_path / "t.db"))
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    from control_plane.schedule_payload_sources import ticket_source_payload

    schedule = {"project_id": "proj-2"}
    config = {"type": TICKET_SOURCE, "source_id": source["id"]}
    with pytest.raises(SystemPayloadSourceError):
        await ticket_source_payload(config, schedule, None, store)


@pytest.mark.asyncio
async def test_run_store_list_runs_by_state(tmp_path):
    from control_plane.orchestration.run_store import OrchestrationRunStore

    store = OrchestrationRunStore(db_path=str(tmp_path / "r.db"))
    run = await store.create_run(
        conversation_id="conv-1",
        project_id="proj-1",
        pm_bot_id="pm-orchestrator",
        instruction="build a feature",
        graph_snapshot={"nodes": [], "edges": []},
        node_overrides={},
    )
    await store.update_orch_state(run["id"], "plan_pending_approval", reason="test", actor="test")

    pending = await store.list_runs(state="plan_pending_approval")
    assert len(pending) == 1
    assert pending[0]["id"] == run["id"]

    not_pending = await store.list_runs(state="running")
    assert len(not_pending) == 0


@pytest.mark.asyncio
async def test_run_store_update_run_metadata(tmp_path):
    from control_plane.orchestration.run_store import OrchestrationRunStore

    store = OrchestrationRunStore(db_path=str(tmp_path / "r.db"))
    run = await store.create_run(
        conversation_id="conv-1",
        project_id="proj-1",
        pm_bot_id="pm-orchestrator",
        instruction="x",
        graph_snapshot={"nodes": [], "edges": []},
        node_overrides={},
    )
    updated = await store.update_run_metadata(run["id"], {"plan_approval_required": True, "step": 1})
    assert updated["metadata"]["plan_approval_required"] is True
    assert updated["metadata"]["step"] == 1

    # Merge, don't overwrite
    updated2 = await store.update_run_metadata(run["id"], {"step": 2})
    assert updated2["metadata"]["plan_approval_required"] is True
    assert updated2["metadata"]["step"] == 2