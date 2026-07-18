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
