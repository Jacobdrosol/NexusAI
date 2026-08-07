"""Integration tests for chat API routes."""

import asyncio
import base64
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


def test_web_search_only_matches_current_or_lookup_prompts():
    from control_plane.chat.web_search import should_search_web

    assert should_search_web("What is the current market price for this part?") is True
    assert should_search_web("Look up this serial number for me") is True
    assert should_search_web("Help me word a private message to my colleague") is False


@pytest.mark.anyio
async def test_chat_injects_self_hosted_web_context_only_for_enabled_bot(cp_app, monkeypatch):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "The cited price is current."}

    cp_app.state.scheduler.schedule = _capture_schedule
    search = AsyncMock(return_value=["[web:example.test] Example price\nURL: https://example.test/price\nSnippet: $12.34"])
    monkeypatch.setattr("control_plane.api.chat.resolve_web_context_items", search)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        bot = await client.post(
            "/v1/bots",
            json={
                "id": "web-chat-bot",
                "name": "Web Chat Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "routing_rules": {"chat_tool_access": {"enabled": True, "web_search": True}},
                "enabled": True,
            },
        )
        assert bot.status_code == 200
        conversation = await client.post("/v1/chat/conversations", json={"title": "Current prices"})
        assert conversation.status_code == 200
        sent = await client.post(
            f"/v1/chat/conversations/{conversation.json()['id']}/messages",
            json={"content": "What is the current price?", "bot_id": "web-chat-bot"},
        )
        assert sent.status_code == 200

    search.assert_awaited_once_with("What is the current price?")
    context_text = "\n".join(str(item.get("content") or "") for item in captured_payloads[0])
    assert "https://example.test/price" in context_text
    assert "cite the exact URL" in context_text


def test_ollama_message_normalization_preserves_image_content_parts():
    from control_plane.scheduler.scheduler import _messages_for_ollama, _payload_to_messages

    payload = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this image."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
            ],
        }
    ]

    normalized = _messages_for_ollama(_payload_to_messages(payload))

    assert normalized == [{"role": "user", "content": "Inspect this image.", "images": ["aGVsbG8="]}]


def test_document_and_binary_attachments_are_retained_and_described_to_the_model():
    from docx import Document

    from control_plane.api.chat import ChatAttachmentInput, _attachment_payload_dicts, _message_attachment_parts

    document = Document()
    document.add_paragraph("The attached DOCX contains this exact note.")
    buffer = BytesIO()
    document.save(buffer)
    docx_data_url = (
        "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
        + base64.b64encode(buffer.getvalue()).decode("ascii")
    )

    attachments = _attachment_payload_dicts(
        [
            ChatAttachmentInput(
                name="brief.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                kind="document",
                data_url=docx_data_url,
            ),
            ChatAttachmentInput(
                name="archive.zip",
                mime_type="application/zip",
                kind="binary",
                data_url="data:application/zip;base64,AAEC",
            ),
        ]
    )

    assert attachments[0]["kind"] == "document"
    assert attachments[0]["data_url"] == docx_data_url
    assert attachments[0]["extraction_status"] == "extracted"
    assert "exact note" in attachments[0]["text_content"]
    assert attachments[1]["data_url"] == "data:application/zip;base64,AAEC"

    parts = _message_attachment_parts({"attachments": attachments})
    assert any("The attached DOCX contains this exact note." in part["text"] for part in parts)
    assert any("raw contents were not inlined" in part["text"] for part in parts)


@pytest.mark.anyio
async def test_create_conversation_and_post_message(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={"output": "assistant reply", "usage": {"prompt_tokens": 12, "completion_tokens": 8}}
    )
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
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
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
        assert data["assistant_message"]["bot_id"] == "bot-chat"
        assert data["assistant_message"]["model"] == "qwen3.5:397b"
        assert data["assistant_message"]["provider"] == "ollama_cloud"
        metadata = data["assistant_message"]["metadata"]
        assert metadata["bot"]["id"] == "bot-chat"
        assert metadata["bot"]["name"] == "Chat Bot"
        assert metadata["bot"]["updated_at"]
        assert metadata["model"]["provider"] == "ollama_cloud"
        assert metadata["model"]["model"] == "qwen3.5:397b"
        assert metadata["model"]["source"] == "bot_config"
        assert metadata["usage"] == {"prompt_tokens": 12, "completion_tokens": 8}

        usage_resp = await client.get("/v1/chat/usage?hours=24&limit_conversations=5")
        assert usage_resp.status_code == 200
        usage = usage_resp.json()
        assert usage["totals"]["total_tokens"] == 20
        assert usage["totals"]["messages_with_usage"] == 1
        assert usage["by_conversation"][0]["conversation_id"] == conversation_id
        assert usage["by_bot"][0]["bot_id"] == "bot-chat"
        assert usage["by_provider_model"][0]["provider"] == "ollama_cloud"
        assert usage["by_provider_model"][0]["model"] == "qwen3.5:397b"
        assert isinstance(usage["chat_token_governor"]["enabled"], bool)
        assert "estimated_tokens_per_message" in usage["chat_token_governor"]["limits"]


@pytest.mark.anyio
async def test_project_chat_messages_are_automatically_ingested_and_unscoped_messages_are_not(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "project response"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-project-ingest",
                "name": "Project Ingest Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        project_conversation = await client.post(
            "/v1/chat/conversations",
            json={"title": "Project context", "scope": "project", "project_id": "project-vector"},
        )
        project_id = project_conversation.json()["id"]
        sent = await client.post(
            f"/v1/chat/conversations/{project_id}/messages",
            json={"content": "The release checklist requires browser verification.", "bot_id": "bot-project-ingest"},
        )
        assert sent.status_code == 200
        project_items = await cp_app.state.vault_manager.list_items(
            namespace="project:project-vector:chat",
            project_id="project-vector",
        )
        assert len(project_items) == 2
        assert {item.metadata["conversation_id"] for item in project_items} == {project_id}
        assert all(item.metadata["automatic"] is True for item in project_items)

        unscoped_conversation = await client.post("/v1/chat/conversations", json={"title": "Unscoped"})
        unscoped_id = unscoped_conversation.json()["id"]
        unscoped_sent = await client.post(
            f"/v1/chat/conversations/{unscoped_id}/messages",
            json={"content": "This remains in its own conversation.", "bot_id": "bot-project-ingest"},
        )
        assert unscoped_sent.status_code == 200
        assert await cp_app.state.vault_manager.list_items(namespace="project:project-vector:chat") == project_items


@pytest.mark.anyio
async def test_delete_message_pair_replaces_transcript_turn_and_removes_project_vectors(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "assistant message that must be deleted"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "bot-delete-message-pair",
                "name": "Delete Message Pair Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200
        conversation_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Delete Pair", "scope": "project", "project_id": "project-delete-pair"},
        )
        assert conversation_resp.status_code == 200
        conversation_id = conversation_resp.json()["id"]

        sent = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "Private project detail that must be removed.", "bot_id": "bot-delete-message-pair"},
        )
        assert sent.status_code == 200
        user_message = sent.json()["user_message"]
        assistant_message = sent.json()["assistant_message"]
        project_items = await cp_app.state.vault_manager.list_items(
            namespace="project:project-delete-pair:chat",
            project_id="project-delete-pair",
        )
        assert len(project_items) == 2

        deleted = await client.delete(
            f"/v1/chat/conversations/{conversation_id}/messages/{assistant_message['id']}",
        )
        assert deleted.status_code == 200
        assert set(deleted.json()["deleted_message_ids"]) == {user_message["id"], assistant_message["id"]}

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert messages_resp.status_code == 200
        messages = messages_resp.json()
        assert [message["content"] for message in messages] == ["Message deleted", "Message deleted"]
        assert all(message["metadata"]["deleted"] is True for message in messages)
        assert await cp_app.state.vault_manager.list_items(
            namespace="project:project-delete-pair:chat",
            project_id="project-delete-pair",
        ) == []
        assert await cp_app.state.chat_manager.search_message_memory(
            conversation_id,
            "Private project detail",
        ) == []

        follow_up = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "What remains in this chat?", "bot_id": "bot-delete-message-pair"},
        )
        assert follow_up.status_code == 200

    payload_text = "\n".join(str(item.get("content") or "") for item in captured_payloads[-1])
    assert "Private project detail that must be removed." not in payload_text
    assert "assistant message that must be deleted" not in payload_text
    assert "Message deleted" not in payload_text


@pytest.mark.anyio
async def test_delete_message_pair_rejects_unpaired_or_deleted_message(cp_app):
    manager = cp_app.state.chat_manager
    conversation = await manager.create_conversation(title="Incomplete Turn")
    message = await manager.add_message(conversation.id, role="user", content="still waiting")

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        response = await client.delete(f"/v1/chat/conversations/{conversation.id}/messages/{message.id}")

    assert response.status_code == 400
    assert "completed assistant response" in response.json()["detail"]


@pytest.mark.anyio
async def test_delete_failed_delivery_message_replaces_single_unsatisfied_turn(cp_app):
    manager = cp_app.state.chat_manager
    conversation = await manager.create_conversation(title="Failed delivery")
    message = await manager.add_message(
        conversation.id,
        role="user",
        content="The provider did not answer.",
        metadata={"delivery_failed": True, "delivery_error": "upstream unavailable"},
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        response = await client.delete(f"/v1/chat/conversations/{conversation.id}/messages/{message.id}")

    assert response.status_code == 200
    assert response.json()["deleted_message_ids"] == [message.id]
    messages = await manager.list_messages(conversation.id)
    assert messages[0].content == "Message deleted"
    assert messages[0].metadata["deleted"] is True


@pytest.mark.anyio
async def test_project_chat_context_is_retrieved_across_project_conversations(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "retrieval response"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-project-retrieval",
                "name": "Project Retrieval Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        source = await client.post(
            "/v1/chat/conversations",
            json={"title": "Release notes", "scope": "project", "project_id": "project-retrieval"},
        )
        source_id = source.json()["id"]
        await cp_app.state.chat_manager.add_message(
            source_id,
            "user",
            "The release checklist requires browser verification before production deployment.",
        )
        target = await client.post(
            "/v1/chat/conversations",
            json={"title": "Deployment review", "scope": "project", "project_id": "project-retrieval"},
        )
        target_id = target.json()["id"]
        sent = await client.post(
            f"/v1/chat/conversations/{target_id}/messages",
            json={"content": "What does the release checklist require for browser verification?", "bot_id": "bot-project-retrieval"},
        )
        assert sent.status_code == 200

    payload_text = "\n".join(str(item.get("content") or "") for item in captured_payloads[-1])
    assert "[project-chat:project-retrieval]" in payload_text
    assert "browser verification before production deployment" in payload_text
    assert source_id in payload_text


@pytest.mark.anyio
async def test_explicit_conversation_reference_includes_unscoped_transcript_for_same_user(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "reference response"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-conversation-reference",
                "name": "Conversation Reference Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        source = await client.post(
            "/v1/chat/conversations",
            json={"title": "Private unscoped notes", "owner_user_id": "jacob@example.com"},
        )
        source_id = source.json()["id"]
        await cp_app.state.chat_manager.add_message(source_id, "user", "My telescope calibration uses a 12 minute warmup.")
        target = await client.post(
            "/v1/chat/conversations",
            json={"title": "Follow up", "owner_user_id": "jacob@example.com"},
        )
        target_id = target.json()["id"]
        sent = await client.post(
            f"/v1/chat/conversations/{target_id}/messages",
            json={
                "content": f"Use conversation:{source_id} to answer the calibration question.",
                "bot_id": "bot-conversation-reference",
                "user_id": "jacob@example.com",
            },
        )
        assert sent.status_code == 200

    payload_text = "\n".join(str(item.get("content") or "") for item in captured_payloads[-1])
    assert f"[conversation:{source_id}]" in payload_text
    assert "12 minute warmup" in payload_text


@pytest.mark.anyio
async def test_explicit_conversation_reference_does_not_cross_owner_boundary(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "private response"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-private-reference",
                "name": "Private Reference Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        source = await client.post(
            "/v1/chat/conversations",
            json={"title": "Another user", "owner_user_id": "other@example.com"},
        )
        source_id = source.json()["id"]
        await cp_app.state.chat_manager.add_message(source_id, "user", "This private conversation must not leak.")
        target = await client.post(
            "/v1/chat/conversations",
            json={"title": "Jacob", "owner_user_id": "jacob@example.com"},
        )
        target_id = target.json()["id"]
        sent = await client.post(
            f"/v1/chat/conversations/{target_id}/messages",
            json={
                "content": f"Use conversation:{source_id}.",
                "bot_id": "bot-private-reference",
                "user_id": "jacob@example.com",
            },
        )
        assert sent.status_code == 200

    payload_text = "\n".join(str(item.get("content") or "") for item in captured_payloads[-1])
    assert "This private conversation must not leak." not in payload_text


@pytest.mark.anyio
async def test_chat_usage_recovers_provider_model_from_assistant_metadata(cp_app):
    chat_manager = cp_app.state.chat_manager
    conversation = await chat_manager.create_conversation("Legacy Metadata Usage")
    await chat_manager.add_message(
        conversation_id=conversation.id,
        role="assistant",
        content="legacy answer",
        bot_id="legacy-chat-bot",
        metadata={
            "model": {"provider": "ollama_cloud", "model": "legacy-model", "source": "bot_config"},
            "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        usage_resp = await client.get("/v1/chat/usage?hours=24&limit_conversations=5")

    assert usage_resp.status_code == 200
    usage = usage_resp.json()
    assert usage["totals"]["total_tokens"] == 11
    assert usage["by_provider_model"][0]["provider"] == "ollama_cloud"
    assert usage["by_provider_model"][0]["model"] == "legacy-model"


@pytest.mark.anyio
async def test_chat_default_model_id_is_attached_to_scheduled_task(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Chat Model Default",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-chat-model",
                "name": "Chat Model Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-chat-model"},
        )
        assert post_resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        assert task_arg.metadata is not None
        assert task_arg.metadata.preferred_model_id == "ollama-cloud-gpt-oss-120b"


@pytest.mark.anyio
async def test_chat_default_model_id_is_not_attached_to_explicit_bot_override(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Chat Model Override",
                "default_bot_id": "bot-default-model",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        for bot_id, provider, model in (
            ("bot-default-model", "ollama_cloud", "gpt-oss:120b"),
            ("bot-explicit-override", "openai", "gpt-5"),
        ):
            await client.post(
                "/v1/bots",
                json={
                    "id": bot_id,
                    "name": bot_id,
                    "role": "assistant",
                    "backends": [{"type": "cloud_api", "provider": provider, "model": model}],
                    "enabled": True,
                },
            )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-explicit-override"},
        )
        assert post_resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        assert task_arg.metadata is not None
        assert task_arg.metadata.preferred_model_id is None


@pytest.mark.anyio
async def test_chat_token_governor_blocks_global_hourly_limit(cp_app, monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setattr(
        chat_module,
        "_chat_token_governor_config",
        lambda: {
            "enabled": True,
            "global_hourly_limit": 10,
            "bot_hourly_limit": 0,
            "bot_hourly_limits": {},
            "estimated_tokens_per_message": 20,
        },
    )
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "should not run"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Governor"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello"},
        )

        assert post_resp.status_code == 429
        assert "chat token governor rejected message" in post_resp.text
        cp_app.state.scheduler.schedule.assert_not_awaited()
        messages = await cp_app.state.chat_manager.list_messages(conversation_id)
        assert messages == []


@pytest.mark.anyio
async def test_chat_token_governor_blocks_bot_hourly_limit(cp_app, monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setattr(
        chat_module,
        "_chat_token_governor_config",
        lambda: {
            "enabled": True,
            "global_hourly_limit": 0,
            "bot_hourly_limit": 15,
            "bot_hourly_limits": {"bot-chat-budget": 5},
            "estimated_tokens_per_message": 8,
        },
    )
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "should not run"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Bot Governor"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-chat-budget",
                "name": "Budgeted Chat Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-chat-budget"},
        )

        assert post_resp.status_code == 429
        assert "bot 'bot-chat-budget'" in post_resp.text
        cp_app.state.scheduler.schedule.assert_not_awaited()


@pytest.mark.anyio
async def test_chat_token_governor_uses_payload_size_estimate(cp_app, monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setattr(
        chat_module,
        "_chat_token_governor_config",
        lambda: {
            "enabled": True,
            "global_hourly_limit": 100,
            "bot_hourly_limit": 0,
            "bot_hourly_limits": {},
            "estimated_tokens_per_message": 1,
        },
    )
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "should not run"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Payload Governor"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "x" * 500},
        )

    assert post_resp.status_code == 429
    assert "plus estimate 125" in post_resp.text
    cp_app.state.scheduler.schedule.assert_not_awaited()
    messages = await cp_app.state.chat_manager.list_messages(conversation_id)
    assert messages == []


@pytest.mark.anyio
async def test_chat_stream_token_governor_uses_payload_size_estimate(cp_app, monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setattr(
        chat_module,
        "_chat_token_governor_config",
        lambda: {
            "enabled": True,
            "global_hourly_limit": 100,
            "bot_hourly_limit": 0,
            "bot_hourly_limits": {},
            "estimated_tokens_per_message": 1,
        },
    )
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "should not run"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Stream Payload Governor"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "x" * 500},
        )
        body = stream_resp.text

    assert stream_resp.status_code == 200
    assert "event: error" in body
    assert "plus estimate 125" in body
    cp_app.state.scheduler.schedule.assert_not_awaited()
    messages = await cp_app.state.chat_manager.list_messages(conversation_id)
    assert messages == []


@pytest.mark.anyio
async def test_chat_token_governor_uses_shared_settings_instance(cp_app, tmp_path):
    from shared.settings_manager import SettingsManager

    original_settings = SettingsManager._instance
    SettingsManager._instance = SettingsManager(str(tmp_path / "chat-governor-settings.db"))
    try:
        SettingsManager._instance.set("token_governor_enabled", "true", changed_by="test")
        SettingsManager._instance.set("token_governor_chat_global_hourly_limit", "10", changed_by="test")
        SettingsManager._instance.set("token_governor_estimated_tokens_per_chat_message", "20", changed_by="test")

        cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "should not run"})
        async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
            create_resp = await client.post("/v1/chat/conversations", json={"title": "Shared Settings Governor"})
            assert create_resp.status_code == 200
            conversation_id = create_resp.json()["id"]

            post_resp = await client.post(
                f"/v1/chat/conversations/{conversation_id}/messages",
                json={"content": "hello"},
            )

        assert post_resp.status_code == 429
        assert "chat token governor rejected message" in post_resp.text
        cp_app.state.scheduler.schedule.assert_not_awaited()
    finally:
        SettingsManager._instance = original_settings


@pytest.mark.anyio
async def test_user_scoped_memory_profile_retrieved_on_later_eligible_turn(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": f"assistant reply {len(captured_payloads)}"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Memory Chat", "owner_user_id": "user@example.com"},
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]
        assert create_resp.json()["memory_profiles_enabled"] is True

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-memory",
                "name": "Memory Bot",
                "role": "assistant",
                "memory_profiles_enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )

        first = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "My preferred name is Jacob.", "bot_id": "bot-memory", "user_id": "user@example.com"},
        )
        assert first.status_code == 200
        assert first.json()["assistant_message"]["metadata"]["memory_profile"]["eligible"] is True
        assert first.json()["assistant_message"]["metadata"]["memory_profile"]["hit_count"] == 0

        second = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "What name should you use for me?", "bot_id": "bot-memory", "user_id": "user@example.com"},
        )
        assert second.status_code == 200
        memory_blocks = [
            item for item in captured_payloads[-1]
            if item["role"] == "system" and "Personal Memory Profile:" in str(item["content"])
        ]
        assert memory_blocks
        assert "My preferred name is Jacob." in memory_blocks[0]["content"]
        assert second.json()["assistant_message"]["metadata"]["memory_profile"]["hit_count"] >= 1


@pytest.mark.anyio
async def test_project_memory_gate_blocks_profile_retrieval_when_project_disabled(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "assistant reply"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-memory-project",
                "name": "Project Memory Bot",
                "role": "assistant",
                "memory_profiles_enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        seed_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Seed Memory", "owner_user_id": "user@example.com"},
        )
        seed_id = seed_resp.json()["id"]
        await client.post(
            f"/v1/chat/conversations/{seed_id}/messages",
            json={"content": "I prefer concise answers.", "bot_id": "bot-memory-project", "user_id": "user@example.com"},
        )

        project_resp = await client.post(
            "/v1/projects",
            json={"id": "project-memory-off", "name": "Memory Off Project"},
        )
        assert project_resp.status_code == 200
        assert project_resp.json()["memory_profiles_enabled"] is False

        convo_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Project Chat",
                "scope": "project",
                "project_id": "project-memory-off",
                "owner_user_id": "user@example.com",
            },
        )
        project_conversation_id = convo_resp.json()["id"]
        gated = await client.post(
            f"/v1/chat/conversations/{project_conversation_id}/messages",
            json={"content": "Use my preferences.", "bot_id": "bot-memory-project", "user_id": "user@example.com"},
        )

        assert gated.status_code == 200
        assert gated.json()["assistant_message"]["metadata"]["memory_profile"]["eligible"] is False
        assert gated.json()["assistant_message"]["metadata"]["memory_profile"]["gates"]["project_enabled"] is False
        assert not any(
            item["role"] == "system" and "Personal Memory Profile:" in str(item["content"])
            for item in captured_payloads[-1]
        )


@pytest.mark.anyio
async def test_memory_profile_ignores_low_relevance_hits(cp_app):
    captured_payloads = []

    async def _capture_schedule(task):
        captured_payloads.append(task.payload)
        return {"output": "assistant reply"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-memory-relevance",
                "name": "Memory Relevance Bot",
                "role": "assistant",
                "memory_profiles_enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        seed_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Seed Memory", "owner_user_id": "user@example.com"},
        )
        seed_id = seed_resp.json()["id"]
        await client.post(
            f"/v1/chat/conversations/{seed_id}/messages",
            json={
                "content": "My preferred project codename is Blue Lantern.",
                "bot_id": "bot-memory-relevance",
                "user_id": "user@example.com",
            },
        )

        math_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Math Chat", "owner_user_id": "user@example.com"},
        )
        math_id = math_resp.json()["id"]
        turn = await client.post(
            f"/v1/chat/conversations/{math_id}/messages",
            json={
                "content": "Solve: a two kilogram cart accelerates at three meters per second squared for four seconds.",
                "bot_id": "bot-memory-relevance",
                "user_id": "user@example.com",
            },
        )

        assert turn.status_code == 200
        assert turn.json()["assistant_message"]["metadata"]["memory_profile"]["hit_count"] == 0
        assert not any(
            item["role"] == "system" and "Personal Memory Profile:" in str(item["content"])
            for item in captured_payloads[-1]
        )


@pytest.mark.anyio
async def test_memory_profile_item_crud_is_user_scoped(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/memory-profile/items",
            json={"user_id": "user-a@example.com", "content": "Use concise answers.", "role": "user"},
        )
        assert create_resp.status_code == 200
        item = create_resp.json()
        assert item["user_id"] == "user-a@example.com"
        assert item["profile_id"] == "default"
        assert item["content"] == "Use concise answers."
        assert item["updated_at"]

        isolated = await client.get(
            "/v1/chat/memory-profile/items",
            params={"user_id": "user-b@example.com", "query": "concise"},
        )
        assert isolated.status_code == 200
        assert isolated.json() == []

        found = await client.get(
            "/v1/chat/memory-profile/items",
            params={"user_id": "user-a@example.com", "query": "concise"},
        )
        assert found.status_code == 200
        assert found.json()[0]["id"] == item["id"]

        update_resp = await client.put(
            f"/v1/chat/memory-profile/items/{item['id']}",
            json={"user_id": "user-a@example.com", "content": "Use direct answers.", "role": "assistant"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["content"] == "Use direct answers."
        assert update_resp.json()["role"] == "assistant"

        forbidden_delete = await client.delete(
            f"/v1/chat/memory-profile/items/{item['id']}",
            params={"user_id": "user-b@example.com"},
        )
        assert forbidden_delete.status_code == 404

        delete_resp = await client.delete(
            f"/v1/chat/memory-profile/items/{item['id']}",
            params={"user_id": "user-a@example.com"},
        )
        assert delete_resp.status_code == 204


@pytest.mark.anyio
async def test_chat_message_rejects_image_attachment_for_non_vision_bot(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Image Reject"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-no-vision",
                "name": "No Vision Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "llama3.1:8b"}],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Check this screenshot",
                "bot_id": "bot-no-vision",
                "attachments": [
                    {
                        "name": "failure.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert post_resp.status_code == 400
    assert "does not support image attachments" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_chat_message_includes_attachments_in_scheduler_payload(cp_app):
    captured = {}

    async def _capture_schedule(task):
        captured["payload"] = task.payload
        return {"output": "assistant reply"}

    cp_app.state.scheduler.schedule = _capture_schedule
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Attachments"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-vision",
                "name": "Vision Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Use these attachments.",
                "bot_id": "bot-vision",
                "attachments": [
                    {
                        "name": "notes.md",
                        "mime_type": "text/markdown",
                        "kind": "text",
                        "text_content": "# Notes\n- one\n",
                        "size_bytes": 14,
                    },
                    {
                        "name": "failure.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                        "size_bytes": 5,
                    },
                    {
                        "name": "bundle.zip",
                        "mime_type": "application/zip",
                        "kind": "binary",
                        "size_bytes": 2048,
                    },
                ],
            },
        )

    assert post_resp.status_code == 200
    payload = captured["payload"]
    user_message = payload[-1]
    assert user_message["role"] == "user"
    assert isinstance(user_message["content"], list)
    assert any(part.get("type") == "text" and "Use these attachments." in str(part.get("text") or "") for part in user_message["content"])
    assert any(part.get("type") == "text" and "Attached file: notes.md" in str(part.get("text") or "") for part in user_message["content"])
    assert any(part.get("type") == "text" and "Attached file: bundle.zip" in str(part.get("text") or "") for part in user_message["content"])
    assert any(part.get("type") == "image_url" for part in user_message["content"])


@pytest.mark.anyio
async def test_chat_message_accepts_image_attachment_for_ollama_cloud_qwen35_bot(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Image Qwen"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-qwen-vision",
                "name": "Qwen Vision Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b-cloud"}],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Inspect this image",
                "bot_id": "bot-qwen-vision",
                "attachments": [
                    {
                        "name": "image.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert post_resp.status_code == 200


@pytest.mark.anyio
async def test_chat_message_uses_default_model_capabilities_for_image_attachment(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-vision",
                "name": "qwen3.5:397b-cloud",
                "provider": "ollama_cloud",
                "capabilities": ["vision"],
                "enabled": True,
            },
        )
        await client.post(
            "/v1/models",
            json={
                "id": "ollama-text-base",
                "name": "llama3.1:8b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-text-base-vision-default",
                "name": "Text Base Vision Default",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "llama3.1:8b"}],
                "enabled": True,
            },
        )
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Default Vision Model",
                "default_bot_id": "bot-text-base-vision-default",
                "default_model_id": "ollama-qwen-vision",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Inspect this image",
                "attachments": [
                    {
                        "name": "image.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert post_resp.status_code == 200


@pytest.mark.anyio
async def test_chat_message_rejects_image_when_default_model_is_text_only(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-vision",
                "name": "qwen3.5:397b-cloud",
                "provider": "ollama_cloud",
                "capabilities": ["vision"],
                "enabled": True,
            },
        )
        await client.post(
            "/v1/models",
            json={
                "id": "ollama-text-default",
                "name": "llama3.1:8b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-vision-base-text-default",
                "name": "Vision Base Text Default",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b-cloud"}],
                "enabled": True,
            },
        )
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Default Text Model",
                "default_bot_id": "bot-vision-base-text-default",
                "default_model_id": "ollama-text-default",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Inspect this image",
                "attachments": [
                    {
                        "name": "image.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert post_resp.status_code == 400
    assert "does not support image attachments" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_chat_message_rejects_more_than_15_attachments(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Too Many Attachments"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-attach-limit",
                "name": "Attach Limit Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        attachments = [
            {
                "name": f"note-{index}.md",
                "mime_type": "text/markdown",
                "kind": "text",
                "text_content": "# Note",
                "size_bytes": 6,
            }
            for index in range(16)
        ]
        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "too many", "bot_id": "bot-attach-limit", "attachments": attachments},
        )

    assert post_resp.status_code == 400
    assert "Maximum is 15 files" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_chat_message_rejects_attachment_total_size_over_1gb(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Attachment Size Limit"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-attach-size-limit",
                "name": "Attach Size Limit Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "too big",
                "bot_id": "bot-attach-size-limit",
                "attachments": [
                    {
                        "name": "archive.zip",
                        "mime_type": "application/zip",
                        "kind": "binary",
                        "size_bytes": (1024 * 1024 * 1024) + 1,
                    }
                ],
            },
        )

    assert post_resp.status_code == 400
    assert "Maximum total attachment size is 1 GB" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_create_bridged_conversation_stores_bridge_projects(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Bridge Chat",
                "scope": "bridged",
                "project_id": "proj-a",
                "bridge_project_ids": ["proj-b", "proj-c", "proj-a"],
            },
        )
        assert create_resp.status_code == 200
        data = create_resp.json()
        assert data["project_id"] == "proj-a"
        assert data["bridge_project_ids"] == ["proj-b", "proj-c"]


@pytest.mark.anyio
async def test_delete_conversation_removes_messages(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Delete Me"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-delete",
                "name": "Delete Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )
        await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-delete"},
        )

        not_archived_delete = await client.delete(f"/v1/chat/conversations/{conversation_id}")
        assert not_archived_delete.status_code == 400

        archive_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/archive")
        assert archive_resp.status_code == 200
        assert archive_resp.json()["archived_at"] is not None

        delete_resp = await client.delete(f"/v1/chat/conversations/{conversation_id}")
        assert delete_resp.status_code == 204

        missing_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert missing_resp.status_code == 404


@pytest.mark.anyio
async def test_archive_and_restore_conversation_visibility(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Archive Me"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        active_resp = await client.get("/v1/chat/conversations")
        assert active_resp.status_code == 200
        assert len(active_resp.json()) == 1

        archive_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/archive")
        assert archive_resp.status_code == 200

        active_after_archive = await client.get("/v1/chat/conversations")
        assert active_after_archive.status_code == 200
        assert active_after_archive.json() == []

        archived_resp = await client.get("/v1/chat/conversations?archived=archived")
        assert archived_resp.status_code == 200
        assert len(archived_resp.json()) == 1

        restore_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/restore")
        assert restore_resp.status_code == 200
        assert restore_resp.json()["archived_at"] is None

        active_after_restore = await client.get("/v1/chat/conversations")
        assert active_after_restore.status_code == 200
        assert len(active_after_restore.json()) == 1


@pytest.mark.anyio
async def test_list_messages_returns_more_than_legacy_120_default(cp_app, monkeypatch):
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_COUNT", "1000")
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_WINDOW_SECONDS", "60")
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Long History"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-history",
                "name": "History Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        for index in range(130):
            post_resp = await client.post(
                f"/v1/chat/conversations/{conversation_id}/messages",
                json={"content": f"message {index}", "bot_id": "bot-history"},
            )
            assert post_resp.status_code == 200

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert messages_resp.status_code == 200
        messages = messages_resp.json()
        assert len(messages) == 260
        assert messages[0]["content"] == "message 0"


@pytest.mark.anyio
async def test_stream_message_endpoint(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "stream "}
        yield {"event": "token", "text": "reply"}
        yield {"event": "final", "output": "stream reply", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    cp_app.state.scheduler.stream = _stream
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
        assert "event: status" in stream_resp.text
        assert "event: token" in stream_resp.text
        assert "event: assistant_message" in stream_resp.text
        assert "event: done" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        messages = messages_resp.json()
        assert len(messages) == 2
        assert messages[-1]["content"] == "stream reply"
        assert messages[-1]["model"] == "llama3.1:8b"
        assert messages[-1]["provider"] == "ollama"
        assert messages[-1]["metadata"]["streaming"] is False
        assert messages[-1]["metadata"]["bot"]["id"] == "bot-stream"
        assert messages[-1]["metadata"]["bot"]["updated_at"]
        assert messages[-1]["metadata"]["model"]["provider"] == "ollama"
        assert messages[-1]["metadata"]["model"]["model"] == "llama3.1:8b"
        assert messages[-1]["metadata"]["model"]["worker_id"] == "w1"
        assert messages[-1]["metadata"]["model"]["source"] == "scheduler"
        assert messages[-1]["metadata"]["usage"] == {"prompt_tokens": 1, "completion_tokens": 2}

        usage_resp = await client.get("/v1/chat/usage?hours=24&limit_conversations=5")
        assert usage_resp.status_code == 200
        usage = usage_resp.json()
        assert usage["totals"]["total_tokens"] == 3
        assert usage["totals"]["messages_with_usage"] == 1
        assert usage["by_conversation"][0]["conversation_id"] == conversation_id
        assert usage["by_bot"][0]["bot_id"] == "bot-stream"
        assert usage["by_provider_model"][0]["provider"] == "ollama"
        assert usage["by_provider_model"][0]["model"] == "llama3.1:8b"


@pytest.mark.anyio
async def test_stream_error_marks_user_turn_failed_and_excludes_it_from_later_context(cp_app):
    captured_payloads = []

    async def _failing_stream(_task):
        yield {"event": "error", "error": "Ollama Cloud request failed (500)"}

    cp_app.state.scheduler.stream = _failing_stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        conversation = await client.post("/v1/chat/conversations", json={"title": "Failed stream"})
        conversation_id = conversation.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-failed-stream", "name": "Failed Stream Bot", "role": "assistant", "backends": [], "enabled": True},
        )

        failed = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "Image request that failed", "bot_id": "bot-failed-stream"},
        )
        assert failed.status_code == 200
        assert "event: error" in failed.text

        failed_messages = (await client.get(f"/v1/chat/conversations/{conversation_id}/messages")).json()
        assert len(failed_messages) == 1
        assert failed_messages[0]["metadata"]["delivery_failed"] is True
        assert "Ollama Cloud request failed" in failed_messages[0]["metadata"]["delivery_error"]

        async def _successful_stream(task):
            captured_payloads.append(task.payload)
            yield {"event": "final", "output": "new response"}

        cp_app.state.scheduler.stream = _successful_stream
        retried = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "A new message", "bot_id": "bot-failed-stream"},
        )
        assert retried.status_code == 200

    assert all("Image request that failed" not in str(row.get("content") or "") for row in captured_payloads[-1])


@pytest.mark.anyio
async def test_stream_message_regeneration_reuses_user_turn_and_preserves_response_variants(cp_app):
    captured_payloads = []

    async def _stream(task):
        captured_payloads.append(task.payload)
        yield {"event": "token", "text": "replacement "}
        yield {"event": "final", "output": "replacement answer", "usage": {}}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        conversation = await client.post("/v1/chat/conversations", json={"title": "Regenerate"})
        conversation_id = conversation.json()["id"]
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-regenerate",
                "name": "Regenerate Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )
        original_user = await cp_app.state.chat_manager.add_message(
            conversation_id, "user", "Explain the deployment plan."
        )
        original_assistant = await cp_app.state.chat_manager.add_message(
            conversation_id,
            "assistant",
            "old answer that must not be sent back to the model",
            bot_id="bot-regenerate",
        )

        regenerated = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": original_user.content,
                "bot_id": "bot-regenerate",
                "rerun_assistant_message_id": original_assistant.id,
            },
        )
        assert regenerated.status_code == 200
        assert "event: user_message" not in regenerated.text
        assert "event: assistant_message" in regenerated.text

        payload_text = "\n".join(str(row.get("content") or "") for row in captured_payloads[-1])
        assert original_user.content in payload_text
        assert original_assistant.content not in payload_text

        visible = (await client.get(f"/v1/chat/conversations/{conversation_id}/messages")).json()
        assert [message["content"] for message in visible] == [original_user.content, "replacement answer"]
        variants = visible[-1]["metadata"]["response_variants"]
        assert len(variants) == 2

        switched = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages/{original_assistant.id}/select-response"
        )
        assert switched.status_code == 200
        visible_after_switch = (await client.get(f"/v1/chat/conversations/{conversation_id}/messages")).json()
        assert [message["content"] for message in visible_after_switch] == [
            original_user.content,
            original_assistant.content,
        ]


@pytest.mark.anyio
async def test_stream_message_uses_bot_config_provider_model_when_backend_event_missing(cp_app):
    async def _stream(_task):
        yield {"event": "token", "text": "config "}
        yield {"event": "token", "text": "fallback"}
        yield {"event": "final", "output": "config fallback", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Config Fallback"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-stream-config",
                "name": "Stream Config Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-stream-config"},
        )
        assert stream_resp.status_code == 200
        assert "event: assistant_message" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        messages = messages_resp.json()
        assistant = messages[-1]
        assert assistant["content"] == "config fallback"
        assert assistant["model"] == "gpt-oss:120b"
        assert assistant["provider"] == "ollama_cloud"
        assert assistant["metadata"]["model"]["provider"] == "ollama_cloud"
        assert assistant["metadata"]["model"]["model"] == "gpt-oss:120b"
        assert assistant["metadata"]["model"]["source"] == "bot_config"
        assert assistant["metadata"]["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}

        usage_resp = await client.get("/v1/chat/usage?hours=24&limit_conversations=5")
        usage = usage_resp.json()
        assert usage["totals"]["total_tokens"] == 7
        assert usage["by_provider_model"][0]["provider"] == "ollama_cloud"
        assert usage["by_provider_model"][0]["model"] == "gpt-oss:120b"


@pytest.mark.anyio
async def test_stream_message_blocks_oversized_context_item_ids(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Context Limit"})
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "context_item_ids": [f"item-{idx}" for idx in range(201)]},
        )

    assert stream_resp.status_code == 422
    assert "context_item_ids" in stream_resp.text


@pytest.mark.anyio
async def test_stream_message_blocks_oversized_context_item_id(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Context Id Limit"})
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "context_item_ids": ["x" * 257]},
        )

    assert stream_resp.status_code == 422
    assert "context_item_ids" in stream_resp.text


@pytest.mark.anyio
async def test_stream_message_blocks_oversized_content(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Content Limit"})
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "x" * 120001},
        )

    assert stream_resp.status_code == 422
    assert "content" in stream_resp.text


@pytest.mark.anyio
async def test_stream_message_rejects_more_than_15_attachments(cp_app):
    cp_app.state.scheduler.stream = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Too Many Attachments"})
        conversation_id = create_resp.json()["id"]

        attachments = [
            {
                "name": f"note-{index}.md",
                "mime_type": "text/markdown",
                "kind": "text",
                "text_content": "# Note",
                "size_bytes": 6,
            }
            for index in range(16)
        ]
        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "too many", "attachments": attachments},
        )

    assert stream_resp.status_code == 400
    assert "Maximum is 15 files" in str(stream_resp.json().get("detail") or "")
    cp_app.state.scheduler.stream.assert_not_called()


@pytest.mark.anyio
async def test_stream_message_rejects_attachment_total_size_over_1gb(cp_app):
    cp_app.state.scheduler.stream = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream Attachment Size Limit"})
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "too big",
                "attachments": [
                    {
                        "name": "archive.zip",
                        "mime_type": "application/zip",
                        "kind": "binary",
                        "size_bytes": (1024 * 1024 * 1024) + 1,
                    }
                ],
            },
        )

    assert stream_resp.status_code == 400
    assert "Maximum total attachment size is 1 GB" in str(stream_resp.json().get("detail") or "")
    cp_app.state.scheduler.stream.assert_not_called()


@pytest.mark.anyio
async def test_stream_message_uses_default_model_capabilities_for_image_attachment(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama_cloud", "model": "qwen3.5:397b-cloud"}
        yield {"event": "final", "output": "vision stream reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-vision-stream",
                "name": "qwen3.5:397b-cloud",
                "provider": "ollama_cloud",
                "capabilities": ["vision"],
                "enabled": True,
            },
        )
        assert model_resp.status_code == 200
        text_backend_model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-llama-text-stream-base",
                "name": "llama3.1:8b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        assert text_backend_model_resp.status_code == 200
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "bot-text-base-vision-stream-default",
                "name": "Text Base Vision Stream Default",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "llama3.1:8b"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200, bot_resp.text
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Default Vision Stream Model",
                "default_bot_id": "bot-text-base-vision-stream-default",
                "default_model_id": "ollama-qwen-vision-stream",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "Inspect this image",
                "bot_id": "bot-text-base-vision-stream-default",
                "attachments": [
                    {
                        "name": "image.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert stream_resp.status_code == 200
    assert "event: assistant_message" in stream_resp.text


@pytest.mark.anyio
async def test_stream_message_rejects_image_when_default_model_is_text_only(cp_app):
    cp_app.state.scheduler.stream = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-text-stream-default",
                "name": "llama3.1:8b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        assert model_resp.status_code == 200
        vision_backend_model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-vision-stream-base",
                "name": "qwen3.5:397b-cloud",
                "provider": "ollama_cloud",
                "capabilities": ["vision"],
                "enabled": True,
            },
        )
        assert vision_backend_model_resp.status_code == 200
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "bot-vision-base-text-stream-default",
                "name": "Vision Base Text Stream Default",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b-cloud"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200, bot_resp.text
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Default Text Stream Model",
                "default_bot_id": "bot-vision-base-text-stream-default",
                "default_model_id": "ollama-text-stream-default",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "Inspect this image",
                "attachments": [
                    {
                        "name": "image.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "data_url": "data:image/png;base64,aGVsbG8=",
                    }
                ],
            },
        )

    assert stream_resp.status_code == 400
    assert "does not support image attachments" in str(stream_resp.json().get("detail") or "")
    cp_app.state.scheduler.stream.assert_not_called()


@pytest.mark.anyio
async def test_stream_message_retrieves_user_scoped_memory_profile(cp_app):
    captured_payloads = []

    async def _stream(task):
        captured_payloads.append(task.payload)
        yield {"event": "backend_selected", "provider": "ollama_cloud", "model": "qwen3.5:397b", "worker_id": None}
        yield {"event": "final", "output": f"stream reply {len(captured_payloads)}"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-memory-stream",
                "name": "qwen3.5:397b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        assert model_resp.status_code == 200
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "bot-memory-stream",
                "name": "Memory Stream Bot",
                "role": "assistant",
                "memory_profiles_enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200, bot_resp.text
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Memory Stream Chat", "owner_user_id": "user@example.com"},
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        first = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "My preferred name is Jacob.", "bot_id": "bot-memory-stream", "user_id": "user@example.com"},
        )
        assert first.status_code == 200
        assert "event: assistant_message" in first.text

        second = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "What name should you use for me?", "bot_id": "bot-memory-stream", "user_id": "user@example.com"},
        )
        assert second.status_code == 200

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")

    assert any(
        item["role"] == "system" and "Personal Memory Profile:" in str(item["content"])
        for item in captured_payloads[-1]
    )
    assert "My preferred name is Jacob." in str(captured_payloads[-1][0]["content"])
    messages = messages_resp.json()
    assert messages[-1]["metadata"]["memory_profile"]["eligible"] is True
    assert messages[-1]["metadata"]["memory_profile"]["hit_count"] >= 1


@pytest.mark.anyio
async def test_stream_message_project_memory_gate_blocks_profile_retrieval(cp_app):
    captured_payloads = []

    async def _stream(task):
        captured_payloads.append(task.payload)
        yield {"event": "backend_selected", "provider": "ollama_cloud", "model": "qwen3.5:397b", "worker_id": None}
        yield {"event": "final", "output": "stream reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        model_resp = await client.post(
            "/v1/models",
            json={
                "id": "ollama-qwen-memory-stream-project",
                "name": "qwen3.5:397b",
                "provider": "ollama_cloud",
                "capabilities": ["chat"],
                "enabled": True,
            },
        )
        assert model_resp.status_code == 200
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "bot-memory-stream-project",
                "name": "Project Memory Stream Bot",
                "role": "assistant",
                "memory_profiles_enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200, bot_resp.text
        seed_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Seed Stream Memory", "owner_user_id": "user@example.com"},
        )
        seed_id = seed_resp.json()["id"]
        await client.post(
            f"/v1/chat/conversations/{seed_id}/stream",
            json={"content": "I prefer concise answers.", "bot_id": "bot-memory-stream-project", "user_id": "user@example.com"},
        )

        project_resp = await client.post(
            "/v1/projects",
            json={"id": "project-memory-stream-off", "name": "Memory Stream Off Project"},
        )
        assert project_resp.status_code == 200
        assert project_resp.json()["memory_profiles_enabled"] is False

        convo_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Project Stream Chat",
                "scope": "project",
                "project_id": "project-memory-stream-off",
                "owner_user_id": "user@example.com",
            },
        )
        project_conversation_id = convo_resp.json()["id"]
        gated = await client.post(
            f"/v1/chat/conversations/{project_conversation_id}/stream",
            json={"content": "Use my preferences.", "bot_id": "bot-memory-stream-project", "user_id": "user@example.com"},
        )
        assert gated.status_code == 200

        messages_resp = await client.get(f"/v1/chat/conversations/{project_conversation_id}/messages")

    assert not any(
        item["role"] == "system" and "Personal Memory Profile:" in str(item["content"])
        for item in captured_payloads[-1]
    )
    messages = messages_resp.json()
    assert messages[-1]["metadata"]["memory_profile"]["eligible"] is False
    assert messages[-1]["metadata"]["memory_profile"]["gates"]["project_enabled"] is False


@pytest.mark.anyio
async def test_stream_default_model_id_is_attached_to_scheduled_task(cp_app):
    captured_tasks = []

    async def _stream(task):
        captured_tasks.append(task)
        yield {"event": "backend_selected", "provider": "ollama_cloud", "model": "gpt-oss:120b", "worker_id": None}
        yield {"event": "final", "output": "stream reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Chat Stream Model Default",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-stream-model",
                "name": "Stream Model Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-stream-model"},
        )
        assert stream_resp.status_code == 200
        assert len(captured_tasks) == 1
        assert captured_tasks[0].metadata is not None
        assert captured_tasks[0].metadata.preferred_model_id == "ollama-cloud-gpt-oss-120b"


@pytest.mark.anyio
async def test_stream_default_model_id_is_not_attached_to_explicit_bot_override(cp_app):
    captured_tasks = []

    async def _stream(task):
        captured_tasks.append(task)
        yield {"event": "backend_selected", "provider": "openai", "model": "gpt-5", "worker_id": None}
        yield {"event": "final", "output": "stream reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Chat Stream Model Override",
                "default_bot_id": "bot-stream-default-model",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        for bot_id, provider, model in (
            ("bot-stream-default-model", "ollama_cloud", "gpt-oss:120b"),
            ("bot-stream-explicit-override", "openai", "gpt-5"),
        ):
            await client.post(
                "/v1/bots",
                json={
                    "id": bot_id,
                    "name": bot_id,
                    "role": "assistant",
                    "backends": [{"type": "cloud_api", "provider": provider, "model": model}],
                    "enabled": True,
                },
            )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-stream-explicit-override"},
        )
        assert stream_resp.status_code == 200
        assert len(captured_tasks) == 1
        assert captured_tasks[0].metadata is not None
        assert captured_tasks[0].metadata.preferred_model_id is None


@pytest.mark.anyio
async def test_stream_message_persists_partial_when_final_missing(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "partial "}
        yield {"event": "token", "text": "reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Partial"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-partial",
                "name": "Partial Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-partial"},
        )
        assert stream_resp.status_code == 200
        assert "event: assistant_message" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        messages = messages_resp.json()
        assert len(messages) == 2
        assert messages[-1]["content"] == "partial reply"
        assert messages[-1]["model"] == "llama3.1:8b"
        assert messages[-1]["provider"] == "ollama"
        assert messages[-1]["metadata"]["partial"] is True
        assert messages[-1]["metadata"]["streaming"] is False


@pytest.mark.anyio
async def test_assign_message_creates_task_graph_and_summary(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"steps": []})

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Chat"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-pm",
                "name": "PM Bot",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build authentication API and tests", "bot_id": "bot-pm"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["mode"] == "assign"
        assert len(data["assignment"]["tasks"]) == 1
        assert data["user_message"]["metadata"]["mode"] == "assign_request"
        assert data["user_message"]["metadata"]["requested_pm_bot_id"] == "bot-pm"
        assert data["user_message"]["metadata"]["assigned_pm_bot_id"] == "bot-pm"
        assert data["user_message"]["metadata"]["orchestration_id"] == data["assignment"]["orchestration_id"]
        assert data["user_message"]["metadata"]["assignment_context_message_count"] == 0
        assert "Assignment queued" in data["assistant_message"]["content"]
        assert "Assigned Bot: bot-pm" in data["assistant_message"]["content"]
        assert data["assistant_message"]["metadata"]["mode"] == "assign_pending"
        assert data["assistant_message"]["metadata"]["assigned_pm_bot_id"] == "bot-pm"
        assert data["assistant_message"]["metadata"]["assignment_context_strategy"] == "empty"
        assert data["assignment"]["allowed_bot_ids"] == ["bot-pm", "pm-research-analyst"]
        assert data["assignment"]["tasks"][0]["metadata"]["root_pm_bot_id"] == "bot-pm"

        tasks_resp = await client.get("/v1/tasks")
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        assert len(tasks) >= 1
        first_payload = tasks[0].get("payload") if isinstance(tasks[0], dict) else {}
        assert isinstance(first_payload, dict)
        assert "acceptance_criteria" in first_payload
        assert "quality_gates" in first_payload

        for _ in range(60):
            messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
            messages = messages_resp.json()
            if any(str((message.get("metadata") or {}).get("mode") or "") == "pm_run_report" for message in messages):
                break
            await asyncio.sleep(0.05)

        run_report = next(message for message in messages if str((message.get("metadata") or {}).get("mode") or "") == "pm_run_report")
        assert "PM run failed." in str(run_report.get("content") or "")
        assert "missing_downstream_stage:pm-database-engineer" in str(run_report.get("content") or "")
        assert run_report["metadata"]["run_status"] == "failed"


@pytest.mark.anyio
async def test_stream_assign_emits_task_events(cp_app):
    async def _schedule(_task):
        import asyncio

        await asyncio.sleep(0.05)
        return {"steps": []}

    cp_app.state.scheduler.schedule = AsyncMock(side_effect=_schedule)
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Stream"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-pm",
                "name": "PM Bot",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
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
async def test_assign_message_bootstraps_selected_pm_bot_workflow(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Workflow Chat"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "pm-workflow",
                "name": "PM Workflow",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "PM Research Analyst", "role": "researcher", "backends": [], "enabled": True},
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build the workflow", "bot_id": "pm-workflow"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["mode"] == "assign"
        assert len(data["assignment"]["tasks"]) == 1
        assert data["assignment"]["tasks"][0]["bot_id"] == "pm-workflow"
        assert data["assignment"]["plan"]["steps"][0]["bot_id"] == "pm-workflow"
        assert "Assigned Bot: pm-workflow" in data["assistant_message"]["content"]
        assert set(data["assignment"]["tasks"][0]["metadata"]["allowed_bot_ids"]) == {"pm-research-analyst", "pm-workflow"}


@pytest.mark.anyio
async def test_assign_message_persists_prior_user_conversation_brief_into_assignment_scope(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign With Prior Intent"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "pm-workflow",
                "name": "PM Workflow",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "PM Research Analyst", "role": "researcher", "backends": [], "enabled": True},
        )

        pre_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Help me plan the mathematics blocks from algebra through multivariable calculus. "
                    "Build as much as possible in house and avoid external APIs like Desmos."
                )
            },
        )
        assert pre_resp.status_code == 200

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "@assign Build documentation only in docs/blocks for the mathematics blocks",
                "bot_id": "pm-workflow",
            },
        )
        assert post_resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "pm-workflow")
    scope = root_task.payload.get("assignment_scope") or {}
    assert "multivariable calculus" in str(scope.get("conversation_brief") or "").lower()
    assert "user: help me plan the mathematics blocks" in str(scope.get("conversation_transcript") or "").lower()
    assert int(scope.get("conversation_message_count") or 0) >= 1
    assert scope.get("prefer_in_house") is True
    assert scope.get("avoid_external_apis") is True


@pytest.mark.anyio
async def test_assign_message_uses_semantic_transcript_excerpt_for_large_chat(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Huge Assign Chat"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "pm-workflow",
                "name": "PM Workflow",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "PM Research Analyst", "role": "researcher", "backends": [], "enabled": True},
        )

        early_message = (
            "Help me plan mathematics blocks from algebra through multivariable calculus. "
            "Build as much as possible in house and avoid external APIs like Desmos."
        )
        first_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": early_message},
        )
        assert first_resp.status_code == 200

        for idx in range(130):
            await cp_app.state.chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=f"Filler planning note {idx}: keep iterating on lesson-builder ideas and editorial details.",
            )

        assign_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "@assign Build documentation only in docs/blocks for the mathematics blocks",
                "bot_id": "pm-workflow",
            },
        )
        assert assign_resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "pm-workflow")
    scope = root_task.payload.get("assignment_scope") or {}
    assert scope.get("conversation_transcript_strategy") == "semantic_excerpt"
    assert int(scope.get("conversation_message_count") or 0) >= 131
    transcript = str(scope.get("conversation_transcript") or "").lower()
    assert "multivariable calculus" in transcript
    assert "desmos" in transcript
    assert int(scope.get("assignment_memory_hit_count") or 0) >= 1
    memory_hits = list(scope.get("assignment_memory_hits") or [])
    assert memory_hits
    assert any("desmos" in str(hit.get("snippet") or "").lower() for hit in memory_hits)

    messages = await cp_app.state.chat_manager.list_messages(conversation_id)
    assign_message = next(
        message
        for message in messages
        if str((message.metadata or {}).get("mode") or "") == "assign_request"
    )
    metadata = assign_message.metadata or {}
    assert metadata.get("assignment_context_strategy") == "semantic_excerpt"
    assert int(metadata.get("assignment_memory_hit_count") or 0) >= 1


@pytest.mark.anyio
async def test_assign_message_excludes_pm_workflow_messages_from_context_count(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Context Filtering"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "pm-workflow",
                "name": "PM Workflow",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "PM Research Analyst", "role": "researcher", "backends": [], "enabled": True},
        )

        await cp_app.state.chat_manager.add_message(
            conversation_id,
            "user",
            "We need mathematics blocks from algebra through multivariable calculus.",
        )
        await cp_app.state.chat_manager.add_message(
            conversation_id,
            "assistant",
            "Here is a roadmap for the mathematics block stack.",
        )
        await cp_app.state.chat_manager.add_message(
            conversation_id,
            "user",
            "@assign old failed assignment",
            metadata={"mode": "assign_request", "orchestration_id": "orch-old"},
        )
        await cp_app.state.chat_manager.add_message(
            conversation_id,
            "assistant",
            "PM run failed.",
            metadata={
                "mode": "pm_run_report",
                "orchestration_id": "orch-old",
                "run_status": "failed",
                "ingest_allowed": False,
            },
        )
        await cp_app.state.chat_manager.add_message(
            conversation_id,
            "assistant",
            "Assignment queued.",
            metadata={"mode": "assign_pending", "orchestration_id": "orch-old"},
        )

        assign_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "@assign Build documentation only in docs/blocks for the mathematics blocks",
                "bot_id": "pm-workflow",
            },
        )
        assert assign_resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "pm-workflow")
    scope = root_task.payload.get("assignment_scope") or {}
    assert int(scope.get("conversation_message_count") or 0) == 2
    transcript = str(scope.get("conversation_transcript") or "").lower()
    assert "multivariable calculus" in transcript
    assert "pm run failed" not in transcript
    assert "assignment queued" not in transcript

    messages = await cp_app.state.chat_manager.list_messages(conversation_id)
    assign_message = next(
        message
        for message in messages
        if str((message.metadata or {}).get("mode") or "") == "assign_request"
        and str((message.metadata or {}).get("orchestration_id") or "").strip() == str(root_task.metadata.orchestration_id or "").strip()
    )
    metadata = assign_message.metadata or {}
    assert int(metadata.get("assignment_context_message_count") or 0) == 2


def test_build_assignment_conversation_transcript_prioritizes_user_intent_when_excerpting():
    from control_plane.api.chat import _build_assignment_conversation_transcript
    from shared.models import ChatMessage

    messages = [
        ChatMessage(
            id="m-1",
            conversation_id="conv-1",
            role="user",
            content="Help me plan the mathematics blocks from algebra through multivariable calculus.",
            created_at="2026-03-29T00:00:00Z",
        ),
        ChatMessage(
            id="m-2",
            conversation_id="conv-1",
            role="assistant",
            content="I will outline the available research paths.",
            created_at="2026-03-29T00:00:01Z",
        ),
    ]
    for index in range(18):
        messages.append(
            ChatMessage(
                id=f"filler-{index}",
                conversation_id="conv-1",
                role="assistant" if index % 2 else "user",
                content=(
                    f"Filler planning note {index}: "
                    + ("editorial detail " * 50)
                ),
                created_at=f"2026-03-29T00:00:{index + 2:02d}Z",
            )
        )
    messages.extend(
        [
            ChatMessage(
                id="m-constraints",
                conversation_id="conv-1",
                role="user",
                content="Build as much as possible in house, avoid the Desmos API, and keep the docs under docs/blocks.",
                created_at="2026-03-29T00:01:00Z",
            ),
            ChatMessage(
                id="m-last",
                conversation_id="conv-1",
                role="assistant",
                content="Understood. I will preserve those constraints in the assignment context.",
                created_at="2026-03-29T00:01:01Z",
            ),
        ]
    )

    transcript = _build_assignment_conversation_transcript(
        messages,
        max_messages=8,
        max_chars=900,
        head_messages=1,
    )

    rendered = str(transcript.get("conversation_transcript") or "").lower()
    assert transcript.get("conversation_transcript_strategy") == "excerpt"
    assert "multivariable calculus" in rendered
    assert "desmos api" in rendered
    assert "docs/blocks" in rendered
    assert "omitted for size" in rendered
    assert rendered.count("filler planning note") < 6


@pytest.mark.anyio
async def test_chat_message_memory_prefers_user_intent_hits(cp_app):
    chat_manager = cp_app.state.chat_manager
    conversation = await chat_manager.create_conversation("Memory Ranking")
    await chat_manager.add_message(
        conversation.id,
        "user",
        "We need in-house mathematics blocks and must avoid the Desmos API for this lesson builder.",
    )
    await chat_manager.add_message(
        conversation.id,
        "assistant",
        "You could avoid the Desmos API and still build in-house mathematics blocks over time.",
    )

    hits = await chat_manager.search_message_memory(
        conversation.id,
        "in-house mathematics blocks avoid desmos api",
        limit=2,
        roles=["user", "assistant"],
    )

    assert len(hits) >= 2
    assert hits[0]["role"] == "user"
    assert float(hits[0]["weighted_score"]) > float(hits[1]["weighted_score"])


@pytest.mark.anyio
async def test_assign_message_requires_explicit_pm_bot_selection(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Missing PM"})
        conversation_id = create_resp.json()["id"]

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build the workflow"},
        )

    assert post_resp.status_code == 400
    assert "explicit PM bot selection" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_assign_message_rejects_non_pm_bot(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Wrong Bot"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={"id": "not-pm", "name": "Not PM", "role": "assistant", "backends": [], "enabled": True},
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build the workflow", "bot_id": "not-pm"},
        )

    assert post_resp.status_code == 404
    assert "not configured as a project manager" in str(post_resp.json().get("detail") or "")


@pytest.mark.anyio
async def test_mark_pm_run_failed_reclassifies_run_report(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"steps": []})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Reclassify"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "pm-workflow",
                "name": "PM Workflow",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
        )

        assign_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build the workflow", "bot_id": "pm-workflow"},
        )
        orchestration_id = assign_resp.json()["assignment"]["orchestration_id"]

        for _ in range(60):
            messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
            messages = messages_resp.json()
            if any(str((message.get("metadata") or {}).get("mode") or "") == "pm_run_report" for message in messages):
                break
            await asyncio.sleep(0.05)

        reclassify_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/orchestrations/{orchestration_id}/mark-failed"
        )

        assert reclassify_resp.status_code == 200
        body = reclassify_resp.json()
        assert body["metadata"]["run_status"] == "failed"
        assert body["metadata"]["ingest_allowed"] is False
        assert body["metadata"]["operator_marked_failed"] is True
        assert body["content"].startswith("PM run failed")

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert messages_resp.status_code == 200
        messages = messages_resp.json()
        pending = next(
            message
            for message in messages
            if str((message.get("metadata") or {}).get("mode") or "") == "assign_pending"
        )
        assert pending["metadata"]["run_status"] == "failed"
        assert pending["metadata"]["ingest_allowed"] is False
        assert pending["metadata"]["operator_marked_failed"] is True


@pytest.mark.anyio
async def test_assign_message_includes_repo_profile_context_for_language_selection(cp_app, tmp_path):
    workspace_root = tmp_path / "repo-profile"
    (workspace_root / "App" / "Pages").mkdir(parents=True, exist_ok=True)
    (workspace_root / "App" / "Services").mkdir(parents=True, exist_ok=True)
    (workspace_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (workspace_root / "App" / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")
    (workspace_root / "App" / "Pages" / "Index.razor").write_text("<h1>Hello</h1>\n", encoding="utf-8")
    (workspace_root / "App" / "Services" / "LessonService.cs").write_text("public class LessonService {}\n", encoding="utf-8")

    cp_app.state.scheduler.schedule = AsyncMock(return_value={"steps": []})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": "proj-repo-profile",
                "name": "Repo Profile",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    },
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(workspace_root),
                        "allow_push": False,
                        "allow_command_execution": False,
                    },
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Assign Repo Profile",
                "project_id": "proj-repo-profile",
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-pm-profile",
                "name": "PM Profile Bot",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
        )

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Make a new lesson page", "bot_id": "bot-pm-profile"},
        )
        assert resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "bot-pm-profile")
    context_items = root_task.payload.get("context_items")
    joined_context = "\n".join(context_items or [])
    assert "[repo-profile] Workspace stack summary" in joined_context
    assert "Likely primary stack: .NET" in joined_context
    assert "Pages and UI components should prefer `.razor` files" in joined_context
    assert "App/Pages/Index.razor" in joined_context


@pytest.mark.anyio
async def test_assign_message_includes_repo_profile_context_without_filesystem_tool_access(cp_app, tmp_path):
    workspace_root = tmp_path / "repo-profile-no-fs"
    (workspace_root / "App").mkdir(parents=True, exist_ok=True)
    (workspace_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (workspace_root / "App" / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")

    cp_app.state.scheduler.schedule = AsyncMock(return_value={"steps": []})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": "proj-repo-profile-no-fs",
                "name": "Repo Profile No FS",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": False,
                        "repo_search": False,
                    },
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(workspace_root),
                        "allow_push": False,
                        "allow_command_execution": False,
                    },
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Assign Repo Profile No FS",
                "project_id": "proj-repo-profile-no-fs",
                "tool_access_enabled": True,
                "tool_access_filesystem": False,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-pm-profile-no-fs",
                "name": "PM Profile Bot No FS",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": False,
                        "repo_search": False,
                    }
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
        )

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Make a new lesson page", "bot_id": "bot-pm-profile-no-fs"},
        )
        assert resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "bot-pm-profile-no-fs")
    context_items = root_task.payload.get("context_items")
    joined_context = "\n".join(context_items or [])
    assert "[repo-profile] Workspace stack summary" in joined_context
    assert "Likely primary stack: .NET" in joined_context


@pytest.mark.anyio
async def test_assign_message_includes_repo_profile_context_even_when_tool_access_disabled(cp_app, tmp_path):
    workspace_root = tmp_path / "repo-profile-disabled"
    (workspace_root / "App").mkdir(parents=True, exist_ok=True)
    (workspace_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (workspace_root / "App" / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")

    cp_app.state.scheduler.schedule = AsyncMock(return_value={"steps": []})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": "proj-repo-profile-disabled",
                "name": "Repo Profile Disabled",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": False,
                        "filesystem": False,
                        "repo_search": False,
                    },
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(workspace_root),
                        "allow_push": False,
                        "allow_command_execution": False,
                    },
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Assign Repo Profile Disabled",
                "project_id": "proj-repo-profile-disabled",
                "tool_access_enabled": False,
                "tool_access_filesystem": False,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-pm-profile-disabled",
                "name": "PM Profile Bot Disabled",
                "role": "pm",
                "backends": [],
                "enabled": True,
                "assignment_capabilities": {"is_project_manager": True},
                "workflow": {
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                            "fan_out_field": "source_result.steps",
                        }
                    ]
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": False,
                        "filesystem": False,
                        "repo_search": False,
                    }
                },
            },
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-research-analyst", "name": "Research Bot", "role": "researcher", "backends": [], "enabled": True},
        )

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Make a new lesson page", "bot_id": "bot-pm-profile-disabled"},
        )
        assert resp.status_code == 200

    tasks = await cp_app.state.task_manager.list_tasks()
    root_task = next(task for task in tasks if task.bot_id == "bot-pm-profile-disabled")
    context_items = root_task.payload.get("context_items")
    joined_context = "\n".join(context_items or [])
    assert "[repo-profile] Workspace stack summary" in joined_context
    assert "Likely primary stack: .NET" in joined_context


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


@pytest.mark.anyio
async def test_chat_message_blocks_oversized_context_items(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post("/v1/chat/conversations", json={"title": "Context Limit"})
        conversation_id = convo.json()["id"]

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "Use context", "context_items": [f"context {idx}" for idx in range(51)]},
        )

    assert resp.status_code == 422
    assert "context_items" in resp.text


@pytest.mark.anyio
async def test_chat_message_blocks_oversized_context_item_text(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post("/v1/chat/conversations", json={"title": "Context Text Limit"})
        conversation_id = convo.json()["id"]

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "Use context", "context_items": ["x" * 12001]},
        )

    assert resp.status_code == 422
    assert "context_items" in resp.text


@pytest.mark.anyio
async def test_chat_message_blocks_oversized_content(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post("/v1/chat/conversations", json={"title": "Content Limit"})
        conversation_id = convo.json()["id"]

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "x" * 120001},
        )

    assert resp.status_code == 422
    assert "content" in resp.text


@pytest.mark.anyio
async def test_chat_project_repo_context_is_attached_when_requested(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-ctx"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Context Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Project Repo Context",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-ctx",
                "name": "Repo Ctx Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_CONTEXT_TOKEN architecture note for chat retrieval.",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "How is this repo structured?",
                "bot_id": "bot-repo-ctx",
                "include_project_context": True,
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[repo:proj-repo-ctx]" in payload[0]["content"]
        assert "PROJECT_REPO_CONTEXT_TOKEN" in payload[0]["content"]
        assert any(
            m.get("role") == "system" and "Repository Evidence Policy:" in str(m.get("content", ""))
            for m in payload
        )
        assert any("Files inspected" in str(m.get("content", "")) for m in payload if m.get("role") == "system")


@pytest.mark.anyio
async def test_chat_project_repo_context_is_not_attached_by_default(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-off"
        create_project = await client.post(
            "/v1/projects",
            json={"id": project_id, "name": "Repo Off Project"},
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={"title": "Project Repo Off", "project_id": project_id},
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-repo-off", "name": "Repo Off Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_CONTEXT_DISABLED_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Hello",
                "bot_id": "bot-repo-off",
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "user"
        assert "PROJECT_REPO_CONTEXT_DISABLED_TOKEN" not in str(payload)


@pytest.mark.anyio
async def test_workspace_tools_do_not_force_repo_evidence_or_truncate_response(cp_app, tmp_path):
    long_output = "\n".join(
        f"Math block idea {idx}: detailed planning note for client-side rendering and authoring workflows."
        for idx in range(1, 60)
    )
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": long_output})
    workspace_root = tmp_path / "workspace-full-response"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "WORKSPACE_CONTEXT_TOKEN mathematics block roadmap",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-full-response"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Full Response",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Full Response Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-full-response",
                "name": "Workspace Full Response Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Summarize the workspace note in detail.",
                "bot_id": "bot-workspace-full-response",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_message"]["content"] == long_output
        assert not body["assistant_message"]["content"].startswith("Files inspected (verified context)")

        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "WORKSPACE_CONTEXT_TOKEN" in payload[0]["content"]
        assert not any(
            m.get("role") == "system" and "Repository Evidence Policy:" in str(m.get("content", ""))
            for m in payload
        )


@pytest.mark.anyio
async def test_chat_explicit_workspace_tools_attach_project_context(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-auto"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Auto Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Auto Chat",
                "project_id": project_id,
                "scope": "project",
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-auto",
                "name": "Repo Auto Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_AUTO_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search the repository and explain auth hardening gaps.",
                "bot_id": "bot-repo-auto",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[repo:proj-repo-auto]" in payload[0]["content"]
        assert "PROJECT_REPO_AUTO_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_explicit_workspace_tools_support_repo_review(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-go-through"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Go Through Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Go Through Chat",
                "project_id": project_id,
                "scope": "project",
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-go-through",
                "name": "Repo Go Through Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_GO_THROUGH_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you go through my repo and determine what's already there and what we will need to build out?",
                "bot_id": "bot-repo-go-through",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[repo:proj-repo-go-through]" in payload[0]["content"]
        assert "PROJECT_REPO_GO_THROUGH_TOKEN" in payload[0]["content"]
        assert any(
            m.get("role") == "system" and "Repository Evidence Policy:" in str(m.get("content", ""))
            for m in payload
        )


@pytest.mark.anyio
async def test_chat_repo_prose_does_not_auto_enable_workspace_context(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-noise"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Noise Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Noise Chat",
                "project_id": project_id,
                "scope": "project",
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-noise",
                "name": "Repo Noise Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_NOISE_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "I am working on a repository and want to explain what I am planning before asking for any file work."
                ),
                "bot_id": "bot-repo-noise",
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "user"
        assert "PROJECT_REPO_NOISE_TOKEN" not in str(payload)


@pytest.mark.anyio
async def test_chat_repo_context_search_uses_focused_query_terms(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    cp_app.state.vault_manager.search = AsyncMock(
        return_value=[
            {
                "chunk_id": "row-lesson-1",
                "title": "GlobeIQ.Server/Services/LessonBuilderService.cs",
                "content": "FOCUSED_LESSON_CONTEXT_TOKEN",
                "score": 0.72,
            }
        ]
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-focus-query"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Focus Query Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Focus Query Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-focus-query",
                "name": "Repo Focus Query Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Testing repo awareness and proper file searching. Can you look through everything "
                    "related to my lesson builder system and lesson blocks and tell me what is done?"
                ),
                "bot_id": "bot-repo-focus-query",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        assert cp_app.state.vault_manager.search.await_count >= 1
        search_query = str(cp_app.state.vault_manager.search.await_args_list[0].kwargs.get("query") or "")
        assert "lesson" in search_query
        assert "builder" in search_query
        assert "blocks" in search_query
        assert "awareness" not in search_query
        assert "proper" not in search_query

        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert "FOCUSED_LESSON_CONTEXT_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_explicit_workspace_tools_prefers_workspace_as_source_of_truth(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace-repo-truth"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "backend" / "auth").mkdir(parents=True, exist_ok=True)
    (workspace_root / "backend" / "auth" / "login.ts").write_text(
        "WORKSPACE_AUTH_TOKEN current login implementation",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-truth"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Truth Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": True,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Truth Chat",
                "project_id": project_id,
                "scope": "project",
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-truth",
                "name": "Repo Truth Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": True,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "docs/legacy-auth.md",
                "content": "INGESTED_AUTH_TOKEN historical note",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search the repository auth implementation and hardening opportunities",
                "bot_id": "bot-repo-truth",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        context_blob = payload[0]["content"]
        assert "[workspace:file]" in context_blob or "[workspace:search]" in context_blob
        assert "WORKSPACE_AUTH_TOKEN" in context_blob
        assert "INGESTED_AUTH_TOKEN" in context_blob
        assert context_blob.index("WORKSPACE_AUTH_TOKEN") < context_blob.index("INGESTED_AUTH_TOKEN")
        policy_blob = payload[1]["content"] if len(payload) > 1 else ""
        assert "Treat workspace snippets as source of truth" in policy_blob


@pytest.mark.anyio
async def test_chat_workspace_filesystem_context_requires_three_switches(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "WORKSPACE_FILESYSTEM_TOKEN architecture details",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-files"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Files",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Files Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-files",
                "name": "Workspace Files Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Please inspect README.md",
                "bot_id": "bot-workspace-files",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200

        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[workspace:file]" in payload[0]["content"] or "[workspace:search]" in payload[0]["content"]
        assert "WORKSPACE_FILESYSTEM_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_workspace_tools_blocked_when_chat_switch_off(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace-blocked"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "WORKSPACE_BLOCKED_TOKEN should not appear",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-blocked"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Blocked",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Blocked Chat",
                "project_id": project_id,
                "tool_access_enabled": False,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-blocked",
                "name": "Workspace Blocked Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Please inspect README.md",
                "bot_id": "bot-workspace-blocked",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_message"]["content"] == "ok"
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert "WORKSPACE_BLOCKED_TOKEN" not in str(payload)
        assert not any(
            m.get("role") == "system" and "Repository Evidence Policy:" in str(m.get("content", ""))
            for m in payload
        )


@pytest.mark.anyio
async def test_post_message_code_phrase_does_not_trigger_inline_without_flag(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "non-inline reply"})
    workspace_root = tmp_path / "workspace-inline-require-flag"
    workspace_root.mkdir(parents=True, exist_ok=True)
    cp_app.state.task_manager.create_task = AsyncMock()

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-require-flag"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Require Flag",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Require Flag Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-require-flag",
                "name": "Inline Require Flag Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you code this?",
                "bot_id": "bot-inline-require-flag",
                # inline_coding_enabled intentionally omitted
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_message"]["content"] == "non-inline reply"
        assert body["assistant_message"]["metadata"]["bot"]["id"] == "bot-inline-require-flag"
        assert body["assistant_message"]["metadata"]["model"]["source"] == "bot_config"

    assert cp_app.state.scheduler.schedule.await_count == 1
    cp_app.state.task_manager.create_task.assert_not_awaited()


@pytest.mark.anyio
async def test_post_message_inline_code_uses_task_manager_temp_workspace(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "scheduler fallback"})
    workspace_root = tmp_path / "workspace-inline-post"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-post-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-post",
        bot_id="bot-inline-post",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "Inline coding complete."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-post"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "README.md",
                    "path": "README.md",
                    "content": "updated",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["README.md"],
            [],
        )

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result, "updated_at": "2026-01-01T00:00:03Z"})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-post"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Post",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Post Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-post",
                "name": "Inline Post Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Can you look into GlobeIQ's repo and sketch month-end accounting reporting support? "
                    "Can you code this?"
                ),
                "bot_id": "bot-inline-post",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["mode"] == "pm_run_report"
        assert assistant["metadata"]["run_status"] == "passed"
        assert assistant["metadata"]["inline_code"] is True
        assert assistant["metadata"]["orchestration_id"]
        assert "Files touched in temp workspace:" in assistant["content"]
        assert "README.md" in assistant["content"]

    assert cp_app.state.scheduler.schedule.await_count == 0
    cp_app.state.task_manager.create_task.assert_awaited_once()
    create_kwargs = cp_app.state.task_manager.create_task.await_args.kwargs
    assert create_kwargs["metadata"].source == "chat_assign"
    assert create_kwargs["metadata"].orchestration_id == assistant["metadata"]["orchestration_id"]
    payload = create_kwargs["payload"]
    assert any(isinstance(item, dict) and str(item.get("_workspace_root") or "") == str(temp_root) for item in payload)
    assert any(
        isinstance(item, dict)
        and item.get("role") == "system"
        and "Coding task for this turn (execute now):" in str(item.get("content") or "")
        and "Can you look into GlobeIQ's repo and sketch month-end accounting reporting support?" in str(item.get("content") or "")
        for item in payload
    )


@pytest.mark.anyio
async def test_post_message_inline_code_bot_default_runs_without_inline_flag(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "scheduler fallback"})
    workspace_root = tmp_path / "workspace-inline-default"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-default-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-default",
        bot_id="bot-inline-default",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "Inline coding default complete."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-default"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "README.md",
                    "path": "README.md",
                    "content": "updated",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["README.md"],
            [],
        )

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result, "updated_at": "2026-01-01T00:00:03Z"})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-default"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Default",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Default Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-default",
                "name": "Inline Default Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                    "inline_coding_default": True,
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you code this?",
                "bot_id": "bot-inline-default",
                # inline_coding_enabled intentionally omitted: bot policy default should trigger inline mode
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["mode"] == "pm_run_report"
        assert assistant["metadata"]["inline_code"] is True

    cp_app.state.task_manager.create_task.assert_awaited_once()
    assert cp_app.state.scheduler.schedule.await_count == 0


@pytest.mark.anyio
async def test_stream_message_inline_code_uses_task_manager_temp_workspace(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-stream"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-stream-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-stream",
        bot_id="bot-inline-stream",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    running_task = created_task.model_copy(update={"status": "running", "updated_at": "2026-01-01T00:00:01Z"})
    completed_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "Inline stream coding complete."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)
    cp_app.state.task_manager.get_task = AsyncMock(side_effect=[running_task, completed_task])

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "src/app.ts",
                    "path": "src/app.ts",
                    "content": "console.log('ok');",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["src/app.ts"],
            [],
        )

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result, "updated_at": "2026-01-01T00:00:03Z"})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)

    async def _unexpected_stream(_task):
        raise AssertionError("scheduler.stream should not run for inline coding mode")
        yield  # pragma: no cover

    cp_app.state.scheduler.stream = _unexpected_stream

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-stream"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Stream",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Stream Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-stream",
                "name": "Inline Stream Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "please implement this in the project",
                "bot_id": "bot-inline-stream",
                "inline_coding_enabled": True,
            },
        )
        assert stream_resp.status_code == 200
        assert "Preparing temp workspace for inline coding" in stream_resp.text
        assert "event: assistant_message" in stream_resp.text
        assert "event: done" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert messages_resp.status_code == 200
        messages = messages_resp.json()
        assert len(messages) == 2
        assistant = messages[-1]
        assert assistant["metadata"]["mode"] == "pm_run_report"
        assert assistant["metadata"]["run_status"] == "passed"
        assert assistant["metadata"]["inline_code"] is True
        assert "src/app.ts" in str(assistant["metadata"]["files_touched"])

    cp_app.state.task_manager.create_task.assert_awaited_once()
    assert cp_app.state.task_manager.get_task.await_count >= 1
    create_kwargs = cp_app.state.task_manager.create_task.await_args.kwargs
    assert create_kwargs["metadata"].source == "chat_assign"
    payload = create_kwargs["payload"]
    assert any(isinstance(item, dict) and str(item.get("_workspace_root") or "") == str(temp_root) for item in payload)
    assert any(
        isinstance(item, dict)
        and item.get("role") == "system"
        and "Coding task for this turn (execute now):" in str(item.get("content") or "")
        and "please implement this in the project" in str(item.get("content") or "")
        for item in payload
    )


@pytest.mark.anyio
async def test_post_message_inline_code_marks_failed_when_no_file_changes(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-no-changes"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-no-changes-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-no-changes",
        bot_id="bot-inline-no-changes",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "I reviewed your request and need more details before coding."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-no-changes"
        return terminal_task

    async def _fake_collect(_temp_root):
        return ([], [], [])

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-no-changes"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline No Changes",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline No Changes Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-no-changes",
                "name": "Inline No Changes Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "Can you code this?", "bot_id": "bot-inline-no-changes", "inline_coding_enabled": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["mode"] == "pm_run_report"
        assert assistant["metadata"]["run_status"] == "failed"
        assert "produced no file edits" in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_forwards_user_prompt_to_scheduler(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module

    workspace_root = tmp_path / "workspace-inline-forward"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-forward-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {}

    async def _capture_schedule(task):
        captured["payload"] = task.payload
        return {"output": "Applied first implementation slice."}

    cp_app.state.scheduler.schedule = _capture_schedule

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Program.cs",
                    "path": "GlobeIQ.Server/Program.cs",
                    "content": "builder.Services.AddScoped<IMonthEndReportService, MonthEndReportService>();",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["GlobeIQ.Server/Program.cs"],
            [],
        )

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-forward"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Forward",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Forward Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-forward",
                "name": "Inline Forward Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        prompt = (
            "Can you look into GlobeIQ's repo and add month-end scheduling/reporting? "
            "Can you code this?"
        )
        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": prompt, "bot_id": "bot-inline-forward", "inline_coding_enabled": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_message"]["metadata"]["run_status"] == "passed"

    scheduled_payload = captured.get("payload")
    assert isinstance(scheduled_payload, list)
    assert any(
        isinstance(item, dict)
        and item.get("role") == "user"
        and "month-end scheduling/reporting" in str(item.get("content") or "")
        for item in scheduled_payload
    )
    assert any(
        isinstance(item, dict)
        and item.get("role") == "system"
        and "Coding task for this turn (execute now):" in str(item.get("content") or "")
        for item in scheduled_payload
    )


@pytest.mark.anyio
async def test_post_message_inline_code_warns_when_only_new_files_for_integration_request(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-new-only"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-new-only-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-new-only",
        bot_id="bot-inline-new-only",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "Implemented by adding report services and DTO files."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-new-only"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                    "path": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                    "content": "public class MonthEndReportService {}",
                    "status": "created",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["GlobeIQ.Server/Services/MonthEndReportService.cs"],
            [],
        )

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-new-only"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline New Only",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline New Only Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-new-only",
                "name": "Inline New Only Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you add a feature to the existing accounting view and code this?",
                "bot_id": "bot-inline-new-only",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "failed"
        assert "Inline coding run failed quality gates." in assistant["content"]
        assert "created new files but did not modify existing tracked files" in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_fails_when_write_tool_evidence_missing(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-write-evidence"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-write-evidence-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-write-evidence",
        bot_id="bot-inline-write-evidence",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {
                "output": "Updated scheduler code.",
                "agent_loop_diagnostics": {"observed_write_tool_call": False},
                "tool_calls_executed": [
                    {"name": "read_file", "arguments": {"path": "GlobeIQ.Server/Program.cs"}},
                ],
            },
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-write-evidence"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Program.cs",
                    "path": "GlobeIQ.Server/Program.cs",
                    "content": "builder.Services.AddScoped<IMonthEndReportService, MonthEndReportService>();",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["GlobeIQ.Server/Program.cs"],
            [],
        )

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_no_change_repair_attempt_limit", lambda: 0)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)
    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result})
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-write-evidence"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Write Evidence",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Write Evidence Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-write-evidence",
                "name": "Inline Write Evidence Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you add a feature and code this?",
                "bot_id": "bot-inline-write-evidence",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "failed"
        assert "no write-tool evidence was observed" in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_replaces_low_signal_output_with_change_summary(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-low-signal-output"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-low-signal-output-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-low-signal-output",
        bot_id="bot-inline-low-signal-output",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {
                "output": "Now let me check the Controllers directory to understand the existing structure.",
                "agent_loop_diagnostics": {"observed_write_tool_call": True},
                "tool_calls_executed": [
                    {
                        "name": "edit_file",
                        "arguments": {
                            "path": "GlobeIQ.Server/Controllers/Admin/ProgramsAdminController.cs",
                            "old_text": "old",
                            "new_text": "new",
                        },
                    },
                ],
            },
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-low-signal-output"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Controllers/Admin/ProgramsAdminController.cs",
                    "path": "GlobeIQ.Server/Controllers/Admin/ProgramsAdminController.cs",
                    "content": "public class ProgramsAdminController {}",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["GlobeIQ.Server/Controllers/Admin/ProgramsAdminController.cs"],
            [],
        )

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_no_change_repair_attempt_limit", lambda: 0)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)
    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result})
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-low-signal-output"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Low Signal Output",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Low Signal Output Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-low-signal-output",
                "name": "Inline Low Signal Output Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you add this feature and code this in the existing backend?",
                "bot_id": "bot-inline-low-signal-output",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "passed"
        assert "Inline coding task completed with concrete repository edits." in assistant["content"]
        assert "Now let me check" not in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_fails_when_required_surfaces_missing(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-surfaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-surfaces-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-surfaces",
        bot_id="bot-inline-surfaces",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    terminal_task = created_task.model_copy(
        update={
            "status": "completed",
            "result": {
                "output": "Updated server scheduler.",
                "agent_loop_diagnostics": {"observed_write_tool_call": True},
                "tool_calls_executed": [
                    {"name": "write_file", "arguments": {"path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs"}},
                ],
            },
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-surfaces"
        return terminal_task

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "content": "public class ProgramSchedulerService {}",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                }
            ],
            ["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
            [],
        )

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-surfaces"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Surface Gate",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Surface Gate Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-surfaces",
                "name": "Inline Surface Gate Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Can you add a feature and code this? "
                    "I'm expecting to see edits to the server and webapp at least."
                ),
                "bot_id": "bot-inline-surfaces",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "failed"
        assert "required code surfaces were not all edited" in assistant["content"]
        assert "Missing surfaces: webapp" in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_runs_integration_remediation_pass(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-remediate"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-remediate-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-remediate-first",
        bot_id="bot-inline-remediate",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    first_completed = created_task.model_copy(
        update={
            "status": "completed",
            "result": {"output": "Created initial reporting files."},
            "updated_at": "2026-01-01T00:00:01Z",
        }
    )
    remediation_completed = created_task.model_copy(
        update={
            "id": "inline-task-remediate-second",
            "status": "completed",
            "result": {"output": "Integrated into existing program startup and scheduler wiring."},
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-remediate-first"
        return first_completed

    collect_state = {"count": 0}

    async def _fake_collect(_temp_root):
        collect_state["count"] += 1
        if collect_state["count"] == 1:
            return (
                [
                    {
                        "kind": "file",
                        "label": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                        "path": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                        "content": "public class MonthEndReportService {}",
                        "status": "created",
                        "source": "inline_temp_workspace",
                        "truncated": False,
                    }
                ],
                ["GlobeIQ.Server/Services/MonthEndReportService.cs"],
                [],
            )
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                    "path": "GlobeIQ.Server/Services/MonthEndReportService.cs",
                    "content": "public class MonthEndReportService {}",
                    "status": "created",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                },
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Program.cs",
                    "path": "GlobeIQ.Server/Program.cs",
                    "content": "builder.Services.AddScoped<IMonthEndReportService, MonthEndReportService>();",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                },
            ],
            ["GlobeIQ.Server/Services/MonthEndReportService.cs", "GlobeIQ.Server/Program.cs"],
            [],
        )

    async def _fake_repair(**_kwargs):
        return remediation_completed

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_attempt_integration_repair", _fake_repair)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-remediate"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Remediation",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Remediation Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-remediate",
                "name": "Inline Remediation Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Can you add a feature to the existing accounting view and code this?",
                "bot_id": "bot-inline-remediate",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "passed"
        assert "GlobeIQ.Server/Program.cs" in assistant["content"]
        assert "Quality warning: this run created new files but did not modify existing tracked files." not in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_runs_surface_remediation_pass(cp_app, tmp_path, monkeypatch):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-surface-remediate"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-surface-remediate-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-surface-remediate-first",
        bot_id="bot-inline-surface-remediate",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    first_completed = created_task.model_copy(
        update={
            "status": "completed",
            "result": {
                "output": "Updated scheduler service.",
                "agent_loop_diagnostics": {"observed_write_tool_call": True},
                "tool_calls_executed": [{"name": "edit_file", "arguments": {"path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs"}}],
            },
            "updated_at": "2026-01-01T00:00:01Z",
        }
    )
    surface_remediation_completed = created_task.model_copy(
        update={
            "id": "inline-task-surface-remediate-second",
            "status": "completed",
            "result": {
                "output": "Added webapp admin wiring updates.",
                "agent_loop_diagnostics": {"observed_write_tool_call": True},
                "tool_calls_executed": [{"name": "edit_file", "arguments": {"path": "GlobeIQ.WebApp/Pages/Admin/Programs.razor"}}],
            },
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-surface-remediate-first"
        return first_completed

    collect_state = {"count": 0}

    async def _fake_collect(_temp_root):
        collect_state["count"] += 1
        if collect_state["count"] == 1:
            return (
                [
                    {
                        "kind": "file",
                        "label": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                        "path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                        "content": "public class ProgramSchedulerService {}",
                        "status": "updated",
                        "source": "inline_temp_workspace",
                        "truncated": False,
                    }
                ],
                ["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
                [],
            )
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "content": "public class ProgramSchedulerService {}",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                },
                {
                    "kind": "file",
                    "label": "GlobeIQ.WebApp/Pages/Admin/Programs.razor",
                    "path": "GlobeIQ.WebApp/Pages/Admin/Programs.razor",
                    "content": "@code { }",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                },
            ],
            ["GlobeIQ.Server/Services/ProgramSchedulerService.cs", "GlobeIQ.WebApp/Pages/Admin/Programs.razor"],
            [],
        )

    async def _fake_surface_repair(**_kwargs):
        return surface_remediation_completed

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_attempt_surface_repair", _fake_surface_repair)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-surface-remediate"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Surface Remediation",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Surface Remediation Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-surface-remediate",
                "name": "Inline Surface Remediation Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Can you add this feature and code this? "
                    "I expect edits to existing files in server and webapp."
                ),
                "bot_id": "bot-inline-surface-remediate",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "passed"
        assert "GlobeIQ.WebApp/Pages/Admin/Programs.razor" in assistant["content"]


@pytest.mark.anyio
async def test_post_message_inline_code_preserves_cumulative_write_evidence_across_surface_remediation(
    cp_app, tmp_path, monkeypatch
):
    from control_plane.api import chat as chat_module
    from shared.models import Task, TaskMetadata

    workspace_root = tmp_path / "workspace-inline-cumulative-write-evidence"
    workspace_root.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "workspace-inline-cumulative-write-evidence-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    created_task = Task(
        id="inline-task-cumulative-write-evidence-first",
        bot_id="bot-inline-cumulative-write-evidence",
        payload=[],
        metadata=TaskMetadata(source="chat_assign"),
        status="queued",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    first_completed = created_task.model_copy(
        update={
            "status": "completed",
            "result": {
                "output": "Updated backend scheduling service.",
                "agent_loop_diagnostics": {"observed_write_tool_call": True},
                "tool_calls_executed": [
                    {"name": "edit_file", "arguments": {"path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs"}}
                ],
            },
            "updated_at": "2026-01-01T00:00:01Z",
        }
    )
    surface_remediation_completed = created_task.model_copy(
        update={
            "id": "inline-task-cumulative-write-evidence-second",
            "status": "completed",
            "result": {
                "output": "Now let me inspect additional webapp files.",
                "agent_loop_diagnostics": {"observed_write_tool_call": False},
                "tool_calls_executed": [
                    {"name": "search_files", "arguments": {"query": "Programs page"}},
                ],
            },
            "updated_at": "2026-01-01T00:00:02Z",
        }
    )
    cp_app.state.task_manager.create_task = AsyncMock(return_value=created_task)

    async def _fake_prepare(**_kwargs):
        return {"temp_root": str(temp_root), "repo_root": str(workspace_root)}

    async def _fake_wait(_task_manager, *, task_id: str, max_wait_seconds: float = 1800.0):
        assert task_id == "inline-task-cumulative-write-evidence-first"
        return first_completed

    async def _fake_collect(_temp_root):
        return (
            [
                {
                    "kind": "file",
                    "label": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                    "content": "public class ProgramSchedulerService {}",
                    "status": "updated",
                    "source": "inline_temp_workspace",
                    "truncated": False,
                },
            ],
            ["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
            [],
        )

    async def _fake_surface_repair(**_kwargs):
        return surface_remediation_completed

    async def _fake_persist(_task_manager, *, task: Task, result: dict):
        return task.model_copy(update={"result": result})

    monkeypatch.setattr(chat_module, "_inline_code_prepare_temp_workspace", _fake_prepare)
    monkeypatch.setattr(chat_module, "_inline_code_wait_for_task", _fake_wait)
    monkeypatch.setattr(chat_module, "_inline_code_collect_workspace_artifacts", _fake_collect)
    monkeypatch.setattr(chat_module, "_inline_code_attempt_surface_repair", _fake_surface_repair)
    monkeypatch.setattr(chat_module, "_inline_code_persist_result_without_trigger_dispatch", _fake_persist)
    monkeypatch.setattr(chat_module, "_inline_code_require_deliverable_contract", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: False)
    monkeypatch.setattr(chat_module, "_inline_code_no_change_repair_attempt_limit", lambda: 0)
    monkeypatch.setattr(chat_module, "_inline_code_surface_repair_attempt_limit", lambda: 1)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-inline-cumulative-write-evidence"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Inline Cumulative Write Evidence",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Inline Cumulative Write Evidence Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-inline-cumulative-write-evidence",
                "name": "Inline Cumulative Write Evidence Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "execution_policy": {
                    "workspace_context_injection": True,
                    "repo_output_mode": "allow",
                },
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Can you add this feature and code this? "
                    "I expect edits to existing files in server and webapp."
                ),
                "bot_id": "bot-inline-cumulative-write-evidence",
                "inline_coding_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assistant = body["assistant_message"]
        assert assistant["metadata"]["run_status"] == "failed"
        assert "required code surfaces were not all edited" in assistant["content"]
        assert "no write-tool evidence was observed" not in assistant["content"]


def test_inline_code_compact_payload_preserves_context_and_limits_size():
    from control_plane.api import chat as chat_module

    payload = [{"role": "system", "content": "Context:\nrepo profile"}]
    for idx in range(30):
        role = "user" if idx % 2 == 0 else "assistant"
        payload.append({"role": role, "content": f"message-{idx}-" + ("x" * 2000)})

    compacted = chat_module._inline_code_compact_payload(payload, max_messages=12, max_chars=12000)
    assert isinstance(compacted, list)
    assert len(compacted) <= 12
    assert compacted[0]["role"] == "system"
    assert str(compacted[0]["content"]).startswith("Context:")
    total_chars = sum(len(str(item.get("content") or "")) for item in compacted if isinstance(item, dict))
    assert total_chars <= 12000 + len("Context:\nrepo profile")
    assert any(str(item.get("content") or "").startswith("message-29-") for item in compacted if isinstance(item, dict))


def test_inline_code_compact_payload_limits_chars_even_with_few_messages():
    from control_plane.api import chat as chat_module

    payload = [
        {"role": "system", "content": "Context:\n" + ("repo-line\n" * 5000)},
        {"role": "user", "content": "please code this in the connected workspace"},
    ]

    compacted = chat_module._inline_code_compact_payload(payload, max_messages=12, max_chars=9000)
    assert isinstance(compacted, list)
    assert len(compacted) <= 12
    total_chars = sum(len(str(item.get("content") or "")) for item in compacted if isinstance(item, dict))
    assert total_chars <= 9000
    assert "truncated for prompt budget" in str(compacted[0].get("content") or "")


def test_inline_code_compact_payload_dedupes_retry_noise_messages():
    from control_plane.api import chat as chat_module

    repeated_user = "Please implement month-end scheduler and edit existing files."
    payload = [
        {"role": "system", "content": "Context:\nrepo profile"},
        {"role": "user", "content": repeated_user},
        {"role": "assistant", "content": "Inline coding mode could not start: fatal: detected dubious ownership"},
        {"role": "user", "content": repeated_user},
        {"role": "assistant", "content": "PM pipeline failed\nInline coding run completed but produced no file edits."},
        {"role": "assistant", "content": "I looked at Program.cs and ProgramSchedulerService.cs"},
    ]

    compacted = chat_module._inline_code_compact_payload(payload, max_messages=12, max_chars=12000)
    contents = [str(item.get("content") or "") for item in compacted if isinstance(item, dict)]
    user_contents = [str(item.get("content") or "") for item in compacted if isinstance(item, dict) and str(item.get("role") or "") == "user"]
    assistant_contents = [
        str(item.get("content") or "")
        for item in compacted
        if isinstance(item, dict) and str(item.get("role") or "") == "assistant"
    ]

    assert len(user_contents) == 1
    assert user_contents[0] == repeated_user
    assert not any("Inline coding mode could not start" in text for text in contents)
    assert not any("produced no file edits" in text for text in contents)
    assert len(assistant_contents) <= 1


def test_inline_code_no_change_repair_prompt_adds_surface_requirement_when_requested():
    from control_plane.api import chat as chat_module

    prompt = chat_module._inline_code_no_change_repair_prompt(
        "Please update the server and webapp admin UI for this feature."
    )
    assert "server/backend" in prompt
    assert "webapp/frontend" in prompt


def test_inline_code_surface_repair_prompt_mentions_missing_surfaces():
    from control_plane.api import chat as chat_module

    prompt = chat_module._inline_code_surface_repair_prompt(
        ["webapp"],
        ["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
    )
    assert "Missing surfaces: webapp" in prompt
    assert "edit existing UI files" in prompt


def test_inline_code_surface_repair_candidate_map_finds_existing_surface_files(tmp_path):
    from control_plane.api import chat as chat_module

    repo_root = tmp_path / "repo"
    (repo_root / "GlobeIQ.WebApp/Pages/Admin").mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.Server/Services").mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.WebApp/Pages/Admin/Programs.razor").write_text("@code { }", encoding="utf-8")
    (repo_root / "GlobeIQ.Server/Services/ProgramSchedulerService.cs").write_text(
        "public class ProgramSchedulerService {}",
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text("ignore", encoding="utf-8")

    candidates = chat_module._inline_code_surface_repair_candidate_map(
        workspace_root=str(repo_root),
        missing_surfaces=["server", "webapp"],
        touched_paths=["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
    )

    assert "webapp" in candidates
    assert "GlobeIQ.WebApp/Pages/Admin/Programs.razor" in candidates["webapp"]
    assert "GlobeIQ.Server/Services/ProgramSchedulerService.cs" not in candidates.get("server", [])


def test_inline_code_surface_repair_prompt_lists_candidate_files():
    from control_plane.api import chat as chat_module

    prompt = chat_module._inline_code_surface_repair_prompt(
        ["webapp"],
        ["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
        candidate_map={"webapp": ["GlobeIQ.WebApp/Pages/Admin/Programs.razor"]},
    )

    assert "Candidate existing webapp files to edit now" in prompt
    assert "GlobeIQ.WebApp/Pages/Admin/Programs.razor" in prompt


def test_inline_code_has_write_tool_evidence_ignores_noop_edit_call():
    from control_plane.api import chat as chat_module

    result = {
        "tool_calls_executed": [
            {
                "name": "edit_file",
                "arguments": {
                    "path": "GlobeIQ.Server/Program.cs",
                    "old_text": "same",
                    "new_text": "same",
                },
            }
        ]
    }
    assert chat_module._inline_code_has_write_tool_evidence(result) is False


def test_inline_code_existing_code_surface_coverage_requires_code_edits():
    from control_plane.api import chat as chat_module

    breakdown = {
        "updated_paths": [
            "GlobeIQ.Server/GlobeIQ.Server.csproj",
            "GlobeIQ.WebApp/Pages/Admin/Programs.razor",
        ],
        "deleted_paths": [],
    }
    coverage = chat_module._inline_code_existing_code_surface_coverage(
        breakdown,
        ["server", "webapp"],
    )
    assert coverage["passed"] is False
    assert "server" in coverage["missing_surfaces"]
    assert "webapp" not in coverage["missing_surfaces"]


def test_inline_code_output_quality_marks_action_plan_lines_low_signal():
    from control_plane.api import chat as chat_module

    assessment = chat_module._inline_code_output_quality_assessment(
        "Now let me create a new reporting program. First, let me check if there is a program seeder."
    )
    assert assessment["low_signal"] is True
    assert assessment["usable"] is False


def test_inline_code_deliverable_contract_detects_missing_reporting_and_pdf():
    from control_plane.api import chat as chat_module

    coverage = chat_module._inline_code_deliverable_contract_coverage(
        requested_task=(
            "Add end of month scheduling and accounting reporting with PDF exports in the admin programs page."
        ),
        files_touched=[
            "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
            "GlobeIQ.WebApp/Pages/Admin/Programs.razor",
        ],
        artifacts=[
            {
                "path": "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
                "content": "if (schedule.Frequency.StartsWith(\"End of \")) { return ShouldRunEndOfPeriodSchedule(schedule, now); }",
            }
        ],
    )
    assert "scheduling" in coverage["required_deliverables"]
    assert "reporting" in coverage["required_deliverables"]
    assert "pdf_export" in coverage["required_deliverables"]
    assert "scheduling" in coverage["touched_deliverables"]
    assert "reporting" in coverage["missing_deliverables"]
    assert "pdf_export" in coverage["missing_deliverables"]
    assert coverage["passed"] is False


def test_inline_code_test_coverage_requires_test_edits_for_feature_work(monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setattr(chat_module, "_inline_code_require_feature_test_edits", lambda: True)
    coverage = chat_module._inline_code_test_coverage(
        requested_task="Can you add this feature and code this in the existing admin app?",
        integration_required=True,
        files_touched=["GlobeIQ.Server/Services/ProgramSchedulerService.cs"],
        deleted_paths=[],
    )
    assert coverage["tests_required"] is True
    assert coverage["passed"] is False

    passing = chat_module._inline_code_test_coverage(
        requested_task="Can you add this feature and code this in the existing admin app?",
        integration_required=True,
        files_touched=[
            "GlobeIQ.Server/Services/ProgramSchedulerService.cs",
            "GlobeIQ.Server.Tests/ProgramSchedulerServiceTests.cs",
        ],
        deleted_paths=[],
    )
    assert passing["tests_required"] is True
    assert passing["passed"] is True
    assert "GlobeIQ.Server.Tests/ProgramSchedulerServiceTests.cs" in passing["test_paths"]


def test_inline_code_missing_tests_gate_is_advisory_by_default(monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.delenv("NEXUSAI_INLINE_CODE_FAIL_ON_MISSING_TESTS", raising=False)
    assert chat_module._inline_code_fail_on_missing_tests() is False


def test_inline_code_missing_tests_gate_can_be_enabled_via_env(monkeypatch):
    from control_plane.api import chat as chat_module

    monkeypatch.setenv("NEXUSAI_INLINE_CODE_FAIL_ON_MISSING_TESTS", "1")
    assert chat_module._inline_code_fail_on_missing_tests() is True


def test_inline_code_workspace_marker_adds_surface_execution_hint_when_requested():
    from control_plane.api import chat as chat_module

    payload = [{"role": "user", "content": "do the thing"}]
    marked = chat_module._inject_inline_workspace_marker(
        payload,
        workspace_root="/tmp/workspace",
        requested_task="Please make backend server and webapp ui edits.",
        workspace_tree_preview="",
    )
    system_messages = [
        str(item.get("content") or "")
        for item in marked
        if isinstance(item, dict) and str(item.get("role") or "") == "system"
    ]
    assert any("explicitly asks for both server/backend and webapp/frontend updates" in text for text in system_messages)


@pytest.mark.anyio
async def test_stream_message_emits_context_summary_event_when_repo_context_loaded(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "ok"}
        yield {"event": "final", "output": "ok", "usage": {}}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-context-stream"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Context Stream",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Context Stream",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-context-stream",
                "name": "Context Stream Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "STREAM_CONTEXT_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "Review auth hardening",
                "bot_id": "bot-context-stream",
                "include_project_context": True,
            },
        )
        assert stream_resp.status_code == 200
        assert "event: context_summary" in stream_resp.text
        assert "Files inspected (verified context)" in stream_resp.text
        assert "STREAM_CONTEXT_TOKEN" not in stream_resp.text
        assert "event: token" not in stream_resp.text


@pytest.mark.anyio
async def test_stream_workspace_tools_keep_token_streaming_without_repo_evidence(cp_app, tmp_path):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "Part one. "}
        yield {"event": "token", "text": "Part two. "}
        yield {"event": "final", "output": "Part one. Part two.", "usage": {}}

    cp_app.state.scheduler.stream = _stream
    workspace_root = tmp_path / "workspace-stream-full"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "STREAM_WORKSPACE_CONTEXT_TOKEN",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-stream-full"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Stream Full",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Stream Full Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-stream-full",
                "name": "Workspace Stream Full Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "Summarize the workspace note in detail.",
                "bot_id": "bot-workspace-stream-full",
                "use_workspace_tools": True,
            },
        )
        assert stream_resp.status_code == 200
        assert "event: token" in stream_resp.text
        assert "Part one." in stream_resp.text
        assert "Files inspected (verified context)" not in stream_resp.text


@pytest.mark.anyio
async def test_repo_grounded_output_sanitizes_unverifiable_action_lines(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "Let me search for authentication files.\n"
                "I'll read through the actual files in your repository to give you a proper review.\n"
                "Now let me read what I found:\n"
                "Now I have the actual file contents.\n"
                "GlobeIQ.Server/Models/Lesson.cs\n"
                "GlobeIQ.Server/Controllers/LessonsController.cs\n"
                "\"BlockType\" \"LessonBlock\" \"BlockSettings\"\n"
                "Please confirm which files you'd like me to read first.\n"
                "Should I start with the controller files?\n"
                "**/auth*.ts\n"
                "Based on verified context, auth is configured."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-sanitize"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Sanitize Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Sanitize Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-sanitize",
                "name": "Repo Sanitize Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_SANITIZE_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-sanitize",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Source-of-truth (workspace repo)" in content
        assert "Supporting context (ingested repo/docs/history)" in content
        assert "Let me search" not in content
        assert "I'll read through the actual files" not in content
        assert "Now let me read what I found" not in content
        assert "Now I have the actual file contents" not in content
        assert "GlobeIQ.Server/Models/Lesson.cs" not in content
        assert '"BlockType" "LessonBlock" "BlockSettings"' not in content
        assert "Please confirm which files you'd like me to read first" not in content
        assert "Should I start with the controller files" not in content
        assert "**/auth*.ts" not in content


@pytest.mark.anyio
async def test_repo_grounded_output_adds_grounding_note_when_citations_missing(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "Authentication is configured with modern defaults."})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-required"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation",
                "name": "Repo Citation Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Authentication is configured with modern defaults." in content
        assert "I can only provide a limited grounded response for this turn" not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." not in content
        assert "[S1]" in content


@pytest.mark.anyio
async def test_repo_grounded_output_keeps_cited_claims(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "Authentication middleware exists in current setup [S1]."})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-kept"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Kept Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Kept Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation-kept",
                "name": "Repo Citation Kept Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_KEPT_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation-kept",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Authentication middleware exists in current setup [S1]." in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." not in content


@pytest.mark.anyio
async def test_repo_grounded_output_rejects_weak_front_loaded_citations(cp_app):
    long_uncited_body = " ".join(["Detailed claim without citation."] * 220)
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={"output": f"Short cited opener [S1].\n\n{long_uncited_body}"}
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-weak-density"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Weak Density",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Weak Density Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation-weak-density",
                "name": "Repo Citation Weak Density Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_WEAK_DENSITY_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation-weak-density",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "I can only provide a limited grounded response for this turn" not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." not in content
        assert len(content) < 3200


@pytest.mark.anyio
async def test_repo_grounded_output_ignores_model_generated_files_inspected_block(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "Files inspected (verified context)\n"
                "Source-of-truth (workspace repo)\n"
                "- [S1] workspace:search fake/path1.cs\n"
                "- [S2] workspace:search fake/path2.cs\n"
                "Code Review: very long uncited analysis text."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-model-files-inspected"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Model Header Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Model Header Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-model-header",
                "name": "Repo Model Header Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_MODEL_HEADER_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-model-header",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "workspace:search fake/path1.cs" not in content
        assert "workspace:search fake/path2.cs" not in content
        assert "Code Review: very long uncited analysis text." not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." not in content


@pytest.mark.anyio
async def test_repo_grounded_output_replaces_permission_prompt_with_direct_fallback(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "Please confirm which files you'd like me to read first.\n"
                "Should I start with the controller files and then move to models?"
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-permission-fallback"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Permission Fallback",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Permission Fallback Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-permission-fallback",
                "name": "Repo Permission Fallback Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_PERMISSION_FALLBACK_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-permission-fallback",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Please confirm which files you'd like me to read first" not in content
        assert "Should I start with the controller files" not in content
        assert "Actionable next steps from verified context:" in content


@pytest.mark.anyio
async def test_repo_grounded_output_strips_workspace_access_denial_language(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "I appreciate you setting that up, but I need to be transparent: I do not currently see any workspace tools or file system access active in this specific chat session.\n"
                "This sometimes happens depending on the configuration.\n"
                "Here are the concrete gaps I can already identify from the verified context.\n"
                "1. Add a submission workflow status enum.\n"
                "2. Reuse the existing user submissions controller for assignment intake.\n"
                "3. Add premium gating before presigned upload issuance."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-access-denial-strip"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Access Denial Strip",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Access Denial Strip Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-access-denial-strip",
                "name": "Repo Access Denial Strip Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_ACCESS_DENIAL_STRIP_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Go through my repo and tell me what is already there.",
                "bot_id": "bot-repo-access-denial-strip",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "do not currently see any workspace tools" not in content.lower()
        assert "depending on the configuration" not in content.lower()
        assert "submission workflow status enum" in content


@pytest.mark.anyio
async def test_repo_grounded_output_does_not_hard_truncate_normal_uncited_response(cp_app):
    long_lines = [
        f"{idx}. Detailed grounded repo finding about assignment grading and submission handling."
        for idx in range(1, 46)
    ]
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "\n".join(long_lines)})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-no-hard-truncate"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo No Hard Truncate",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo No Hard Truncate Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-no-hard-truncate",
                "name": "Repo No Hard Truncate Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_NO_HARD_TRUNCATE_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search the repository and summarize the current grading pipeline.",
                "bot_id": "bot-repo-no-hard-truncate",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "45. Detailed grounded repo finding" in content
        assert not content.rstrip().endswith("...")


@pytest.mark.anyio
async def test_repo_grounded_output_strips_model_grounding_note_only_output(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={"output": "Grounding note: inline [S#] citations were not generated; response kept concise."}
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-grounding-note-only"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Grounding Note Only",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Grounding Note Only Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-grounding-note-only",
                "name": "Repo Grounding Note Only Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_GROUNDING_NOTE_ONLY_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-grounding-note-only",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Actionable next steps from verified context:" in content


@pytest.mark.anyio
async def test_repo_grounded_output_strips_planning_preamble_only_output(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "I'll help you conduct a thorough code review of the lesson blocks and lesson builder system.\n"
                "Let me start by reading through the key files to understand the current architecture."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-planning-preamble-only"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Planning Preamble Only",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Planning Preamble Only Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-planning-preamble-only",
                "name": "Repo Planning Preamble Only Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_PLANNING_PREAMBLE_ONLY_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-planning-preamble-only",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "I'll help you conduct a thorough code review" not in content
        assert "Let me start by reading through the key files" not in content
        assert "Actionable next steps from verified context:" in content


@pytest.mark.anyio
async def test_repo_grounded_output_strips_tool_echo_only_output(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "read_file\n"
                "read_file\n"
                "search_file\n"
                "pattern: **/Blocks/**/*.cs\n"
                "pattern: /Blocks//*.cspattern: /LessonBuilder//*.razorpattern: /Models//Lesson*.cs\n"
                "```text\n"
                "read_file\n"
                "```"
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-tool-echo-only"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Tool Echo Only",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Tool Echo Only Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-tool-echo-only",
                "name": "Repo Tool Echo Only Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_TOOL_ECHO_ONLY_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-tool-echo-only",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "read_file" not in content
        assert "search_file" not in content
        assert "pattern: **/Blocks/**/*.cs" not in content
        assert "pattern: /Blocks//*.cspattern: /LessonBuilder//*.razorpattern: /Models//Lesson*.cs" not in content
        assert "Actionable next steps from verified context:" in content


@pytest.mark.anyio
async def test_update_conversation_tool_access_endpoint(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Tool Access Conversation", "scope": "project", "project_id": "globeiq"},
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/tool-access",
            json={"enabled": True, "filesystem": True, "repo_search": True},
        )
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["tool_access_enabled"] is True
        assert body["tool_access_filesystem"] is True
        assert body["tool_access_repo_search"] is True


@pytest.mark.anyio
async def test_create_conversation_clears_tool_modes_when_access_disabled(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Disabled Tool Modes",
                "tool_access_enabled": False,
                "tool_access_filesystem": True,
                "tool_access_repo_search": True,
            },
        )

        assert create_resp.status_code == 200
        body = create_resp.json()
        assert body["tool_access_enabled"] is False
        assert body["tool_access_filesystem"] is False
        assert body["tool_access_repo_search"] is False


@pytest.mark.anyio
async def test_create_conversation_blocks_workspace_tools_without_enabled_mode(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Mode-less Tool Conversation",
                "scope": "project",
                "project_id": "globeiq",
                "tool_access_enabled": True,
                "tool_access_filesystem": False,
                "tool_access_repo_search": False,
            },
        )

        assert create_resp.status_code == 400
        assert "workspace tools require at least one enabled tool mode" in create_resp.text


@pytest.mark.anyio
async def test_create_conversation_blocks_workspace_tools_without_project_scope(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Unsafe Tool Conversation",
                "scope": "global",
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )

        assert create_resp.status_code == 400
        assert "workspace tools require a project-scoped or bridged conversation" in create_resp.text


@pytest.mark.anyio
async def test_create_conversation_blocks_invalid_scope(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Bad Scope", "scope": "site-admin"},
        )

        assert create_resp.status_code == 400
        assert "scope must be one of: global, project, bridged" in create_resp.text


@pytest.mark.anyio
async def test_update_conversation_tool_access_blocks_unscoped_conversation(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Unscoped Tool Guard"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/tool-access",
            json={"enabled": True, "filesystem": True, "repo_search": True},
        )

        assert update_resp.status_code == 400
        assert "workspace tools require a project-scoped or bridged conversation" in update_resp.text


@pytest.mark.anyio
async def test_update_conversation_tool_access_clears_modes_when_disabled(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Disabled Update Modes", "scope": "project", "project_id": "globeiq"},
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/tool-access",
            json={"enabled": False, "filesystem": True, "repo_search": True},
        )

        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["tool_access_enabled"] is False
        assert body["tool_access_filesystem"] is False
        assert body["tool_access_repo_search"] is False


@pytest.mark.anyio
async def test_update_conversation_tool_access_blocks_enabled_without_mode(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Mode-less Update Guard", "scope": "project", "project_id": "globeiq"},
        )
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/tool-access",
            json={"enabled": True, "filesystem": False, "repo_search": False},
        )

        assert update_resp.status_code == 400
        assert "workspace tools require at least one enabled tool mode" in update_resp.text


@pytest.mark.anyio
async def test_update_conversation_route_defaults_endpoint(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Route Defaults"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/route-defaults",
            json={
                "default_bot_id": "personal-research-chat",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["default_bot_id"] == "personal-research-chat"
        assert body["default_model_id"] == "ollama-cloud-gpt-oss-120b"


@pytest.mark.anyio
async def test_create_conversation_blocks_disabled_catalog_default_model(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        model_resp = await client.post(
            "/v1/models",
            json={
                "id": "disabled-chat-model",
                "name": "disabled-model",
                "provider": "ollama_cloud",
                "enabled": False,
            },
        )
        assert model_resp.status_code == 200

        create_resp = await client.post(
            "/v1/chat/conversations",
            json={"title": "Disabled Model", "default_model_id": "disabled-chat-model"},
        )

        assert create_resp.status_code == 400
        assert "default_model_id is disabled" in create_resp.text


@pytest.mark.anyio
async def test_update_conversation_route_defaults_blocks_unknown_catalog_default_model(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/models",
            json={
                "id": "known-chat-model",
                "name": "known-model",
                "provider": "ollama_cloud",
                "enabled": True,
            },
        )
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Route Default Guard"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/route-defaults",
            json={"default_model_id": "missing-chat-model"},
        )

        assert update_resp.status_code == 400
        assert "default_model_id is not in the enabled model catalog" in update_resp.text


@pytest.mark.anyio
async def test_create_conversation_blocks_default_model_provider_mismatch(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/models",
            json={
                "id": "openai-chat-model",
                "name": "gpt-5",
                "provider": "openai",
                "enabled": True,
            },
        )
        await client.post(
            "/v1/models",
            json={
                "id": "ollama-chat-model",
                "name": "gpt-oss:120b",
                "provider": "ollama_cloud",
                "enabled": True,
            },
        )
        bot_resp = await client.post(
            "/v1/bots",
            json={
                "id": "ollama-chat-bot",
                "name": "Ollama Chat Bot",
                "role": "assistant",
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                "enabled": True,
            },
        )
        assert bot_resp.status_code == 200

        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Provider Mismatch",
                "default_bot_id": "ollama-chat-bot",
                "default_model_id": "openai-chat-model",
            },
        )

        assert create_resp.status_code == 400
        assert "is not available on default_bot_id" in create_resp.text


@pytest.mark.anyio
async def test_chat_task_metadata_includes_project_id(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post(
            "/v1/chat/conversations",
            json={"title": "Project Scoped", "project_id": "proj-meta-1"},
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-meta", "name": "Meta Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-meta"},
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        assert task_arg.metadata is not None
        assert task_arg.metadata.project_id == "proj-meta-1"
