import bcrypt
from datetime import datetime, timezone
from unittest.mock import patch

from dashboard.work_overview import build_work_overview


def _login_admin(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        if db.query(User).count() == 0:
            db.add(User(email="admin@test.com", password_hash=password_hash, role="admin", is_active=True))
            db.commit()
    finally:
        db.close()

    resp = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


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
        ],
        holds=[
            {
                "id": "globeiq::globeiq-pm",
                "project_id": "globeiq",
                "manager_id": "globeiq-pm",
                "reason": "quality review",
                "queued_task_count": 1,
                "bot_count": 1,
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
                "metadata": {"project_id": "globeiq", "root_pm_bot_id": "globeiq-pm"},
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
    assert overview["workers"]["online"] == 1
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
    assert manager["latest_tasks"][0]["age_label"] == "1h 5m"
    assert overview["recent_problem_tasks"][0]["id"] == "task-failed"
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
            ]

        def list_projects(self, **kwargs):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_bots(self, **kwargs):
            return [{"id": "globeiq-pm", "name": "GlobeIQ Manager", "role": "project-manager"}]

        def list_workers(self, **kwargs):
            return [{"id": "worker-a", "name": "Worker A", "status": "online", "enabled": True, "metrics": {"queue_depth": 2, "load": 0.25}}]

        def list_work_dispatch_holds(self, **kwargs):
            return {"holds": [{"id": "globeiq::*", "project_id": "globeiq", "manager_id": "", "reason": "operator hold"}]}

        def task_usage(self, **kwargs):
            return {
                "window": {"hours": 24},
                "totals": {"total_tokens": 140},
                "by_project": [{"project_id": "globeiq", "total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 0}],
                "by_manager": [{"project_id": "globeiq", "manager_id": "globeiq-pm", "total_tokens": 140, "tasks_with_usage": 1}],
                "by_provider_model": [{"provider": "ollama_cloud", "model": "qwen3.5:cloud", "total_tokens": 140, "tasks_with_usage": 1}],
            }

    fake = FakeCP()
    with patch("dashboard.routes.work.get_cp_client", return_value=fake):
        resp = dashboard_client.get("/work")

    assert resp.status_code == 200
    assert b"Work" in resp.data
    assert b"GlobeIQ" in resp.data
    assert b"GlobeIQ Manager" in resp.data
    assert b"lesson-writer" in resp.data
    assert b"Worker Load" in resp.data
    assert b"worker-a" in resp.data
    assert b"Worker Queue" in resp.data
    assert b"Metadata Gaps" in resp.data
    assert b"Loaded task summaries include project and manager metadata." in resp.data
    assert b"Orchestrations" in resp.data
    assert b"orch-1" in resp.data
    assert b"Problem Sources" in resp.data
    assert b"Usage By Project And Manager" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:cloud" in resp.data
    assert b"Stale Work" in resp.data
    assert b"Oldest" in resp.data
    assert b"Held Lanes" in resp.data
    assert b"operator hold" in resp.data
    assert b"Release Project" in resp.data
    assert b"dry_run: true" in resp.data
    assert b"Stop preview failed" in resp.data
    assert b"Lane Details" in resp.data
    assert b"showLaneDetails" in resp.data
    assert b"Work snapshot" in resp.data
    assert b"2 task summaries loaded" in resp.data
    assert b"Stop Project" in resp.data
    assert b"Stop Lane" in resp.data
    assert fake.task_calls[0]["statuses"] == ["blocked", "failed", "queued", "retried", "running"]
    assert fake.task_calls[0]["limit"] == 1000
    assert fake.task_calls[1]["limit"] == 250


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
                    "error": {"code": "qc_failed", "message": "needs repair"},
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
