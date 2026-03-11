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
