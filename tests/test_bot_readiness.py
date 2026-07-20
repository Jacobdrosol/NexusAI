import pytest
from unittest.mock import AsyncMock, Mock

from control_plane.bot_readiness import assess_bot_instance_readiness
from shared.models import BackendConfig, Bot, CatalogModel


@pytest.mark.anyio
async def test_bot_readiness_reports_ready_worker_backend(cp_client):
    worker_id = "ready-worker"
    bot_id = "ready-bot"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Ready Worker",
            "host": "ready-worker",
            "port": 8001,
            "capabilities": [{"type": "llm", "provider": "ollama_cloud", "models": ["ready-model"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Ready Bot",
            "role": "worker",
            "backends": [{"type": "remote_llm", "worker_id": worker_id, "provider": "ollama_cloud", "model": "ready-model"}],
        },
    )

    response = await cp_client.get(f"/v1/bots/{bot_id}/readiness")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["enabled"] is True
    assert response.json()["state"] == "ready"
    assert response.json()["summary"]["failed"] == 0


@pytest.mark.anyio
async def test_bot_project_binding_requires_an_enabled_project(cp_client):
    payload = {
        "id": "project-bound-bot",
        "name": "Project Bound Bot",
        "role": "reviewer",
        "project_id": "globeiq",
        "backends": [],
    }

    missing = await cp_client.post("/v1/bots", json=payload)
    assert missing.status_code == 409
    assert missing.json()["detail"]["reason_code"] == "bot_project_not_found"

    project = await cp_client.post(
        "/v1/projects",
        json={"id": "globeiq", "name": "GlobeIQ", "mode": "isolated"},
    )
    assert project.status_code == 200

    created = await cp_client.post("/v1/bots", json=payload)
    assert created.status_code == 200
    assert created.json()["project_id"] == "globeiq"


@pytest.mark.anyio
async def test_bot_activation_blocks_backend_missing_from_nonempty_model_catalog(cp_app, cp_client):
    await cp_app.state.model_registry.register(
        CatalogModel(id="other-model", name="other-model", provider="ollama_cloud")
    )
    await cp_client.post(
        "/v1/workers",
        json={
            "id": "catalog-worker",
            "name": "Catalog Worker",
            "host": "catalog-worker",
            "port": 8001,
            "capabilities": [
                {"type": "llm", "provider": "ollama_cloud", "models": ["missing-model"]}
            ],
        },
    )

    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "missing-catalog-bot",
            "name": "Missing Catalog Bot",
            "role": "worker",
            "backends": [
                {
                    "type": "remote_llm",
                    "worker_id": "catalog-worker",
                    "provider": "ollama_cloud",
                    "model": "missing-model",
                }
            ],
        },
    )

    assert response.status_code == 409
    checks = response.json()["detail"]["readiness"]["checks"]
    assert any(
        check["message"]
        == "Model 'missing-model' (provider 'ollama_cloud') is not present/enabled in the model catalog."
        for check in checks
    )


@pytest.mark.anyio
async def test_bot_readiness_allows_http_connection_without_catalog_model():
    model_registry = AsyncMock()
    model_registry.has_any.return_value = True
    model_registry.exists.return_value = False
    connection_resolver = Mock()
    connection_resolver.list_bot_connections.return_value = [
        {"id": 1, "name": "Catalog API", "kind": "http", "enabled": True}
    ]
    bot = Bot(
        id="catalog-intake",
        name="Catalog Intake",
        role="reviewer",
        backends=[
            BackendConfig(
                type="custom",
                provider="http_connection",
                model="declared-catalog-api",
            )
        ],
    )

    readiness = await assess_bot_instance_readiness(
        bot,
        worker_registry=AsyncMock(),
        connection_resolver=connection_resolver,
        model_registry=model_registry,
    )

    assert readiness["ready"] is True
    assert readiness["summary"]["failed"] == 0
    model_registry.has_any.assert_not_awaited()
    model_registry.exists.assert_not_awaited()


@pytest.mark.anyio
async def test_bot_readiness_allows_a_ready_fallback_backend(cp_client):
    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "fallback-ready-bot",
            "name": "Fallback Ready Bot",
            "role": "worker",
            "backends": [
                {
                    "type": "remote_llm",
                    "worker_id": "retired-worker",
                    "provider": "ollama_cloud",
                    "model": "retired-model",
                },
                {
                    "type": "cloud_api",
                    "provider": "openai",
                    "model": "fallback-model",
                    "api_key_ref": "FALLBACK_API_KEY",
                },
            ],
        },
    )
    assert response.status_code == 200

    readiness = await cp_client.get("/v1/bots/fallback-ready-bot/readiness")

    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    assert readiness.json()["summary"]["failed"] == 1
    assert readiness.json()["summary"]["blocking"] == 0


@pytest.mark.anyio
async def test_production_bot_activation_requires_configured_vault_credential(cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_ENV", "production")

    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "missing-vault-credential-bot",
            "name": "Missing Vault Credential Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {
                    "type": "cloud_api",
                    "provider": "ollama_cloud",
                    "model": "ready-model",
                    "api_key_ref": "MISSING_OLLAMA_KEY",
                }
            ],
        },
    )
    stored = await cp_client.get("/v1/bots/missing-vault-credential-bot")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "bot_not_ready"
    assert any(
        check["message"] == "Vault credential 'MISSING_OLLAMA_KEY' is not configured."
        for check in detail["readiness"]["checks"]
    )
    assert stored.status_code == 404


@pytest.mark.anyio
async def test_production_bot_activation_requires_explicit_vault_credential_reference(cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_ENV", "production")

    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "implicit-vault-credential-bot",
            "name": "Implicit Vault Credential Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {
                    "type": "cloud_api",
                    "provider": "ollama_cloud",
                    "model": "ready-model",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert any(
        check["message"]
        == "Cloud API backends require an explicit vault credential reference in production."
        for check in response.json()["detail"]["readiness"]["checks"]
    )


@pytest.mark.anyio
async def test_production_bot_activation_accepts_configured_vault_credential(cp_app, cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_ENV", "production")
    await cp_app.state.key_vault.set_key(
        name="Ollama_Cloud1", provider="ollama_cloud", value="test-credential"
    )

    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "configured-vault-credential-bot",
            "name": "Configured Vault Credential Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {
                    "type": "cloud_api",
                    "provider": "ollama_cloud",
                    "model": "ready-model",
                    "api_key_ref": "Ollama_Cloud1",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True


@pytest.mark.anyio
async def test_bot_readiness_does_not_use_cloud_fallback_for_required_worker_tools(cp_client):
    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "fallback-tools-bot",
            "name": "Fallback Tools Bot",
            "role": "worker",
            "enabled": False,
            "backends": [
                {
                    "type": "remote_llm",
                    "worker_id": "retired-worker",
                    "provider": "ollama_cloud",
                    "model": "retired-model",
                },
                {
                    "type": "cloud_api",
                    "provider": "openai",
                    "model": "fallback-model",
                    "api_key_ref": "FALLBACK_API_KEY",
                },
            ],
            "execution_policy": {"required_worker_tools": ["browser-ui"]},
        },
    )
    assert response.status_code == 200

    readiness = await cp_client.get("/v1/bots/fallback-tools-bot/readiness")

    assert readiness.status_code == 200
    assert readiness.json()["ready"] is False
    assert any(
        check["component"] == "worker-tools" and check["status"] == "failed"
        for check in readiness.json()["checks"]
    )


@pytest.mark.anyio
async def test_bot_readiness_list_returns_each_registered_bot(cp_client):
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "listed-bot",
            "name": "Listed Bot",
            "role": "worker",
            "backends": [],
        },
    )

    response = await cp_client.get("/v1/bots/readiness")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["readiness"][0]["bot_id"] == "listed-bot"
    assert response.json()["readiness"][0]["ready"] is False
    assert response.json()["readiness"][0]["enabled"] is True
    assert response.json()["readiness"][0]["state"] == "blocked"
    assert response.json()["summary"] == {"ready": 0, "blocked": 1, "disabled": 0}


@pytest.mark.anyio
async def test_bot_readiness_list_separates_disabled_templates_from_active_blockers(cp_client):
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "disabled-template",
            "name": "Disabled Template",
            "role": "worker",
            "enabled": False,
            "backends": [],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "enabled-blocker",
            "name": "Enabled Blocker",
            "role": "worker",
            "backends": [],
        },
    )

    response = await cp_client.get("/v1/bots/readiness")

    assert response.status_code == 200
    payload = response.json()
    readiness_by_id = {item["bot_id"]: item for item in payload["readiness"]}
    assert readiness_by_id["disabled-template"]["enabled"] is False
    assert readiness_by_id["disabled-template"]["state"] == "disabled"
    assert readiness_by_id["enabled-blocker"]["enabled"] is True
    assert readiness_by_id["enabled-blocker"]["state"] == "blocked"
    assert payload["summary"] == {"ready": 0, "blocked": 1, "disabled": 1}


@pytest.mark.anyio
async def test_schedule_activation_requires_a_ready_bot(cp_client):
    bot_id = "unready-bot"
    await cp_client.post(
        "/v1/bots",
        json={"id": bot_id, "name": "Unready Bot", "role": "worker", "backends": []},
    )

    paused = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Draft schedule",
            "cron_expression": "0 * * * *",
            "prompt": "Draft only",
            "target_bot_id": bot_id,
        },
    )
    active = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Unsafe schedule",
            "cron_expression": "0 * * * *",
            "prompt": "Do not run",
            "target_bot_id": bot_id,
            "status": "active",
        },
    )

    assert paused.status_code == 200
    assert paused.json()["schedule"]["status"] == "paused"
    assert active.status_code == 409
    assert active.json()["detail"]["reason_code"] == "schedule_target_not_ready"


@pytest.mark.anyio
async def test_active_schedule_requires_explicit_non_mutating_attestation(cp_client):
    bot_id = "scheduled-review-bot"
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Scheduled Review Bot",
            "role": "reviewer",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "review-model"}],
        },
    )

    unattested = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Unattested review",
            "cron_expression": "0 * * * *",
            "prompt": "Review the current queue and report findings.",
            "target_bot_id": bot_id,
            "status": "active",
        },
    )
    attested = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Attested review",
            "cron_expression": "0 * * * *",
            "prompt": "Review the current queue and report findings.",
            "target_bot_id": bot_id,
            "status": "active",
            "metadata": {"mutation_safe": True},
        },
    )

    assert unattested.status_code == 409
    assert unattested.json()["detail"]["reason_code"] == "schedule_autonomy_not_attested"
    assert attested.status_code == 200
    assert attested.json()["schedule"]["status"] == "active"


@pytest.mark.anyio
async def test_active_schedule_rejects_mutation_capable_target(cp_client):
    bot_id = "scheduled-writer-bot"
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Scheduled Writer Bot",
            "role": "writer",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "writer-model"}],
            "execution_policy": {"repo_output_mode": "allow"},
        },
    )

    response = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Unsafe writer",
            "cron_expression": "0 * * * *",
            "prompt": "Write the requested change.",
            "target_bot_id": bot_id,
            "status": "active",
            "metadata": {"mutation_safe": True},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "schedule_target_not_autonomy_safe"
    assert "repository writes" in response.json()["detail"]["message"]


@pytest.mark.anyio
async def test_active_schedule_allows_attested_read_only_browser_inspection(cp_client):
    worker_id = "scheduled-browser-inspector-worker"
    bot_id = "scheduled-browser-inspector-bot"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Scheduled Browser Inspector Worker",
            "host": "browser-worker",
            "port": 8010,
            "status": "online",
            "capabilities": [{"type": "tool", "provider": "browser", "models": ["browser-ui"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Scheduled Browser Inspector",
            "role": "browser-inspector",
            "backends": [
                {
                    "type": "browser",
                    "worker_id": worker_id,
                    "provider": "browser",
                    "model": "browser-ui",
                    "api_key_ref": "BROWSER_WORKER_TOKEN",
                }
            ],
            "execution_policy": {
                "repo_output_mode": "deny",
                "can_apply_db_actions": False,
                "required_worker_tools": ["browser-ui"],
            },
            "routing_rules": {
                "worker_profile": {
                    "role": "browser-inspector",
                    "task_scope": "read-only-browser-inspection",
                    "can_edit": False,
                },
                "input_contract": {
                    "enabled": True,
                    "required_fields": ["path"],
                    "non_empty_fields": ["path"],
                },
            },
        },
    )

    response = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Read-only browser inspection",
            "cron_expression": "0 * * * *",
            "prompt": "Inspect the approved page without mutation.",
            "target_bot_id": bot_id,
            "status": "active",
            "task_payload": {"path": "/admin/dashboard"},
            "metadata": {"mutation_safe": True, "connection_operation": "inspect"},
        },
    )

    assert response.status_code == 200
    assert response.json()["schedule"]["status"] == "active"


@pytest.mark.anyio
async def test_active_schedule_requires_complete_task_payload(cp_client):
    bot_id = "scheduled-structured-review-bot"
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Scheduled Structured Review Bot",
            "role": "reviewer",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "review-model"}],
            "routing_rules": {
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["artifact", "acceptance_criteria"],
                    "non_empty_fields": ["artifact", "acceptance_criteria"],
                }
            },
        },
    )

    incomplete = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Incomplete review",
            "cron_expression": "0 * * * *",
            "prompt": "Review the artifact.",
            "target_bot_id": bot_id,
            "status": "active",
            "metadata": {"mutation_safe": True},
        },
    )
    complete = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Complete review",
            "cron_expression": "0 * * * *",
            "prompt": "Review the artifact.",
            "target_bot_id": bot_id,
            "status": "active",
            "metadata": {"mutation_safe": True},
            "task_payload": {"artifact": "A bounded draft", "acceptance_criteria": "No errors"},
        },
    )

    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["reason_code"] == "schedule_payload_contract_incomplete"
    assert complete.status_code == 200
    assert complete.json()["schedule"]["task_payload"]["artifact"] == "A bounded draft"


@pytest.mark.anyio
async def test_bot_enable_requires_a_ready_backend(cp_client):
    bot_id = "staged-unready-bot"
    created = await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Staged Unready Bot",
            "role": "worker",
            "enabled": False,
            "backends": [],
        },
    )
    assert created.status_code == 200

    enabled = await cp_client.post(f"/v1/bots/{bot_id}/enable")
    current = await cp_client.get(f"/v1/bots/{bot_id}")

    assert enabled.status_code == 409
    assert enabled.json()["detail"]["reason_code"] == "bot_not_ready"
    assert current.json()["enabled"] is False


@pytest.mark.anyio
async def test_bot_preflight_reports_unready_backend_without_registering_it(cp_client):
    response = await cp_client.post(
        "/v1/bots/preflight",
        json={
            "id": "preflight-unready-bot",
            "name": "Preflight Unready Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {"type": "remote_llm", "worker_id": "missing-worker", "provider": "ollama_cloud", "model": "ready-model"}
            ],
        },
    )
    stored = await cp_client.get("/v1/bots/preflight-unready-bot")

    assert response.status_code == 200
    assert response.json()["ready_to_enable"] is False
    assert response.json()["readiness"]["ready"] is False
    assert stored.status_code == 404


@pytest.mark.anyio
async def test_bot_create_rejects_enabled_unready_backend(cp_client):
    response = await cp_client.post(
        "/v1/bots",
        json={
            "id": "create-unready-bot",
            "name": "Create Unready Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {"type": "remote_llm", "worker_id": "missing-worker", "provider": "ollama_cloud", "model": "ready-model"}
            ],
        },
    )
    stored = await cp_client.get("/v1/bots/create-unready-bot")

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "bot_not_ready"
    assert stored.status_code == 404


@pytest.mark.anyio
async def test_bot_enable_allows_a_ready_worker_backend(cp_client):
    worker_id = "enable-ready-worker"
    bot_id = "enable-ready-bot"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Enable Ready Worker",
            "host": "enable-ready-worker",
            "port": 8001,
            "status": "online",
            "capabilities": [{"type": "llm", "provider": "ollama_cloud", "models": ["ready-model"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Enable Ready Bot",
            "role": "worker",
            "enabled": False,
            "backends": [{"type": "remote_llm", "worker_id": worker_id, "provider": "ollama_cloud", "model": "ready-model"}],
        },
    )

    enabled = await cp_client.post(f"/v1/bots/{bot_id}/enable")

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True


@pytest.mark.anyio
async def test_bot_update_cannot_bypass_readiness_on_activation(cp_client):
    bot_id = "update-unready-bot"
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Update Unready Bot",
            "role": "worker",
            "enabled": False,
            "backends": [],
        },
    )

    updated = await cp_client.put(
        f"/v1/bots/{bot_id}",
        json={
            "id": bot_id,
            "name": "Update Unready Bot",
            "role": "worker",
            "enabled": True,
            "backends": [],
        },
    )

    assert updated.status_code == 409
    assert updated.json()["detail"]["reason_code"] == "bot_not_ready"


@pytest.mark.anyio
async def test_enabled_bot_backend_update_requires_fresh_readiness(cp_client):
    bot_id = "backend-update-bot"
    created = await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Backend Update Bot",
            "role": "worker",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "ready-model"}],
        },
    )
    updated = await cp_client.put(
        f"/v1/bots/{bot_id}",
        json={
            "id": bot_id,
            "name": "Backend Update Bot",
            "role": "worker",
            "enabled": True,
            "backends": [
                {"type": "remote_llm", "worker_id": "missing-worker", "provider": "ollama_cloud", "model": "ready-model"}
            ],
        },
    )
    current = await cp_client.get(f"/v1/bots/{bot_id}")

    assert created.status_code == 200
    assert updated.status_code == 409
    assert updated.json()["detail"]["reason_code"] == "bot_not_ready"
    assert current.json()["backends"][0]["type"] == "cloud_api"
    assert current.json()["backends"][0]["provider"] == "ollama_cloud"
    assert current.json()["backends"][0]["model"] == "ready-model"


@pytest.mark.anyio
async def test_bot_readiness_requires_declared_worker_tools(cp_client):
    worker_id = "browser-worker"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Browser Worker",
            "host": "browser-worker",
            "port": 8001,
            "status": "online",
            "capabilities": [{"type": "llm", "provider": "ollama_cloud", "models": ["ready-model"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "browser-bot",
            "name": "Browser Bot",
            "role": "worker",
            "enabled": False,
            "backends": [{"type": "remote_llm", "worker_id": worker_id, "provider": "ollama_cloud", "model": "ready-model"}],
            "execution_policy": {"required_worker_tools": ["browser-ui"]},
        },
    )

    blocked = await cp_client.get("/v1/bots/browser-bot/readiness")
    assert blocked.status_code == 200
    assert blocked.json()["ready"] is False
    assert "browser-ui" in blocked.json()["checks"][-1]["message"]

    await cp_client.put(
        f"/v1/workers/{worker_id}",
        json={
            "id": worker_id,
            "name": "Browser Worker",
            "host": "browser-worker",
            "port": 8001,
            "status": "online",
            "capabilities": [
                {"type": "llm", "provider": "ollama_cloud", "models": ["ready-model"]},
                {"type": "tool", "provider": "cli", "models": ["browser-ui", "playwright"]},
            ],
        },
    )

    activated = await cp_client.put(
        "/v1/bots/browser-bot",
        json={
            "id": "browser-bot",
            "name": "Browser Bot",
            "role": "worker",
            "enabled": True,
            "backends": [{"type": "remote_llm", "worker_id": worker_id, "provider": "ollama_cloud", "model": "ready-model"}],
            "execution_policy": {"required_worker_tools": ["browser-ui"]},
        },
    )
    assert activated.status_code == 200

    ready = await cp_client.get("/v1/bots/browser-bot/readiness")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


@pytest.mark.anyio
async def test_bot_readiness_accepts_attested_read_only_browser_backend(cp_client):
    worker_id = "attested-browser-worker"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Attested Browser Worker",
            "host": "browser-worker",
            "port": 8010,
            "status": "online",
            "capabilities": [{"type": "tool", "provider": "browser", "models": ["browser-ui"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "browser-inspector-bot",
            "name": "Browser Inspector",
            "role": "worker",
            "backends": [
                {
                    "type": "browser",
                    "worker_id": worker_id,
                    "provider": "browser",
                    "model": "browser-ui",
                    "api_key_ref": "BROWSER_WORKER_TOKEN",
                }
            ],
            "execution_policy": {"required_worker_tools": ["browser-ui"]},
        },
    )

    response = await cp_client.get("/v1/bots/browser-inspector-bot/readiness")

    assert response.status_code == 200
    assert response.json()["ready"] is True


@pytest.mark.anyio
async def test_bot_readiness_blocks_attested_unauthenticated_cli_backend(cp_client, cp_app):
    worker_id = "cli-worker"
    bot_id = "cli-bot"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "CLI Worker",
            "host": "cli-worker",
            "port": 8010,
            "status": "online",
            "capabilities": [{"type": "tool", "provider": "cli", "models": ["claude"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "CLI Bot",
            "role": "worker",
            "enabled": False,
            "backends": [
                {
                    "type": "cli",
                    "worker_id": worker_id,
                    "provider": "cli",
                    "model": "claude",
                    "command": "claude -p",
                }
            ],
        },
    )
    await cp_app.state.worker_probe_store.record(
        {
            "worker_id": worker_id,
            "probe_status": "ready",
            "capability_attestation": {"unauthenticated_cli_tools": ["claude"]},
            "checks": [],
        }
    )

    response = await cp_client.get(f"/v1/bots/{bot_id}/readiness")
    enabled = await cp_client.post(f"/v1/bots/{bot_id}/enable")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert "requires CLI authentication for claude" in response.json()["checks"][-1]["message"]
    assert enabled.status_code == 409


@pytest.mark.anyio
async def test_bot_readiness_blocks_attested_missing_cli_backend(cp_client, cp_app):
    worker_id = "missing-cli-worker"
    bot_id = "missing-cli-bot"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Missing CLI Worker",
            "host": "missing-cli-worker",
            "port": 8010,
            "status": "online",
            "capabilities": [{"type": "tool", "provider": "cli", "models": ["claude"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Missing CLI Bot",
            "role": "worker",
            "enabled": False,
            "backends": [
                {
                    "type": "cli",
                    "worker_id": worker_id,
                    "provider": "cli",
                    "model": "claude",
                    "command": "claude -p",
                }
            ],
        },
    )
    await cp_app.state.worker_probe_store.record(
        {
            "worker_id": worker_id,
            "probe_status": "ready",
            "capability_attestation": {"unavailable_cli_tools": ["claude"]},
            "checks": [],
        }
    )

    response = await cp_client.get(f"/v1/bots/{bot_id}/readiness")
    enabled = await cp_client.post(f"/v1/bots/{bot_id}/enable")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert "missing required CLI tool(s): claude" in response.json()["checks"][-1]["message"]
    assert enabled.status_code == 409
