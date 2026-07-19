import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from control_plane.schedule_payload_sources import (
    CSV_WORK_ITEMS_SOURCE,
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


@pytest.mark.anyio
async def test_fleet_summary_reports_latest_bounded_activity_per_worker():
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
                    id="older-browser-task",
                    bot_id="browser-inspector",
                    status="completed",
                    updated_at=(now - timedelta(minutes=3)).isoformat(),
                    metadata={
                        "execution_provenance": {
                            "worker_id": "browser-worker",
                            "backend_type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                        }
                    },
                ),
                SimpleNamespace(
                    id="latest-browser-task",
                    bot_id="browser-inspector",
                    status="completed",
                    updated_at=(now - timedelta(minutes=1)).isoformat(),
                    metadata={
                        "execution_provenance": {
                            "worker_id": "browser-worker",
                            "backend_type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                        }
                    },
                ),
                SimpleNamespace(
                    id="planner-task",
                    bot_id="content-planner",
                    status="failed",
                    updated_at=now.isoformat(),
                    metadata={
                        "execution_provenance": {
                            "worker_id": "planner-worker",
                            "backend_type": "remote_llm",
                            "provider": "ollama_cloud",
                            "model": "glm-5.2:cloud",
                        }
                    },
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

    activity = json.loads(payload["monitoring_events"])["tasks"]["recent_worker_activity"]
    assert len(activity) == 2
    assert activity[0]["worker_id"] == "planner-worker"
    assert activity[1] == {
        "worker_id": "browser-worker",
        "bot_id": "browser-inspector",
        "task_id": "latest-browser-task",
        "status": "completed",
        "updated_at": (now - timedelta(minutes=1)).isoformat(),
        "backend_type": "browser",
        "provider": "browser",
        "model": "browser-ui",
    }


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


def _csv_schedule(*, max_age_hours: int = 48):
    return {
        "metadata": {
            "system_payload_source": {
                "type": CSV_WORK_ITEMS_SOURCE,
                "target_field": "revision_items",
                "relative_path": "course-62.csv",
                "columns": [
                    "course_id",
                    "lesson_id",
                    "lesson_title",
                    "lesson_type",
                    "audit_status",
                    "work_status",
                    "recommended_fix_summary",
                ],
                "include_equals": {
                    "audit_status": ["audited-needs-fixes"],
                    "work_status": ["not_started"],
                },
                "exclude_equals": {
                    "lesson_type": ["assessment", "project"],
                    "owner_action_needed": ["true", "yes"],
                },
                "max_rows": 2,
                "max_age_hours": max_age_hours,
            }
        }
    }


@pytest.mark.anyio
async def test_csv_work_items_source_is_bounded_filtered_and_draft_only(tmp_path, monkeypatch):
    source = tmp_path / "course-62.csv"
    source.write_text(
        "course_id,lesson_id,lesson_title,lesson_type,audit_status,work_status,owner_action_needed,recommended_fix_summary,secret_notes\n"
        "62,1001,Normal lesson,lesson,audited-needs-fixes,not_started,false,Repair body,do-not-send\n"
        "62,1002,Assessment,assessment,audited-needs-fixes,not_started,false,Repair questions,do-not-send\n"
        "62,1003,Blocked lesson,lesson,audited-needs-fixes,not_started,true,Wait for owner,do-not-send\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))
    schedule = _csv_schedule()
    draft_bot = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": False, "task_scope": "draft-only-content-planning"}}
    )
    validate_system_payload_source(schedule, draft_bot)

    payload = await materialize_system_schedule_payload(
        schedule,
        worker_registry=_FakeWorkerRegistry(),
        worker_probe_store=_FakeProbeStore(),
        bot_registry=_FakeBotRegistry(),
        task_manager=_FakeTaskManager(),
        schedule_engine=_FakeScheduleEngine(),
    )

    items = json.loads(payload["revision_items"])
    assert items["source"] == CSV_WORK_ITEMS_SOURCE
    assert items["source_name"] == "course-62.csv"
    assert items["selected_count"] == 1
    assert items["selected_rows"] == [
        {
            "audit_status": "audited-needs-fixes",
            "course_id": "62",
            "lesson_id": "1001",
            "lesson_title": "Normal lesson",
            "lesson_type": "lesson",
            "recommended_fix_summary": "Repair body",
            "work_status": "not_started",
        }
    ]
    assert "secret_notes" not in payload["revision_items"]


@pytest.mark.anyio
async def test_csv_work_items_source_refuses_stale_input_and_editing_profiles(tmp_path, monkeypatch):
    source = tmp_path / "course-62.csv"
    source.write_text(
        "course_id,lesson_id,lesson_title,lesson_type,audit_status,work_status,owner_action_needed,recommended_fix_summary\n"
        "62,1001,Normal lesson,lesson,audited-needs-fixes,not_started,false,Repair body\n",
        encoding="utf-8",
    )
    stale_at = datetime.now().timestamp() - (2 * 3600)
    os.utime(source, (stale_at, stale_at))
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))
    schedule = _csv_schedule(max_age_hours=1)

    editing_bot = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": True, "task_scope": "draft-only-content-planning"}}
    )
    with pytest.raises(SystemPayloadSourceError, match="non-editing"):
        validate_system_payload_source(schedule, editing_bot)

    with pytest.raises(SystemPayloadSourceError, match="stale"):
        await materialize_system_schedule_payload(
            schedule,
            worker_registry=_FakeWorkerRegistry(),
            worker_probe_store=_FakeProbeStore(),
            bot_registry=_FakeBotRegistry(),
            task_manager=_FakeTaskManager(),
            schedule_engine=_FakeScheduleEngine(),
        )
