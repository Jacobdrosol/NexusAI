"""Tests for named memory profile CRUD."""

import pytest

from control_plane.chat.chat_manager import ChatManager


@pytest.fixture
def manager(tmp_path):
    return ChatManager(db_path=str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_create_and_get_profile(manager):
    profile = await manager.create_memory_profile(
        user_id="user@example.com",
        profile_id="research-notes",
        name="Research Notes",
        description="Notes for research discussions",
    )
    assert profile["id"] == "research-notes"
    assert profile["name"] == "Research Notes"
    assert profile["description"] == "Notes for research discussions"
    assert profile["enabled"] is True

    fetched = await manager.get_memory_profile(user_id="user@example.com", profile_id="research-notes")
    assert fetched is not None
    assert fetched["name"] == "Research Notes"


@pytest.mark.asyncio
async def test_list_profiles(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Profile One")
    await manager.create_memory_profile(user_id="u1", profile_id="p2", name="Profile Two")
    await manager.create_memory_profile(user_id="u2", profile_id="p3", name="Other User")

    profiles = await manager.list_memory_profiles(user_id="u1")
    assert len(profiles) == 2
    assert {p["id"] for p in profiles} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_update_profile(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Old")
    updated = await manager.update_memory_profile(
        user_id="u1", profile_id="p1", name="New", enabled=False
    )
    assert updated["name"] == "New"
    assert updated["enabled"] is False


@pytest.mark.asyncio
async def test_delete_profile_removes_items(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Profile")
    await manager.create_memory_profile_item(
        user_id="u1", profile_id="p1", content="some memory", role="user"
    )
    items = await manager.list_memory_profile_items(user_id="u1", profile_id="p1")
    assert len(items) == 1

    ok = await manager.delete_memory_profile(user_id="u1", profile_id="p1")
    assert ok is True
    assert await manager.get_memory_profile(user_id="u1", profile_id="p1") is None
    items = await manager.list_memory_profile_items(user_id="u1", profile_id="p1")
    assert len(items) == 0


@pytest.mark.asyncio
async def test_profiles_are_user_scoped(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Mine")
    assert await manager.get_memory_profile(user_id="u2", profile_id="p1") is None


@pytest.mark.asyncio
async def test_create_profile_requires_fields(manager):
    with pytest.raises(ValueError):
        await manager.create_memory_profile(user_id="", profile_id="p1", name="X")
    with pytest.raises(ValueError):
        await manager.create_memory_profile(user_id="u1", profile_id="", name="X")
    with pytest.raises(ValueError):
        await manager.create_memory_profile(user_id="u1", profile_id="p1", name="")


@pytest.mark.asyncio
async def test_clear_memory_profile_items(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Profile")
    await manager.create_memory_profile_item(user_id="u1", profile_id="p1", content="one", role="user")
    await manager.create_memory_profile_item(user_id="u1", profile_id="p1", content="two", role="user")

    assert await manager.count_memory_profile_items(user_id="u1", profile_id="p1") == 2
    deleted = await manager.clear_memory_profile_items(user_id="u1", profile_id="p1")
    assert deleted == 2
    assert await manager.count_memory_profile_items(user_id="u1", profile_id="p1") == 0
    # Profile record survives a clear.
    assert await manager.get_memory_profile(user_id="u1", profile_id="p1") is not None


@pytest.mark.asyncio
async def test_count_memory_profile_items(manager):
    await manager.create_memory_profile(user_id="u1", profile_id="p1", name="Profile")
    assert await manager.count_memory_profile_items(user_id="u1", profile_id="p1") == 0
    await manager.create_memory_profile_item(user_id="u1", profile_id="p1", content="x", role="user")
    assert await manager.count_memory_profile_items(user_id="u1", profile_id="p1") == 1