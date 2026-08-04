"""Integration tests for control plane FastAPI routes."""
import asyncio
import hashlib
import hmac
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient


@pytest.mark.anyio
async def test_health(cp_client):
    resp = await cp_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_metrics_endpoint_exposes_prometheus_text(cp_client):
    await cp_client.get("/health")
    resp = await cp_client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "nexus_control_plane_http_requests_total" in text
    assert "nexus_control_plane_http_request_duration_seconds_bucket" in text


@pytest.mark.anyio
async def test_control_plane_optional_api_token_auth(cp_app):
    cp_app.state.control_plane_api_token = "test-token"
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200

        unauthorized = await client.get("/v1/workers")
        assert unauthorized.status_code == 401

        authorized_header = await client.get("/v1/workers", headers={"X-Nexus-API-Key": "test-token"})
        assert authorized_header.status_code == 200

        authorized_bearer = await client.get(
            "/v1/workers",
            headers={"Authorization": "Bearer test-token"},
        )
        assert authorized_bearer.status_code == 200


@pytest.mark.anyio
async def test_chat_message_rate_limit_guard(cp_client, monkeypatch):
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_COUNT", "1")
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_WINDOW_SECONDS", "60")

    create_resp = await cp_client.post("/v1/chat/conversations", json={"title": "Rate Limit"})
    conversation_id = create_resp.json()["id"]
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-rate",
            "name": "Rate Bot",
            "role": "assistant",
            "backends": [],
            "enabled": True,
        },
    )

    first = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json={"content": "hello", "bot_id": "bot-rate"},
    )
    assert first.status_code in (200, 500)

    second = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json={"content": "again", "bot_id": "bot-rate"},
    )
    assert second.status_code == 429


@pytest.mark.anyio
async def test_chat_message_body_size_guard(cp_client, monkeypatch):
    monkeypatch.setenv("CP_MAX_BODY_BYTES_CHAT_MESSAGES", "60")
    create_resp = await cp_client.post("/v1/chat/conversations", json={"title": "Body Size"})
    conversation_id = create_resp.json()["id"]
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-size",
            "name": "Size Bot",
            "role": "assistant",
            "backends": [],
            "enabled": True,
        },
    )
    payload = {"content": "x" * 200, "bot_id": "bot-size"}
    resp = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json=payload,
    )
    assert resp.status_code == 413


@pytest.mark.anyio
async def test_list_workers_empty(cp_client):
    resp = await cp_client.get("/v1/workers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_public_default_config_keeps_example_bot_seeding_opt_in():
    from shared.config_loader import ConfigLoader

    repo_root = Path(__file__).resolve().parents[1]
    control_plane = ConfigLoader.load_yaml(str(repo_root / "config" / "nexus_config.yaml"))["control_plane"]
    example_bot = ConfigLoader.load_yaml(str(repo_root / "config" / "bots" / "example_bot.yaml"))

    assert control_plane["seed_bots_from_config"] is False
    assert control_plane["force_seed_bots_from_config"] is False
    assert example_bot["enabled"] is False


def test_create_app_does_not_seed_workers_from_config_by_default(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workers_dir = config_dir / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "seeded-worker.yaml").write_text(
        "\n".join(
            [
                'id: "seeded-worker"',
                'name: "Seeded Worker"',
                'host: "127.0.0.1"',
                "port: 8001",
                'status: "offline"',
                "capabilities: []",
                "enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "bots").mkdir(parents=True)
    (config_dir / "nexus_config.yaml").write_text(
        "\n".join(
            [
                "control_plane:",
                "  host: 0.0.0.0",
                "  port: 8000",
                f"  workers_config_dir: {workers_dir.as_posix()}",
                f"  bots_config_dir: {(config_dir / 'bots').as_posix()}",
                "  seed_bots_from_config: false",
                "  heartbeat_timeout_seconds: 30",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_CONFIG_PATH", str(config_dir / "nexus_config.yaml"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'control-plane.db').as_posix()}")

    import control_plane.main as main_module
    importlib.reload(main_module)

    create_app = main_module.create_app
    app = create_app()
    with TestClient(app):
        workers = asyncio.run(app.state.worker_registry.list())
        assert workers == []
    monkeypatch.delenv("NEXUS_CONFIG_PATH", raising=False)


def test_create_app_can_seed_workers_from_config_when_enabled(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workers_dir = config_dir / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "seeded-worker.yaml").write_text(
        "\n".join(
            [
                'id: "seeded-worker"',
                'name: "Seeded Worker"',
                'host: "127.0.0.1"',
                "port: 8001",
                'status: "offline"',
                "capabilities: []",
                "enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "bots").mkdir(parents=True)
    (config_dir / "nexus_config.yaml").write_text(
        "\n".join(
            [
                "control_plane:",
                "  host: 0.0.0.0",
                "  port: 8000",
                f"  workers_config_dir: {workers_dir.as_posix()}",
                f"  bots_config_dir: {(config_dir / 'bots').as_posix()}",
                "  seed_workers_from_config: true",
                "  seed_bots_from_config: false",
                "  heartbeat_timeout_seconds: 30",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_CONFIG_PATH", str(config_dir / "nexus_config.yaml"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'control-plane.db').as_posix()}")

    import control_plane.main as main_module
    importlib.reload(main_module)

    create_app = main_module.create_app
    app = create_app()
    with TestClient(app):
        workers = asyncio.run(app.state.worker_registry.list())
        assert len(workers) == 1
        assert workers[0].id == "seeded-worker"
    monkeypatch.delenv("NEXUS_CONFIG_PATH", raising=False)


@pytest.mark.anyio
async def test_register_worker(cp_client):
    worker = {
        "id": "w1",
        "name": "Test Worker",
        "host": "localhost",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    resp = await cp_client.post("/v1/workers", json=worker)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "w1"
    assert data["status"] == "online"
    assert data["last_heartbeat_at"]


@pytest.mark.anyio
async def test_register_worker_queues_delayed_runtime_probe(cp_client, cp_app, monkeypatch):
    from control_plane.api import workers as workers_api

    worker = {
        "id": "registration-probe-worker",
        "name": "Registration Probe Worker",
        "host": "worker.test",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    captured = []

    def capture_task(coroutine):
        captured.append(coroutine)
        return None

    async def fake_probe(registered_worker):
        return {
            "worker_id": registered_worker.id,
            "probe_status": "ready",
            "checked_at": "2026-07-19T00:00:00+00:00",
            "dispatch_eligible": True,
            "checks": [],
        }

    monkeypatch.setattr(workers_api.asyncio, "create_task", capture_task)
    monkeypatch.setattr(workers_api, "_REGISTRATION_PROBE_DELAY_SECONDS", 0)
    monkeypatch.setattr(workers_api, "probe_worker", fake_probe)

    response = await cp_client.post("/v1/workers", json=worker)

    assert response.status_code == 200
    assert len(captured) == 1
    await captured[0]
    stored = await cp_app.state.worker_probe_store.get("registration-probe-worker")
    assert stored is not None
    assert stored["probe_status"] == "ready"


@pytest.mark.anyio
async def test_provision_worker_starts_offline(cp_client):
    worker = {
        "id": "provisioned-worker",
        "name": "Provisioned Worker",
        "host": "localhost",
        "port": 8001,
        "capabilities": [],
    }

    response = await cp_client.post("/v1/workers/provision", json=worker)

    assert response.status_code == 201
    assert response.json()["status"] == "offline"
    assert response.json()["last_heartbeat_at"] is None


@pytest.mark.anyio
async def test_register_worker_with_ollama_cloud_capability(cp_client):
    worker = {
        "id": "cloud-worker-1",
        "name": "Cloud Worker",
        "host": "cloud-worker-1",
        "port": 8010,
        "status": "offline",
        "capabilities": [
            {
                "type": "llm",
                "provider": "ollama_cloud",
                "models": ["glm-5.2:cloud"],
            }
        ],
        "metrics": {},
        "enabled": True,
    }
    resp = await cp_client.post("/v1/workers", json=worker)
    assert resp.status_code == 200
    data = resp.json()
    assert data["capabilities"][0]["provider"] == "ollama_cloud"


@pytest.mark.anyio
async def test_get_worker_not_found(cp_client):
    resp = await cp_client.get("/v1/workers/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_worker_heartbeat(cp_client):
    worker = {"id": "w1", "name": "W1", "host": "h1", "port": 8001, "status": "offline", "capabilities": [], "metrics": {}, "enabled": True}
    await cp_client.post("/v1/workers", json=worker)
    resp = await cp_client.post("/v1/workers/w1/heartbeat", json={})
    assert resp.status_code == 200


def test_worker_probe_refresh_becomes_due_before_schedule_freshness_expires(monkeypatch):
    from control_plane.api.workers import _worker_probe_refresh_due

    monkeypatch.setenv("NEXUSAI_AUTONOMOUS_WORKER_PROBE_MAX_AGE_SECONDS", "300")
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    assert _worker_probe_refresh_due(
        {"probe_status": "ready", "checked_at": (now - timedelta(seconds=224)).isoformat()},
        now=now,
    ) is False
    assert _worker_probe_refresh_due(
        {"probe_status": "ready", "checked_at": (now - timedelta(seconds=225)).isoformat()},
        now=now,
    ) is True
    assert _worker_probe_refresh_due(
        {"probe_status": "degraded", "checked_at": (now - timedelta(seconds=30)).isoformat()},
        now=now,
    ) is True
    assert _worker_probe_refresh_due(None, now=now) is True


@pytest.mark.anyio
async def test_worker_heartbeat_queues_due_probe_refresh(cp_client, cp_app, monkeypatch):
    from control_plane.api import workers as workers_api

    worker = {
        "id": "refresh-on-heartbeat-worker",
        "name": "Refresh On Heartbeat Worker",
        "host": "worker.test",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    await cp_client.post("/v1/workers", json=worker)
    await cp_app.state.worker_probe_store.record(
        {
            "worker_id": worker["id"],
            "probe_status": "ready",
            "checked_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "dispatch_eligible": True,
            "checks": [],
        }
    )
    queued: list[str] = []
    monkeypatch.setattr(
        workers_api,
        "_queue_worker_probe_refresh",
        lambda _request, queued_worker_id: queued.append(queued_worker_id),
    )

    response = await cp_client.post(f"/v1/workers/{worker['id']}/heartbeat", json={})

    assert response.status_code == 200
    assert queued == [worker["id"]]


@pytest.mark.anyio
async def test_worker_probe_returns_non_mutating_runtime_result(cp_client, monkeypatch):
    worker = {
        "id": "w1",
        "name": "W1",
        "host": "h1",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    await cp_client.post("/v1/workers", json=worker)

    async def fake_probe(registered_worker):
        assert registered_worker.id == "w1"
        return {
            "worker_id": "w1",
            "worker_status": "online",
            "worker_enabled": True,
            "dispatch_eligible": True,
            "checked_at": "2026-07-18T00:00:00+00:00",
            "probe_status": "ready",
            "health": {
                "status": "ok",
                "worker_id": "w1",
                "enabled_cli_tools": [],
            },
            "reported_capabilities": [],
            "capability_attestation": {},
            "checks": [
                {"name": "health", "status": "pass", "detail": "health endpoint returned ok"}
            ],
        }

    monkeypatch.setattr("control_plane.api.workers.probe_worker", fake_probe)
    response = await cp_client.post("/v1/workers/w1/probe")

    assert response.status_code == 200
    assert response.json()["probe_status"] == "ready"
    persisted = await cp_client.get("/v1/workers/w1/probe")
    assert persisted.status_code == 200
    assert persisted.json()["checked_at"] == "2026-07-18T00:00:00+00:00"


@pytest.mark.anyio
async def test_worker_probe_returns_unknown_before_first_probe(cp_client):
    worker = {
        "id": "w-unprobed",
        "name": "Unprobed Worker",
        "host": "h1",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    await cp_client.post("/v1/workers", json=worker)

    response = await cp_client.get("/v1/workers/w-unprobed/probe")

    assert response.status_code == 200
    assert response.json() == {
        "worker_id": "w-unprobed",
        "probe_status": "unknown",
        "checked_at": None,
        "dispatch_eligible": False,
        "checks": [],
    }


@pytest.mark.anyio
async def test_update_worker(cp_client):
    worker = {"id": "w1", "name": "W1", "host": "h1", "port": 8001, "status": "offline", "capabilities": [], "metrics": {}, "enabled": True}
    await cp_client.post("/v1/workers", json=worker)
    update = {"id": "w1", "name": "W1 Renamed", "host": "h1", "port": 8001, "status": "online", "capabilities": [], "metrics": {}, "enabled": False}
    resp = await cp_client.put("/v1/workers/w1", json=update)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.anyio
async def test_worker_lifecycle_rejects_disabling_or_deleting_dependent_worker(cp_app, cp_client):
    worker = {
        "id": "dependent-worker",
        "name": "Dependent Worker",
        "host": "worker.internal",
        "port": 8010,
        "status": "online",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    assert (await cp_client.post("/v1/workers", json=worker)).status_code == 200
    bot_payload = {
        "id": "dependent-bot",
        "name": "Dependent Bot",
        "role": "assistant",
        "enabled": False,
        "backends": [
            {"type": "remote_llm", "provider": "ollama_cloud", "model": "qwen3.5:cloud", "worker_id": "dependent-worker"}
        ],
    }
    assert (await cp_client.post("/v1/bots", json=bot_payload)).status_code == 200
    bot = await cp_app.state.bot_registry.get("dependent-bot")
    await cp_app.state.bot_registry.update("dependent-bot", bot.model_copy(update={"enabled": True}))

    dependencies = await cp_client.get("/v1/workers/dependent-worker/dependencies")
    assert dependencies.status_code == 200
    assert dependencies.json()["enabled_bot_ids"] == ["dependent-bot"]
    assert dependencies.json()["can_disable"] is False
    assert dependencies.json()["can_delete"] is False

    worker["enabled"] = False
    disable = await cp_client.put("/v1/workers/dependent-worker", json=worker)
    assert disable.status_code == 409
    assert disable.json()["detail"]["reason_code"] == "worker_disable_blocked"

    delete = await cp_client.delete("/v1/workers/dependent-worker")
    assert delete.status_code == 409
    assert delete.json()["detail"]["reason_code"] == "worker_delete_blocked"


@pytest.mark.anyio
async def test_worker_update_rejects_worker_id_change(cp_client):
    worker = {"id": "fixed-worker", "name": "Fixed Worker", "host": "worker.internal", "port": 8010, "capabilities": []}
    assert (await cp_client.post("/v1/workers", json=worker)).status_code == 200
    worker["id"] = "different-worker"

    response = await cp_client.put("/v1/workers/fixed-worker", json=worker)
    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "worker_id_immutable"


@pytest.mark.anyio
async def test_bot_lifecycle_rejects_disabling_or_deleting_referenced_bot(cp_app, cp_client):
    target = {"id": "target-bot", "name": "Target Bot", "role": "assistant", "enabled": True, "backends": []}
    upstream = {
        "id": "upstream-bot",
        "name": "Upstream Bot",
        "role": "assistant",
        "enabled": True,
        "backends": [],
        "workflow": {
            "triggers": [
                {
                    "id": "handoff",
                    "event": "task_completed",
                    "target_bot_id": "target-bot",
                    "enabled": True,
                }
            ]
        },
    }
    assert (await cp_client.post("/v1/bots", json=target)).status_code == 200
    assert (await cp_client.post("/v1/bots", json=upstream)).status_code == 200
    await cp_app.state.agent_schedule_engine.create_schedule(
        {
            "name": "Target schedule",
            "cron_expression": "*/5 * * * *",
            "timezone": "UTC",
            "prompt": "Read-only status check.",
            "status": "active",
            "target_bot_id": "target-bot",
        }
    )

    dependencies = await cp_client.get("/v1/bots/target-bot/dependencies")
    assert dependencies.status_code == 200
    payload = dependencies.json()
    assert payload["can_disable"] is False
    assert payload["can_delete"] is False
    assert payload["schedule_references"][0]["relation"] == "target_bot"
    assert payload["workflow_references"][0]["relation"] == "workflow_trigger"

    target["enabled"] = False
    update = await cp_client.put("/v1/bots/target-bot", json=target)
    assert update.status_code == 409
    assert update.json()["detail"]["reason_code"] == "bot_disable_blocked"

    disable = await cp_client.post("/v1/bots/target-bot/disable")
    assert disable.status_code == 409
    assert disable.json()["detail"]["reason_code"] == "bot_disable_blocked"

    delete = await cp_client.delete("/v1/bots/target-bot")
    assert delete.status_code == 409
    assert delete.json()["detail"]["reason_code"] == "bot_delete_blocked"


@pytest.mark.anyio
async def test_list_bots_empty(cp_client):
    resp = await cp_client.get("/v1/bots")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_create_bot(cp_client):
    bot = {"id": "bot1", "name": "Bot 1", "role": "test", "priority": 0, "enabled": True, "backends": []}
    resp = await cp_client.post("/v1/bots", json=bot)
    assert resp.status_code == 200
    assert resp.json()["id"] == "bot1"


@pytest.mark.anyio
async def test_create_bot_returns_structured_validation_errors_for_invalid_schema(cp_client):
    resp = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-invalid-policy",
            "name": "Bot Invalid Policy",
            "role": "assistant",
            "backends": [],
            "execution_policy": {"repo_output_mode": "constrained"},
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "bot_validation_failed"
    assert detail["message"] == "Bot payload failed schema validation."
    assert any(
        item.get("field_path") == "execution_policy.repo_output_mode"
        and item.get("invalid_value") == "constrained"
        for item in detail["validation_errors"]
    )


@pytest.mark.anyio
async def test_create_bot_returns_structured_validation_errors_for_workflow_policy(cp_client):
    resp = await cp_client.post(
        "/v1/bots",
        json={
            "id": "pm-without-workflow",
            "name": "PM Without Workflow",
            "role": "assistant",
            "backends": [],
            "assignment_capabilities": {"is_project_manager": True},
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "bot_validation_failed"
    assert detail["message"] == "Bot payload failed workflow validation."
    assert any(
        item.get("field_path") == "workflow.triggers"
        and "project manager" in str(item.get("message") or "").lower()
        for item in detail["validation_errors"]
    )


@pytest.mark.anyio
async def test_update_bot_returns_structured_validation_errors_for_id_mismatch(cp_client):
    await cp_client.post(
        "/v1/bots",
        json={"id": "bot-update", "name": "Bot Update", "role": "assistant", "backends": []},
    )
    resp = await cp_client.put(
        "/v1/bots/bot-update",
        json={
            "id": "bot-different",
            "name": "Bot Different",
            "role": "assistant",
            "backends": [],
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "bot_id_mismatch"
    assert detail["message"] == "bot.id must match the path bot_id"
    assert any(
        item.get("field_path") == "id"
        and item.get("invalid_value") == "bot-different"
        for item in detail["validation_errors"]
    )


@pytest.mark.anyio
async def test_create_bot_returns_structured_validation_errors_for_reference_graph_fields(cp_client):
    resp = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-with-bad-graph",
            "name": "Bot With Bad Graph",
            "role": "assistant",
            "backends": [],
            "workflow": {
                "triggers": [
                    {
                        "id": "trigger-1",
                        "event": "task_completed",
                        "target_bot_id": "missing-target-bot",
                    }
                ],
                "reference_graph": {
                    "graph_id": "test-graph",
                    "entry_bot_id": "bot-with-bad-graph",
                    "current_bot_id": "wrong-bot-id",
                    "nodes": [{"bot_id": "bot-with-bad-graph"}],
                    "edges": [],
                },
            },
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "bot_validation_failed"
    assert detail["message"] == "Bot payload failed workflow validation."

    validation_errors = detail["validation_errors"]
    assert any(
        item.get("field_path") == "workflow.reference_graph.current_bot_id"
        and "current_bot_id" in str(item.get("message") or "").lower()
        for item in validation_errors
    )
    assert any(
        item.get("field_path") == "workflow.reference_graph.nodes"
        and "missing node" in str(item.get("message") or "").lower()
        for item in validation_errors
    )


@pytest.mark.anyio
async def test_external_bot_trigger_creates_task_with_auth_and_payload_field(cp_app, cp_client):
    await cp_app.state.key_vault.set_key("external-trigger-token", "webhook", "topsecret")
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext",
            "name": "External Trigger Bot",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": True,
                    "auth_header": "X-Nexus-Trigger-Token",
                    "auth_token_ref": "external-trigger-token",
                    "payload_field": "event.data",
                    "allow_metadata": True,
                    "source": "webhook",
                }
            },
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext/trigger",
        json={
            "event": {"data": {"instruction": "continue", "course_id": "course-1"}},
            "metadata": {"project_id": "proj-1", "priority": 3},
        },
        headers={"X-Nexus-Trigger-Token": "topsecret"},
    )
    assert trigger.status_code == 200
    body = trigger.json()
    assert body["bot_id"] == "bot-ext"
    assert body["payload"] == {"instruction": "continue", "course_id": "course-1"}
    assert (body.get("metadata") or {}).get("source") == "webhook"
    assert (body.get("metadata") or {}).get("project_id") == "proj-1"
    assert (body.get("metadata") or {}).get("priority") == 3


@pytest.mark.anyio
async def test_external_bot_trigger_requires_explicit_autonomy_attestation(cp_app, cp_client):
    await cp_app.state.key_vault.set_key("external-trigger-token", "webhook", "topsecret")
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-unattested",
            "name": "Unattested External Trigger",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "require_auth": True,
                    "auth_token_ref": "external-trigger-token",
                }
            },
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext-unattested/trigger",
        json={"payload": {"instruction": "run"}},
        headers={"X-Nexus-Trigger-Token": "topsecret"},
    )

    assert trigger.status_code == 409
    assert trigger.json()["detail"]["reason_code"] == "external_trigger_autonomy_not_attested"


@pytest.mark.anyio
async def test_external_bot_trigger_rejects_mutation_capable_target(cp_app, cp_client):
    await cp_app.state.key_vault.set_key("external-trigger-token", "webhook", "topsecret")
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-writer",
            "name": "External Trigger Writer",
            "role": "writer",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "execution_policy": {"repo_output_mode": "allow"},
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": True,
                    "auth_token_ref": "external-trigger-token",
                }
            },
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext-writer/trigger",
        json={"payload": {"instruction": "run"}},
        headers={"X-Nexus-Trigger-Token": "topsecret"},
    )

    assert trigger.status_code == 409
    assert trigger.json()["detail"]["reason_code"] == "external_trigger_target_not_autonomy_safe"


@pytest.mark.anyio
async def test_external_bot_trigger_requires_dedicated_auth(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-open",
            "name": "Open External Trigger",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": False,
                    "auth_token_ref": "external-trigger-token",
                }
            },
        },
    )
    assert create.status_code == 400
    assert create.json()["detail"]["reason_code"] == "bot_validation_failed"


@pytest.mark.anyio
async def test_external_bot_trigger_rejects_inline_secret_configuration(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-inline-secret",
            "name": "Inline Secret External Trigger",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": True,
                    "auth_token": "not-allowed",
                    "auth_token_ref": "external-trigger-token",
                }
            },
        },
    )

    assert create.status_code == 400
    validation_errors = create.json()["detail"]["validation_errors"]
    assert any("auth_token is not permitted" in item["message"] for item in validation_errors)


@pytest.mark.anyio
async def test_external_bot_trigger_rejects_missing_vault_secret(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-missing-secret",
            "name": "Missing Secret External Trigger",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": True,
                    "auth_token_ref": "missing-external-trigger-token",
                }
            },
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext-missing-secret/trigger",
        json={"payload": {"instruction": "run"}},
        headers={"X-Nexus-Trigger-Token": "unused"},
    )

    assert trigger.status_code == 409
    assert trigger.json()["detail"]["reason_code"] == "external_trigger_secret_unavailable"


@pytest.mark.anyio
async def test_external_bot_trigger_rejects_when_disabled(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-disabled",
            "name": "External Trigger Disabled",
            "role": "assistant",
            "enabled": True,
            "backends": [],
            "routing_rules": {"external_trigger": {"enabled": False}},
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext-disabled/trigger",
        json={"payload": {"instruction": "ignored"}},
    )
    assert trigger.status_code == 403


@pytest.mark.anyio
async def test_external_bot_trigger_bypasses_global_cp_token_when_bot_auth_is_valid(cp_app):
    cp_app.state.control_plane_api_token = "global-cp-token"
    await cp_app.state.key_vault.set_key("external-trigger-token", "webhook", "external-secret")
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create = await client.post(
            "/v1/bots",
            json={
            "id": "bot-ext-auth",
            "name": "External Trigger Auth",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "test-model"}],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "autonomy_safe": True,
                    "require_auth": True,
                        "auth_header": "X-External-Token",
                        "auth_token_ref": "external-trigger-token",
                    }
                },
            },
            headers={"X-Nexus-API-Key": "global-cp-token"},
        )
        assert create.status_code == 200

        trigger = await client.post(
            "/v1/bots/bot-ext-auth/trigger",
            json={"payload": {"instruction": "run"}},
            headers={"X-External-Token": "external-secret"},
        )
        assert trigger.status_code == 200
        assert trigger.json()["bot_id"] == "bot-ext-auth"


@pytest.mark.anyio
async def test_list_tasks_empty(cp_client):
    resp = await cp_client.get("/v1/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_create_project(cp_client):
    project = {"id": "p1", "name": "Project 1", "mode": "isolated"}
    resp = await cp_client.post("/v1/projects", json=project)
    assert resp.status_code == 200
    assert resp.json()["id"] == "p1"


@pytest.mark.anyio
async def test_add_project_bridge(cp_client):
    await cp_client.post("/v1/projects", json={"id": "p1", "name": "One", "mode": "bridged"})
    await cp_client.post("/v1/projects", json={"id": "p2", "name": "Two", "mode": "bridged"})

    resp = await cp_client.post("/v1/projects/p1/bridges/p2")
    assert resp.status_code == 200

    p1 = (await cp_client.get("/v1/projects/p1")).json()
    p2 = (await cp_client.get("/v1/projects/p2")).json()
    assert "p2" in p1["bridge_project_ids"]
    assert "p1" in p2["bridge_project_ids"]


@pytest.mark.anyio
async def test_add_project_bridge_rejects_isolated_mode(cp_client):
    await cp_client.post("/v1/projects", json={"id": "p1", "name": "One", "mode": "isolated"})
    await cp_client.post("/v1/projects", json={"id": "p2", "name": "Two", "mode": "bridged"})

    resp = await cp_client.post("/v1/projects/p1/bridges/p2")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_create_and_get_api_key_metadata(cp_client):
    resp = await cp_client.post(
        "/v1/keys",
        json={"name": "openai-dev", "provider": "openai", "value": "sk-test"},
    )
    assert resp.status_code == 200

    meta = await cp_client.get("/v1/keys/openai-dev")
    assert meta.status_code == 200
    data = meta.json()
    assert data["name"] == "openai-dev"
    assert data["provider"] == "openai"
    assert "value" not in data


@pytest.mark.anyio
async def test_delete_api_key(cp_client):
    await cp_client.post(
        "/v1/keys",
        json={"name": "gemini-dev", "provider": "gemini", "value": "gk-test"},
    )
    delete_resp = await cp_client.delete("/v1/keys/gemini-dev")
    assert delete_resp.status_code == 200

    get_resp = await cp_client.get("/v1/keys/gemini-dev")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_create_and_get_catalog_model(cp_client):
    model = {
        "id": "openai-gpt-4o-mini",
        "name": "gpt-4o-mini",
        "provider": "openai",
        "context_window": 128000,
        "capabilities": ["chat"],
        "input_cost_per_1k": 0.00015,
        "output_cost_per_1k": 0.0006,
        "notes": "fast baseline model",
        "enabled": True,
    }
    resp = await cp_client.post("/v1/models", json=model)
    assert resp.status_code == 200
    assert resp.json()["id"] == "openai-gpt-4o-mini"

    get_resp = await cp_client.get("/v1/models/openai-gpt-4o-mini")
    assert get_resp.status_code == 200
    assert get_resp.json()["provider"] == "openai"


@pytest.mark.anyio
async def test_delete_catalog_model(cp_client):
    await cp_client.post(
        "/v1/models",
        json={"id": "gemini-2-flash", "name": "gemini-2.0-flash", "provider": "gemini"},
    )
    delete_resp = await cp_client.delete("/v1/models/gemini-2-flash")
    assert delete_resp.status_code == 200

    get_resp = await cp_client.get("/v1/models/gemini-2-flash")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_vault_ingest_and_search(cp_client):
    ingest_resp = await cp_client.post(
        "/v1/vault/items",
        json={
            "title": "Design Notes",
            "content": "NexusAI uses a control plane and worker nodes.",
            "namespace": "global",
        },
    )
    assert ingest_resp.status_code == 200
    item_id = ingest_resp.json()["id"]

    chunks_resp = await cp_client.get(f"/v1/vault/items/{item_id}/chunks")
    assert chunks_resp.status_code == 200
    assert len(chunks_resp.json()) >= 1

    search_resp = await cp_client.post(
        "/v1/vault/search",
        json={"query": "control plane", "limit": 3},
    )
    assert search_resp.status_code == 200
    assert len(search_resp.json()) >= 1


@pytest.mark.anyio
async def test_vault_upsert_reuses_existing_source_ref(cp_client):
    first = await cp_client.post(
        "/v1/vault/items/upsert",
        json={
            "title": "Doc",
            "content": "v1",
            "namespace": "project:test:data",
            "project_id": "test",
            "source_ref": "project-data://test/docs/readme.md",
        },
    )
    assert first.status_code == 200

    second = await cp_client.post(
        "/v1/vault/items/upsert",
        json={
            "title": "Doc",
            "content": "v2",
            "namespace": "project:test:data",
            "project_id": "test",
            "source_ref": "project-data://test/docs/readme.md",
        },
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    items_resp = await cp_client.get("/v1/vault/items?project_id=test&limit=10")
    assert items_resp.status_code == 200
    items = items_resp.json()
    assert len(items) == 1
    assert items[0]["content"] == "v2"


@pytest.mark.anyio
async def test_vault_context_endpoint(cp_client):
    await cp_client.post(
        "/v1/vault/items",
        json={"title": "JWT", "content": "JWT refresh token flow and auth middleware."},
    )
    context_resp = await cp_client.post(
        "/v1/vault/context",
        json={"query": "auth token", "limit": 2},
    )
    assert context_resp.status_code == 200
    data = context_resp.json()
    assert "contexts" in data
    assert data["context_count"] >= 1


@pytest.mark.anyio
async def test_vault_delete_item_and_list_namespaces(cp_client):
    item_a = (
        await cp_client.post(
            "/v1/vault/items",
            json={"title": "A", "content": "alpha", "namespace": "alpha"},
        )
    ).json()
    await cp_client.post(
        "/v1/vault/items",
        json={"title": "B", "content": "beta", "namespace": "beta"},
    )

    ns_resp = await cp_client.get("/v1/vault/namespaces")
    assert ns_resp.status_code == 200
    namespaces = ns_resp.json()
    assert "alpha" in namespaces
    assert "beta" in namespaces

    del_resp = await cp_client.delete(f"/v1/vault/items/{item_a['id']}")
    assert del_resp.status_code == 200

    get_resp = await cp_client.get(f"/v1/vault/items/{item_a['id']}")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_list_tasks_filtered_by_orchestration_id(cp_client):
    await cp_client.post(
        "/v1/tasks",
        json={
            "bot_id": "bot-a",
            "payload": {"instruction": "a"},
            "metadata": {"source": "chat_assign", "orchestration_id": "orch-1"},
        },
    )
    await cp_client.post(
        "/v1/tasks",
        json={
            "bot_id": "bot-b",
            "payload": {"instruction": "b"},
            "metadata": {"source": "chat_assign", "orchestration_id": "orch-2"},
        },
    )

    resp = await cp_client.get("/v1/tasks?orchestration_id=orch-1")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    assert all((r.get("metadata") or {}).get("orchestration_id") == "orch-1" for r in rows)


@pytest.mark.anyio
async def test_cancel_orchestration_endpoint_returns_task_scope(cp_client):
    create = await cp_client.post(
        "/v1/tasks",
        json={
            "bot_id": "bot-a",
            "payload": {"instruction": "cancel"},
            "metadata": {"source": "chat_assign", "orchestration_id": "orch-api-cancel"},
        },
    )
    assert create.status_code == 200

    response = await cp_client.post(
        "/v1/tasks/orchestrations/orch-api-cancel/cancel",
        json={"reason": "operator_test"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["orchestration_id"] == "orch-api-cancel"
    assert result["reason"] == "operator_test"
    assert result["task_count"] == 1


@pytest.mark.anyio
async def test_cancel_task_endpoint_preserves_reason(cp_client, cp_app):
    from datetime import datetime, timezone

    from shared.models import Task

    now = datetime.now(timezone.utc).isoformat()
    seeded = Task(
        id="task-cancel-reason",
        bot_id="bot-cancel-reason",
        payload={"instruction": "cancel"},
        status="queued",
        created_at=now,
        updated_at=now,
    )
    async with cp_app.state.task_manager._lock:
        cp_app.state.task_manager._tasks[seeded.id] = seeded

    response = await cp_client.post(
        f"/v1/tasks/{seeded.id}/cancel",
        json={"reason": "work_overview_stop"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "cancelled"
    assert result["error"]["details"]["reason"] == "work_overview_stop"


@pytest.mark.anyio
async def test_work_dispatch_hold_endpoints_set_list_and_release_scope(cp_client):
    set_resp = await cp_client.post(
        "/v1/tasks/work-dispatch-holds",
        json={
            "project_id": "globeiq",
            "manager_id": "manager-a",
            "reason": "operator checkpoint",
            "operator_id": "admin@test.com",
        },
    )
    assert set_resp.status_code == 200
    set_data = set_resp.json()
    assert set_data["status"] == "held"
    assert set_data["hold"]["id"] == "globeiq::manager-a"
    assert set_data["hold"]["reason"] == "operator checkpoint"

    list_resp = await cp_client.get("/v1/tasks/work-dispatch-holds")
    assert list_resp.status_code == 200
    holds = list_resp.json()["holds"]
    assert any(hold["id"] == "globeiq::manager-a" for hold in holds)

    release_resp = await cp_client.post(
        "/v1/tasks/work-dispatch-holds/release",
        json={"project_id": "globeiq", "manager_id": "manager-a", "operator_id": "admin@test.com"},
    )
    assert release_resp.status_code == 200
    assert release_resp.json()["status"] == "released"


@pytest.mark.anyio
async def test_bot_runs_and_artifacts_endpoints_expose_task_history(cp_client):
    await cp_client.post(
        "/v1/bots",
        json={"id": "bot-history", "name": "Bot History", "role": "assistant", "backends": []},
    )
    await cp_client.post(
        "/v1/tasks",
        json={"bot_id": "bot-history", "payload": {"instruction": "hello"}},
    )

    for _ in range(30):
        runs = await cp_client.get("/v1/bots/bot-history/runs")
        if runs.status_code == 200 and runs.json():
            first = runs.json()[0]
            if first["status"] in {"completed", "failed"}:
                break
        await asyncio.sleep(0.1)

    runs = await cp_client.get("/v1/bots/bot-history/runs")
    assert runs.status_code == 200
    run_rows = runs.json()
    assert len(run_rows) >= 1
    assert run_rows[0]["task_id"]

    artifacts = await cp_client.get("/v1/bots/bot-history/artifacts")
    assert artifacts.status_code == 200
    artifact_rows = artifacts.json()
    labels = {row["label"] for row in artifact_rows}
    assert "Task Payload" in labels


@pytest.mark.anyio
async def test_project_github_pat_connect_status_disconnect(cp_client):
    create_resp = await cp_client.post(
        "/v1/projects",
        json={"id": "gh-proj", "name": "GitHub Project", "mode": "isolated"},
    )
    assert create_resp.status_code == 200

    connect_resp = await cp_client.post(
        "/v1/projects/gh-proj/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )
    assert connect_resp.status_code == 200
    assert connect_resp.json()["status"] == "connected"

    status_resp = await cp_client.get("/v1/projects/gh-proj/github/status")
    assert status_resp.status_code == 200
    status = status_resp.json()
    assert status["connected"] is True
    assert status["repo_full_name"] == "owner/repo"

    disconnect_resp = await cp_client.delete("/v1/projects/gh-proj/github/pat")
    assert disconnect_resp.status_code == 200

    status_after = await cp_client.get("/v1/projects/gh-proj/github/status")
    assert status_after.status_code == 200
    assert status_after.json()["connected"] is False


@pytest.mark.anyio
async def test_project_github_status_validate_reports_ingest_permission_failures(cp_client, monkeypatch):
    from control_plane.api import projects as projects_api

    create_resp = await cp_client.post(
        "/v1/projects",
        json={"id": "gh-proj-validate", "name": "GitHub Validate Project", "mode": "isolated"},
    )
    assert create_resp.status_code == 200

    connect_resp = await cp_client.post(
        "/v1/projects/gh-proj-validate/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )
    assert connect_resp.status_code == 200

    async def _fake_identity(token: str, repo_full_name: str | None = None):
        return {"user_login": "octocat", "user_id": 1, "repo": {"full_name": repo_full_name}}

    async def _fake_ingest_validation(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": False,
            "repo_full_name": repo_full_name,
            "default_branch": "main",
            "checks": [
                {
                    "name": "issues",
                    "method": "GET",
                    "endpoint": f"/repos/{repo_full_name}/issues",
                    "status_code": 401,
                    "ok": False,
                    "detail": "Bad credentials",
                }
            ],
            "error": "missing required ingest access on: issues",
        }

    monkeypatch.setattr(projects_api, "_fetch_github_identity", _fake_identity)
    monkeypatch.setattr(projects_api, "_validate_github_ingest_access", _fake_ingest_validation)

    status_resp = await cp_client.get("/v1/projects/gh-proj-validate/github/status?validate=true")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["connected"] is True
    assert body["validated"] is False
    assert body["ingest_validated"] is False
    assert "missing required ingest access" in str(body.get("validation_error") or "").lower()
    ingest_validation = body.get("ingest_validation") or {}
    assert ingest_validation.get("ok") is False
    checks = ingest_validation.get("checks") or []
    assert checks and checks[0].get("status_code") == 401


@pytest.mark.anyio
async def test_project_github_pat_connect_rejects_when_ingest_validation_fails(cp_client, monkeypatch):
    from control_plane.api import projects as projects_api

    create_resp = await cp_client.post(
        "/v1/projects",
        json={"id": "gh-proj-connect-fail", "name": "GitHub Connect Fail", "mode": "isolated"},
    )
    assert create_resp.status_code == 200

    async def _fake_identity(token: str, repo_full_name: str | None = None):
        return {"user_login": "octocat", "user_id": 1, "repo": {"full_name": repo_full_name}}

    async def _fake_ingest_validation(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": False,
            "repo_full_name": repo_full_name,
            "default_branch": "main",
            "checks": [],
            "error": "missing required ingest access on: commits",
        }

    monkeypatch.setattr(projects_api, "_fetch_github_identity", _fake_identity)
    monkeypatch.setattr(projects_api, "_validate_github_ingest_access", _fake_ingest_validation)

    connect_resp = await cp_client.post(
        "/v1/projects/gh-proj-connect-fail/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": True,
        },
    )
    assert connect_resp.status_code == 400
    assert "missing required ingest access" in str(connect_resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_github_pat_connect_normalizes_prefixed_token(cp_client, monkeypatch):
    from control_plane.api import projects as projects_api

    create_resp = await cp_client.post(
        "/v1/projects",
        json={"id": "gh-proj-normalize", "name": "GitHub Normalize", "mode": "isolated"},
    )
    assert create_resp.status_code == 200

    captured: dict[str, str] = {}

    async def _fake_identity(token: str, repo_full_name: str | None = None):
        captured["token"] = token
        return {"user_login": "octocat", "user_id": 1}

    monkeypatch.setattr(projects_api, "_fetch_github_identity", _fake_identity)

    connect_resp = await cp_client.post(
        "/v1/projects/gh-proj-normalize/github/pat",
        json={
            "token": "Bearer ghp_example_token_for_tests_only",
            "repo_full_name": None,
            "validate": True,
        },
    )
    assert connect_resp.status_code == 200
    assert captured.get("token") == "ghp_example_token_for_tests_only"


@pytest.mark.anyio
async def test_project_github_context_sync_fails_fast_on_auth_validation(cp_client, monkeypatch):
    from control_plane.api import projects as projects_api

    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-sync-auth-fail", "name": "GitHub Sync Auth Fail", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-sync-auth-fail/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    async def _fake_ingest_validation(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": False,
            "repo_full_name": repo_full_name,
            "default_branch": branch or "main",
            "checks": [
                {
                    "name": "repo_metadata",
                    "method": "GET",
                    "endpoint": f"/repos/{repo_full_name}",
                    "status_code": 401,
                    "ok": False,
                    "detail": "Bad credentials",
                }
            ],
            "error": "missing required ingest access on: repo_metadata",
        }

    async def _never_fetch_files(*args, **kwargs):
        raise AssertionError("repo file fetch should not run after failed ingest auth validation")

    monkeypatch.setattr(projects_api, "_validate_github_ingest_access", _fake_ingest_validation)
    monkeypatch.setattr(projects_api, "_fetch_repo_context_files", _never_fetch_files)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-sync-auth-fail/github/context/sync",
        json={"sync_mode": "update"},
    )
    assert sync_resp.status_code == 200
    for _ in range(30):
        status_resp = await cp_client.get("/v1/projects/gh-sync-auth-fail/github/context/sync")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body.get("status") == "failed":
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "failed"
    assert "missing required ingest access" in str(body.get("error") or "").lower()
    assert "/repos/owner/repo" in str(body.get("error") or "")
    assert "status=401" in str(body.get("error") or "")


@pytest.mark.anyio
async def test_project_cloud_context_policy_update_and_get(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-policy", "name": "Policy Project", "mode": "isolated"},
    )

    update = await cp_client.put(
        "/v1/projects/p-policy/cloud-context-policy",
        json={
            "provider_policies": {"openai": "redact", "claude": "allow", "gemini": "block"},
            "bot_overrides": {
                "bot-a": {"openai": "block", "claude": "allow"},
            },
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["provider_policies"]["openai"] == "redact"
    assert body["bot_overrides"]["bot-a"]["openai"] == "block"

    get_resp = await cp_client.get("/v1/projects/p-policy/cloud-context-policy")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["provider_policies"]["gemini"] == "block"


@pytest.mark.anyio
async def test_project_cloud_context_policy_rejects_invalid_bot_allow_under_redact(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-policy-bad", "name": "Policy Project Bad", "mode": "isolated"},
    )
    resp = await cp_client.put(
        "/v1/projects/p-policy-bad/cloud-context-policy",
        json={
            "provider_policies": {"openai": "redact"},
            "bot_overrides": {"bot-a": {"openai": "allow"}},
        },
    )
    assert resp.status_code == 400
    assert "not allowed" in (resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_chat_tool_access_update_and_get(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-chat-tools", "name": "Project Chat Tools", "mode": "isolated"},
    )

    update = await cp_client.put(
        "/v1/projects/p-chat-tools/chat-tool-access",
        json={
            "enabled": True,
            "filesystem": True,
            "repo_search": True,
            "workspace_root": "C:\\repo\\workspace",
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["enabled"] is True
    assert body["filesystem"] is True
    assert body["repo_search"] is True
    assert body["workspace_root"] == "C:\\repo\\workspace"

    get_resp = await cp_client.get("/v1/projects/p-chat-tools/chat-tool-access")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["enabled"] is True
    assert got["filesystem"] is True
    assert got["repo_search"] is True
    assert got["workspace_root"] == "C:\\repo\\workspace"


@pytest.mark.anyio
async def test_project_chat_tool_access_rejects_too_long_workspace_root(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-chat-tools-bad", "name": "Project Chat Tools Bad", "mode": "isolated"},
    )
    root = "x" * 2000
    update = await cp_client.put(
        "/v1/projects/p-chat-tools-bad/chat-tool-access",
        json={
            "enabled": True,
            "filesystem": True,
            "repo_search": True,
            "workspace_root": root,
        },
    )
    assert update.status_code == 400
    assert "workspace_root" in (update.json().get("detail") or "")


@pytest.mark.anyio
async def test_project_repo_workspace_update_and_get(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-workspace", "name": "Repo Workspace Project", "mode": "isolated"},
    )
    update = await cp_client.put(
        "/v1/projects/p-repo-workspace/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "clone_url": "https://github.com/example/repo.git",
            "default_branch": "main",
            "allow_push": True,
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["enabled"] is True
    assert body["managed_path_mode"] is True
    assert body["root_path"] is None
    assert body["clone_url"] == "https://github.com/example/repo.git"
    assert body["default_branch"] == "main"
    assert body["allow_push"] is True
    assert body["allow_command_execution"] is True

    get_resp = await cp_client.get("/v1/projects/p-repo-workspace/repo/workspace")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["enabled"] is True
    assert got["managed_path_mode"] is True
    assert got["root_path"] is None
    assert got["allow_push"] is True
    assert got["allow_command_execution"] is True


def test_extract_project_repo_workspace_uses_chat_workspace_root_fallback():
    from control_plane.api.projects import _extract_project_repo_workspace
    from shared.models import Project

    workspace_root = "C:\\repo\\workspace"
    project = Project(
        id="p-repo-fallback",
        name="Repo Fallback",
        mode="isolated",
        settings_overrides={
            "chat_tool_access": {
                "enabled": True,
                "filesystem": True,
                "repo_search": True,
                "workspace_root": workspace_root,
            }
        },
    )

    cfg = _extract_project_repo_workspace(project)
    assert cfg["_raw_root_path"] == workspace_root
    assert cfg["root_path"] is None


def test_resolve_repo_workspace_root_managed_can_disable_raw_fallback(tmp_path, monkeypatch):
    from control_plane.api.projects import _resolve_repo_workspace_root

    base_root = tmp_path / "repo-workspaces"
    raw_root = tmp_path / "chat-workspace"
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))

    cfg = {
        "enabled": True,
        "managed_path_mode": True,
        "_raw_root_path": str(raw_root),
    }

    managed_root = _resolve_repo_workspace_root(
        "p-managed-strict",
        cfg,
        require_enabled=True,
        allow_raw_fallback=False,
    )
    fallback_root = _resolve_repo_workspace_root(
        "p-managed-strict",
        cfg,
        require_enabled=True,
        allow_raw_fallback=True,
    )

    assert managed_root == (base_root / "p-managed-strict" / "repo").resolve(strict=False)
    assert fallback_root == raw_root.resolve(strict=False)


@pytest.mark.anyio
async def test_ensure_orchestration_temp_workspace_uses_managed_root_not_chat_workspace_root(tmp_path, monkeypatch):
    from control_plane.api.projects import _ensure_orchestration_temp_workspace
    from shared.models import Project

    base_root = tmp_path / "repo-workspaces"
    chat_workspace_root = tmp_path / "chat-workspace"
    chat_workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))

    project = Project(
        id="p-inline-managed",
        name="Inline Managed",
        mode="isolated",
        settings_overrides={
            "chat_tool_access": {
                "enabled": True,
                "filesystem": True,
                "repo_search": True,
                "workspace_root": str(chat_workspace_root),
            },
            "repo_workspace": {
                "enabled": True,
                "managed_path_mode": True,
            },
        },
    )

    class _ProjectRegistry:
        async def get(self, project_id):
            assert project_id == "p-inline-managed"
            return project

    class _WorkspaceStore:
        async def get(self, *, project_id, orchestration_id):
            return None

        async def register(self, **kwargs):
            return {
                **kwargs,
                "lifecycle_state": "retained",
                "path_exists": True,
                "temp_root": kwargs.get("temp_root"),
            }

    seen = {"root": None}
    temp_root = tmp_path / "temp-workspace"
    temp_root.mkdir(parents=True, exist_ok=True)

    async def _fake_snapshot(*, root, cfg):
        seen["root"] = root
        return {"is_repo": True}

    async def _fake_prepare_temp_workspace(*, project_id, root, ref=None):
        assert project_id == "p-inline-managed"
        assert ref is None
        return {"mode": "copy", "path": temp_root, "setup_result": None}

    monkeypatch.setattr("control_plane.api.projects._repo_status_snapshot", _fake_snapshot)
    monkeypatch.setattr("control_plane.api.projects._prepare_temp_workspace", _fake_prepare_temp_workspace)

    entry = await _ensure_orchestration_temp_workspace(
        project_id="p-inline-managed",
        orchestration_id="orch-1",
        project_registry=_ProjectRegistry(),
        workspace_store=_WorkspaceStore(),
        strict=True,
        key_vault=None,
    )

    expected_root = (base_root / "p-inline-managed" / "repo").resolve(strict=False)
    assert seen["root"] == expected_root
    assert entry is not None
    assert entry["source_root"] == str(expected_root)
    assert entry["temp_root"] == str(temp_root)


@pytest.mark.anyio
async def test_repo_status_snapshot_treats_dot_git_marker_as_repo_when_rev_parse_fails(tmp_path, monkeypatch):
    from control_plane.api.projects import _repo_status_snapshot

    root = tmp_path / "repo-with-git-marker"
    (root / ".git").mkdir(parents=True, exist_ok=True)

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {"ok": False, "stdout": "", "stderr": "dubious ownership", "command": args}

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    snapshot = await _repo_status_snapshot(
        root=root,
        cfg={
            "enabled": True,
            "managed_path_mode": True,
            "allow_push": False,
            "allow_command_execution": False,
        },
    )

    assert snapshot["workspace_exists"] is True
    assert snapshot["is_repo"] is True


@pytest.mark.anyio
async def test_repo_status_snapshot_treats_parent_dot_git_marker_as_repo_when_rev_parse_fails(tmp_path, monkeypatch):
    from control_plane.api.projects import _repo_status_snapshot

    repo_root = tmp_path / "repo-root"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    root = repo_root / "nested" / "workspace"
    root.mkdir(parents=True, exist_ok=True)

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {"ok": False, "stdout": "", "stderr": "dubious ownership", "command": args}

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    snapshot = await _repo_status_snapshot(
        root=root,
        cfg={
            "enabled": True,
            "managed_path_mode": True,
            "allow_push": False,
            "allow_command_execution": False,
        },
    )

    assert snapshot["workspace_exists"] is True
    assert snapshot["is_repo"] is True


@pytest.mark.anyio
async def test_is_git_repository_treats_parent_git_marker_as_repo(tmp_path, monkeypatch):
    from control_plane.api.projects import _is_git_repository

    repo_root = tmp_path / "repo-root"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    nested = repo_root / "src" / "module"
    nested.mkdir(parents=True, exist_ok=True)

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {"ok": False, "stdout": "", "stderr": "dubious ownership", "command": args}

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    assert await _is_git_repository(nested) is True


@pytest.mark.anyio
async def test_run_repo_command_injects_safe_directory_for_git(tmp_path, monkeypatch):
    from control_plane.api.projects import _run_repo_command

    repo_root = tmp_path / "repo-root"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    nested = repo_root / "src" / "module"
    nested.mkdir(parents=True, exist_ok=True)

    captured = {}

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr("control_plane.api.projects.run_repo_command", _fake_run)

    await _run_repo_command(["git", "status", "--porcelain"], cwd=nested, timeout_seconds=10)

    assert captured["args"][0] == "git"
    assert captured["args"][1] == "-c"
    assert str(captured["args"][2]) == f"safe.directory={repo_root.resolve(strict=False)}"
    assert captured["args"][3:] == ["status", "--porcelain"]


@pytest.mark.anyio
async def test_run_repo_command_does_not_duplicate_safe_directory(tmp_path, monkeypatch):
    from control_plane.api.projects import _run_repo_command

    repo_root = tmp_path / "repo-root"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)

    captured = {}

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        captured["args"] = list(args)
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr("control_plane.api.projects.run_repo_command", _fake_run)

    await _run_repo_command(
        ["git", "-c", f"safe.directory={repo_root}", "status"],
        cwd=repo_root,
        timeout_seconds=10,
    )

    assert captured["args"] == ["git", "-c", f"safe.directory={repo_root}", "status"]


@pytest.mark.anyio
async def test_project_repo_workspace_redacts_and_preserves_clone_url_when_omitted(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-workspace-redact", "name": "Repo Workspace Redact", "mode": "isolated"},
    )
    update = await cp_client.put(
        "/v1/projects/p-repo-workspace-redact/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "clone_url": "https://octocat:ghp_super_secret_token@github.com/example/private-repo.git",
            "default_branch": "main",
            "allow_push": False,
            "allow_command_execution": False,
        },
    )
    assert update.status_code == 200
    assert update.json()["clone_url"] == "https://octocat:***@github.com/example/private-repo.git"

    update_without_clone = await cp_client.put(
        "/v1/projects/p-repo-workspace-redact/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "default_branch": "main",
            "allow_push": True,
            "allow_command_execution": True,
        },
    )
    assert update_without_clone.status_code == 200
    assert update_without_clone.json()["clone_url"] == "https://octocat:***@github.com/example/private-repo.git"

    get_resp = await cp_client.get("/v1/projects/p-repo-workspace-redact/repo/workspace")
    assert get_resp.status_code == 200
    assert get_resp.json()["clone_url"] == "https://octocat:***@github.com/example/private-repo.git"


@pytest.mark.anyio
async def test_project_repo_workspace_clone_resets_stale_managed_workspace(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-clone-reset", "name": "Repo Clone Reset", "mode": "isolated"},
    )
    base_root = tmp_path / "repo-workspaces"
    root = base_root / "p-repo-clone-reset" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    stale_file = root / "stale.txt"
    stale_file.write_text("stale", encoding="utf-8")
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))

    update = await cp_client.put(
        "/v1/projects/p-repo-clone-reset/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "clone_url": "https://github.com/example/repo.git",
            "allow_push": False,
            "allow_command_execution": False,
        },
    )
    assert update.status_code == 200

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": args,
        }

    async def _fake_snapshot(*, root, cfg):
        return {
            "enabled": True,
            "managed_path_mode": True,
            "workspace_binding": "managed",
            "root_path": None,
            "clone_url": cfg.get("clone_url"),
            "default_branch": cfg.get("default_branch"),
            "allow_push": False,
            "allow_command_execution": False,
            "workspace_exists": True,
            "is_repo": True,
            "branch": "Main",
            "clean": True,
            "porcelain": [],
            "remotes": [],
            "last_commit": {},
        }

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)
    monkeypatch.setattr("control_plane.api.projects._repo_status_snapshot", _fake_snapshot)

    clone_resp = await cp_client.post("/v1/projects/p-repo-clone-reset/repo/workspace/clone", json={})
    assert clone_resp.status_code == 200
    assert stale_file.exists() is False


@pytest.mark.anyio
async def test_project_repo_workspace_rejects_relative_root_path(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-workspace-bad", "name": "Repo Workspace Bad", "mode": "isolated"},
    )
    update = await cp_client.put(
        "/v1/projects/p-repo-workspace-bad/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": False,
            "root_path": "relative/path",
        },
    )
    assert update.status_code == 400
    assert "absolute path" in (update.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_repo_workspace_run_command_requires_policy(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-run-policy", "name": "Repo Run Policy", "mode": "isolated"},
    )
    root = tmp_path / "repo-workspaces" / "p-repo-run-policy" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(tmp_path / "repo-workspaces"))
    update = await cp_client.put(
        "/v1/projects/p-repo-run-policy/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "allow_command_execution": False,
        },
    )
    assert update.status_code == 200

    run_resp = await cp_client.post(
        "/v1/projects/p-repo-run-policy/repo/workspace/run",
        json={"command": ["py", "-m", "pytest", "-q"]},
    )
    assert run_resp.status_code == 403
    assert "disabled" in (run_resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_repo_workspace_run_command_executes_allowed_command(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-run", "name": "Repo Run", "mode": "isolated"},
    )
    root = tmp_path / "repo-workspaces" / "p-repo-run" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(tmp_path / "repo-workspaces"))
    update = await cp_client.put(
        "/v1/projects/p-repo-run/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200

    captured = {}

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        captured["args"] = args
        captured["cwd"] = str(cwd)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "tests passed",
            "stderr": "",
            "command": args,
            "timeout_seconds": timeout_seconds or 120,
        }

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    run_resp = await cp_client.post(
        "/v1/projects/p-repo-run/repo/workspace/run",
        json={"command": ["py", "-m", "pytest", "-q"], "timeout_seconds": 90},
    )
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body["status"] == "ok"
    assert body["result"]["ok"] is True
    assert captured["args"] == ["py", "-m", "pytest", "-q"]
    assert captured["cwd"] == str(root.resolve())
    assert captured["timeout_seconds"] == 90


@pytest.mark.anyio
async def test_project_repo_workspace_status_lists_untracked_files_individually(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-status-all", "name": "Repo Status All", "mode": "isolated"},
    )
    root = tmp_path / "repo-workspaces" / "p-repo-status-all" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(tmp_path / "repo-workspaces"))
    update = await cp_client.put(
        "/v1/projects/p-repo-status-all/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "default_branch": "main",
        },
    )
    assert update.status_code == 200

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        if args == ["git", "rev-parse", "--is-inside-work-tree"]:
            return {"ok": True, "stdout": "true\n", "stderr": "", "command": args}
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return {"ok": True, "stdout": "main\n", "stderr": "", "command": args}
        if args == ["git", "status", "--porcelain", "-b", "--untracked-files=all"]:
            return {
                "ok": True,
                "stdout": "## main...origin/main\n?? src/demo.py\n?? tests/test_demo.py\n",
                "stderr": "",
                "command": args,
            }
        if args == ["git", "remote", "-v"]:
            return {"ok": True, "stdout": "", "stderr": "", "command": args}
        if args == ["git", "log", "-1", "--pretty=format:%H%n%an%n%ad%n%s"]:
            return {"ok": True, "stdout": "", "stderr": "", "command": args}
        raise AssertionError(f"Unexpected git command: {args}")

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    resp = await cp_client.get("/v1/projects/p-repo-status-all/repo/workspace/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_repo"] is True
    assert body["porcelain"] == ["## main...origin/main", "?? src/demo.py", "?? tests/test_demo.py"]


@pytest.mark.anyio
async def test_project_repo_workspace_discard_untracked_removes_generated_files(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-discard", "name": "Repo Discard", "mode": "isolated"},
    )
    root = tmp_path / "repo-discard"
    generated_src = root / "src" / "demo.py"
    generated_test = root / "tests" / "test_demo.py"
    generated_src.parent.mkdir(parents=True, exist_ok=True)
    generated_test.parent.mkdir(parents=True, exist_ok=True)
    generated_src.write_text("print('demo')\n", encoding="utf-8")
    generated_test.write_text("def test_demo():\n    assert True\n", encoding="utf-8")

    update = await cp_client.put(
        "/v1/projects/p-repo-discard/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": False,
            "root_path": str(root),
        },
    )
    assert update.status_code == 200

    calls = {"count": 0}

    async def _fake_snapshot(*, root, cfg):
        calls["count"] += 1
        return {
            "enabled": True,
            "managed_path_mode": False,
            "workspace_binding": "custom",
            "root_path": None,
            "clone_url": None,
            "default_branch": "main",
            "allow_push": False,
            "allow_command_execution": False,
            "workspace_exists": True,
            "is_repo": True,
            "branch": "main",
            "clean": calls["count"] > 1,
            "porcelain": [] if calls["count"] > 1 else ["## main", "?? src/demo.py", "?? tests/test_demo.py"],
            "remotes": [],
            "last_commit": {},
        }

    monkeypatch.setattr("control_plane.api.projects._repo_status_snapshot", _fake_snapshot)

    resp = await cp_client.post("/v1/projects/p-repo-discard/repo/workspace/discard-untracked", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_paths"] == ["src/demo.py", "tests/test_demo.py"]
    assert generated_src.exists() is False
    assert generated_test.exists() is False
    assert body["workspace"]["clean"] is True


@pytest.mark.anyio
async def test_project_repo_workspace_discard_untracked_decodes_quoted_git_paths(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-discard-quoted", "name": "Repo Discard Quoted", "mode": "isolated"},
    )
    root = tmp_path / "repo-discard-quoted"
    generated = root / "All coordinates are normalized to a 0-1000 viewBox space for scalability."
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("junk\n", encoding="utf-8")

    update = await cp_client.put(
        "/v1/projects/p-repo-discard-quoted/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": False,
            "root_path": str(root),
        },
    )
    assert update.status_code == 200

    calls = {"count": 0}

    async def _fake_snapshot(*, root, cfg):
        calls["count"] += 1
        return {
            "enabled": True,
            "managed_path_mode": False,
            "workspace_binding": "custom",
            "root_path": None,
            "clone_url": None,
            "default_branch": "main",
            "allow_push": False,
            "allow_command_execution": False,
            "workspace_exists": True,
            "is_repo": True,
            "branch": "main",
            "clean": calls["count"] > 1,
            "porcelain": [] if calls["count"] > 1 else ['## main', '?? "All coordinates are normalized to a 0-1000 viewBox space for scalability."'],
            "remotes": [],
            "last_commit": {},
        }

    monkeypatch.setattr("control_plane.api.projects._repo_status_snapshot", _fake_snapshot)

    resp = await cp_client.post("/v1/projects/p-repo-discard-quoted/repo/workspace/discard-untracked", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_paths"] == ["All coordinates are normalized to a 0-1000 viewBox space for scalability."]
    assert generated.exists() is False


@pytest.mark.anyio
async def test_project_repo_workspace_discard_untracked_removes_symlink_path(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-discard-symlink", "name": "Repo Discard Symlink", "mode": "isolated"},
    )
    root = tmp_path / "repo-discard-symlink"
    venv_dir = root / ".nexusai_venv"
    venv_dir.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external-lib"
    external.mkdir(parents=True, exist_ok=True)
    symlink_path = venv_dir / "lib64"
    try:
        symlink_path.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this test environment")

    update = await cp_client.put(
        "/v1/projects/p-repo-discard-symlink/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": False,
            "root_path": str(root),
        },
    )
    assert update.status_code == 200

    calls = {"count": 0}

    async def _fake_snapshot(*, root, cfg):
        calls["count"] += 1
        return {
            "enabled": True,
            "managed_path_mode": False,
            "workspace_binding": "custom",
            "root_path": None,
            "clone_url": None,
            "default_branch": "main",
            "allow_push": False,
            "allow_command_execution": False,
            "workspace_exists": True,
            "is_repo": True,
            "branch": "main",
            "clean": calls["count"] > 1,
            "porcelain": [] if calls["count"] > 1 else ["## main", "?? .nexusai_venv/lib64"],
            "remotes": [],
            "last_commit": {},
        }

    monkeypatch.setattr("control_plane.api.projects._repo_status_snapshot", _fake_snapshot)

    resp = await cp_client.post("/v1/projects/p-repo-discard-symlink/repo/workspace/discard-untracked", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_paths"] == [".nexusai_venv/lib64"]
    assert symlink_path.exists() is False
    assert external.exists() is True


@pytest.mark.anyio
async def test_project_repo_workspace_run_command_creates_missing_managed_workspace(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-run-create-root", "name": "Repo Run Create Root", "mode": "isolated"},
    )
    base_root = tmp_path / "repo-workspaces"
    root = base_root / "p-repo-run-create-root" / "repo"
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    update = await cp_client.put(
        "/v1/projects/p-repo-run-create-root/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200
    assert root.exists() is False

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "git version 2.47.3",
            "stderr": "",
            "command": args,
            "timeout_seconds": timeout_seconds or 120,
        }

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)
    run_resp = await cp_client.post(
        "/v1/projects/p-repo-run-create-root/repo/workspace/run",
        json={"command": ["git", "--version"]},
    )
    assert run_resp.status_code == 200
    assert root.exists() is True
    assert run_resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_project_repo_workspace_push_requires_allow_push(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-push", "name": "Repo Push", "mode": "isolated"},
    )
    root = tmp_path / "repo-workspaces" / "p-repo-push" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(tmp_path / "repo-workspaces"))
    update = await cp_client.put(
        "/v1/projects/p-repo-push/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "allow_push": False,
        },
    )
    assert update.status_code == 200

    push_resp = await cp_client.post(
        "/v1/projects/p-repo-push/repo/workspace/push",
        json={"remote": "origin", "branch": "main"},
    )
    assert push_resp.status_code == 403
    assert "disabled" in (push_resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_repo_workspace_run_records_usage_history(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-usage", "name": "Repo Usage", "mode": "isolated"},
    )
    root = tmp_path / "repo-workspaces" / "p-repo-usage" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(tmp_path / "repo-workspaces"))
    update = await cp_client.put(
        "/v1/projects/p-repo-usage/repo/workspace",
        json={
            "enabled": True,
            "managed_path_mode": True,
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "command": args,
            "timeout_seconds": timeout_seconds or 120,
            "started_at": "2026-03-11T00:00:00+00:00",
            "finished_at": "2026-03-11T00:00:03+00:00",
            "duration_ms": 3000,
            "resource_usage": {
                "wall_time_ms": 3000,
                "cpu_user_seconds": 1.2,
                "cpu_system_seconds": 0.3,
                "peak_rss_bytes": 12345678,
                "peak_vms_bytes": 22334455,
                "io_read_bytes": 1024,
                "io_write_bytes": 2048,
                "sample_count": 10,
            },
        }

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    run_resp = await cp_client.post(
        "/v1/projects/p-repo-usage/repo/workspace/run",
        json={"command": ["py", "-m", "pytest", "-q"]},
    )
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == "ok"
    assert run_resp.json()["usage"]["peak_rss_bytes"] == 12345678

    runs_resp = await cp_client.get("/v1/projects/p-repo-usage/repo/workspace/runs?limit=20")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["action"] == "run"
    assert runs[0]["status"] == "ok"
    assert (runs[0]["metrics"] or {}).get("peak_rss_bytes") == 12345678

    summary_resp = await cp_client.get("/v1/projects/p-repo-usage/repo/workspace/runs/summary?since_hours=4000")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()
    totals = summary["totals"]
    assert int(totals["total_runs"]) >= 1
    assert int(totals["success_runs"]) >= 1
    assert int(totals["peak_rss_bytes_max"]) >= 12345678


@pytest.mark.anyio
async def test_project_github_webhook_ingestion_and_list(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-hook", "name": "GitHub Hook Project", "mode": "isolated"},
    )
    set_secret = await cp_client.post(
        "/v1/projects/gh-hook/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    assert set_secret.status_code == 200

    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 42},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    ingest = await cp_client.post(
        "/v1/projects/gh-hook/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
        },
    )
    assert ingest.status_code == 200
    assert ingest.json()["status"] == "accepted"

    events = await cp_client.get("/v1/projects/gh-hook/github/webhook/events")
    assert events.status_code == 200
    rows = events.json()["events"]
    assert len(rows) >= 1
    assert rows[0]["event_type"] == "pull_request"
    assert rows[0]["action"] == "opened"


@pytest.mark.anyio
async def test_project_github_webhook_rejects_bad_signature(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-hook-bad", "name": "GitHub Hook Bad", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-hook-bad/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    ingest = await cp_client.post(
        "/v1/projects/gh-hook-bad/github/webhook",
        json={"repository": {"full_name": "owner/repo"}},
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-bad-sig",
        },
    )
    assert ingest.status_code == 401


@pytest.mark.anyio
async def test_project_github_context_sync_ingests_vault_items(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-sync", "name": "GitHub Sync", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-sync/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    async def _fake_fetch(token, repo_full_name, branch):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "files": [
                {"path": "README.md", "content": "# test", "size": 6, "sha": "abc"},
                {"path": "src/app.py", "content": "print('ok')", "size": 11, "sha": "def"},
            ],
        }

    async def _fake_validate_ingest(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": True,
            "repo_full_name": repo_full_name,
            "default_branch": branch or "main",
            "checks": [],
        }

    monkeypatch.setattr("control_plane.api.projects._validate_github_ingest_access", _fake_validate_ingest)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-sync/github/context/sync",
        json={"sync_mode": "full"},
    )
    assert sync_resp.status_code == 200
    for _ in range(30):
        status_resp = await cp_client.get("/v1/projects/gh-sync/github/context/sync")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "completed"
    assert body["ingested_count"] == 2

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-sync&limit=20")
    assert items_resp.status_code == 200
    assert len(items_resp.json()) >= 2


@pytest.mark.anyio
async def test_project_github_context_sync_can_ingest_commits_prs_and_issues(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-full-sync", "name": "GitHub Full Sync", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-full-sync/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    async def _fake_fetch_files(token, repo_full_name, branch):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "files": [
                {"path": "README.md", "content": "# test", "size": 6, "sha": "abc"},
            ],
        }

    async def _fake_fetch_commits(token, repo_full_name, branch, since=None):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "commits": [
                {"sha": "deadbeef", "html_url": "https://example/commit/deadbeef", "message": "Initial import", "author_name": "Jake", "authored_at": "2026-03-07T00:00:00Z"},
            ],
        }

    async def _fake_fetch_pulls(token, repo_full_name, include_conversations, updated_after=None):
        return [
            {
                "number": 12,
                "title": "Add orchestration",
                "body": "Implements chained bots",
                "state": "open",
                "draft": False,
                "html_url": "https://example/pull/12",
                "user": "octocat",
                "created_at": "2026-03-06T00:00:00Z",
                "updated_at": "2026-03-07T00:00:00Z",
                "merged_at": None,
                "base_ref": "main",
                "head_ref": "feature/orchestration",
                "issue_comments": [{"user": "reviewer", "created_at": "2026-03-07T01:00:00Z", "body": "Looks good"}],
                "review_comments": [{"user": "reviewer", "created_at": "2026-03-07T02:00:00Z", "path": "bot.py", "body": "Tighten this"}],
            }
        ]

    async def _fake_fetch_issues(token, repo_full_name, include_conversations, updated_after=None):
        return [
            {
                "number": 8,
                "title": "Ingestion backlog",
                "body": "Need docs and vectors",
                "state": "open",
                "html_url": "https://example/issues/8",
                "user": "octocat",
                "created_at": "2026-03-05T00:00:00Z",
                "updated_at": "2026-03-07T00:00:00Z",
                "comments": [{"user": "teammate", "created_at": "2026-03-07T03:00:00Z", "body": "Agreed"}],
            }
        ]

    async def _fake_validate_ingest(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": True,
            "repo_full_name": repo_full_name,
            "default_branch": branch or "main",
            "checks": [],
        }

    monkeypatch.setattr("control_plane.api.projects._validate_github_ingest_access", _fake_validate_ingest)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch_files)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_commits", _fake_fetch_commits)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_pull_requests", _fake_fetch_pulls)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_issues", _fake_fetch_issues)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-full-sync/github/context/sync",
        json={
            "sync_mode": "full",
        },
    )
    assert sync_resp.status_code == 200
    for _ in range(30):
        status_resp = await cp_client.get("/v1/projects/gh-full-sync/github/context/sync")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "completed"
    assert body["ingested_count"] == 4
    assert body["counts"]["files"] == 1
    assert body["counts"]["commits"] == 1
    assert body["counts"]["pull_requests"] == 1
    assert body["counts"]["issues"] == 1
    assert body["counts"]["conversations"] == 3

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-full-sync&limit=20")
    assert items_resp.status_code == 200
    assert len(items_resp.json()) >= 4


@pytest.mark.anyio
async def test_project_github_context_update_ingests_only_newer_items(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-update", "name": "GitHub Update", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-update/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    file_versions = [
        [{"path": "README.md", "content": "# v1", "size": 4, "sha": "sha-1"}],
        [{"path": "README.md", "content": "# v2", "size": 4, "sha": "sha-2"}],
    ]
    commit_versions = [
        [{"sha": "aaa", "html_url": "https://example/commit/aaa", "message": "one", "author_name": "Jake", "authored_at": "2026-03-07T00:00:00Z"}],
        [{"sha": "bbb", "html_url": "https://example/commit/bbb", "message": "two", "author_name": "Jake", "authored_at": "2026-03-08T00:00:00Z"}],
    ]
    pr_versions = [
        [{"number": 1, "title": "One", "body": "body", "state": "open", "draft": False, "html_url": "https://example/pull/1", "user": "octocat", "created_at": "2026-03-07T00:00:00Z", "updated_at": "2026-03-07T00:00:00Z", "merged_at": None, "base_ref": "main", "head_ref": "feat/one", "issue_comments": [], "review_comments": []}],
        [{"number": 2, "title": "Two", "body": "body", "state": "open", "draft": False, "html_url": "https://example/pull/2", "user": "octocat", "created_at": "2026-03-08T00:00:00Z", "updated_at": "2026-03-08T00:00:00Z", "merged_at": None, "base_ref": "main", "head_ref": "feat/two", "issue_comments": [], "review_comments": []}],
    ]
    issue_versions = [
        [{"number": 8, "title": "Old", "body": "body", "state": "open", "html_url": "https://example/issues/8", "user": "octocat", "created_at": "2026-03-07T00:00:00Z", "updated_at": "2026-03-07T00:00:00Z", "comments": []}],
        [{"number": 9, "title": "New", "body": "body", "state": "open", "html_url": "https://example/issues/9", "user": "octocat", "created_at": "2026-03-08T00:00:00Z", "updated_at": "2026-03-08T00:00:00Z", "comments": []}],
    ]
    state = {"index": 0}

    async def _fake_fetch_files(token, repo_full_name, branch):
        return {"repo_full_name": repo_full_name, "branch": branch or "main", "files": file_versions[state["index"]]}

    async def _fake_fetch_commits(token, repo_full_name, branch, since=None):
        return {"repo_full_name": repo_full_name, "branch": branch or "main", "commits": commit_versions[state["index"]]}

    async def _fake_fetch_pulls(token, repo_full_name, include_conversations, updated_after=None):
        return pr_versions[state["index"]]

    async def _fake_fetch_issues(token, repo_full_name, include_conversations, updated_after=None):
        return issue_versions[state["index"]]

    async def _fake_validate_ingest(token: str, repo_full_name: str, *, branch: str | None = None):
        return {
            "ok": True,
            "repo_full_name": repo_full_name,
            "default_branch": branch or "main",
            "checks": [],
        }

    monkeypatch.setattr("control_plane.api.projects._validate_github_ingest_access", _fake_validate_ingest)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch_files)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_commits", _fake_fetch_commits)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_pull_requests", _fake_fetch_pulls)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_issues", _fake_fetch_issues)

    first = await cp_client.post("/v1/projects/gh-update/github/context/sync", json={"sync_mode": "full"})
    assert first.status_code == 200
    for _ in range(30):
        first_status = await cp_client.get("/v1/projects/gh-update/github/context/sync")
        assert first_status.status_code == 200
        first_body = first_status.json()
        if first_body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert first_body["ingested_count"] == 4

    state["index"] = 1
    second = await cp_client.post("/v1/projects/gh-update/github/context/sync", json={"sync_mode": "update"})
    assert second.status_code == 200
    for _ in range(30):
        second_status = await cp_client.get("/v1/projects/gh-update/github/context/sync")
        assert second_status.status_code == 200
        second_body = second_status.json()
        if second_body.get("status") == "completed" and second_body.get("sync_mode") == "update":
            break
        await asyncio.sleep(0.1)
    assert second_body["ingested_count"] == 4
    assert second_body["sync_mode"] == "update"

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-update&limit=20")
    assert items_resp.status_code == 200
    items = items_resp.json()
    titles = {item["title"] for item in items}
    assert "owner/repo:commit:bbb" in titles
    assert "owner/repo:pr:2" in titles
    assert "owner/repo:issue:9" in titles


@pytest.mark.anyio
async def test_project_github_pr_review_workflow_creates_task(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-pr", "name": "GitHub PR", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-pr/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )
    await cp_client.post(
        "/v1/projects/gh-pr/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    cfg = await cp_client.post(
        "/v1/projects/gh-pr/github/pr-review/config",
        json={"enabled": True, "bot_id": "bot-reviewer"},
    )
    assert cfg.status_code == 200

    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 7,
            "title": "Add auth",
            "body": "Please review",
            "html_url": "https://github.com/owner/repo/pull/7",
            "base": {"ref": "main"},
            "head": {"ref": "feature/auth"},
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    ingest = await cp_client.post(
        "/v1/projects/gh-pr/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-pr-1",
        },
    )
    assert ingest.status_code == 200
    review_task_id = ingest.json().get("review_task_id")
    assert review_task_id

    tasks = await cp_client.get("/v1/tasks")
    assert tasks.status_code == 200
    rows = tasks.json()
    assert any((r.get("payload") or {}).get("source") == "github_pr_review" for r in rows)


@pytest.mark.anyio
async def test_project_github_webhook_rejects_duplicate_delivery_id(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-dup", "name": "GitHub Dup", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-dup/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    payload = {"repository": {"full_name": "owner/repo"}}
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={sig}",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-dup-1",
    }
    first = await cp_client.post("/v1/projects/gh-dup/github/webhook", content=raw, headers=headers)
    assert first.status_code == 200
    second = await cp_client.post("/v1/projects/gh-dup/github/webhook", content=raw, headers=headers)
    assert second.status_code == 409


@pytest.mark.anyio
async def test_project_github_webhook_rejects_old_date_header(cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS", "1")
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-date", "name": "GitHub Date", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-date/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    payload = {"repository": {"full_name": "owner/repo"}}
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    old_date = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=10))
    ingest = await cp_client.post(
        "/v1/projects/gh-date/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-date-1",
            "Date": old_date,
        },
    )
    assert ingest.status_code == 401


@pytest.mark.anyio
async def test_project_github_webhook_events_self_heal_legacy_payload_json_schema(cp_app, tmp_path):
    legacy_db = tmp_path / "legacy_webhooks.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        """
        CREATE TABLE github_webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            delivery_id TEXT,
            event_type TEXT NOT NULL,
            action TEXT,
            repository_full_name TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO github_webhook_events
            (project_id, delivery_id, event_type, action, repository_full_name, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "gh-legacy",
            "delivery-legacy",
            "push",
            "synchronize",
            "owner/repo",
            json.dumps({"hello": "world"}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    from control_plane.github.webhook_store import GitHubWebhookStore

    cp_app.state.github_webhook_store = GitHubWebhookStore(db_path=str(legacy_db))

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/projects",
            json={"id": "gh-legacy", "name": "Legacy GH", "mode": "isolated"},
        )
        assert create_resp.status_code == 200

        events = await client.get("/v1/projects/gh-legacy/github/webhook/events")
        assert events.status_code == 200
        rows = events.json()["events"]
        assert len(rows) == 1
        assert rows[0]["event_type"] == "push"
        assert rows[0]["payload"] == {"hello": "world"}


@pytest.mark.anyio
async def test_audit_events_record_privileged_actions(cp_client):
    upsert = await cp_client.post(
        "/v1/keys",
        json={"name": "audit-key", "provider": "openai", "value": "sk-test"},
    )
    assert upsert.status_code == 200

    create_model = await cp_client.post(
        "/v1/models",
        json={"id": "audit-model", "name": "audit-model", "provider": "openai"},
    )
    assert create_model.status_code == 200

    events = await cp_client.get("/v1/audit/events?limit=20")
    assert events.status_code == 200
    rows = events.json()
    actions = {r.get("action") for r in rows}
    assert "keys.upsert" in actions
    assert "models.create" in actions
