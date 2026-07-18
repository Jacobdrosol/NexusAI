from types import SimpleNamespace

import pytest

from control_plane.schedule_payload_sources import (
    FLEET_HEALTH_SUMMARY_SOURCE,
    SystemPayloadSourceError,
    materialize_system_schedule_payload,
    validate_system_payload_source,
)


class _FakeWorkerRegistry:
    async def list(self):
        return [
            SimpleNamespace(id="healthy", status="online"),
            SimpleNamespace(id="browser", status="online"),
        ]


class _FakeProbeStore:
    async def list_for_workers(self, worker_ids):
        assert worker_ids == ["healthy", "browser"]
        return {
            "healthy": {"probe_status": "ready"},
            "browser": {
                "probe_status": "ready",
                "capability_attestation": {"browser": {"configured": True, "ready": False}},
            },
        }


class _FakeBotRegistry:
    async def list(self):
        return [SimpleNamespace(enabled=True), SimpleNamespace(enabled=False)]


class _FakeTaskManager:
    async def list_tasks(self, limit):
        assert limit == 200
        return [SimpleNamespace(status="completed"), SimpleNamespace(status="failed")]


class _FakeScheduleEngine:
    async def list_schedules(self, limit):
        assert limit == 100
        return [
            {"id": "healthy", "status": "active", "last_run_status": "completed"},
            {"id": "failing", "status": "paused", "last_run_status": "failed"},
        ]


@pytest.mark.anyio
async def test_materialized_fleet_summary_is_sanitized_and_bounded():
    payload = await materialize_system_schedule_payload(
        {
            "metadata": {
                "system_payload_source": {
                    "type": FLEET_HEALTH_SUMMARY_SOURCE,
                    "target_field": "monitoring_events",
                }
            }
        },
        worker_registry=_FakeWorkerRegistry(),
        worker_probe_store=_FakeProbeStore(),
        bot_registry=_FakeBotRegistry(),
        task_manager=_FakeTaskManager(),
        schedule_engine=_FakeScheduleEngine(),
    )

    assert set(payload) == {"monitoring_events"}
    assert '"browser"' in payload["monitoring_events"]
    assert '"failed_recent_schedule_ids":["failing"]' in payload["monitoring_events"]
    assert "prompt" not in payload["monitoring_events"]


def test_system_payload_source_requires_read_only_monitoring_worker():
    schedule = {
        "metadata": {
            "system_payload_source": {"type": FLEET_HEALTH_SUMMARY_SOURCE}
        }
    }
    allowed = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": False, "task_scope": "read-only-monitoring-analysis"}}
    )
    validate_system_payload_source(schedule, allowed)

    blocked = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": False, "task_scope": "draft-only-documentation"}}
    )
    with pytest.raises(SystemPayloadSourceError, match="read-only monitoring"):
        validate_system_payload_source(schedule, blocked)
