"""Coverage for the bounded manager-facing fleet summary endpoint."""

import pytest


@pytest.mark.anyio
async def test_fleet_summary_aggregates_operational_counts_without_task_content(cp_client):
    worker = await cp_client.post(
        "/v1/workers",
        json={
            "id": "summary-worker",
            "name": "Summary Worker",
            "host": "worker.internal",
            "port": 8010,
            "capabilities": [],
        },
    )
    bot = await cp_client.post(
        "/v1/bots",
        json={
            "id": "summary-bot",
            "name": "Summary Bot",
            "role": "monitor",
            "enabled": False,
            "backends": [],
        },
    )
    schedule = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Summary Schedule",
            "cron_expression": "0 * * * *",
            "prompt": "Report only.",
            "target_bot_id": "summary-bot",
        },
    )
    response = await cp_client.get("/v1/workers/fleet-summary")

    assert worker.status_code == 200
    assert bot.status_code == 200
    assert schedule.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "control_plane_fleet_summary_v1"
    assert payload["workers"] == {
        "registered": 1,
        "online": 1,
        "offline": 0,
        "runtime_attention": [],
    }
    assert payload["bots"]["registered"] == 1
    assert payload["bots"]["enabled"] == 0
    assert payload["schedules"] == {
        "registered": 1,
        "active": 0,
        "failed_recent_schedule_ids": [],
    }
    assert "error" not in payload["tasks"]
    assert "content" not in payload["tasks"]


@pytest.mark.anyio
async def test_fleet_summary_exposes_safe_runtime_attention_reason_codes(cp_app, cp_client):
    worker = await cp_client.post(
        "/v1/workers",
        json={
            "id": "browser-summary-worker",
            "name": "Browser Summary Worker",
            "host": "worker.internal",
            "port": 8010,
            "capabilities": [],
        },
    )
    assert worker.status_code == 200
    await cp_app.state.worker_probe_store.record(
        {
            "worker_id": "browser-summary-worker",
            "probe_status": "ready",
            "checked_at": "2026-07-19T00:00:00+00:00",
            "dispatch_eligible": True,
            "checks": [],
            "capability_attestation": {
                "browser": {"configured": True, "ready": False},
                "unauthenticated_cli_tools": [],
            },
        }
    )

    response = await cp_client.get("/v1/workers/fleet-summary")

    assert response.status_code == 200
    assert response.json()["workers"]["runtime_attention"] == [
        {
            "worker_id": "browser-summary-worker",
            "probe_status": "ready",
            "reason_codes": ["browser_session_unavailable"],
        }
    ]


@pytest.mark.anyio
async def test_fleet_summary_ignores_disabled_worker_probe_noise(cp_app, cp_client):
    worker = await cp_client.post(
        "/v1/workers",
        json={
            "id": "retired-worker",
            "name": "Retired Worker",
            "host": "worker.internal",
            "port": 8010,
            "enabled": False,
            "status": "offline",
            "capabilities": [],
        },
    )
    assert worker.status_code == 200
    await cp_app.state.worker_probe_store.record(
        {
            "worker_id": "retired-worker",
            "probe_status": "unreachable",
            "checked_at": "2026-07-19T00:00:00+00:00",
            "dispatch_eligible": False,
            "checks": [],
        }
    )

    response = await cp_client.get("/v1/workers/fleet-summary")

    assert response.status_code == 200
    assert response.json()["workers"]["runtime_attention"] == []
