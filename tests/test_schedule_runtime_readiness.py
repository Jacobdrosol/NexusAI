from types import SimpleNamespace

import pytest

from control_plane.schedule_safety import (
    ScheduleAutonomySafetyError,
    require_schedule_autonomy_safety,
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


def _project_bound_schedule_bot() -> Bot:
    return Bot(
        id="project-bound-reviewer",
        name="Project Bound Reviewer",
        role="quality_reviewer",
        enabled=True,
        backends=[],
        routing_rules={
            "specialist": {
                "kind": "quality_reviewer",
                "risk_level": "read_only",
                "project_id": "acme",
            }
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("project_id", "reason_code"),
    [
        (None, "schedule_project_scope_required"),
        ("another-project", "schedule_project_scope_mismatch"),
    ],
)
async def test_autonomous_schedule_rejects_missing_or_mismatched_specialist_project_scope(
    project_id,
    reason_code,
):
    bot = _project_bound_schedule_bot()
    schedule = {
        "target_bot_id": bot.id,
        "project_id": project_id,
        "metadata": {"mutation_safe": True},
    }

    with pytest.raises(ScheduleAutonomySafetyError) as exc_info:
        await require_schedule_autonomy_safety(
            schedule,
            bot_registry=_BotRegistry(bot),
            only_when_active=False,
        )

    assert exc_info.value.reason_code == reason_code


@pytest.mark.anyio
async def test_autonomous_schedule_allows_matching_specialist_project_scope():
    bot = _project_bound_schedule_bot()

    await require_schedule_autonomy_safety(
        {
            "target_bot_id": bot.id,
            "project_id": "acme",
            "metadata": {"mutation_safe": True},
        },
        bot_registry=_BotRegistry(bot),
        only_when_active=False,
    )


@pytest.mark.anyio
async def test_autonomous_schedule_allows_attested_docs_hub_write():
    backend = BackendConfig(
        type="documentation",
        provider="documentation",
        model="documentation-v1",
        worker_id="docs-writer-01",
        api_key_ref="DOCUMENTATION_WORKER_TOKEN",
    )
    bot = Bot(
        id="docs-hub-writer",
        name="Docs Hub Writer",
        role="docs-hub-writer",
        project_id="acme",
        enabled=True,
        backends=[backend],
        execution_policy={
            "repo_output_mode": "deny",
            "can_apply_db_actions": False,
            "documentation_action_allowlist": ["documentation.create"],
        },
        routing_rules={
            "worker_profile": {
                "role": "docs-hub-writer",
                "task_scope": "allowlisted-documentation-write",
                "can_edit": False,
            }
        },
    )

    await require_schedule_autonomy_safety(
        {
            "target_bot_id": bot.id,
            "project_id": "acme",
            "task_payload": {
                "action": "create",
                "path": "docs/Automation_Workforce/Docs_Dana/activity.md",
                "content": "# Activity",
            },
            "metadata": {"mutation_safe": True, "connection_operation": "documentation_write"},
        },
        bot_registry=_BotRegistry(bot),
        only_when_active=False,
    )


@pytest.mark.anyio
async def test_autonomous_schedule_enforces_explicit_bot_project_scope():
    bot = Bot(
        id="project-bound-monitor",
        name="Project Bound Monitor",
        role="monitor",
        project_id="acme",
        enabled=True,
        backends=[],
    )

    with pytest.raises(ScheduleAutonomySafetyError) as exc_info:
        await require_schedule_autonomy_safety(
            {
                "target_bot_id": bot.id,
                "project_id": "another-project",
                "metadata": {"mutation_safe": True},
            },
            bot_registry=_BotRegistry(bot),
            only_when_active=False,
        )

    assert exc_info.value.reason_code == "schedule_project_scope_mismatch"
