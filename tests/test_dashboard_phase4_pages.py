"""Smoke tests for new Phase 4 dashboard pages."""

import bcrypt
from unittest.mock import patch


def _login_admin(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    pw = "password123"
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        if db.query(User).count() == 0:
            db.add(User(email="admin@test.com", password_hash=pw_hash, role="admin", is_active=True))
            db.commit()
    finally:
        db.close()

    resp = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": pw},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_projects_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/projects")
    assert resp.status_code == 200
    assert b"Projects" in resp.data


def test_project_detail_page_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/projects/proj-x")
    assert resp.status_code == 502
    assert b"Project Detail" in resp.data


def test_project_detail_page_renders_with_partial_github_status(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_project(self, project_id):
            return {
                "id": project_id,
                "name": "GlobeIQ",
                "mode": "isolated",
                "enabled": True,
                "description": "test project",
                "settings_overrides": {},
                "bridge_project_ids": [],
                "bot_ids": [],
            }

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "mode": "isolated", "enabled": True, "bridge_project_ids": [], "bot_ids": []}]

        def list_bots(self):
            return []

        def list_tasks(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

        def get_project_github_status(self, project_id):
            return {"connected": True}

        def list_project_github_webhook_events(self, project_id, limit=30):
            return {"events": []}

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/projects/globeiq")

    assert resp.status_code == 200
    assert b"GitHub Integration (PAT)" in resp.data
    assert b"Connection Flags" in resp.data


def test_chat_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/chat")
    assert resp.status_code == 200
    assert b"Chat" in resp.data
    assert b"New Conversation" in resp.data


def test_vault_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/vault")
    assert resp.status_code == 200
    assert b"Vault" in resp.data
    assert b"Upload / Ingest" in resp.data


def test_bot_detail_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    from dashboard.db import get_db
    from dashboard.models import Bot

    db = get_db()
    try:
        bot = Bot(name="Detail Bot", role="assistant", priority=1, enabled=True, backends="[]", routing_rules="{}")
        db.add(bot)
        db.commit()
        db.refresh(bot)
        bot_id = bot.id
    finally:
        db.close()

    resp = dashboard_client.get(f"/bots/{bot_id}")
    assert resp.status_code == 200
    assert b"Task Board" in resp.data
    assert b"Backend Chain Editor" in resp.data
    assert b"Backlog" in resp.data


def test_chat_ingest_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/chat/ingest", json={})
    assert resp.status_code == 400


def test_chat_message_to_vault_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/chat/message-to-vault", json={})
    assert resp.status_code == 400


def test_chat_stream_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/chat/stream", json={})
    assert resp.status_code == 400


def test_chat_message_api_surfaces_control_plane_error(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            return None

        def last_error(self):
            return {"status_code": 400, "detail": "Bot backend chain is empty"}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 400
    assert b"Bot backend chain is empty" in resp.data


def test_chat_stream_forwards_control_plane_auth_header(dashboard_client):
    _login_admin(dashboard_client)

    class FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=True):
            yield "event: done"
            yield 'data: {"ok":true}'

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def _headers(self):
            return {"X-Nexus-API-Key": "cp-token"}

    fake_cp = FakeCP()
    captured = {}

    def _fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeStreamResponse()

    with patch("dashboard.routes.chat.get_cp_client", return_value=fake_cp), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/chat/conversations/c1/stream")
    assert captured["headers"]["X-Nexus-API-Key"] == "cp-token"


def test_chat_orchestration_graph_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/chat/orchestrations/test-orch/graph")
    assert resp.status_code == 502


def test_project_github_pat_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/projects/proj-x/github/pat", json={})
    assert resp.status_code == 400


def test_project_github_pat_api_surfaces_control_plane_error(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def connect_project_github_pat(self, **kwargs):
            return None

        def last_error(self):
            return {
                "status_code": 400,
                "detail": "GitHub validation failed: 404 Not Found for branch Main",
            }

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/projects/proj-x/github/pat",
            json={"token": "ghp_x", "repo_full_name": "owner/repo", "validate": True},
        )

    assert resp.status_code == 400
    assert b"GitHub validation failed" in resp.data


def test_project_github_status_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/projects/proj-x/github/status")
    assert resp.status_code == 502


def test_project_webhook_secret_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/projects/proj-x/github/webhook/secret", json={})
    assert resp.status_code == 400


def test_project_webhook_events_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/projects/proj-x/github/webhook/events")
    assert resp.status_code == 502


def test_project_github_context_sync_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/projects/proj-x/github/context/sync", json={})
    assert resp.status_code == 502


def test_project_pr_review_config_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/projects/proj-x/github/pr-review/config", json={"enabled": True, "bot_id": "bot1"})
    assert resp.status_code == 502


def test_worker_detail_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    from dashboard.db import get_db
    from dashboard.models import Worker

    db = get_db()
    try:
        worker = Worker(name="Worker Detail", host="localhost", port=8001, status="online", capabilities="[]", metrics="{}")
        db.add(worker)
        db.commit()
        db.refresh(worker)
        worker_id = worker.id
    finally:
        db.close()

    resp = dashboard_client.get(f"/workers/{worker_id}")
    assert resp.status_code == 200
    assert b"Resource Snapshot" in resp.data
    assert b"Recent Signals" in resp.data
    assert b"GPU Activity" in resp.data


def test_settings_page_loads_for_admin(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/settings")
    assert resp.status_code == 200
    assert b"Settings" in resp.data
    assert b"Export/Import" in resp.data
    assert b"Audit Log" in resp.data
    assert b"Deploy" in resp.data
    assert b'data-target="section-export-import"' in resp.data
    assert b'data-target="section-audit-log"' in resp.data
    assert b'data-target="section-deploy"' in resp.data


def test_worker_live_endpoint_returns_payload(dashboard_client):
    _login_admin(dashboard_client)
    from dashboard.db import get_db
    from dashboard.models import Worker

    db = get_db()
    try:
        worker = Worker(name="Worker Live", host="localhost", port=8001, status="online", capabilities="[]", metrics="{}")
        db.add(worker)
        db.commit()
        db.refresh(worker)
        worker_id = worker.id
    finally:
        db.close()

    resp = dashboard_client.get(f"/api/workers/{worker_id}/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "worker" in data
    assert "running_tasks" in data


def test_create_bot_uses_control_plane_when_available(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.created = None

        def list_bots(self):
            return [{"id": "assistant-bot"}]

        def create_bot(self, body):
            self.created = body
            return body

    fake_cp = FakeCP()
    with patch("dashboard.cp_client.get_cp_client", return_value=fake_cp):
        resp = dashboard_client.post(
            "/api/bots",
            json={"name": "My Test Bot", "role": "assistant", "priority": 3},
        )

    assert resp.status_code == 201
    data = resp.get_json()
    assert data["id"] == "my-test-bot"
    assert data["name"] == "My Test Bot"
    assert fake_cp.created["backends"] == []


def test_vault_upload_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/vault/upload", data={"source_mode": "paste"})
    assert resp.status_code == 400


def test_vault_bulk_delete_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/vault/bulk-delete", json={})
    assert resp.status_code == 400


def test_vault_namespaces_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/vault/namespaces")
    assert resp.status_code == 502


def test_overview_page_shows_enhanced_sections(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/")
    assert resp.status_code == 200
    assert b"Open-Source Setup Checklist" in resp.data
    assert b"Control Plane Checks" in resp.data
    assert b"Control plane health and auth" in resp.data
    assert b"/v1/projects" in resp.data
    assert b"Required complete" in resp.data
    assert b"System Alerts" in resp.data
    assert b"Recent Activity" in resp.data
    assert b"Worker Health" in resp.data
    assert b"Quick Links" in resp.data
