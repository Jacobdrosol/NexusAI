import json

import bcrypt
from datetime import datetime, timezone
from unittest.mock import patch

from dashboard.routes.work import _quality_gate_overall_action, _quality_gate_recommended_action
from dashboard.work_overview import build_work_overview


def test_quality_gate_recommended_action_maps_operator_steps():
    assert _quality_gate_recommended_action("passed")["label"] == "continue monitoring"
    assert _quality_gate_recommended_action("failed")["label"] == "review failed gates"
    assert _quality_gate_recommended_action("error")["level"] == "critical"
    assert _quality_gate_recommended_action("running")["label"] == "wait for gate result"
    assert _quality_gate_recommended_action("not_run")["label"] == "run quality gates"


def test_quality_gate_overall_action_prioritizes_operator_risk():
    assert _quality_gate_overall_action({"failed": 1}, available=True, suite_count=2)["level"] == "critical"
    assert _quality_gate_overall_action({"running": 1}, available=True, suite_count=2)["label"] == "wait for gate result"
    assert _quality_gate_overall_action({"not_run": 1}, available=True, suite_count=2)["label"] == "run quality gates"
    assert _quality_gate_overall_action({"passed": 2}, available=True, suite_count=2)["level"] == "ready"
    assert _quality_gate_overall_action({}, available=True, suite_count=0)["label"] == "create quality gates"
    assert _quality_gate_overall_action({}, available=False, suite_count=0)["level"] == "unknown"


def _login_user(dashboard_client, *, email="admin@test.com", role="admin"):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user:
            user.password_hash = password_hash
            user.role = role
            user.is_active = True
        else:
            db.add(User(email=email, password_hash=password_hash, role=role, is_active=True))
        db.commit()
    finally:
        db.close()

    resp = dashboard_client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def _login_admin(dashboard_client):
    _login_user(dashboard_client, email="admin@test.com", role="admin")


def test_work_overview_groups_tasks_by_project_and_manager():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[
            {"id": "globeiq-pm", "name": "GlobeIQ Manager", "role": "project-manager"},
            {"id": "lesson-qc", "name": "Lesson QC", "role": "quality-control"},
        ],
        workers=[
            {"id": "worker-a", "name": "Worker A", "status": "online", "enabled": True, "metrics": {"queue_depth": 3, "load": 0.4}},
            {"id": "worker-b", "name": "Worker B", "status": "offline", "enabled": True, "metrics": {"queue_depth": 1}},
            {"id": "worker-c", "name": "Worker C", "status": "online", "enabled": True, "metrics": {"queue_depth": 0, "load": 0.91}},
            {"id": "worker-d", "name": "Worker D", "status": "offline", "enabled": False, "metrics": {"queue_depth": 0}},
        ],
        holds=[
            {
                "id": "globeiq::globeiq-pm",
                "project_id": "globeiq",
                "manager_id": "globeiq-pm",
                "reason": "quality review",
                "queued_task_count": 1,
                "bot_count": 1,
                "created_by": "admin@test.com",
                "created_at": "2026-08-04T10:00:00+00:00",
            }
        ],
        now=datetime(2026, 8, 4, 11, 10, tzinfo=timezone.utc),
        tasks=[
            {
                "id": "task-running",
                "bot_id": "lesson-writer",
                "status": "running",
                "created_at": "2026-08-04T10:00:00+00:00",
                "updated_at": "2026-08-04T10:01:00+00:00",
                "metadata": {
                    "project_id": "globeiq",
                    "root_pm_bot_id": "globeiq-pm",
                    "orchestration_id": "orch-1",
                    "execution_provenance": {
                        "backend_type": "browser",
                        "provider": "browser",
                        "model": "browser-ui",
                        "worker_id": "browser-worker-01",
                    },
                },
            },
            {
                "id": "task-qc",
                "bot_id": "lesson-qc",
                "status": "queued",
                "created_at": "2026-08-04T10:02:00+00:00",
                "updated_at": "2026-08-04T10:03:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "step_id": "final_qc"},
            },
            {
                "id": "task-failed",
                "bot_id": "repo-coder",
                "status": "failed",
                "created_at": "2026-08-04T10:04:00+00:00",
                "updated_at": "2026-08-04T10:05:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "source": "repair_lane"},
                "has_error": True,
                "error_summary": {"type": "dict", "code": "qc_failed", "message": "Needs repair."},
            },
            {
                "id": "task-completed",
                "bot_id": "lesson-writer",
                "status": "completed",
                "created_at": "2026-08-04T09:00:00+00:00",
                "updated_at": "2026-08-04T09:30:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm"},
            },
        ],
    )

    assert overview["totals"]["active"] == 1
    assert overview["totals"]["waiting"] == 1
    assert overview["totals"]["problem"] == 1
    assert overview["totals"]["qc"] == 1
    assert overview["totals"]["completed"] == 1
    assert overview["totals"]["stale_active"] == 1
    assert overview["totals"]["stale_waiting"] == 1
    assert overview["freshness"]["oldest_active_label"] == "1h 9m"
    assert overview["freshness"]["oldest_waiting_label"] == "1h 8m"
    assert overview["freshness"]["latest_updated_at"] == "2026-08-04T10:05:00+00:00"
    assert overview["freshness"]["latest_update_label"] == "1h 5m"
    assert overview["workers"]["queue_depth"] == 4
    assert overview["workers"]["online"] == 2
    assert overview["workers"]["offline_enabled"] == 1
    assert overview["workers"]["overloaded"] == 1
    assert overview["workers"]["disabled"] == 1
    assert overview["workers"]["queued_workers"] == 2
    assert overview["workers"]["issue_count"] == 3
    assert overview["capacity"]["level"] == "ready"
    assert overview["capacity"]["reason"] == "capacity available"
    assert overview["capacity"]["active_work"] == 1
    assert overview["capacity"]["waiting_work"] == 1
    assert overview["capacity"]["online_workers"] == 2
    assert overview["capacity"]["worker_queue_depth"] == 4
    assert overview["capacity"]["total_pressure"] == 6
    assert overview["capacity"]["pressure_per_online_worker"] == 3.0
    project = overview["projects"][0]
    assert project["project_id"] == "globeiq"
    assert project["project_name"] == "GlobeIQ"
    manager = project["managers"][0]
    assert manager["manager_id"] == "globeiq-pm"
    assert manager["manager_name"] == "GlobeIQ Manager"
    assert manager["totals"]["running"] == 1
    assert manager["totals"]["queued"] == 1
    assert manager["totals"]["failed"] == 1
    assert manager["totals"]["completed"] == 1
    assert manager["totals"]["total"] == 4
    assert manager["problem_labels"] == [{"code": "qc_failed", "count": 1}]
    assert manager["freshness"]["stale_active"] == 1
    assert manager["freshness"]["stale_waiting"] == 1
    assert manager["freshness"]["latest_updated_at"] == "2026-08-04T10:05:00+00:00"
    assert manager["freshness"]["latest_update_label"] == "1h 5m"
    assert manager["held"] is True
    assert manager["hold"]["reason"] == "quality review"
    assert manager["route_evidence"]["attributed_task_count"] == 1
    assert manager["route_evidence"]["missing_worker_count"] == 2
    assert manager["route_evidence"]["missing_active_problem_count"] == 1
    assert manager["route_evidence"]["missing_waiting_count"] == 1
    assert manager["route_evidence"]["by_worker"] == [{"worker_id": "browser-worker-01", "task_count": 1}]
    assert manager["lane_health"]["level"] == "critical"
    assert manager["lane_health"]["label"] == "needs intervention"
    assert manager["lane_health"]["reasons"] == [
        "1 problem task(s)",
        "1 stale active task(s)",
        "1 active/problem task(s) missing worker evidence",
        "1 stale waiting task(s)",
        "1 waiting task(s) missing worker evidence",
        "dispatch hold active",
    ]
    assert project["lane_health"]["level"] == "critical"
    assert project["lane_health"]["counts"]["critical"] == 1
    assert overview["route_evidence"]["task_count"] == 3
    assert overview["route_evidence"]["attributed_task_count"] == 1
    assert overview["route_evidence"]["missing_active_problem_count"] == 1
    assert overview["route_evidence"]["missing_waiting_count"] == 1
    attention_lane = overview["attention_lanes"][0]
    assert attention_lane["project_id"] == "globeiq"
    assert attention_lane["manager_id"] == "globeiq-pm"
    assert attention_lane["problem"] == 1
    assert attention_lane["stale"] == 2
    assert attention_lane["route_gaps"] == 1
    assert attention_lane["held"] is True
    assert attention_lane["hold_manager_id"] == "globeiq-pm"
    assert attention_lane["held_by_project"] is False
    assert attention_lane["reasons"] == ["1 problem", "2 stale", "1 route gap", "held"]
    assert attention_lane["recommended_action"]["label"] == "review failed output"
    assert attention_lane["recommended_action"]["level"] == "critical"
    queue_lane = overview["queue_pressure_lanes"][0]
    assert queue_lane["project_id"] == "globeiq"
    assert queue_lane["manager_id"] == "globeiq-pm"
    assert queue_lane["active"] == 1
    assert queue_lane["waiting"] == 1
    assert queue_lane["queued"] == 1
    assert queue_lane["blocked"] == 0
    assert queue_lane["problem"] == 1
    assert queue_lane["stale_waiting"] == 1
    assert queue_lane["held"] is True
    assert queue_lane["hold_manager_id"] == "globeiq-pm"
    assert queue_lane["held_by_project"] is False
    assert queue_lane["recommended_action"]["label"] == "unblock before adding work"
    assert queue_lane["recommended_action"]["level"] == "critical"
    brief = overview["operations_brief"]
    assert brief["status_breakdown"]["active"] == 1
    assert brief["status_breakdown"]["waiting"] == 1
    assert brief["status_breakdown"]["problem"] == 1
    assert brief["status_breakdown"]["stale"] == 2
    assert brief["status_breakdown"]["worker_queue"] == 4
    assert brief["lane_health_counts"]["critical"] == 1
    assert brief["capacity_level"] == "ready"
    assert brief["top_active_lanes"][0]["manager_id"] == "globeiq-pm"
    assert brief["top_active_lanes"][0]["lane_health"]["label"] == "needs intervention"
    assert brief["top_active_lanes"][0]["active"] == 1
    assert brief["top_waiting_lanes"][0]["waiting"] == 1
    assert brief["top_problem_lanes"][0]["problem"] == 1
    assert brief["attention_lanes"][0]["reasons"] == ["1 problem", "2 stale", "1 route gap", "held"]
    assert brief["attention_lanes"][0]["recommended_action"]["label"] == "review failed output"
    assert manager["latest_tasks"][2]["worker_id"] == "browser-worker-01"
    assert overview["holds"][0]["id"] == "globeiq::globeiq-pm"
    assert overview["holds"][0]["queued_task_count"] == 1
    assert overview["holds"][0]["bot_count"] == 1
    assert overview["holds"][0]["created_by"] == "admin@test.com"
    assert overview["holds"][0]["created_at"] == "2026-08-04T10:00:00+00:00"
    assert manager["latest_tasks"][0]["age_label"] == "1h 5m"
    assert any(task["source"] == "repair_lane" for task in manager["latest_tasks"])
    assert overview["recent_problem_tasks"][0]["id"] == "task-failed"
    assert overview["recent_problem_tasks"][0]["problem_label"] == "qc_failed"
    assert overview["recent_problem_tasks"][0]["source"] == "repair_lane"
    assert overview["metadata_health"]["missing_project_count"] == 0
    assert overview["metadata_health"]["inferred_manager_count"] == 0


def test_work_overview_surfaces_metadata_routing_gaps():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[],
        workers=[],
        tasks=[
            {
                "id": "missing-project-bot-manager",
                "bot_id": "loose-worker",
                "status": "queued",
                "created_at": "2026-08-04T10:00:00+00:00",
            },
            {
                "id": "parent-derived-manager",
                "bot_id": "lesson-worker",
                "status": "running",
                "metadata": {"project_id": "globeiq", "parent_task_id": "parent-task-123456789"},
            },
            {
                "id": "missing-manager",
                "status": "failed",
                "metadata": {"project_id": "globeiq"},
            },
        ],
        now=datetime(2026, 8, 4, 11, 10, tzinfo=timezone.utc),
    )

    health = overview["metadata_health"]
    assert health["task_count"] == 3
    assert health["missing_project_count"] == 1
    assert health["inferred_manager_count"] == 3
    assert health["missing_manager_count"] == 1
    assert {sample["issue"] for sample in health["sample_tasks"]} == {"missing_project", "inferred_manager"}
    assert any(sample["manager_source"] == "task.bot_id" for sample in health["sample_tasks"])
    assert any(sample["manager_source"] == "metadata.parent_task_id" for sample in health["sample_tasks"])
    assert any(sample["manager_source"] == "missing" for sample in health["sample_tasks"])
    assert any(project["project_id"] == "unassigned" for project in overview["projects"])


def test_work_overview_marks_capacity_critical_when_work_has_no_online_workers():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[],
        workers=[
            {"id": "worker-offline", "status": "offline", "enabled": True, "metrics": {"queue_depth": 2}},
        ],
        tasks=[
            {
                "id": "queued-task",
                "bot_id": "lesson-worker",
                "status": "queued",
                "created_at": "2026-08-04T10:00:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm"},
            }
        ],
        now=datetime(2026, 8, 4, 10, 5, tzinfo=timezone.utc),
    )

    assert overview["capacity"]["level"] == "critical"
    assert overview["capacity"]["reason"] == "work waiting with no online workers"
    assert overview["capacity"]["online_workers"] == 0
    assert overview["capacity"]["waiting_work"] == 1
    assert overview["capacity"]["worker_queue_depth"] == 2
    assert overview["capacity"]["total_pressure"] == 3
    assert overview["capacity"]["pressure_per_online_worker"] is None


def test_work_overview_groups_problem_sources_from_error_summaries():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[],
        workers=[],
        tasks=[
            {
                "id": "failed-qc",
                "bot_id": "lesson-qc",
                "status": "failed",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-a", "source": "lesson_audit"},
                "has_error": True,
                "error_summary": {"type": "dict", "code": "browser_evidence_missing", "message": "No browser evidence."},
            },
            {
                "id": "retried-qc",
                "bot_id": "lesson-qc",
                "status": "retried",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-a", "source": "lesson_audit"},
                "has_error": True,
                "error_summary": {"type": "dict", "code": "browser_evidence_missing"},
            },
            {
                "id": "failed-writer",
                "bot_id": "lesson-writer",
                "status": "failed",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-a", "source": "repair_lane"},
                "has_error": True,
                "error_type": "TimeoutError",
            },
        ],
    )

    assert overview["problem_summary"]["total"] == 3
    assert overview["problem_summary"]["by_code"][0] == {"code": "browser_evidence_missing", "count": 2}
    assert {"code": "TimeoutError", "count": 1} in overview["problem_summary"]["by_code"]
    assert overview["problem_summary"]["by_source"][0] == {"source": "lesson_audit", "count": 2}
    assert overview["problem_summary"]["by_bot"][0] == {"bot_id": "lesson-qc", "count": 2}


def test_work_overview_rolls_up_orchestration_activity():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[],
        workers=[],
        tasks=[
            {
                "id": "orch-a-running",
                "bot_id": "lesson-writer",
                "status": "running",
                "created_at": "2026-08-04T09:00:00+00:00",
                "updated_at": "2026-08-04T09:05:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-a", "orchestration_id": "orch-a"},
            },
            {
                "id": "orch-a-failed",
                "bot_id": "lesson-qc",
                "status": "failed",
                "created_at": "2026-08-04T10:00:00+00:00",
                "updated_at": "2026-08-04T10:10:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-a", "orchestration_id": "orch-a"},
            },
            {
                "id": "orch-b-queued",
                "bot_id": "asset-worker",
                "status": "queued",
                "created_at": "2026-08-04T10:50:00+00:00",
                "updated_at": "2026-08-04T10:51:00+00:00",
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "pm-b", "orchestration_id": "orch-b"},
            },
        ],
        now=datetime(2026, 8, 4, 11, 10, tzinfo=timezone.utc),
    )

    assert [row["orchestration_id"] for row in overview["orchestrations"]] == ["orch-a", "orch-b"]
    orch_a = overview["orchestrations"][0]
    assert orch_a["project_id"] == "globeiq"
    assert orch_a["manager_id"] == "pm-a"
    assert orch_a["task_count"] == 2
    assert orch_a["active"] == 1
    assert orch_a["waiting"] == 0
    assert orch_a["problem_count"] == 1
    assert orch_a["stale_active"] == 1
    assert orch_a["latest_task_id"] == "orch-a-failed"
    assert orch_a["state"] == "problem"


def test_work_page_renders_project_manager_and_worker_load(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.task_calls = []

        def list_tasks(self, **kwargs):
            self.task_calls.append(kwargs)
            assert kwargs["include_content"] is False
            if kwargs.get("statuses"):
                return [
                    {
                        "id": "task-running",
                        "bot_id": "lesson-writer",
                        "status": "running",
                        "created_at": "2026-08-04T10:00:00+00:00",
                        "updated_at": "2026-08-04T10:01:00+00:00",
                        "metadata": {
                            "project_id": "globeiq",
                            "root_pm_bot_id": "globeiq-pm",
                            "orchestration_id": "orch-1",
                            "execution_provenance": {
                                "backend_type": "browser",
                                "provider": "browser",
                                "model": "browser-ui",
                                "worker_id": "browser-worker-01",
                            },
                        },
                    },
                    {
                        "id": "task-blocked",
                        "bot_id": "lesson-qc",
                        "status": "blocked",
                        "created_at": "2026-08-04T10:02:00+00:00",
                        "updated_at": "2026-08-04T10:03:00+00:00",
                        "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "step_id": "quality_gate"},
                    },
                ]
            return [
                {
                    "id": "task-running",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2026-08-04T10:01:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "globeiq-pm",
                        "orchestration_id": "orch-1",
                        "execution_provenance": {
                            "backend_type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                            "worker_id": "browser-worker-01",
                        },
                    },
                },
                {
                    "id": "task-blocked",
                    "bot_id": "lesson-qc",
                    "status": "blocked",
                    "created_at": "2026-08-04T10:02:00+00:00",
                    "updated_at": "2026-08-04T10:03:00+00:00",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "step_id": "quality_gate"},
                },
                {
                    "id": "task-failed",
                    "bot_id": "lesson-qc",
                    "status": "failed",
                    "created_at": "2026-08-04T10:04:00+00:00",
                    "updated_at": "2026-08-04T10:05:00+00:00",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "source": "lesson_audit"},
                    "has_error": True,
                    "error_summary": {"type": "dict", "code": "browser_evidence_missing"},
                },
            ]

        def list_projects(self, **kwargs):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_bots(self, **kwargs):
            return [{"id": "globeiq-pm", "name": "GlobeIQ Manager", "role": "project-manager"}]

        def list_workers(self, **kwargs):
            return [
                {"id": "worker-a", "name": "Worker A", "status": "online", "enabled": True, "metrics": {"queue_depth": 2, "load": 0.25}},
                {"id": "worker-b", "name": "Worker B", "status": "offline", "enabled": True, "metrics": {"queue_depth": 1}},
                {"id": "worker-c", "name": "Worker C", "status": "online", "enabled": True, "metrics": {"queue_depth": 0, "load": 0.9}},
            ]

        def list_work_dispatch_holds(self, **kwargs):
            return {
                "holds": [
                    {
                        "id": "globeiq::*",
                        "project_id": "globeiq",
                        "manager_id": "",
                        "reason": "operator hold",
                        "queued_task_count": 4,
                        "bot_count": 3,
                        "created_by": "admin@test.com",
                        "created_at": "2026-08-04T10:07:00+00:00",
                    }
                ]
            }

        def task_usage(self, **kwargs):
            return {
                "window": {"hours": 24},
                "totals": {
                    "prompt_tokens": 60,
                    "completion_tokens": 80,
                    "total_tokens": 140,
                    "tasks_with_usage": 1,
                    "tasks_without_usage": 2,
                },
                "by_project": [{"project_id": "globeiq", "total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 2}],
                "by_manager": [{"project_id": "globeiq", "manager_id": "globeiq-pm", "total_tokens": 140, "tasks_with_usage": 1}],
                "by_project_manager_bot": [
                    {
                        "project_id": "globeiq",
                        "manager_id": "globeiq-pm",
                        "bot_id": "lesson-writer",
                        "total_tokens": 140,
                        "tasks_with_usage": 1,
                        "tasks_without_usage": 0,
                    }
                ],
                "by_bot": [{"bot_id": "lesson-writer", "total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 0}],
                "by_provider_model": [{"provider": "ollama_cloud", "model": "qwen3.5:cloud", "total_tokens": 140, "tasks_with_usage": 1}],
                "token_governor": {
                    "enabled": True,
                    "limits": {
                        "bot_hourly_tokens": 200,
                        "bot_hourly_token_overrides": {"lesson-writer": 150},
                    },
                    "current": {},
                },
            }

        def chat_usage(self, **kwargs):
            return {
                "window": {"hours": 24},
                "totals": {
                    "messages": 2,
                    "messages_with_usage": 1,
                    "messages_without_usage": 1,
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                },
                "by_conversation": [
                    {
                        "conversation_id": "chat-1",
                        "conversation_title": "Planning Chat",
                        "project_id": "nexusai",
                        "scope": "project",
                        "total_tokens": 40,
                        "messages_with_usage": 1,
                        "messages_without_usage": 1,
                        "last_message_at": "2026-08-05T01:02:03+00:00",
                    }
                ],
                "by_bot": [
                    {
                        "bot_id": "general-chat",
                        "total_tokens": 40,
                        "messages_with_usage": 1,
                        "messages_without_usage": 1,
                        "last_message_at": "2026-08-05T01:02:03+00:00",
                    }
                ],
                "by_provider_model": [
                    {
                        "provider": "ollama_cloud",
                        "model": "qwen3.5:397b",
                        "total_tokens": 40,
                        "messages_with_usage": 1,
                        "last_message_at": "2026-08-05T01:02:03+00:00",
                    }
                ],
                "chat_token_governor": {
                    "enabled": True,
                    "limits": {
                        "global_hourly_tokens": 45,
                        "bot_hourly_tokens": 12000,
                        "bot_hourly_token_overrides": {"general-chat": 50},
                        "estimated_tokens_per_message": 3500,
                    },
                },
            }

        def list_platform_ai_quality_suites_global(self, **kwargs):
            return {
                "suites": [
                    {
                        "id": "suite-globeiq-lessons",
                        "name": "GlobeIQ Lesson Quality",
                        "pipeline_bot_id": "globeiq-pm",
                        "suite": {"tests": [{"name": "Browser reader"}, {"name": "LLM content review"}]},
                    }
                ]
            }

        def list_platform_ai_quality_suite_runs(self, suite_id, **kwargs):
            assert suite_id == "suite-globeiq-lessons"
            return {
                "runs": [
                    {
                        "status": "failed",
                        "score": 0.74,
                        "completed_at": "2026-08-05T02:03:04+00:00",
                    }
                ]
            }

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.get("/work")

    assert resp.status_code == 200
    assert b"Work" in resp.data
    assert b"Needs Attention" in resp.data
    assert b"Active Work Brief" in resp.data
    assert b"Compact project and manager snapshot for the loaded task window." in resp.data
    assert b"Top Active Lanes" in resp.data
    assert b"Top Waiting Lanes" in resp.data
    assert b"Top Problem Lanes" in resp.data
    assert b"Action:" in resp.data
    assert b"capacity ready" in resp.data
    assert b"Attention Breakdown" in resp.data
    assert b"Attention Lanes" in resp.data
    assert b"Problem tasks" in resp.data
    assert b"Usage gaps" in resp.data
    assert b"Route gaps" in resp.data
    assert b"Route Coverage" in resp.data
    assert b"1 active/problem unknown" in resp.data
    assert b"1 waiting unknown" in resp.data
    assert b"1 route gap" in resp.data
    assert b"2 stale" in resp.data
    assert b"Review Lane" in resp.data
    assert b"project hold" in resp.data
    assert b"Stop Attention Lane" in resp.data
    assert b"Queue Pressure" in resp.data
    assert b"Review Queue" in resp.data
    assert b"Stop Queue Lane" in resp.data
    assert b"stale waiting" in resp.data
    assert b"Capacity Pressure" in resp.data
    assert b"Capacity Snapshot" in resp.data
    assert b"capacity available" in resp.data
    assert b"Total pressure" in resp.data
    assert b"Usage Health" in resp.data
    assert b"Chat Usage Health" in resp.data
    assert b"Chat Usage Gaps" in resp.data
    assert b"Provider/model attribution:" in resp.data
    assert b"worker provider/model attribution is complete" in resp.data
    assert b"Worker model spend:" in resp.data
    assert b"worker model concentrated spend" in resp.data
    assert b"ollama_cloud / qwen3.5:cloud is using 1.0 of measured worker tokens" in resp.data
    assert b"Chat provider/model attribution:" in resp.data
    assert b"chat provider/model attribution is complete" in resp.data
    assert b"Chat model spend:" in resp.data
    assert b"chat model concentrated spend" in resp.data
    assert b"ollama_cloud / qwen3.5:397b is using 1.0 of measured chat tokens" in resp.data
    assert b"token usage telemetry is incomplete for most measured tasks" in resp.data
    assert b"chat token usage telemetry is incomplete for most assistant messages" in resp.data
    assert b"Missing ratio" in resp.data
    assert b"GlobeIQ" in resp.data
    assert b"GlobeIQ Manager" in resp.data
    assert b"lesson-writer" in resp.data
    assert b"Worker Load" in resp.data
    assert b"worker-a" in resp.data
    assert b"Worker Queue" in resp.data
    assert b"Worker Issues" in resp.data
    assert b"Worker Evidence" in resp.data
    assert b"1 attributed" in resp.data
    assert b"2 unknown" in resp.data
    assert b"browser-worker-01" in resp.data
    assert b"worker browser-worker-01" in resp.data
    assert b"enabled offline" in resp.data
    assert b"high load" in resp.data
    assert b"worker-b" in resp.data
    assert b"worker-c" in resp.data
    assert b"Metadata Gaps" in resp.data
    assert b"Loaded task summaries include project and manager metadata." in resp.data
    assert b"Orchestrations" in resp.data
    assert b"orch-1" in resp.data
    assert b"View Run" in resp.data
    assert b"showOrchestrationDetails" in resp.data
    assert b"Stop Run" in resp.data
    assert b"stopOrchestration" in resp.data
    assert b"Problem Sources" in resp.data
    assert b"Recent Problems" in resp.data
    assert b"browser_evidence_missing" in resp.data
    assert b"lesson_audit" in resp.data
    assert b"Usage By Project And Manager" in resp.data
    assert b"<th>Manager</th>" in resp.data
    assert b"<th>Health</th>" in resp.data
    assert b"needs intervention" in resp.data
    assert b"critical lanes" in resp.data
    assert b"active/problem task(s) missing worker evidence" in resp.data
    assert b"Recommended Action" in resp.data
    assert b"review failed output" in resp.data
    assert b"unblock before adding work" in resp.data
    assert b"<th>Bot</th><th>Tokens</th><th>Measured</th><th>Missing</th>" in resp.data
    assert b"<th>Project</th><th>Manager</th><th>Bot</th><th>Tokens</th><th>Measured</th><th>Missing</th>" in resp.data
    assert b"No project/manager/bot token usage" not in resp.data
    assert b"Bot Usage Pressure" in resp.data
    assert b"override cap" in resp.data
    assert b"warning 0.93" in resp.data
    assert b"watch spend" in resp.data
    assert b"review recent output quality before increasing throughput" in resp.data
    assert b"Cap At Current" in resp.data
    assert b"Clear Cap" in resp.data
    assert b"setBotHourlyCap" in resp.data
    assert b"No bot token usage" not in resp.data
    assert b"Usage Gaps" in resp.data
    assert b"2 missing usage" in resp.data
    assert b"lesson-writer" in resp.data
    assert b"Planning Chat" in resp.data
    assert b"general-chat" in resp.data
    assert b"2026-08-05T01:02:03+00:00" in resp.data
    assert b"Chat governor:" in resp.data
    assert b"global cap 45" in resp.data
    assert b"estimate 3,500" in resp.data
    assert b"Chat Bot Usage Pressure" in resp.data
    assert b"worker caps near cap" in resp.data
    assert b"chat caps near cap" in resp.data
    assert b"chat global cap near" in resp.data
    assert b"override chat cap" in resp.data
    assert b"warning 0.8" in resp.data
    assert b"Cap Chat At Current" in resp.data
    assert b"Clear Chat Cap" in resp.data
    assert b"setChatBotHourlyCap" in resp.data
    assert b"No chat token usage" not in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:cloud" in resp.data
    assert b"Quality Gates" in resp.data
    assert b"Overall action:" in resp.data
    assert b"GlobeIQ Lesson Quality" in resp.data
    assert b"review failed gates" in resp.data
    assert b"hold dependent automation" in resp.data
    assert b"Stale Work" in resp.data
    assert b"Latest Update" in resp.data
    assert b"Latest update" in resp.data
    assert b"latest " in resp.data
    assert b"Oldest" in resp.data
    assert b"Held Lanes" in resp.data
    assert b"operator hold" in resp.data
    assert b"Dispatch Holds" in resp.data
    assert b"4 queued" in resp.data
    assert b"3 bots" in resp.data
    assert b"admin@test.com" in resp.data
    assert b"2026-08-04T10:07:00+00:00" in resp.data
    assert b"Release Project" in resp.data
    assert b"dry_run: true" in resp.data
    assert b"Stop preview failed" in resp.data
    assert b"Lane Details" in resp.data
    assert b"showLaneDetails" in resp.data
    assert b"<th>Age</th>" in resp.data
    assert b"Work snapshot" in resp.data
    assert b"3 task summaries loaded" in resp.data
    assert b"Snapshot Health" in resp.data
    assert b"task snapshot loaded within configured windows" in resp.data
    assert b"Stop Project" in resp.data
    assert b"Stop Lane" in resp.data
    assert fake.task_calls[0]["statuses"] == ["blocked", "failed", "queued", "retried", "running"]
    assert fake.task_calls[0]["limit"] == 1000
    assert fake.task_calls[1]["limit"] == 250

    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        api_resp = dashboard_client.get("/api/work/overview")
    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert data["operations_brief"]["status_breakdown"]["active"] == 1
    assert data["operations_brief"]["top_active_lanes"][0]["project_id"] == "globeiq"
    assert data["operations_brief"]["lane_health_counts"]["critical"] == 1
    assert data["operations_brief"]["top_active_lanes"][0]["lane_health"]["level"] == "critical"
    assert data["operations_brief"]["top_active_lanes"][0]["recommended_action"]["label"] == "review failed output"
    assert data["operations_brief"]["top_waiting_lanes"][0]["recommended_action"]["label"] == "review failed output"
    assert data["operations_brief"]["top_problem_lanes"][0]["manager_id"] == "globeiq-pm"
    assert data["operations_brief"]["top_problem_lanes"][0]["recommended_action"]["level"] == "critical"
    assert data["operations_brief"]["usage_cap_pressure"]["level"] == "warning"
    assert data["operations_brief"]["usage_cap_pressure"]["top_lane"]["bot_id"] == "lesson-writer"
    assert data["operations_brief"]["chat_cap_pressure"]["level"] == "warning"
    assert data["operations_brief"]["chat_cap_pressure"]["top_lane"]["bot_id"] == "general-chat"
    assert data["operations_brief"]["chat_global_cap_pressure"]["level"] == "warning"
    assert data["operations_brief"]["chat_global_cap_pressure"]["usage_ratio"] == 0.89
    assert data["attention_lanes"][0]["recommended_action"]["label"] == "review failed output"
    assert data["queue_pressure_lanes"][0]["recommended_action"]["label"] == "unblock before adding work"
    assert data["attention"]["problem_tasks"] == 1
    assert data["attention"]["stale_work"] == 2
    assert data["attention"]["metadata_gaps"] == 0
    assert data["attention"]["route_gaps"] == 1
    assert data["attention"]["worker_issues"] == 2
    assert data["attention"]["usage_gaps"] == 2
    assert data["attention"]["total"] == 8
    assert data["attention"]["level"] == "critical"
    assert data["snapshot_health"]["level"] == "ready"
    assert data["snapshot_health"]["reason"] == "task snapshot loaded within configured windows"
    assert data["snapshot_health"]["active_rows"] == 2
    assert data["snapshot_health"]["recent_rows"] == 3
    assert data["snapshot_health"]["merged_rows"] == 3
    assert data["snapshot_health"]["capped_windows"] == []
    assert data["snapshot_health"]["unavailable_windows"] == []
    assert data["usage_health"] == {
        "level": "critical",
        "reason": "token usage telemetry is incomplete for most measured tasks",
        "measured_tasks": 1,
        "missing_tasks": 2,
        "total_tasks": 3,
        "missing_ratio": 0.67,
        "total_tokens": 140,
    }

    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        brief_resp = dashboard_client.get("/api/work/brief")
    assert brief_resp.status_code == 200
    brief_data = brief_resp.get_json()
    assert brief_data["operations_brief"]["status_breakdown"]["active"] == 1
    assert brief_data["operations_brief"]["top_active_lanes"][0]["manager_id"] == "globeiq-pm"
    assert brief_data["attention"]["total"] == 8
    assert brief_data["snapshot_health"]["level"] == "ready"
    assert brief_data["usage_health"]["missing_tasks"] == 2
    assert brief_data["usage_cap_pressure"]["label"] == "near cap"
    assert brief_data["usage_global_cap_pressure"]["level"] == "idle"
    assert brief_data["chat_usage_health"]["missing_messages"] == 1
    assert brief_data["chat_usage_health"]["total_tokens"] == 40
    assert brief_data["chat_cap_pressure"]["label"] == "near cap"
    assert brief_data["chat_global_cap_pressure"]["remaining_tokens"] == 5
    assert brief_data["usage_brief"]["totals"]["prompt_tokens"] == 60
    assert brief_data["usage_brief"]["totals"]["completion_tokens"] == 80
    assert brief_data["chat_usage_brief"]["totals"]["prompt_tokens"] == 30
    assert brief_data["chat_usage_brief"]["top_conversations"][0]["conversation_id"] == "chat-1"
    assert brief_data["chat_usage_brief"]["top_conversations"][0]["last_message_at"] == "2026-08-05T01:02:03+00:00"
    assert brief_data["chat_token_governor"]["enabled"] is True
    assert brief_data["chat_token_governor"]["limits"]["global_hourly_tokens"] == 45
    assert brief_data["chat_usage_pressure_lanes"][0]["bot_id"] == "general-chat"
    assert brief_data["chat_usage_pressure_lanes"][0]["usage_ratio"] == 0.8
    assert brief_data["chat_usage_pressure_lanes"][0]["cap_source"] == "override"
    assert brief_data["chat_usage_pressure_lanes"][0]["last_message_at"] == "2026-08-05T01:02:03+00:00"
    assert brief_data["chat_usage_pressure_lanes"][0]["recommended_action"]["label"] == "watch spend"
    assert brief_data["quality_gates"]["status_counts"]["failed"] == 1
    assert brief_data["quality_gates"]["rows"][0]["suite_id"] == "suite-globeiq-lessons"
    assert brief_data["quality_gates"]["rows"][0]["recommended_action"]["label"] == "review failed gates"
    assert brief_data["quality_gates"]["recommended_action"]["level"] == "critical"
    assert brief_data["quality_gates"]["recommended_action"]["label"] == "review failed gates"
    assert brief_data["usage_brief"]["top_bots"][0]["bot_id"] == "lesson-writer"
    assert brief_data["usage_brief"]["top_project_manager_bots"][0]["project_id"] == "globeiq"
    assert brief_data["usage_brief"]["top_project_manager_bots"][0]["manager_id"] == "globeiq-pm"
    assert brief_data["usage_brief"]["top_project_manager_bots"][0]["bot_id"] == "lesson-writer"
    assert brief_data["usage_brief"]["top_provider_models"][0]["provider"] == "ollama_cloud"
    assert brief_data["usage_brief"]["provider_model_attribution"] == {
        "level": "ready",
        "reason": "worker provider/model attribution is complete",
        "unknown_tokens": 0,
        "unknown_ratio": 0.0,
    }
    assert brief_data["usage_brief"]["provider_model_spend"] == {
        "level": "warning",
        "label": "concentrated spend",
        "detail": "ollama_cloud / qwen3.5:cloud is using 1.0 of measured worker tokens; review quality before increasing throughput.",
        "provider": "ollama_cloud",
        "model": "qwen3.5:cloud",
        "total_tokens": 140,
        "top_tokens": 140,
        "top_ratio": 1.0,
    }
    assert brief_data["chat_usage_brief"]["provider_model_attribution"] == {
        "level": "ready",
        "reason": "chat provider/model attribution is complete",
        "unknown_tokens": 0,
        "unknown_ratio": 0.0,
    }
    assert brief_data["chat_usage_brief"]["provider_model_spend"] == {
        "level": "warning",
        "label": "concentrated spend",
        "detail": "ollama_cloud / qwen3.5:397b is using 1.0 of measured chat tokens; review quality before increasing throughput.",
        "provider": "ollama_cloud",
        "model": "qwen3.5:397b",
        "total_tokens": 40,
        "top_tokens": 40,
        "top_ratio": 1.0,
    }
    assert brief_data["usage_pressure_lanes"][0]["bot_id"] == "lesson-writer"
    assert brief_data["usage_pressure_lanes"][0]["usage_ratio"] == 0.93
    assert brief_data["usage_pressure_lanes"][0]["recommended_action"]["label"] == "watch spend"
    assert brief_data["capacity"]["worker_queue_depth"] == 3
    assert brief_data["workers"]["queue_depth"] == 3
    assert "projects" not in brief_data
    assert data["usage_pressure_lanes"] == [
        {
            "bot_id": "lesson-writer",
            "total_tokens": 140,
            "hourly_limit": 150,
            "remaining_tokens": 10,
            "usage_ratio": 0.93,
            "level": "warning",
            "cap_source": "override",
            "tasks_with_usage": 1,
            "tasks_without_usage": 0,
            "recommended_action": {
                "label": "watch spend",
                "level": "warning",
                "detail": "Usage is near the hourly cap; review recent output quality before increasing throughput.",
            },
        }
    ]
    assert data["chat_usage_pressure_lanes"] == [
        {
            "bot_id": "general-chat",
            "total_tokens": 40,
            "hourly_limit": 50,
            "remaining_tokens": 10,
            "usage_ratio": 0.8,
            "level": "warning",
            "cap_source": "override",
            "messages_with_usage": 1,
            "messages_without_usage": 1,
            "last_message_at": "2026-08-05T01:02:03+00:00",
            "recommended_action": {
                "label": "watch spend",
                "level": "warning",
                "detail": "Usage is near the hourly cap; review recent output quality before increasing throughput.",
            },
        }
    ]


def test_work_overview_routes_require_admin_role(dashboard_client):
    _login_user(dashboard_client, email="learner@test.com", role="learner")

    assert dashboard_client.get("/work").status_code == 403
    assert dashboard_client.get("/api/work/overview").status_code == 403
    assert dashboard_client.get("/api/work/lane?project_id=globeiq").status_code == 403
    assert dashboard_client.get("/api/work/orchestration?orchestration_id=orch-1").status_code == 403
    assert dashboard_client.post("/api/work/stop", json={"project_id": "globeiq"}).status_code == 403
    assert dashboard_client.post("/api/work/orchestration/stop", json={"orchestration_id": "orch-1"}).status_code == 403
    assert dashboard_client.post("/api/work/hold", json={"action": "hold", "project_id": "globeiq"}).status_code == 403
    assert dashboard_client.post(
        "/api/work/bot-cap",
        json={"action": "set", "bot_id": "audit-reader", "hourly_limit": 50000},
    ).status_code == 403


def test_work_overview_surfaces_bot_cap_audit_rows(dashboard_client, tmp_path):
    from shared.settings_manager import SettingsManager

    _login_admin(dashboard_client)
    original_settings = SettingsManager._instance
    SettingsManager._instance = SettingsManager(str(tmp_path / "work-bot-cap-audit.db"))

    class FakeCP:
        def list_tasks(self, *args, **kwargs):
            return []

        def list_projects(self, *args, **kwargs):
            return []

        def list_bots(self, *args, **kwargs):
            return []

        def list_workers(self, *args, **kwargs):
            return []

        def list_work_dispatch_holds(self, *args, **kwargs):
            return {"holds": []}

        def task_usage(self, *args, **kwargs):
            return {
                "totals": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "tasks_with_usage": 0,
                    "tasks_without_usage": 0,
                },
                "by_project": [],
                "by_manager": [],
                "by_bot": [],
                "by_provider_model": [],
                "token_governor": {
                    "enabled": True,
                    "limits": {"bot_hourly_tokens": 100000, "bot_hourly_token_overrides": {"audit-reader": 50000}},
                },
            }

    try:
        SettingsManager._instance.set(
            "token_governor_bot_hourly_limits",
            json.dumps({"audit-reader": 50000}),
            changed_by="operator@test.com",
        )

        with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
            api_resp = dashboard_client.get("/api/work/overview")
            page_resp = dashboard_client.get("/work")

        assert api_resp.status_code == 200
        data = api_resp.get_json()
        assert data["bot_cap_audit"][0]["changed_by"] == "operator@test.com"
        assert data["bot_cap_audit"][0]["override_count"] == 1
        assert data["bot_cap_audit"][0]["changed_bots"] == ["audit-reader"]

        assert page_resp.status_code == 200
        assert b"Bot Cap Audit" in page_resp.data
        assert b"operator@test.com" in page_resp.data
        assert b"audit-reader" in page_resp.data
    finally:
        SettingsManager._instance = original_settings


def test_work_overview_surfaces_token_governor_queue_cap_pressure(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, *args, **kwargs):
            return [
                {
                    "id": "queued-1",
                    "bot_id": "audit-reader",
                    "status": "queued",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "queued-2",
                    "bot_id": "audit-reader",
                    "status": "queued",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
            ]

        def list_projects(self, *args, **kwargs):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_bots(self, *args, **kwargs):
            return [{"id": "manager-a", "name": "Manager A", "role": "project-manager"}]

        def list_workers(self, *args, **kwargs):
            return []

        def list_work_dispatch_holds(self, *args, **kwargs):
            return {"holds": []}

        def task_usage(self, *args, **kwargs):
            return {
                "totals": {"total_tokens": 0, "tasks_with_usage": 0, "tasks_without_usage": 0},
                "by_project": [],
                "by_manager": [],
                "by_bot": [],
                "by_provider_model": [],
                "token_governor": {
                    "enabled": True,
                    "limits": {
                        "max_queued_llm_tasks_per_bot": 2,
                        "max_queued_llm_tasks_per_project": 4,
                        "max_queued_llm_tasks_per_manager": 2,
                    },
                    "current": {},
                },
            }

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        api_resp = dashboard_client.get("/api/work/overview")
        brief_resp = dashboard_client.get("/api/work/brief")
        page_resp = dashboard_client.get("/work")

    assert api_resp.status_code == 200
    rows = {
        (row["scope"], row["value"]): row
        for row in api_resp.get_json()["token_governor_queue_pressure"]
    }
    assert rows[("bot", "audit-reader")]["queued_count"] == 2
    assert rows[("bot", "audit-reader")]["level"] == "critical"
    assert rows[("manager", "globeiq::manager-a")]["queued_count"] == 2
    assert rows[("manager", "globeiq::manager-a")]["level"] == "critical"
    assert rows[("project", "globeiq")]["usage_ratio"] == 0.5
    assert brief_resp.status_code == 200
    brief_rows = {
        (row["scope"], row["value"]): row
        for row in brief_resp.get_json()["token_governor_queue_pressure"]
    }
    assert brief_rows[("bot", "audit-reader")]["queued_count"] == 2
    assert brief_rows[("manager", "globeiq::manager-a")]["level"] == "critical"

    assert page_resp.status_code == 200
    assert b"Token Governor Queue Caps" in page_resp.data
    assert b"audit-reader" in page_resp.data
    assert b"globeiq::manager-a" in page_resp.data
    assert b"critical 1.0" in page_resp.data


def test_work_overview_surfaces_partial_control_plane_data(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.task_calls = 0

        def list_tasks(self, **kwargs):
            self.task_calls += 1
            if self.task_calls % 2 == 1:
                return None
            return [
                {
                    "id": "recent-completed",
                    "bot_id": "lesson-writer",
                    "status": "completed",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2026-08-04T10:05:00+00:00",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm"},
                }
            ]

        def list_projects(self, **kwargs):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_bots(self, **kwargs):
            return [{"id": "globeiq-pm", "name": "GlobeIQ Manager"}]

        def list_workers(self, **kwargs):
            return []

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": []}

        def task_usage(self, **kwargs):
            return {"totals": {"total_tokens": 0}, "by_project": [], "by_manager": [], "by_provider_model": []}

        def unavailable_reason(self):
            return "Control plane timed out."

        def last_error(self):
            return {"status_code": 504, "detail": "task summaries exceeded timeout"}

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        page_resp = dashboard_client.get("/work")
    assert page_resp.status_code == 200
    assert b"partial data" in page_resp.data
    assert b"active/problem task summaries" in page_resp.data
    assert b"active unavailable" in page_resp.data
    assert b"recent unavailable" not in page_resp.data
    assert b"1 task summaries loaded" in page_resp.data
    assert b"task snapshot windows unavailable: active/problem" in page_resp.data

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        api_resp = dashboard_client.get("/api/work/overview")

    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert data["data_degraded"] is True
    assert data["data_warnings"][0]["source"] == "active/problem task summaries"
    assert data["data_warnings"][0]["status_code"] == 504
    assert data["task_snapshot"]["active_unavailable"] is True
    assert data["task_snapshot"]["recent_unavailable"] is False
    assert data["task_snapshot"]["merged_rows"] == 1
    assert data["snapshot_health"]["level"] == "critical"
    assert data["snapshot_health"]["reason"] == "task snapshot windows unavailable: active/problem"
    assert data["snapshot_health"]["unavailable_windows"] == ["active/problem"]
    assert data["snapshot_health"]["capped_windows"] == []
    assert data["usage"]["by_bot"] == []
    assert data["usage_health"]["level"] == "idle"


def test_work_overview_flags_snapshot_windows_at_limit(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            limit = int(kwargs["limit"])
            return [
                {
                    "id": f"task-{index}",
                    "bot_id": "lesson-worker",
                    "status": "queued",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm"},
                }
                for index in range(limit)
            ]

        def list_projects(self, **kwargs):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_bots(self, **kwargs):
            return [{"id": "globeiq-pm", "name": "GlobeIQ Manager"}]

        def list_workers(self, **kwargs):
            return []

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": []}

        def task_usage(self, **kwargs):
            return {"totals": {"total_tokens": 0}, "by_project": [], "by_manager": [], "by_bot": [], "by_provider_model": []}

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        api_resp = dashboard_client.get("/api/work/overview")

    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert data["task_snapshot"]["active_window_at_limit"] is True
    assert data["task_snapshot"]["recent_window_at_limit"] is True
    assert data["snapshot_health"]["level"] == "warning"
    assert data["snapshot_health"]["reason"] == "task snapshot windows at limit: active/problem, recent"
    assert data["snapshot_health"]["capped_windows"] == ["active/problem", "recent"]
    assert data["snapshot_health"]["unavailable_windows"] == []


def test_work_overview_usage_fallback_has_stable_shape(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return []

        def list_projects(self, **kwargs):
            return []

        def list_bots(self, **kwargs):
            return []

        def list_workers(self, **kwargs):
            return []

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": []}

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/work/overview")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["usage"]["totals"]["total_tokens"] == 0
    assert data["usage"]["totals"]["tasks_with_usage"] == 0
    assert data["usage"]["totals"]["tasks_without_usage"] == 0
    assert data["usage"]["by_project"] == []
    assert data["usage"]["by_manager"] == []
    assert data["usage"]["by_project_manager_bot"] == []
    assert data["usage"]["by_bot"] == []
    assert data["usage"]["by_provider_model"] == []
    assert data["chat_usage"]["totals"]["total_tokens"] == 0
    assert data["chat_usage"]["totals"]["messages_with_usage"] == 0
    assert data["chat_usage"]["totals"]["messages_without_usage"] == 0
    assert data["chat_usage"]["by_conversation"] == []
    assert data["chat_usage"]["by_bot"] == []
    assert data["chat_usage"]["by_provider_model"] == []
    assert data["usage_pressure_lanes"] == []
    assert data["chat_usage_pressure_lanes"] == []
    assert data["usage_health"] == {
        "level": "idle",
        "reason": "no token usage recorded in this window",
        "measured_tasks": 0,
        "missing_tasks": 0,
        "total_tasks": 0,
        "missing_ratio": 0.0,
        "total_tokens": 0,
    }
    assert data["chat_usage_health"] == {
        "level": "idle",
        "reason": "no chat token usage recorded in this window",
        "measured_messages": 0,
        "missing_messages": 0,
        "total_messages": 0,
        "missing_ratio": 0.0,
        "total_tokens": 0,
    }
    assert data["usage_brief"]["provider_model_attribution"] == {
        "level": "idle",
        "reason": "no worker token usage recorded in this window",
        "unknown_tokens": 0,
        "unknown_ratio": 0.0,
    }
    assert data["usage_brief"]["provider_model_spend"] == {
        "level": "idle",
        "label": "no spend",
        "detail": "No worker provider/model token spend recorded in this window.",
        "provider": "unknown",
        "model": "unknown",
        "total_tokens": 0,
        "top_tokens": 0,
        "top_ratio": 0.0,
    }
    assert data["chat_usage_brief"]["provider_model_attribution"] == {
        "level": "idle",
        "reason": "no chat token usage recorded in this window",
        "unknown_tokens": 0,
        "unknown_ratio": 0.0,
    }
    assert data["chat_usage_brief"]["provider_model_spend"] == {
        "level": "idle",
        "label": "no spend",
        "detail": "No chat provider/model token spend recorded in this window.",
        "provider": "unknown",
        "model": "unknown",
        "total_tokens": 0,
        "top_tokens": 0,
        "top_ratio": 0.0,
    }


def test_work_overview_flags_missing_provider_model_attribution(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return []

        def list_projects(self, **kwargs):
            return []

        def list_bots(self, **kwargs):
            return []

        def list_workers(self, **kwargs):
            return []

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": []}

        def task_usage(self, **kwargs):
            return {
                "totals": {"total_tokens": 200, "tasks_with_usage": 1, "tasks_without_usage": 0},
                "by_bot": [{"bot_id": "worker-bot", "total_tokens": 200, "tasks_with_usage": 1}],
                "by_provider_model": [],
                "token_governor": {"enabled": False, "limits": {}},
            }

        def chat_usage(self, **kwargs):
            return {
                "totals": {"total_tokens": 100, "messages_with_usage": 1, "messages_without_usage": 0},
                "by_bot": [{"bot_id": "chat-bot", "total_tokens": 100, "messages_with_usage": 1}],
                "by_provider_model": [{"provider": "unknown", "model": "", "total_tokens": 100, "messages_with_usage": 1}],
                "chat_token_governor": {"enabled": False, "limits": {}},
            }

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/work/brief")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["usage_brief"]["provider_model_attribution"] == {
        "level": "critical",
        "reason": "worker token usage exists but no provider/model attribution was reported",
        "unknown_tokens": 200,
        "unknown_ratio": 1.0,
    }
    assert data["usage_brief"]["provider_model_spend"]["level"] == "critical"
    assert data["usage_brief"]["provider_model_spend"]["label"] == "unattributed spend"
    assert data["chat_usage_brief"]["provider_model_attribution"] == {
        "level": "critical",
        "reason": "most chat token usage is missing provider/model attribution",
        "unknown_tokens": 100,
        "unknown_ratio": 1.0,
    }
    assert data["chat_usage_brief"]["provider_model_spend"]["level"] == "critical"
    assert data["chat_usage_brief"]["provider_model_spend"]["label"] == "unattributed spend"


def test_work_overview_renders_held_lane_without_loaded_tasks():
    overview = build_work_overview(
        projects=[{"id": "globeiq", "name": "GlobeIQ"}],
        bots=[{"id": "manager-a", "name": "Manager A"}],
        workers=[],
        tasks=[],
        holds=[
            {
                "id": "globeiq::manager-a",
                "project_id": "globeiq",
                "manager_id": "manager-a",
                "reason": "release checkpoint",
                "queued_task_count": "not numeric",
            }
        ],
    )

    assert overview["holds"][0]["queued_task_count"] == 0
    assert overview["projects"][0]["held"] is False
    manager = overview["projects"][0]["managers"][0]
    assert manager["held"] is True
    assert manager["hold"]["reason"] == "release checkpoint"


def test_work_stop_api_dry_run_filters_stoppable_project_manager_tasks(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            assert kwargs["include_content"] is False
            assert kwargs["statuses"] == ["blocked", "queued", "running"]
            return [
                {
                    "id": "running-target",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "queued-target",
                    "bot_id": "lesson-qc",
                    "status": "queued",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "completed-ignore",
                    "bot_id": "lesson-writer",
                    "status": "completed",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "other-manager-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-b"},
                },
                {
                    "id": "other-project-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "other", "root_pm_bot_id": "manager-a"},
                },
            ]

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/work/stop",
            json={"project_id": "globeiq", "manager_id": "manager-a", "dry_run": True},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "dry_run"
    assert data["matched_task_count"] == 2
    assert [task["id"] for task in data["tasks"]] == ["running-target", "queued-target"]


def test_work_orchestration_api_returns_bounded_run_task_details(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.request = None

        def list_tasks(self, **kwargs):
            self.request = kwargs
            return [
                {
                    "id": "older-running",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2000-01-01T00:00:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "manager-a",
                        "orchestration_id": "orch-target",
                        "step_id": "lesson_write",
                        "execution_provenance": {
                            "backend_type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                            "worker_id": "browser-worker-01",
                        },
                    },
                },
                {
                    "id": "newer-failed",
                    "bot_id": "lesson-qc",
                    "status": "failed",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2026-08-04T10:06:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "manager-a",
                        "orchestration_id": "orch-target",
                        "source": "lesson_audit",
                    },
                    "has_error": True,
                    "error_summary": {"type": "dict", "code": "browser_evidence_missing", "message": "No browser evidence."},
                },
                {
                    "id": "other-orch-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-other"},
                },
            ]

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.get("/api/work/orchestration?orchestration_id=orch-target&limit=1")
        all_resp = dashboard_client.get("/api/work/orchestration?orchestration_id=orch-target&limit=10")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["orchestration_id"] == "orch-target"
    assert data["count"] == 2
    assert data["counts"] == {"failed": 1, "running": 1}
    assert data["stoppable_count"] == 1
    assert data["truncated"] is True
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["id"] == "newer-failed"
    assert data["tasks"][0]["project_id"] == "globeiq"
    assert data["tasks"][0]["manager_id"] == "manager-a"
    assert data["tasks"][0]["source"] == "lesson_audit"
    assert data["tasks"][0]["error_code"] == "browser_evidence_missing"
    assert data["tasks"][0]["worker_id"] == ""
    running = next(task for task in all_resp.get_json()["tasks"] if task["id"] == "older-running")
    assert running["worker_id"] == "browser-worker-01"
    assert running["backend_type"] == "browser"
    assert running["provider"] == "browser"
    assert running["model"] == "browser-ui"
    assert running["age_basis"] == "updated_at"
    assert running["age_seconds"] >= 3600
    assert running["stale"] is True
    assert fake.request["orchestration_id"] == "orch-target"
    assert fake.request["include_content"] is False
    assert fake.request["limit"] == 1000


def test_work_orchestration_api_rejects_missing_orchestration_or_invalid_limit(dashboard_client):
    _login_admin(dashboard_client)

    missing = dashboard_client.get("/api/work/orchestration")
    bad_limit = dashboard_client.get("/api/work/orchestration?orchestration_id=orch-target&limit=wide")

    assert missing.status_code == 400
    assert "orchestration_id is required" in missing.get_data(as_text=True)
    assert bad_limit.status_code == 400
    assert "limit must be an integer" in bad_limit.get_data(as_text=True)


def test_work_orchestration_stop_api_dry_run_counts_cancellable_tasks(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.request = None

        def list_tasks(self, **kwargs):
            self.request = kwargs
            return [
                {
                    "id": "running-target",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-target"},
                },
                {
                    "id": "blocked-target",
                    "bot_id": "lesson-qc",
                    "status": "blocked",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-target"},
                },
                {
                    "id": "completed-target",
                    "bot_id": "lesson-writer",
                    "status": "completed",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-target"},
                },
                {
                    "id": "other-orch-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-other"},
                },
            ]

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.post(
            "/api/work/orchestration/stop",
            json={"orchestration_id": "orch-target", "dry_run": True},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "dry_run"
    assert data["task_count"] == 3
    assert data["cancellable_task_count"] == 2
    assert data["status_counts"] == {"blocked": 1, "completed": 1, "running": 1}
    assert [task["id"] for task in data["tasks"]] == ["running-target", "blocked-target"]
    assert fake.request["orchestration_id"] == "orch-target"
    assert fake.request["include_content"] is False
    assert fake.request["limit"] == 1000


def test_work_orchestration_stop_api_proxies_cancel_after_preview(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.cancel_request = None

        def list_tasks(self, **kwargs):
            return [
                {
                    "id": "queued-target",
                    "bot_id": "lesson-writer",
                    "status": "queued",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a", "orchestration_id": "orch-target"},
                }
            ]

        def cancel_orchestration(self, orchestration_id, reason=None):
            self.cancel_request = {"orchestration_id": orchestration_id, "reason": reason}
            return {"orchestration_id": orchestration_id, "cancelled_task_count": 1}

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.post(
            "/api/work/orchestration/stop",
            json={"orchestration_id": "orch-target", "reason": "test_orch_stop"},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["preview"]["cancellable_task_count"] == 1
    assert data["result"]["cancelled_task_count"] == 1
    assert fake.cancel_request == {"orchestration_id": "orch-target", "reason": "test_orch_stop"}


def test_work_orchestration_stop_api_rejects_missing_orchestration(dashboard_client):
    _login_admin(dashboard_client)

    resp = dashboard_client.post("/api/work/orchestration/stop", json={})

    assert resp.status_code == 400
    assert "orchestration_id is required" in resp.get_data(as_text=True)


def test_work_lane_api_returns_bounded_project_manager_task_details(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            assert kwargs["include_content"] is False
            assert kwargs["statuses"] == ["blocked", "failed", "queued", "retried", "running"]
            return [
                {
                    "id": "running-target",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2000-01-01T00:00:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "manager-a",
                        "orchestration_id": "orch-1",
                        "step_id": "lesson_write",
                        "execution_provenance": {
                            "backend_type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                            "worker_id": "browser-worker-01",
                        },
                    },
                },
                {
                    "id": "failed-target",
                    "bot_id": "lesson-qc",
                    "status": "failed",
                    "created_at": "2026-08-04T10:00:00+00:00",
                    "updated_at": "2026-08-04T10:06:00+00:00",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                    "has_error": True,
                    "error_summary": {"type": "dict", "code": "qc_failed", "message": "needs repair"},
                },
                {
                    "id": "completed-ignore",
                    "bot_id": "lesson-writer",
                    "status": "completed",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "other-manager-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-b"},
                },
            ]

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": [{"id": "globeiq::manager-a", "project_id": "globeiq", "manager_id": "manager-a", "reason": "checkpoint"}]}

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/work/lane?project_id=globeiq&manager_id=manager-a&limit=1")
        all_resp = dashboard_client.get("/api/work/lane?project_id=globeiq&manager_id=manager-a&limit=10")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    assert data["counts"] == {"failed": 1, "running": 1}
    assert data["stoppable_count"] == 1
    assert data["hold"]["reason"] == "checkpoint"
    assert data["truncated"] is True
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["id"] == "failed-target"
    assert data["tasks"][0]["error_type"] == "dict"
    assert data["tasks"][0]["error_code"] == "qc_failed"
    assert data["tasks"][0]["error_message"] == "needs repair"
    assert data["tasks"][0]["worker_id"] == ""
    all_tasks = all_resp.get_json()
    running = next(task for task in all_tasks["tasks"] if task["id"] == "running-target")
    assert running["worker_id"] == "browser-worker-01"
    assert running["backend_type"] == "browser"
    assert running["provider"] == "browser"
    assert running["model"] == "browser-ui"
    assert running["age_basis"] == "updated_at"
    assert running["age_seconds"] >= 3600
    assert running["stale"] is True


def test_work_lane_api_rejects_missing_project_or_invalid_limit(dashboard_client):
    _login_admin(dashboard_client)

    missing_project = dashboard_client.get("/api/work/lane")
    bad_limit = dashboard_client.get("/api/work/lane?project_id=globeiq&limit=wide")

    assert missing_project.status_code == 400
    assert "project_id is required" in missing_project.get_data(as_text=True)
    assert bad_limit.status_code == 400
    assert "limit must be an integer" in bad_limit.get_data(as_text=True)


def test_work_stop_api_cancels_selected_project_work_only(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.cancelled = []

        def list_tasks(self, **kwargs):
            assert kwargs["statuses"] == ["blocked", "queued", "running"]
            return [
                {
                    "id": "running-target",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "blocked-target",
                    "bot_id": "lesson-qc",
                    "status": "blocked",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-b"},
                },
                {
                    "id": "failed-ignore",
                    "bot_id": "lesson-writer",
                    "status": "failed",
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "manager-a"},
                },
                {
                    "id": "other-project-ignore",
                    "bot_id": "lesson-writer",
                    "status": "running",
                    "metadata": {"project_id": "other", "root_pm_bot_id": "manager-a"},
                },
            ]

        def cancel_task(self, task_id, reason=None):
            self.cancelled.append((task_id, reason))
            return {"id": task_id, "status": "cancelled"}

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.post(
            "/api/work/stop",
            json={"project_id": "globeiq", "reason": "test_stop"},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cancelled_task_count"] == 2
    assert data["failed_task_count"] == 0
    assert fake.cancelled == [("running-target", "test_stop"), ("blocked-target", "test_stop")]


def test_work_stop_api_rejects_missing_project(dashboard_client):
    _login_admin(dashboard_client)

    resp = dashboard_client.post("/api/work/stop", json={"manager_id": "manager-a"})

    assert resp.status_code == 400
    assert "project_id is required" in resp.get_data(as_text=True)


def test_work_hold_api_sets_and_releases_project_manager_scope(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.calls = []

        def set_work_dispatch_hold(self, **kwargs):
            self.calls.append(("hold", kwargs))
            return {"status": "held", "hold": kwargs}

        def release_work_dispatch_hold(self, **kwargs):
            self.calls.append(("release", kwargs))
            return {"status": "released", "holds": []}

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        hold_resp = dashboard_client.post(
            "/api/work/hold",
            json={
                "action": "hold",
                "project_id": "globeiq",
                "manager_id": "manager-a",
                "reason": "audit checkpoint",
            },
        )
        release_resp = dashboard_client.post(
            "/api/work/hold",
            json={"action": "release", "project_id": "globeiq", "manager_id": "manager-a"},
        )

    assert hold_resp.status_code == 200
    assert release_resp.status_code == 200
    assert fake.calls[0][0] == "hold"
    assert fake.calls[0][1]["project_id"] == "globeiq"
    assert fake.calls[0][1]["manager_id"] == "manager-a"
    assert fake.calls[0][1]["reason"] == "audit checkpoint"
    assert fake.calls[1][0] == "release"


def test_work_hold_api_rejects_invalid_action_or_missing_project(dashboard_client):
    _login_admin(dashboard_client)

    bad_action = dashboard_client.post("/api/work/hold", json={"action": "freeze", "project_id": "globeiq"})
    missing_project = dashboard_client.post("/api/work/hold", json={"action": "hold"})

    assert bad_action.status_code == 400
    assert "action must be hold or release" in bad_action.get_data(as_text=True)
    assert missing_project.status_code == 400
    assert "project_id is required" in missing_project.get_data(as_text=True)


def test_work_bot_cap_api_sets_and_clears_override(dashboard_client, tmp_path):
    from shared.settings_manager import SettingsManager

    _login_admin(dashboard_client)
    original_settings = SettingsManager._instance
    SettingsManager._instance = SettingsManager(str(tmp_path / "work-bot-cap.db"))
    try:
        SettingsManager._instance.set(
            "token_governor_bot_hourly_limits",
            json.dumps({"existing-bot": 12345}),
            changed_by="test",
        )

        set_resp = dashboard_client.post(
            "/api/work/bot-cap",
            json={"action": "set", "bot_id": "audit-reader", "hourly_limit": 50000},
        )
        assert set_resp.status_code == 200
        data = set_resp.get_json()
        assert data["token_governor_bot_hourly_limits"] == {
            "audit-reader": 50000,
            "existing-bot": 12345,
        }

        clear_resp = dashboard_client.post(
            "/api/work/bot-cap",
            json={"action": "clear", "bot_id": "audit-reader"},
        )
        assert clear_resp.status_code == 200
        data = clear_resp.get_json()
        assert data["token_governor_bot_hourly_limits"] == {"existing-bot": 12345}
    finally:
        SettingsManager._instance = original_settings


def test_work_bot_cap_api_rejects_invalid_payload(dashboard_client):
    _login_admin(dashboard_client)

    bad_action = dashboard_client.post("/api/work/bot-cap", json={"action": "freeze", "bot_id": "audit-reader"})
    missing_bot = dashboard_client.post("/api/work/bot-cap", json={"action": "set", "hourly_limit": 100})
    bad_limit = dashboard_client.post(
        "/api/work/bot-cap",
        json={"action": "set", "bot_id": "audit-reader", "hourly_limit": 0},
    )

    assert bad_action.status_code == 400
    assert "action must be set or clear" in bad_action.get_data(as_text=True)
    assert missing_bot.status_code == 400
    assert "bot_id is required" in missing_bot.get_data(as_text=True)
    assert bad_limit.status_code == 400
    assert "hourly_limit must be a positive integer" in bad_limit.get_data(as_text=True)


def test_work_chat_bot_cap_api_sets_and_clears_override(dashboard_client, tmp_path):
    from shared.settings_manager import SettingsManager

    _login_admin(dashboard_client)
    original_settings = SettingsManager._instance
    SettingsManager._instance = SettingsManager(str(tmp_path / "work-chat-bot-cap.db"))
    try:
        SettingsManager._instance.set(
            "token_governor_chat_bot_hourly_limits",
            json.dumps({"existing-chat-bot": 12345}),
            changed_by="test",
        )

        set_resp = dashboard_client.post(
            "/api/work/chat-bot-cap",
            json={"action": "set", "bot_id": "general-chat", "hourly_limit": 50000},
        )
        assert set_resp.status_code == 200
        data = set_resp.get_json()
        assert data["token_governor_chat_bot_hourly_limits"] == {
            "existing-chat-bot": 12345,
            "general-chat": 50000,
        }

        clear_resp = dashboard_client.post(
            "/api/work/chat-bot-cap",
            json={"action": "clear", "bot_id": "general-chat"},
        )
        assert clear_resp.status_code == 200
        data = clear_resp.get_json()
        assert data["token_governor_chat_bot_hourly_limits"] == {"existing-chat-bot": 12345}
    finally:
        SettingsManager._instance = original_settings


def test_work_chat_bot_cap_api_rejects_invalid_payload(dashboard_client):
    _login_admin(dashboard_client)

    bad_action = dashboard_client.post("/api/work/chat-bot-cap", json={"action": "freeze", "bot_id": "general-chat"})
    missing_bot = dashboard_client.post("/api/work/chat-bot-cap", json={"action": "set", "hourly_limit": 100})
    bad_limit = dashboard_client.post(
        "/api/work/chat-bot-cap",
        json={"action": "set", "bot_id": "general-chat", "hourly_limit": 0},
    )

    assert bad_action.status_code == 400
    assert "action must be set or clear" in bad_action.get_data(as_text=True)
    assert missing_bot.status_code == 400
    assert "bot_id is required" in missing_bot.get_data(as_text=True)
    assert bad_limit.status_code == 400
    assert "hourly_limit must be a positive integer" in bad_limit.get_data(as_text=True)
