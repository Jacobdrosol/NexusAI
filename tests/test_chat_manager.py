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
