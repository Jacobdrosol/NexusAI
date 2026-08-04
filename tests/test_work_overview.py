import bcrypt
from datetime import datetime, timezone
from unittest.mock import patch

from dashboard.work_overview import build_work_overview


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
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "orchestration_id": "orch-1"},
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
    assert overview["workers"]["queue_depth"] == 4
    assert overview["workers"]["online"] == 2
    assert overview["workers"]["offline_enabled"] == 1
    assert overview["workers"]["overloaded"] == 1
    assert overview["workers"]["disabled"] == 1
    assert overview["workers"]["queued_workers"] == 2
    assert overview["workers"]["issue_count"] == 3
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
    assert manager["freshness"]["stale_active"] == 1
    assert manager["freshness"]["stale_waiting"] == 1
    assert manager["held"] is True
    assert manager["hold"]["reason"] == "quality review"
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
                        "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "orchestration_id": "orch-1"},
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
                    "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm", "orchestration_id": "orch-1"},
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
                "totals": {"total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 2},
                "by_project": [{"project_id": "globeiq", "total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 2}],
                "by_manager": [{"project_id": "globeiq", "manager_id": "globeiq-pm", "total_tokens": 140, "tasks_with_usage": 1}],
                "by_provider_model": [{"provider": "ollama_cloud", "model": "qwen3.5:cloud", "total_tokens": 140, "tasks_with_usage": 1}],
            }

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.get("/work")

    assert resp.status_code == 200
    assert b"Work" in resp.data
    assert b"Needs Attention" in resp.data
    assert b"Attention Breakdown" in resp.data
    assert b"Problem tasks" in resp.data
    assert b"Usage gaps" in resp.data
    assert b"GlobeIQ" in resp.data
    assert b"GlobeIQ Manager" in resp.data
    assert b"lesson-writer" in resp.data
    assert b"Worker Load" in resp.data
    assert b"worker-a" in resp.data
    assert b"Worker Queue" in resp.data
    assert b"Worker Issues" in resp.data
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
    assert b"Usage Gaps" in resp.data
    assert b"2 missing usage" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:cloud" in resp.data
    assert b"Stale Work" in resp.data
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
    assert b"Work snapshot" in resp.data
    assert b"3 task summaries loaded" in resp.data
    assert b"Stop Project" in resp.data
    assert b"Stop Lane" in resp.data
    assert fake.task_calls[0]["statuses"] == ["blocked", "failed", "queued", "retried", "running"]
    assert fake.task_calls[0]["limit"] == 1000
    assert fake.task_calls[1]["limit"] == 250

    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        api_resp = dashboard_client.get("/api/work/overview")
    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert data["attention"]["problem_tasks"] == 1
    assert data["attention"]["stale_work"] == 2
    assert data["attention"]["metadata_gaps"] == 0
    assert data["attention"]["worker_issues"] == 2
    assert data["attention"]["usage_gaps"] == 2
    assert data["attention"]["total"] == 7
    assert data["attention"]["level"] == "critical"


def test_work_overview_routes_require_admin_role(dashboard_client):
    _login_user(dashboard_client, email="learner@test.com", role="learner")

    assert dashboard_client.get("/work").status_code == 403
    assert dashboard_client.get("/api/work/overview").status_code == 403
    assert dashboard_client.get("/api/work/lane?project_id=globeiq").status_code == 403
    assert dashboard_client.get("/api/work/orchestration?orchestration_id=orch-1").status_code == 403


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
    assert data["usage"]["by_provider_model"] == []


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
                    "updated_at": "2026-08-04T10:05:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "manager-a",
                        "orchestration_id": "orch-target",
                        "step_id": "lesson_write",
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
                    "updated_at": "2026-08-04T10:05:00+00:00",
                    "metadata": {
                        "project_id": "globeiq",
                        "root_pm_bot_id": "manager-a",
                        "orchestration_id": "orch-1",
                        "step_id": "lesson_write",
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
