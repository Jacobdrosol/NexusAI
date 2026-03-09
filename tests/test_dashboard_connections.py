"""Tests for bot connections APIs in dashboard."""

import json
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


def test_existing_connection_can_be_attached_to_multiple_bots(dashboard_client):
    _login_admin(dashboard_client)

    first_bot = dashboard_client.post("/api/bots", json={"name": "Schema Bot One"})
    second_bot = dashboard_client.post("/api/bots", json={"name": "Schema Bot Two"})
    assert first_bot.status_code == 201
    assert second_bot.status_code == 201
    first_bot_id = str(first_bot.get_json()["id"])
    second_bot_id = str(second_bot.get_json()["id"])

    create_resp = dashboard_client.post(
        f"/api/bots/{first_bot_id}/connections",
        json={
            "name": "Platform Schema",
            "kind": "http",
            "description": "Reusable site schema",
            "config": {"base_url": "https://api.example.com"},
            "schema_text": '{"lesson_blocks":[{"variant":"paragraph"}]}',
        },
    )
    assert create_resp.status_code == 201
    connection_id = create_resp.get_json()["id"]

    attach_resp = dashboard_client.post(f"/api/bots/{second_bot_id}/connections/{connection_id}/attach")
    assert attach_resp.status_code == 200

    second_list_resp = dashboard_client.get(f"/api/bots/{second_bot_id}/connections")
    assert second_list_resp.status_code == 200
    second_rows = second_list_resp.get_json()
    assert len(second_rows) == 1
    assert second_rows[0]["id"] == connection_id
    assert second_rows[0]["name"] == "Platform Schema"

    detach_resp = dashboard_client.delete(f"/api/bots/{second_bot_id}/connections/{connection_id}/attach")
    assert detach_resp.status_code == 204

    first_list_resp = dashboard_client.get(f"/api/bots/{first_bot_id}/connections")
    assert first_list_resp.status_code == 200
    first_rows = first_list_resp.get_json()
    assert len(first_rows) == 1
    assert first_rows[0]["id"] == connection_id


def test_bot_export_includes_full_bot_config_and_connections(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self._last_error = {}
            self.bots = {
                "course-bot": {
                    "id": "course-bot",
                    "name": "Course Bot",
                    "role": "assistant",
                    "priority": 3,
                    "enabled": True,
                    "system_prompt": "Return strict JSON.",
                    "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
                    "routing_rules": {
                        "workflow": {"triggers": []},
                        "input_contract": {"required_fields": ["course_brief"]},
                        "input_transform": {"enabled": True, "template": {"course_brief": "{{payload.course_brief}}"}},
                        "output_contract": {"required_fields": ["course_shell"]},
                        "launch_profile": {"enabled": True, "label": "Run Bot", "payload": {"instruction": "start"}},
                    },
                    "workflow": {"triggers": []},
                    "input_contract": {"required_fields": ["course_brief"]},
                    "input_transform": {"enabled": True, "template": {"course_brief": "{{payload.course_brief}}"}},
                    "output_contract": {"required_fields": ["course_shell"]},
                    "launch_profile": {"enabled": True, "label": "Run Bot", "payload": {"instruction": "start"}},
                }
            }

        def get_bot(self, bot_id):
            bot = self.bots.get(bot_id)
            self._last_error = {} if bot else {"status_code": 404, "detail": "not found"}
            return bot

        def last_error(self):
            return self._last_error

    fake_cp = FakeCP()
    with patch("dashboard.cp_client.get_cp_client", return_value=fake_cp):
        create_resp = dashboard_client.post(
            "/api/bots/course-bot/connections",
            json={
                "name": "Example API",
                "kind": "http",
                "config": {"base_url": "https://api.example.com"},
                "auth": {"type": "api_key", "name": "X-API-Key", "api_key": "secret-token"},
                "schema_text": "openapi: 3.1.0",
            },
        )
        assert create_resp.status_code == 201

        export_resp = dashboard_client.get("/api/bots/course-bot/export")
        assert export_resp.status_code == 200
        bundle = json.loads(export_resp.data)
        assert bundle["schema_version"] == "nexusai.bot-export.v1"
        assert bundle["bot"]["id"] == "course-bot"
        assert bundle["bot"]["system_prompt"] == "Return strict JSON."
        assert bundle["bot"]["launch_profile"]["label"] == "Run Bot"
        assert len(bundle["connections"]) == 1
        assert bundle["connections"][0]["config"]["base_url"] == "https://api.example.com"
        assert bundle["connections"][0]["auth"]["api_key"] == "secret-token"


def test_bot_import_can_overwrite_existing_bot_config_and_connections(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self._last_error = {}
            self.bots = {
                "course-bot": {
                    "id": "course-bot",
                    "name": "Old Bot",
                    "role": "assistant",
                    "priority": 1,
                    "enabled": True,
                    "backends": [],
                    "routing_rules": {},
                    "workflow": None,
                }
            }

        def get_bot(self, bot_id):
            bot = self.bots.get(bot_id)
            self._last_error = {} if bot else {"status_code": 404, "detail": "not found"}
            return bot

        def update_bot(self, bot_id, body):
            self.bots[bot_id] = dict(body)
            return self.bots[bot_id]

        def create_bot(self, body):
            self.bots[body["id"]] = dict(body)
            return self.bots[body["id"]]

        def last_error(self):
            return self._last_error

    fake_cp = FakeCP()
    with patch("dashboard.cp_client.get_cp_client", return_value=fake_cp):
        old_conn = dashboard_client.post(
            "/api/bots/course-bot/connections",
            json={
                "name": "Old API",
                "kind": "http",
                "config": {"base_url": "https://old.example.com"},
            },
        )
        assert old_conn.status_code == 201

        bundle = {
            "schema_version": "nexusai.bot-export.v1",
            "bot": {
                "id": "course-bot",
                "name": "Imported Bot",
                "role": "planner",
                "priority": 7,
                "enabled": True,
                "system_prompt": "Imported prompt",
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4.1-mini"}],
                "routing_rules": {
                    "launch_profile": {"enabled": True, "label": "Imported Launch", "payload": {"instruction": "go"}}
                },
                "workflow": {"triggers": []},
                "launch_profile": {"enabled": True, "label": "Imported Launch", "payload": {"instruction": "go"}},
            },
            "connections": [
                {
                    "name": "Imported API",
                    "kind": "http",
                    "description": "imported",
                    "config": {"base_url": "https://api.example.com"},
                    "auth": {"type": "api_key", "name": "X-API-Key", "api_key": "new-secret"},
                    "schema_text": "openapi: 3.1.0",
                    "enabled": True,
                }
            ],
        }

        conflict_resp = dashboard_client.post("/api/bots/import", json={"bundle": bundle})
        assert conflict_resp.status_code == 409

        import_resp = dashboard_client.post("/api/bots/import", json={"bundle": bundle, "overwrite": True})
        assert import_resp.status_code == 200
        body = import_resp.get_json()
        assert body["overwritten"] is True
        assert body["bot"]["name"] == "Imported Bot"

        list_resp = dashboard_client.get("/api/bots/course-bot/connections")
        assert list_resp.status_code == 200
        rows = list_resp.get_json()
        assert len(rows) == 1
        assert rows[0]["name"] == "Imported API"


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
