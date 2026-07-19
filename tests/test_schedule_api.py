import pytest


def _paused_schedule_payload() -> dict:
    return {
        "name": "Paused schedule",
        "cron_expression": "0 * * * *",
        "timezone": "UTC",
        "prompt": "Prepare a read-only summary.",
        "status": "paused",
        "target_bot_id": "unconfigured-read-only-bot",
    }


@pytest.mark.anyio
async def test_schedule_api_reports_duplicate_configuration_as_conflict(cp_client):
    payload = _paused_schedule_payload()
    created = await cp_client.post("/v1/schedules", json=payload)

    assert created.status_code == 200

    duplicate = await cp_client.post(
        "/v1/schedules",
        json={**payload, "name": "Same recurring work with a different label"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "schedule_duplicate_exists"
    listed = await cp_client.get("/v1/schedules")
    assert listed.status_code == 200
    assert len(listed.json()["schedules"]) == 1
