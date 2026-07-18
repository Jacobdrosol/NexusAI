import pytest


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
    assert response.json()["summary"]["failed"] == 0


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
async def test_bot_readiness_requires_declared_worker_tools(cp_client):
    worker_id = "browser-worker"
    await cp_client.post(
        "/v1/workers",
        json={
            "id": worker_id,
            "name": "Browser Worker",
            "host": "browser-worker",
            "port": 8001,
            "capabilities": [{"type": "llm", "provider": "ollama_cloud", "models": ["ready-model"]}],
        },
    )
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "browser-bot",
            "name": "Browser Bot",
            "role": "worker",
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
