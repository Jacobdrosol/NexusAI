"""Tests for bot connections APIs in dashboard."""

import bcrypt
from unittest.mock import patch

from dashboard.connections_service import normalize_database_dsn
from dashboard.connections_service import test_http_connection as run_http_connection_test


def _login_admin(client):
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
    resp = client.post("/login", data={"email": "admin@test.com", "password": pw}, follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_create_list_and_delete_bot_connection(dashboard_client):
    _login_admin(dashboard_client)

    bot_resp = dashboard_client.post("/api/bots", json={"name": "Conn Bot"})
    assert bot_resp.status_code == 201
    bot_id = str(bot_resp.get_json()["id"])

    schema = """
openapi: 3.1.0
info: {title: Demo, version: "1.0"}
servers:
  - url: https://api.example.com
paths:
  /health:
    get:
      operationId: healthCheck
      responses:
        "200":
          description: ok
"""
    create_resp = dashboard_client.post(
        f"/api/bots/{bot_id}/connections",
        json={
            "name": "Example API",
            "kind": "http",
            "description": "sample",
            "config": {"base_url": "https://api.example.com"},
            "auth": {"type": "api_key", "name": "X-API-Key", "api_key": "secret-token"},
            "schema_text": schema,
        },
    )
    assert create_resp.status_code == 201
    conn = create_resp.get_json()
    assert conn["name"] == "Example API"
    assert conn["auth"]["api_key"] == "[REDACTED]"
    assert len(conn["actions"]) == 1

    list_resp = dashboard_client.get(f"/api/bots/{bot_id}/connections")
    assert list_resp.status_code == 200
    rows = list_resp.get_json()
    assert len(rows) == 1
    assert rows[0]["id"] == conn["id"]

    actions_resp = dashboard_client.get(f"/api/connections/{conn['id']}/actions")
    assert actions_resp.status_code == 200
    actions = actions_resp.get_json()["actions"]
    assert actions[0]["operation_id"] == "healthCheck"

    update_resp = dashboard_client.put(
        f"/api/connections/{conn['id']}",
        json={
            "name": "Example API Updated",
            "description": "updated",
            "config": {"base_url": "https://globeiq.org", "timeout_seconds": 60},
            "auth": {"type": "api_key", "name": "X-GLOBEIQ-AGENT-KEY"},
            "schema_text": schema,
        },
    )
    assert update_resp.status_code == 200
    updated = update_resp.get_json()
    assert updated["name"] == "Example API Updated"
    assert updated["description"] == "updated"
    assert updated["config"]["base_url"] == "https://globeiq.org"
    assert updated["config"]["timeout_seconds"] == 60
    assert updated["auth"]["name"] == "X-GLOBEIQ-AGENT-KEY"
    assert updated["auth"]["api_key"] == "[REDACTED]"

    del_resp = dashboard_client.delete(f"/api/connections/{conn['id']}")
    assert del_resp.status_code == 204


def test_database_connection_test_endpoint(dashboard_client):
    _login_admin(dashboard_client)

    bot_resp = dashboard_client.post("/api/bots", json={"name": "DB Bot"})
    assert bot_resp.status_code == 201
    bot_id = str(bot_resp.get_json()["id"])

    create_resp = dashboard_client.post(
        f"/api/bots/{bot_id}/connections",
        json={
            "name": "Local SQLite",
            "kind": "database",
            "config": {"dsn": "sqlite:///:memory:", "readonly": True},
        },
    )
    assert create_resp.status_code == 201
    conn_id = create_resp.get_json()["id"]

    test_resp = dashboard_client.post(
        f"/api/connections/{conn_id}/test",
        json={"query": "SELECT 1 AS ok"},
    )
    assert test_resp.status_code == 200
    payload = test_resp.get_json()
    assert payload["ok"] is True
    assert payload["row_count"] >= 1


def test_project_database_connection_create_test_and_schema_ingest(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_project(self, project_id):
            return {"id": project_id, "name": project_id}

        def upsert_vault_item(self, body):
            return {"id": "vault-db-schema", **body}

        def last_error(self):
            return {}

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        create_resp = dashboard_client.post(
            "/api/projects/proj-db/connections",
            json={
                "name": "Project SQLite",
                "dsn": "sqlite:///:memory:",
                "description": "project db",
                "readonly": True,
            },
        )
        assert create_resp.status_code == 201
        connection = create_resp.get_json()
        connection_id = connection["id"]

        list_resp = dashboard_client.get("/api/projects/proj-db/connections")
        assert list_resp.status_code == 200
        rows = list_resp.get_json()
        assert len(rows) == 1
        assert rows[0]["name"] == "Project SQLite"

        test_resp = dashboard_client.post(
            f"/api/projects/proj-db/connections/{connection_id}/test",
            json={"query": "SELECT 1 AS ok"},
        )
        assert test_resp.status_code == 200
        assert test_resp.get_json()["ok"] is True

        schema_resp = dashboard_client.post(
            f"/api/projects/proj-db/connections/{connection_id}/schema-ingest",
            json={"namespace": "project:proj-db:data"},
        )
        assert schema_resp.status_code == 200
        body = schema_resp.get_json()
        assert body["ok"] is True
        assert body["vault_item"]["namespace"] == "project:proj-db:data"
        assert body["vault_item"]["source_type"] == "custom"
        assert body["vault_item"]["metadata"]["kind"] == "project_database_schema"
        assert body["connection"]["schema_totals"]["tables"] >= 0


def test_normalize_database_dsn_supports_postgres_keyword_string():
    dsn = normalize_database_dsn(
        "host=db.example.com port=5432 dbname=globeiq user=jacob password=secret sslmode=require"
    )
    assert dsn.startswith("postgresql+psycopg2://jacob:secret@db.example.com:5432/globeiq")
    assert "sslmode=require" in dsn


def test_normalize_database_dsn_supports_npgsql_style_string():
    dsn = normalize_database_dsn(
        "Host=localhost;Port=5432;Database=globeiq;Username=globeiq;Password=CHANGE_ME;Ssl Mode=Require;Trust Server Certificate=true"
    )
    assert dsn.startswith("postgresql+psycopg2://globeiq:CHANGE_ME@localhost:5432/globeiq")
    assert "sslmode=require" in dsn


def test_http_connection_can_skip_tls_verification(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def read(self, _size):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=0, context=None):
        captured["timeout"] = timeout
        captured["context"] = context
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr("dashboard.connections_service.urllib.request.urlopen", fake_urlopen)

    result = run_http_connection_test(
        config={"base_url": "https://100.113.128.92:5001", "timeout_seconds": 15, "verify_ssl": False},
        auth={"type": "api_key", "name": "X-GLOBEIQ-AGENT-KEY", "api_key": "secret"},
        schema_text="",
        payload={"method": "GET", "path": "/api/agent/courses"},
    )

    assert result["ok"] is True
    assert result["verify_ssl"] is False
    assert captured["url"] == "https://100.113.128.92:5001/api/agent/courses"
    assert captured["context"] is not None
