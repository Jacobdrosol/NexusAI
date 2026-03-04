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
