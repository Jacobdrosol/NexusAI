import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from control_plane.schedule_payload_sources import (
    CSV_WORK_ITEMS_SOURCE,
    FLEET_HEALTH_SUMMARY_SOURCE,
    OPERATIONAL_QUALITY_SNAPSHOT_SOURCE,
    SystemPayloadSourceError,
    _failure_category,
    list_csv_work_items_sources,
    materialize_system_schedule_payload,
    system_payload_source_config,
    system_payload_source_configs,
    validate_system_payload_source,
)
from control_plane.schedule_safety import require_schedule_autonomy_safety
from shared.models import Bot


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


def test_failure_category_classifies_current_and_historical_output_contract_errors():
    assert _failure_category(
        SimpleNamespace(error=SimpleNamespace(code="output_contract_invalid", message=""))
    ) == "output_contract"
    assert _failure_category(
        SimpleNamespace(error=SimpleNamespace(code=None, message="no valid JSON object or array found"))
    ) == "output_contract"


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
async def test_multiple_system_payload_sources_materialize_into_distinct_fields():
    schedule = {
        "metadata": {
            "system_payload_sources": [
                {"type": FLEET_HEALTH_SUMMARY_SOURCE, "target_field": "fleet_snapshot"},
                {
                    "type": OPERATIONAL_QUALITY_SNAPSHOT_SOURCE,
                    "target_field": "operations_snapshot",
                },
            ]
        }
    }

    assert [config["target_field"] for config in system_payload_source_configs(schedule)] == [
        "fleet_snapshot",
        "operations_snapshot",
    ]
    payload = await materialize_system_schedule_payload(
        schedule,
        worker_registry=_FakeWorkerRegistry(),
        worker_probe_store=_FakeProbeStore(),
        bot_registry=_FakeBotRegistry(),
        task_manager=_FakeTaskManager(),
        schedule_engine=_FakeScheduleEngine(),
    )

    assert set(payload) == {"fleet_snapshot", "operations_snapshot"}
    assert json.loads(payload["fleet_snapshot"])["source"] == FLEET_HEALTH_SUMMARY_SOURCE
    assert json.loads(payload["operations_snapshot"])["source"] == OPERATIONAL_QUALITY_SNAPSHOT_SOURCE


def test_multiple_system_payload_sources_reject_duplicate_target_fields():
    with pytest.raises(SystemPayloadSourceError, match="distinct target_field"):
        system_payload_source_configs(
            {
                "metadata": {
                    "system_payload_sources": [
                        {"type": FLEET_HEALTH_SUMMARY_SOURCE, "target_field": "snapshot"},
                        {"type": OPERATIONAL_QUALITY_SNAPSHOT_SOURCE, "target_field": "snapshot"},
                    ]
                }
            }
        )


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


@pytest.mark.anyio
async def test_operational_quality_snapshot_is_aggregate_only():
    payload = await materialize_system_schedule_payload(
        {
            "metadata": {
                "system_payload_source": {
                    "type": OPERATIONAL_QUALITY_SNAPSHOT_SOURCE,
                    "target_field": "artifact",
                }
            }
        },
        worker_registry=_FakeWorkerRegistry(),
        worker_probe_store=_FakeProbeStore(),
        bot_registry=_FakeBotRegistry(),
        task_manager=_FakeTaskManager(),
        schedule_engine=_FakeScheduleEngine(),
    )

    snapshot = json.loads(payload["artifact"])
    assert snapshot["source"] == OPERATIONAL_QUALITY_SNAPSHOT_SOURCE
    assert snapshot["scope"] == "aggregate control-plane operational metadata only"
    assert snapshot["quality_dimensions"]["worker_readiness"] == {
        "enabled": 2,
        "online": 2,
        "offline": 0,
        "runtime_attention_count": 1,
    }
    assert snapshot["quality_dimensions"]["task_reliability"]["recent_failed_by_category"] == {
        "authentication": 1
    }
    assert snapshot["quality_dimensions"]["schedule_reliability"] == {
        "active": 1,
        "failed_active_last_run_count": 0,
    }
    assert "recent_worker_activity" not in payload["artifact"]
    assert "private-token-must-not-leak" not in payload["artifact"]
    assert '"browser"' not in payload["artifact"]


def test_operational_quality_snapshot_requires_read_only_quality_review_worker():
    schedule = {
        "metadata": {
            "system_payload_source": {"type": OPERATIONAL_QUALITY_SNAPSHOT_SOURCE}
        }
    }
    allowed = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": False, "task_scope": "read-only-quality-review"}}
    )
    validate_system_payload_source(schedule, allowed)

    blocked = SimpleNamespace(
        routing_rules={"worker_profile": {"can_edit": False, "task_scope": "read-only-monitoring-analysis"}}
    )
    with pytest.raises(SystemPayloadSourceError, match="quality-review"):
        validate_system_payload_source(schedule, blocked)


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
                "require_non_empty_fields": ["recommended_fix_summary"],
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
        "62,1000,Missing evidence,lesson,audited-needs-fixes,not_started,false,,do-not-send\n"
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


def test_csv_work_items_source_requires_selected_evidence_columns():
    schedule = _csv_schedule()
    source = schedule["metadata"]["system_payload_source"]
    source["require_non_empty_fields"] = ["issue_summary"]

    with pytest.raises(SystemPayloadSourceError, match="selected columns"):
        system_payload_source_config(schedule)


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


@pytest.mark.anyio
async def test_csv_work_items_source_maps_one_fresh_row_to_structured_task_fields(tmp_path, monkeypatch):
    source = tmp_path / "course-62.csv"
    source.write_text(
        "course_id,lesson_id,lesson_title,lesson_type,audit_status,work_status,owner_action_needed,recommended_fix_summary\n"
        "62,1001,Normal lesson,lesson,audited-needs-fixes,not_started,false,Repair body evidence\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))
    schedule = _csv_schedule()
    source_config = schedule["metadata"]["system_payload_source"]
    source_config["max_rows"] = 1
    source_config["require_non_empty_fields"] = [
        "course_id",
        "lesson_id",
        "recommended_fix_summary",
    ]
    source_config["payload_field_map"] = {
        "course_id": "course_id",
        "lesson_id": "lesson_id",
        "instruction": "recommended_fix_summary",
    }

    payload = await materialize_system_schedule_payload(
        schedule,
        worker_registry=_FakeWorkerRegistry(),
        worker_probe_store=_FakeProbeStore(),
        bot_registry=_FakeBotRegistry(),
        task_manager=_FakeTaskManager(),
        schedule_engine=_FakeScheduleEngine(),
    )

    assert payload["course_id"] == "62"
    assert payload["lesson_id"] == "1001"
    assert payload["instruction"] == "Repair body evidence"
    assert "mapped_task_payload" not in json.loads(payload["revision_items"])


@pytest.mark.anyio
async def test_csv_work_items_field_mapping_satisfies_schedule_input_contract():
    class BotRegistry:
        async def get(self, bot_id):
            assert bot_id == "draft-planner"
            return Bot(
                id="draft-planner",
                name="Draft Planner",
                role="content-planner",
                project_id="acme",
                enabled=True,
                backends=[],
                routing_rules={
                    "worker_profile": {
                        "can_edit": False,
                        "task_scope": "draft-only-course-unit-lesson-planning",
                    },
                    "input_contract": {
                        "enabled": True,
                        "format": "json_object",
                        "required_fields": ["instruction", "course_id", "lesson_id"],
                    },
                },
            )

    schedule = _csv_schedule()
    source_config = schedule["metadata"]["system_payload_source"]
    source_config["max_rows"] = 1
    source_config["require_non_empty_fields"] = [
        "course_id",
        "lesson_id",
        "recommended_fix_summary",
    ]
    source_config["payload_field_map"] = {
        "course_id": "course_id",
        "lesson_id": "lesson_id",
        "instruction": "recommended_fix_summary",
    }
    schedule.update(
        {
            "target_bot_id": "draft-planner",
            "project_id": "acme",
            "prompt": "Create a draft plan only from the supplied work item.",
        }
    )
    schedule["metadata"]["mutation_safe"] = True

    await require_schedule_autonomy_safety(
        schedule,
        bot_registry=BotRegistry(),
        only_when_active=False,
    )


@pytest.mark.anyio
async def test_empty_mapped_csv_queue_skips_without_creating_a_task(tmp_path, monkeypatch):
    from control_plane.agent_scheduler.engine import AgentScheduleEngine

    source = tmp_path / "course-62.csv"
    source.write_text(
        "course_id,lesson_id,lesson_title,lesson_type,audit_status,work_status,owner_action_needed,recommended_fix_summary\n"
        "62,1001,Completed lesson,lesson,audited-needs-fixes,completed,false,Already done\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))
    schedule_source = _csv_schedule()
    source_config = schedule_source["metadata"]["system_payload_source"]
    source_config["max_rows"] = 1
    source_config["require_non_empty_fields"] = ["recommended_fix_summary"]
    source_config["payload_field_map"] = {"instruction": "recommended_fix_summary"}

    async def materialize(schedule):
        return await materialize_system_schedule_payload(
            schedule,
            worker_registry=_FakeWorkerRegistry(),
            worker_probe_store=_FakeProbeStore(),
            bot_registry=_FakeBotRegistry(),
            task_manager=_FakeTaskManager(),
            schedule_engine=_FakeScheduleEngine(),
        )

    class TaskManager:
        async def create_task(self, **kwargs):
            raise AssertionError("an empty work queue must not create a task")

    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=TaskManager(),
        db_path=str(tmp_path / "schedules.db"),
        payload_materializer=materialize,
    )
    schedule = await engine.create_schedule(
        {
            "name": "Draft queue",
            "cron_expression": "0 * * * *",
            "timezone": "UTC",
            "prompt": "Draft only from the supplied work item.",
            "target_bot_id": "draft-planner",
            "metadata": schedule_source["metadata"],
        }
    )

    await engine.trigger_schedule(schedule["id"])

    run = (await engine.list_runs(schedule["id"]))[0]
    assert run["status"] == "skipped"
    assert run["error"] == {"reason": "csv_work_items_v1 has no eligible work item for this run"}
    assert (await engine.get_schedule(schedule["id"]))["last_run_status"] == "skipped"


def test_csv_work_item_catalog_returns_only_non_content_metadata(tmp_path, monkeypatch):
    source = tmp_path / "queues" / "course-62.csv"
    source.parent.mkdir()
    source.write_text(
        "course_id,lesson_id,instruction\n62,1001,Do not expose this value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))

    sources = list_csv_work_items_sources()

    assert len(sources) == 1
    item = sources[0]
    assert item["relative_path"] == "queues/course-62.csv"
    assert item["headers"] == ["course_id", "lesson_id", "instruction"]
    assert item["row_count"] == 1
    assert item["size_bytes"] == source.stat().st_size
    assert item["available"] is True
    assert item["issue"] is None
    assert "Do not expose this value" not in json.dumps(item)


@pytest.mark.anyio
async def test_schedule_queue_source_catalog_api_returns_queue_metadata(cp_client, tmp_path, monkeypatch):
    source = tmp_path / "draft-work.csv"
    source.write_text("lesson_id,instruction\n1001,Do not expose this value\n", encoding="utf-8")
    monkeypatch.setenv("NEXUSAI_READONLY_CSV_ROOT", str(tmp_path))

    response = await cp_client.get("/v1/schedules/queue-sources")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"][0]["relative_path"] == "draft-work.csv"
    assert payload["sources"][0]["headers"] == ["lesson_id", "instruction"]
    assert "Do not expose this value" not in response.text
