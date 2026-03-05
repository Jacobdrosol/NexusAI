"""Integration tests for chat API routes."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_create_conversation_and_post_message(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat 1"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        # Register a bot so chat request can target it.
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-chat",
                "name": "Chat Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-chat"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["user_message"]["content"] == "hello"
        assert data["assistant_message"]["content"] == "assistant reply"


@pytest.mark.anyio
async def test_stream_message_endpoint(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "stream reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-stream",
                "name": "Stream Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-stream"},
        )
        assert stream_resp.status_code == 200
        assert "event: user_message" in stream_resp.text
        assert "event: assistant_message" in stream_resp.text
        assert "event: done" in stream_resp.text


@pytest.mark.anyio
async def test_assign_message_creates_task_graph_and_summary(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Chat"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={"id": "bot-pm", "name": "PM Bot", "role": "pm", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-code", "name": "Code Bot", "role": "coder", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-test", "name": "Test Bot", "role": "tester", "backends": [], "enabled": True},
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build authentication API and tests", "bot_id": "bot-pm"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["mode"] == "assign"
        assert len(data["assignment"]["tasks"]) >= 2
        assert "Assignment summary" in data["assistant_message"]["content"]

        tasks_resp = await client.get("/v1/tasks")
        assert tasks_resp.status_code == 200
        assert len(tasks_resp.json()) >= 2


@pytest.mark.anyio
async def test_stream_assign_emits_task_events(cp_app):
    async def _schedule(task):
        import asyncio

        if str(task.id).startswith("pm-plan-"):
            return {
                "output": (
                    '{"steps":['
                    '{"id":"step_1","title":"Design","instruction":"Design API","role_hint":"coder","depends_on":[]},'
                    '{"id":"step_2","title":"Implement","instruction":"Implement API","role_hint":"coder","depends_on":["step_1"]}'
                    "]} "
                )
            }
        await asyncio.sleep(0.05)
        return {"output": f"done:{task.id}"}

    cp_app.state.scheduler.schedule = AsyncMock(side_effect=_schedule)
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Stream"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={"id": "bot-pm", "name": "PM Bot", "role": "pm", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-code", "name": "Code Bot", "role": "coder", "backends": [], "enabled": True},
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "@assign Build API", "bot_id": "bot-pm"},
        )
        assert stream_resp.status_code == 200
        text = stream_resp.text
        assert "event: task_graph" in text
        assert "event: task_status" in text
        assert "event: assistant_message" in text
        assert "event: done" in text


@pytest.mark.anyio
async def test_chat_context_item_ids_are_resolved_from_vault(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post("/v1/chat/conversations", json={"title": "Context IDs"})
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-context", "name": "Ctx Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        vault_item = await client.post(
            "/v1/vault/items",
            json={"title": "Doc", "content": "Secret architecture note", "namespace": "global"},
        )
        item_id = vault_item.json()["id"]

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Use context",
                "bot_id": "bot-context",
                "context_item_ids": [item_id],
            },
        )
        assert resp.status_code == 200
        # Ensure scheduler received a context system message.
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "Context:\n" in payload[0]["content"]
