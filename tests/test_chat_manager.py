"""Unit tests for ChatManager."""

import pytest

from shared.exceptions import ConversationNotFoundError


@pytest.mark.anyio
async def test_create_conversation_and_add_message(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat.db"))
    convo = await mgr.create_conversation(title="Build API")
    msg = await mgr.add_message(convo.id, role="user", content="hello")
    messages = await mgr.list_messages(convo.id)

    assert convo.title == "Build API"
    assert msg.content == "hello"
    assert len(messages) == 1


@pytest.mark.anyio
async def test_list_messages_missing_conversation(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat.db"))
    with pytest.raises(ConversationNotFoundError):
        await mgr.list_messages("missing-conversation")


@pytest.mark.anyio
async def test_update_conversation_tool_access(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat.db"))
    convo = await mgr.create_conversation(title="Tool Access")
    updated = await mgr.update_conversation_tool_access(
        convo.id,
        tool_access_enabled=True,
        tool_access_filesystem=True,
        tool_access_repo_search=False,
    )
    assert updated.tool_access_enabled is True
    assert updated.tool_access_filesystem is True
    assert updated.tool_access_repo_search is False


@pytest.mark.anyio
async def test_update_conversation_route_defaults(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat.db"))
    convo = await mgr.create_conversation(title="Route Defaults")
    updated = await mgr.update_conversation_route_defaults(
        convo.id,
        default_bot_id="personal-research-chat",
        default_model_id="ollama-cloud-gpt-oss-120b",
    )

    assert updated.default_bot_id == "personal-research-chat"
    assert updated.default_model_id == "ollama-cloud-gpt-oss-120b"

    cleared = await mgr.update_conversation_route_defaults(convo.id, default_bot_id=" ", default_model_id="")
    assert cleared.default_bot_id is None
    assert cleared.default_model_id is None


@pytest.mark.anyio
async def test_summarize_message_usage_groups_by_conversation_bot_and_model(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat-usage.db"))
    convo = await mgr.create_conversation(title="Usage Chat", project_id="nexusai", scope="project")
    await mgr.add_message(convo.id, role="user", content="hello")
    await mgr.add_message(
        convo.id,
        role="assistant",
        content="reply",
        bot_id="general-chat",
        provider="ollama_cloud",
        model="qwen3.5:397b",
        metadata={"usage": {"prompt_tokens": 20, "completion_tokens": 10}},
    )
    await mgr.add_message(
        convo.id,
        role="assistant",
        content="unmetered reply",
        bot_id="general-chat",
        provider="ollama_cloud",
        model="qwen3.5:397b",
        metadata={},
    )

    usage = await mgr.summarize_message_usage(hours=24)

    assert usage["totals"]["messages"] == 2
    assert usage["totals"]["messages_with_usage"] == 1
    assert usage["totals"]["messages_without_usage"] == 1
    assert usage["totals"]["total_tokens"] == 30
    assert usage["by_conversation"][0]["conversation_id"] == convo.id
    assert usage["by_conversation"][0]["conversation_title"] == "Usage Chat"
    assert usage["by_conversation"][0]["project_id"] == "nexusai"
    assert usage["by_conversation"][0]["total_tokens"] == 30
    assert usage["by_conversation"][0]["last_message_at"]
    assert usage["by_project"][0]["project_id"] == "nexusai"
    assert usage["by_project"][0]["conversation_count"] == 1
    assert usage["by_project"][0]["messages_with_usage"] == 1
    assert usage["by_project"][0]["messages_without_usage"] == 1
    assert usage["by_project"][0]["total_tokens"] == 30
    assert usage["by_project"][0]["last_message_at"]
    assert usage["by_bot"][0]["bot_id"] == "general-chat"
    assert usage["by_bot"][0]["last_message_at"]
    assert usage["by_provider_model"][0]["provider"] == "ollama_cloud"
    assert usage["by_provider_model"][0]["model"] == "qwen3.5:397b"
    assert usage["by_provider_model"][0]["last_message_at"]


@pytest.mark.anyio
async def test_list_conversations_project_filter_includes_bridged_membership(tmp_path):
    from control_plane.chat.chat_manager import ChatManager

    mgr = ChatManager(db_path=str(tmp_path / "chat.db"))
    primary = await mgr.create_conversation(title="Primary", project_id="acme", scope="project")
    bridged = await mgr.create_conversation(
        title="Bridge",
        project_id="nexusai",
        bridge_project_ids=["acme"],
        scope="bridged",
    )
    await mgr.create_conversation(title="Other", project_id="other", scope="project")

    rows = await mgr.list_conversations(project_id="acme", archived="all")
    ids = {row.id for row in rows}

    assert primary.id in ids
    assert bridged.id in ids
    assert len(rows) == 2
