"""Tests for the planning_bot blueprint and ticket_scope restrictions."""

import pytest

from control_plane.bot_blueprints import (
    SpecialistBlueprintRequest,
    build_specialist_bot,
    list_specialist_blueprints,
)
from shared.models import BackendConfig


def _backend():
    return BackendConfig(type="cloud_api", provider="ollama_cloud", model="gpt-oss:120b-cloud")


def test_planning_bot_in_catalog():
    kinds = [b["kind"] for b in list_specialist_blueprints()]
    assert "planning_bot" in kinds


def test_planning_bot_is_read_only_with_workspace_tools():
    req = SpecialistBlueprintRequest(
        kind="planning_bot",
        name="GlobeIQ Planner",
        backends=[_backend()],
        project_id="globeiq",
        activate=True,
    )
    bot = build_specialist_bot(req)
    assert bot.role == "planning_bot"
    assert bot.project_id == "globeiq"
    assert bot.execution_policy.workspace_context_injection is True
    assert bot.execution_policy.repo_output_mode == "deny"
    assert bot.execution_policy.can_apply_db_actions is False
    profile = bot.routing_rules["worker_profile"]
    assert profile["can_edit"] is False
    assert profile["task_scope"] == "read-only-planning"
    assert "repo" in bot.context_access.can_self_serve
    assert "planning_bot" in bot.routing_rules["specialist"]["kind"]


def test_planning_bot_ticket_scope_lands_in_routing_rules():
    req = SpecialistBlueprintRequest(
        kind="planning_bot",
        name="Scoped Planner",
        backends=[_backend()],
        project_id="globeiq",
        activate=True,
        ticket_scope={
            "source_ids": ["src-1"],
            "tags": ["bug", "frontend"],
            "tag_filter": "all",
            "states": ["open", "doing"],
        },
    )
    bot = build_specialist_bot(req)
    scope = bot.routing_rules["ticket_scope"]
    assert scope["source_ids"] == ["src-1"]
    assert scope["tags"] == ["bug", "frontend"]
    assert scope["tag_filter"] == "all"
    assert scope["states"] == ["open", "doing"]


def test_ticket_scope_invalid_tag_filter_rejected():
    with pytest.raises(ValueError):
        SpecialistBlueprintRequest(
            kind="planning_bot",
            name="Bad",
            backends=[_backend()],
            ticket_scope={"tag_filter": "sometimes"},
        )


def test_ticket_scope_invalid_tags_rejected():
    with pytest.raises(ValueError):
        SpecialistBlueprintRequest(
            kind="planning_bot",
            name="Bad",
            backends=[_backend()],
            ticket_scope={"tags": "not-a-list"},
        )


@pytest.mark.asyncio
async def test_ticket_source_payload_applies_ticket_scope(tmp_path):
    from control_plane.tickets.ticket_source_store import TicketSourceStore
    from control_plane.schedule_payload_sources import ticket_source_payload

    store = TicketSourceStore(db_path=str(tmp_path / "t.db"))
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.upsert_item(source_id=source["id"], external_id="1", title="Bug A", labels=["bug"])
    await store.upsert_item(source_id=source["id"], external_id="2", title="Feature B", labels=["feature"])

    schedule = {"project_id": "proj-1", "target_bot_id": "planner"}
    config = {"type": "ticket_source_v1", "source_id": source["id"], "max_items": 10}

    # tag filter "any" -> only bug items
    payload = await ticket_source_payload(
        config, schedule, None, store,
        ticket_scope={"tags": ["bug"], "tag_filter": "any", "states": []},
    )
    assert payload["item_count"] == 1
    assert payload["items"][0]["external_id"] == "1"

    # tag filter "none" -> only non-bug items
    payload = await ticket_source_payload(
        config, schedule, None, store,
        ticket_scope={"tags": ["bug"], "tag_filter": "none", "states": []},
    )
    assert payload["item_count"] == 1
    assert payload["items"][0]["external_id"] == "2"

    # source restriction excludes this source
    payload = await ticket_source_payload(
        config, schedule, None, store,
        ticket_scope={"source_ids": ["other-source"], "tags": [], "states": []},
    )
    assert payload["item_count"] == 0
    assert payload.get("skipped_reason") == "source not in bot ticket_scope"


@pytest.mark.asyncio
async def test_ticket_source_payload_states_filter(tmp_path):
    from control_plane.tickets.ticket_source_store import TicketSourceStore
    from control_plane.schedule_payload_sources import ticket_source_payload

    store = TicketSourceStore(db_path=str(tmp_path / "t.db"))
    source = await store.create_source(
        project_id="proj-1", name="GH", source_type="github_issues",
    )
    await store.upsert_item(source_id=source["id"], external_id="1", title="Open", state="open")
    await store.upsert_item(source_id=source["id"], external_id="2", title="Done", state="done")

    schedule = {"project_id": "proj-1", "target_bot_id": "planner"}
    config = {"type": "ticket_source_v1", "source_id": source["id"], "max_items": 10}
    payload = await ticket_source_payload(
        config, schedule, None, store,
        ticket_scope={"tags": [], "states": ["open"]},
    )
    assert payload["item_count"] == 1
    assert payload["items"][0]["external_id"] == "1"