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


@pytest.mark.anyio
async def test_persists_across_reloads_and_does_not_reseed_existing(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry

    db_path = str(tmp_path / "bots.db")
    reg = BotRegistry(db_path=db_path)
    await reg.register(Bot(id="bot1", name="Bot 1", role="test", backends=[]))

    reg2 = BotRegistry(db_path=db_path)
    got = await reg2.get("bot1")
    assert got.name == "Bot 1"

    await reg2.seed_from_configs(
        [{"id": "bot1", "name": "Seed Bot", "role": "seed", "backends": []}],
        worker_ids=set(),
    )
    still = await reg2.get("bot1")
    assert still.name == "Bot 1"


@pytest.mark.anyio
async def test_register_rejects_invalid_reference_graph():
    from control_plane.registry.bot_registry import BotRegistry

    reg = BotRegistry()
    with pytest.raises(ValueError):
        await reg.register(
            Bot(
                id="pm-orchestrator",
                name="PM Orchestrator",
                role="pm",
                backends=[],
                assignment_capabilities={"is_project_manager": True},
                workflow={
                    "triggers": [
                        {
                            "id": "pm-to-research",
                            "event": "task_completed",
                            "target_bot_id": "pm-research-analyst",
                            "condition": "has_result",
                        }
                    ],
                    "reference_graph": {
                        "graph_id": "pm-graph",
                        "entry_bot_id": "pm-orchestrator",
                        "current_bot_id": "wrong-bot-id",
                        "nodes": [{"bot_id": "pm-orchestrator"}],
                        "edges": [],
                    },
                },
            )
        )
