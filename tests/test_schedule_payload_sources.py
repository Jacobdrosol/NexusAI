import json
from datetime import datetime, timedelta, timezone
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
        return [
            SimpleNamespace(enabled=True, backends=[SimpleNamespace(worker_id="browser")]),
            SimpleNamespace(enabled=False, backends=[]),
        ]


class _FakeTaskManager:
    async def list_tasks(self, limit):
        assert limit == 200
        return [
            SimpleNamespace(status="completed", updated_at=datetime.now(timezone.utc).isoformat()),
            SimpleNamespace(
                status="failed",
                updated_at=datetime.now(timezone.utc).isoformat(),
                error=SimpleNamespace(
                    message="API key missing: private-token-must-not-leak",
                    code=None,
                ),
            ),
            SimpleNamespace(
                status="failed",
                updated_at="2020-01-01T00:00:00+00:00",
                error=SimpleNamespace(message="policy block", code="policy_violation"),
            ),
        ]


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
    assert '"enabled_with_runtime_attention":1' in payload["monitoring_events"]
    assert '"recent_failed_by_category":{"authentication":1}' in payload["monitoring_events"]
    assert '"recent_by_status":{"completed":1,"failed":1}' in payload["monitoring_events"]
    assert "prompt" not in payload["monitoring_events"]
    assert "policy" not in payload["monitoring_events"]
    assert "private-token-must-not-leak" not in payload["monitoring_events"]


@pytest.mark.anyio
async def test_fleet_summary_excludes_disabled_workers_from_live_offline_count():
    class WorkerRegistry:
        async def list(self):
            return [
                SimpleNamespace(id="online", status="online", enabled=True),
                SimpleNamespace(id="retired", status="offline", enabled=False),
            ]

    class ProbeStore:
        async def list_for_workers(self, worker_ids):
            return {}

    class BotRegistry:
        async def list(self):
            return []

    class TaskManager:
        async def list_tasks(self, limit):
            return []

    class ScheduleEngine:
        async def list_schedules(self, limit):
            return []

    payload = await materialize_system_schedule_payload(
        {"metadata": {"system_payload_source": {"type": FLEET_HEALTH_SUMMARY_SOURCE}}},
        worker_registry=WorkerRegistry(),
        worker_probe_store=ProbeStore(),
        bot_registry=BotRegistry(),
        task_manager=TaskManager(),
        schedule_engine=ScheduleEngine(),
    )

    summary = json.loads(payload["monitoring_events"])
    assert summary["workers"] == {
        "registered": 2,
        "enabled": 1,
        "disabled": 1,
        "online": 1,
        "offline": 0,
        "runtime_attention": [],
    }


@pytest.mark.anyio
async def test_fleet_summary_separates_recovered_failures_from_live_failures():
    now = datetime.now(timezone.utc)

    class WorkerRegistry:
        async def list(self):
            return []

    class ProbeStore:
        async def list_for_workers(self, worker_ids):
            return {}

    class BotRegistry:
        async def list(self):
            return []

    class TaskManager:
        async def list_tasks(self, limit):
            return [
                SimpleNamespace(
                    bot_id="browser-inspector",
                    status="failed",
                    updated_at=(now - timedelta(minutes=2)).isoformat(),
                    error=SimpleNamespace(message="connection refused", code=None),
                ),
                SimpleNamespace(
                    bot_id="browser-inspector",
                    status="completed",
                    updated_at=(now - timedelta(minutes=1)).isoformat(),
                ),
                SimpleNamespace(
                    bot_id="health-analyst",
                    status="failed",
                    updated_at=now.isoformat(),
                    error=SimpleNamespace(message="policy blocked", code="policy_violation"),
                ),
            ]

    class ScheduleEngine:
        async def list_schedules(self, limit):
            return []

    payload = await materialize_system_schedule_payload(
        {"metadata": {"system_payload_source": {"type": FLEET_HEALTH_SUMMARY_SOURCE}}},
        worker_registry=WorkerRegistry(),
        worker_probe_store=ProbeStore(),
        bot_registry=BotRegistry(),
        task_manager=TaskManager(),
        schedule_engine=ScheduleEngine(),
    )

    summary = json.loads(payload["monitoring_events"])
    assert summary["tasks"]["recent_failed_by_category"] == {"policy": 1, "transport": 1}
    assert summary["tasks"]["recent_recovered_failed_by_category"] == {"transport": 1}
    assert summary["tasks"]["recent_unrecovered_failed_by_category"] == {"policy": 1}


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
