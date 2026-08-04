import bcrypt
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
    assert overview["recent_problem_tasks"][0]["id"] == "task-failed"


def test_work_page_renders_project_manager_and_worker_load(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            assert kwargs["include_content"] is False
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

        def task_usage(self, **kwargs):
            return {
                "window": {"hours": 24},
                "totals": {"total_tokens": 140},
                "by_project": [{"project_id": "globeiq", "total_tokens": 140, "tasks_with_usage": 1, "tasks_without_usage": 0}],
                "by_manager": [{"project_id": "globeiq", "manager_id": "globeiq-pm", "total_tokens": 140, "tasks_with_usage": 1}],
                "by_provider_model": [{"provider": "ollama_cloud", "model": "qwen3.5:cloud", "total_tokens": 140, "tasks_with_usage": 1}],
            }

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/work")

    assert resp.status_code == 200
    assert b"Work" in resp.data
    assert b"GlobeIQ" in resp.data
    assert b"GlobeIQ Manager" in resp.data
    assert b"lesson-writer" in resp.data
    assert b"Worker Load" in resp.data
    assert b"worker-a" in resp.data
    assert b"Worker Queue" in resp.data
    assert b"Usage By Project And Manager" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:cloud" in resp.data
