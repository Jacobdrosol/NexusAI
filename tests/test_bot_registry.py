"""Unit tests for BotRegistry."""
import pytest
from shared.models import Bot
from shared.exceptions import BotNotFoundError


@pytest.mark.anyio
async def test_register_and_get():
    from control_plane.registry.bot_registry import BotRegistry
    reg = BotRegistry()
    b = Bot(id="bot1", name="Bot 1", role="test", backends=[])
    await reg.register(b)
    result = await reg.get("bot1")
    assert result.id == "bot1"


@pytest.mark.anyio
async def test_get_not_found():
    from control_plane.registry.bot_registry import BotRegistry
    reg = BotRegistry()
    with pytest.raises(BotNotFoundError):
        await reg.get("nonexistent")


@pytest.mark.anyio
async def test_enable_disable():
    from control_plane.registry.bot_registry import BotRegistry
    reg = BotRegistry()
    await reg.register(Bot(id="bot1", name="Bot 1", role="test", enabled=True, backends=[]))
    await reg.disable("bot1")
    b = await reg.get("bot1")
    assert b.enabled is False
    await reg.enable("bot1")
    b = await reg.get("bot1")
    assert b.enabled is True


@pytest.mark.anyio
async def test_remove():
    from control_plane.registry.bot_registry import BotRegistry
    reg = BotRegistry()
    await reg.register(Bot(id="bot1", name="Bot 1", role="test", backends=[]))
    await reg.remove("bot1")
    with pytest.raises(BotNotFoundError):
        await reg.get("bot1")
