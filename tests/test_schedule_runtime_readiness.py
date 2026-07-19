from types import SimpleNamespace

import pytest

from control_plane.schedule_safety import (
    ScheduleAutonomySafetyError,
    require_schedule_runtime_readiness,
)
from shared.models import BackendConfig, Bot


class _BotRegistry:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def get(self, bot_id: str) -> Bot:
        assert bot_id == self.bot.id
        return self.bot


class _MissingVault:
    async def get_key(self, name: str):
        from shared.exceptions import APIKeyNotFoundError

        raise APIKeyNotFoundError(f"API key not found: {name}")


class _MissingCatalogModel:
    async def has_any(self) -> bool:
        return True

    async def exists(self, provider: str, model: str) -> bool:
        assert provider == "ollama_cloud"
        assert model == "missing-model"
        return False


@pytest.mark.anyio
async def test_schedule_runtime_readiness_blocks_missing_production_vault_credential(monkeypatch):
    monkeypatch.setenv("NEXUSAI_ENV", "production")
    bot = Bot(
        id="scheduled-cloud-bot",
        name="Scheduled Cloud Bot",
        role="monitor",
        enabled=True,
        backends=[
            BackendConfig(
                type="cloud_api",
                provider="ollama_cloud",
                model="ready-model",
                api_key_ref="MISSING_OLLAMA_KEY",
            )
        ],
    )

    with pytest.raises(ScheduleAutonomySafetyError) as exc_info:
        await require_schedule_runtime_readiness(
            {"target_bot_id": bot.id},
            bot_registry=_BotRegistry(bot),
            worker_registry=SimpleNamespace(),
            connection_resolver=SimpleNamespace(),
            key_vault=_MissingVault(),
        )

    assert exc_info.value.reason_code == "schedule_target_not_ready"
    assert exc_info.value.blockers == ["Vault credential 'MISSING_OLLAMA_KEY' is not configured."]


@pytest.mark.anyio
async def test_schedule_runtime_readiness_blocks_model_missing_from_catalog():
    bot = Bot(
        id="scheduled-catalog-bot",
        name="Scheduled Catalog Bot",
        role="monitor",
        enabled=True,
        backends=[
            BackendConfig(
                type="cloud_api",
                provider="ollama_cloud",
                model="missing-model",
            )
        ],
    )

    with pytest.raises(ScheduleAutonomySafetyError) as exc_info:
        await require_schedule_runtime_readiness(
            {"target_bot_id": bot.id},
            bot_registry=_BotRegistry(bot),
            worker_registry=SimpleNamespace(),
            connection_resolver=SimpleNamespace(),
            model_registry=_MissingCatalogModel(),
        )

    assert exc_info.value.reason_code == "schedule_target_not_ready"
    assert exc_info.value.blockers == [
        "Model 'missing-model' (provider 'ollama_cloud') is not present/enabled in the model catalog."
    ]
