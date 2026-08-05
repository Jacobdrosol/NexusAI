from unittest.mock import patch

import bcrypt

from dashboard.bot_tooling_status import build_bot_tooling_status


def _login_admin(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        db.add(User(email="admin@test.com", password_hash=password_hash, role="admin", is_active=True))
        db.commit()
    finally:
        db.close()
    response = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": password},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}


def test_bot_tooling_status_groups_blocked_worker_tool_causes():
    status = build_bot_tooling_status(
        bots=[
            {
                "id": "browser-bot",
                "name": "Browser Bot",
                "role": "browser-inspector",
                "enabled": True,
                "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                "execution_policy": {"required_worker_tools": ["browser-ui"]},
            },
            {
                "id": "ready-bot",
                "name": "Ready Bot",
                "enabled": True,
                "backends": [{"worker_id": "llm-worker", "type": "remote_llm"}],
            },
            {
                "id": "disabled-bot",
                "name": "Disabled Bot",
                "enabled": False,
            },
        ],
        readiness_payload={
            "readiness": [
                {
                    "bot_id": "browser-bot",
                    "state": "blocked",
                    "ready": False,
                    "checks": [
                        {
                            "status": "failed",
                            "message": "Worker 'browser-worker' browser runtime is not ready: browser_session_check_failed",
                        }
                    ],
                },
                {"bot_id": "ready-bot", "state": "ready", "ready": True, "checks": []},
                {"bot_id": "disabled-bot", "state": "disabled", "ready": False, "checks": []},
            ]
        },
        workers=[
            {"id": "browser-worker", "status": "online", "enabled": True},
            {"id": "llm-worker", "status": "online", "enabled": True},
        ],
        worker_probes_payload={
            "probes": [
                {"worker_id": "browser-worker", "probe_status": "degraded"},
                {"worker_id": "llm-worker", "probe_status": "ready"},
            ]
        },
    )

    assert status["summary"]["ready"] == 1
    assert status["summary"]["blocked"] == 1
    assert status["summary"]["disabled"] == 1
    assert status["summary"]["tooling_bot_count"] == 1
    assert status["required_tools"] == [{"tool": "browser-ui", "bot_count": 1}]
    assert status["blocked_groups"][0]["category"] == "browser_session"
    assert status["blocked_groups"][0]["label"] == "Authenticated browser session"
    assert "site account can exist" in status["blocked_groups"][0]["detail"]
    assert status["blocked_groups"][0]["bots"][0]["workers"][0]["probe_status"] == "degraded"


def test_bots_page_surfaces_tooling_readiness_panel(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self._last_error = {}

        def list_bots(self):
            return [
                {
                    "id": "browser-bot",
                    "name": "Browser Bot",
                    "role": "browser-inspector",
                    "enabled": True,
                    "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                    "execution_policy": {"required_worker_tools": ["browser-ui"]},
                }
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "browser-bot",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "browser_session_check_failed"}],
                    }
                ]
            }

        def list_workers(self):
            return [{"id": "browser-worker", "name": "Browser Worker", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {"probes": [{"worker_id": "browser-worker", "probe_status": "degraded"}]}

        def list_schedules(self, limit=200):
            return {"schedules": []}

        def list_models(self):
            return []

        def list_keys(self):
            return []

        def list_projects(self):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        page = dashboard_client.get("/bots")
        api = dashboard_client.get("/api/bots/tooling-status")

    assert page.status_code == 200
    assert b"Bot Tooling Readiness" in page.data
    assert b"Authenticated browser session" in page.data
    assert b"site account can exist" in page.data
    assert b"browser-ui" in page.data
    assert api.status_code == 200
    payload = api.get_json()
    assert payload["summary"]["blocked"] == 1
    assert payload["blocked_groups"][0]["category"] == "browser_session"
    assert payload["blocked_groups"][0]["label"] == "Authenticated browser session"


def test_bots_tooling_status_surfaces_partial_control_plane_data(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return [
                {
                    "id": "browser-bot",
                    "name": "Browser Bot",
                    "role": "browser-inspector",
                    "enabled": True,
                    "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                    "execution_policy": {"required_worker_tools": ["browser-ui"]},
                }
            ]

        def list_bot_readiness(self):
            return None

        def list_schedules(self, limit=200):
            return {"schedules": []}

        def list_workers(self):
            return [{"id": "browser-worker", "name": "Browser Worker", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {"probes": [{"worker_id": "browser-worker", "probe_status": "ready"}]}

        def list_models(self):
            return []

        def list_keys(self):
            return []

        def list_projects(self):
            return []

        def last_error(self):
            return {"status_code": 503, "detail": "readiness endpoint unavailable"}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        page = dashboard_client.get("/bots")
        api = dashboard_client.get("/api/bots/tooling-status")

    assert page.status_code == 200
    assert b"Bot tooling data is incomplete" in page.data
    assert b"bot readiness" in page.data
    assert b"readiness endpoint unavailable" in page.data
    assert api.status_code == 200
    payload = api.get_json()
    assert payload["data_degraded"] is True
    assert payload["data_warnings"][0]["source"] == "bot readiness"
    assert payload["data_warnings"][0]["detail"] == "readiness endpoint unavailable"
