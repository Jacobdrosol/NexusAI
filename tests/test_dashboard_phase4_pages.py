"""Smoke tests for new Phase 4 dashboard pages."""

import bcrypt


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


def test_chat_orchestration_graph_api_handles_unavailable_cp(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/chat/orchestrations/test-orch/graph")
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
    assert b"Live Metrics" in resp.data
    assert b"Resource Graphs" in resp.data


def test_settings_page_loads_for_admin(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/settings")
    assert resp.status_code == 200
    assert b"Settings" in resp.data


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
    assert b"System Alerts" in resp.data
    assert b"Recent Activity" in resp.data
    assert b"Worker Health" in resp.data
    assert b"Quick Links" in resp.data
