"""Smoke tests for new Phase 4 dashboard pages."""

import bcrypt
import io
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
    assert resp.status_code == 200
    assert b"Project Detail" in resp.data
    assert b"Control plane unavailable or project not found." in resp.data


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
    assert b"Project Data Vault" in resp.data
    assert b"Project Database Context" in resp.data
    assert b"GitHub Integration (PAT)" in resp.data
    assert b"Connection Flags" in resp.data
    assert b"Run Data Ingest" in resp.data


def test_project_data_folder_and_upload_apis_write_files(dashboard_client, tmp_path, monkeypatch):
    _login_admin(dashboard_client)
    monkeypatch.setenv("NEXUSAI_PROJECT_DATA_ROOT", str(tmp_path))

    class FakeCP:
        def get_project(self, project_id):
            return {"id": project_id, "name": project_id}

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        folder_resp = dashboard_client.post(
            "/api/projects/proj-data/data/folders",
            json={"parent_path": "docs", "folder_name": "specs"},
        )
        assert folder_resp.status_code == 201

        upload_resp = dashboard_client.post(
            "/api/projects/proj-data/data/upload",
            data={
                "target_path": "docs/specs",
                "files": (io.BytesIO(b"hello project vault"), "overview.md"),
                "relative_paths": "",
            },
            content_type="multipart/form-data",
        )
        assert upload_resp.status_code == 201

        duplicate_upload_resp = dashboard_client.post(
            "/api/projects/proj-data/data/upload",
            data={
                "target_path": "docs/specs",
                "files": (io.BytesIO(b"hello project vault duplicate"), "overview.md"),
                "relative_paths": "",
            },
            content_type="multipart/form-data",
        )
        assert duplicate_upload_resp.status_code == 201

        folder_upload_resp = dashboard_client.post(
            "/api/projects/proj-data/data/upload",
            data={
                "target_path": "docs",
                "files": [
                    (io.BytesIO(b"# Roadmap"), "roadmap.md"),
                    (io.BytesIO(b"ERD"), "schema.txt"),
                ],
                "relative_paths": [
                    "product-specs/roadmap.md",
                    "product-specs/diagrams/schema.txt",
                ],
            },
            content_type="multipart/form-data",
        )
        assert folder_upload_resp.status_code == 201

        files_resp = dashboard_client.get("/api/projects/proj-data/data/files")
        assert files_resp.status_code == 200
        body = files_resp.get_json()
        entries = body["entries"]
        assert any(e["path"] == "docs/specs" and e["type"] == "directory" for e in entries)
        assert any(e["path"] == "docs/specs/overview.md" and e["type"] == "file" for e in entries)
        assert any(e["path"] == "docs/specs/(1) overview.md" and e["type"] == "file" for e in entries)
        assert any(e["path"] == "docs/product-specs/roadmap.md" and e["type"] == "file" for e in entries)
        assert any(e["path"] == "docs/product-specs/diagrams/schema.txt" and e["type"] == "file" for e in entries)

        delete_resp = dashboard_client.post(
            "/api/projects/proj-data/data/delete",
            json={"paths": ["docs/specs/overview.md", "docs/product-specs"]},
        )
        assert delete_resp.status_code == 200
        deleted = delete_resp.get_json()["deleted"]
        assert any(item["type"] == "file" and item["path"] == "docs/specs/overview.md" for item in deleted)
        assert any(item["type"] == "directory" and item["path"] == "docs/product-specs" for item in deleted)

        files_resp = dashboard_client.get("/api/projects/proj-data/data/files")
        assert files_resp.status_code == 200
        entries = files_resp.get_json()["entries"]
        assert not any(e["path"] == "docs/specs/overview.md" for e in entries)
        assert not any(e["path"].startswith("docs/product-specs") for e in entries)
        assert any(e["path"] == "docs/specs/(1) overview.md" for e in entries)

        delete_defaults_resp = dashboard_client.post(
            "/api/projects/proj-data/data/delete",
            json={"paths": ["docs", "exports", "inbox"]},
        )
        assert delete_defaults_resp.status_code == 200

        files_resp = dashboard_client.get("/api/projects/proj-data/data/files")
        assert files_resp.status_code == 200
        entries = files_resp.get_json()["entries"]
        assert not any(e["path"] == "docs" for e in entries)
        assert not any(e["path"] == "exports" for e in entries)
        assert not any(e["path"] == "inbox" for e in entries)
        assert any(e["path"] == "notes" for e in entries)


def test_project_data_ingest_status_and_start_apis(dashboard_client, tmp_path, monkeypatch):
    _login_admin(dashboard_client)
    monkeypatch.setenv("NEXUSAI_PROJECT_DATA_ROOT", str(tmp_path))

    class FakeCP:
        def get_project(self, project_id):
            return {"id": project_id, "name": project_id}

        def upsert_vault_item(self, body):
            return {"id": "vault-1", **body}

        def last_error(self):
            return {}

    project_root = tmp_path / "proj-ingest" / "docs"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "readme.md").write_text("hello world", encoding="utf-8")

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.project_data_ingest.get_cp_client", return_value=FakeCP()):
        start_resp = dashboard_client.post(
            "/api/projects/proj-ingest/data/ingest",
            json={"namespace": "project:proj-ingest:data"},
        )
        assert start_resp.status_code == 200

        status_resp = dashboard_client.get("/api/projects/proj-ingest/data/ingest")
        assert status_resp.status_code == 200
        body = status_resp.get_json()
        assert body["project_id"] == "proj-ingest"
        assert body["status"] in {"queued", "running", "completed", "completed_with_errors"}


def test_chat_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/chat")
    assert resp.status_code == 200
    assert b"Chat" in resp.data
    assert b"New Conversation" in resp.data
    assert b"create-convo-scope" in resp.data
    assert b"create-convo-project-id" in resp.data
    assert b"create-convo-bridge-project-ids" in resp.data


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
    assert b"Workflow Orchestration" in resp.data
    assert b"Run History" in resp.data
    assert b"Run Test" in resp.data
    assert b"Task Board" in resp.data
    assert b"Backend Chain Editor" in resp.data
    assert b"Run Input Contract" in resp.data
    assert b"Input Transform" in resp.data
    assert b"Output Contract" in resp.data
    assert b"Payload Transform" in resp.data
    assert b"Saved Launch Profile" in resp.data
    assert b"Backlog" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:cloud" in resp.data
    assert b"Auto: 1024 for local Ollama chat" in resp.data
    assert b"Context Window" in resp.data
    assert b"GPU Layers" in resp.data


def test_bot_test_run_api_proxies_to_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            return {"id": "task-123", "bot_id": bot_id, "payload": payload, "metadata": metadata}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/bots/bot-1/test-run",
            json={"payload": {"instruction": "hello"}},
        )

    assert resp.status_code == 201
    assert resp.get_json()["id"] == "task-123"


def test_bot_launch_api_uses_saved_launch_profile(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Course Intake",
                "role": "assistant",
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Run Course Pipeline",
                        "payload": {"topic": "AP World History"},
                        "project_id": "globeiq",
                        "priority": 2,
                    }
                },
            }

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            return {"id": "task-launch-1", "bot_id": bot_id, "payload": payload, "metadata": metadata}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post("/api/bots/course-intake/launch", json={})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["payload"]["topic"] == "AP World History"
    assert body["metadata"]["project_id"] == "globeiq"


def test_bot_launch_api_marks_pipeline_runs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Course Intake",
                "role": "assistant",
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Run Course Pipeline",
                        "payload": {"topic": "AP World History"},
                        "is_pipeline": True,
                        "pipeline_name": "Course Generation Pipeline",
                    }
                },
            }

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            return {"id": "task-launch-2", "bot_id": bot_id, "payload": payload, "metadata": metadata}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post("/api/bots/course-intake/launch", json={})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["metadata"]["source"] == "saved_launch_pipeline"
    assert body["metadata"]["pipeline_name"] == "Course Generation Pipeline"
    assert body["metadata"]["pipeline_entry_bot_id"] == "course-intake"
    assert body["pipeline_id"]


def test_bot_artifact_api_and_download_proxy_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot_artifact(self, bot_id, artifact_id):
            return {
                "id": artifact_id,
                "task_id": "task-1",
                "bot_id": bot_id,
                "kind": "result",
                "label": "Task Result",
                "content": '{"ok":true}',
                "path": None,
                "metadata": {},
                "created_at": "2026-03-08T00:00:00+00:00",
            }

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        artifact_resp = dashboard_client.get("/api/bots/bot-1/artifacts/art-1")
        download_resp = dashboard_client.get("/api/bots/bot-1/artifacts/art-1/download")

    assert artifact_resp.status_code == 200
    assert artifact_resp.get_json()["id"] == "art-1"
    assert download_resp.status_code == 200
    assert "attachment" in download_resp.headers.get("Content-Disposition", "")


def test_tasks_page_shows_quick_launch_buttons(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return [
                {
                    "id": "task-running-1",
                    "bot_id": "course-outline",
                    "status": "running",
                    "payload": {"instruction": "go"},
                    "result": None,
                    "error": None,
                    "created_at": "2026-03-08T10:00:00+00:00",
                    "updated_at": "2026-03-08T10:01:00+00:00",
                    "metadata": {"project_id": "proj-1"},
                },
                {
                    "id": "task-queued-1",
                    "bot_id": "course-unit-builder",
                    "status": "queued",
                    "payload": {"instruction": "wait"},
                    "result": None,
                    "error": None,
                    "created_at": "2026-03-08T10:02:00+00:00",
                    "updated_at": "2026-03-08T10:02:00+00:00",
                    "metadata": {},
                },
                {
                    "id": "task-completed-1",
                    "bot_id": "course-intake",
                    "status": "completed",
                    "payload": {"instruction": "done"},
                    "result": {"ok": True},
                    "error": None,
                    "created_at": "2026-03-08T09:50:00+00:00",
                    "updated_at": "2026-03-08T10:03:00+00:00",
                    "metadata": {},
                },
            ]

        def list_bots(self):
            return [
                {
                    "id": "course-intake",
                    "name": "Course Intake",
                    "role": "assistant",
                    "routing_rules": {
                        "launch_profile": {
                            "enabled": True,
                            "label": "Run Course Pipeline",
                            "payload": {"topic": "AP World History"},
                            "show_on_tasks": True,
                        }
                    },
                }
            ]

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/tasks")

    assert resp.status_code == 200
    assert b"Quick Launch" in resp.data
    assert b"Run Course Pipeline" in resp.data
    assert b"Running Now" in resp.data
    assert b"Queued / Blocked" in resp.data
    assert b"Recent Completed (24h)" in resp.data
    assert b"Task Detail" in resp.data
    assert b"Load only when needed" in resp.data


def test_tasks_api_summary_and_download(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_task(self, task_id):
            return {
                "id": task_id,
                "bot_id": "course-outline",
                "status": "completed",
                "payload": {"instruction": "go"},
                "result": {"course_structure": {"units": []}},
                "error": None,
                "created_at": "2026-03-08T10:00:00+00:00",
                "updated_at": "2026-03-08T10:01:00+00:00",
                "metadata": {"project_id": "proj-1"},
            }

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        summary_resp = dashboard_client.get("/api/tasks/task-1")
        section_resp = dashboard_client.get("/api/tasks/task-1?section=result")
        download_resp = dashboard_client.get("/api/tasks/task-1/download?section=payload")

    assert summary_resp.status_code == 200
    assert summary_resp.get_json()["has_payload"] is True
    assert "payload" not in summary_resp.get_json()
    assert section_resp.status_code == 200
    assert section_resp.get_json()["content"]["course_structure"]["units"] == []
    assert download_resp.status_code == 200
    assert "attachment" in download_resp.headers.get("Content-Disposition", "")


def test_tasks_api_retry_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def retry_task(self, task_id, payload=None):
            return {"id": "retried-1", "bot_id": "course-lesson-writer", "payload": payload or {"same": True}}

        def last_error(self):
            return {}

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post("/api/tasks/task-1/retry", json={"payload": {"fixed": True}})

    assert resp.status_code == 201
    assert resp.get_json()["id"] == "retried-1"


def test_tasks_page_shows_retry_actions(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return [
                {
                    "id": "task-failed-1",
                    "bot_id": "course-lesson-writer",
                    "status": "failed",
                    "payload": {"instruction": "go"},
                    "result": None,
                    "error": {"message": "Internal Server Error"},
                    "created_at": "2026-03-08T10:00:00+00:00",
                    "updated_at": "2026-03-08T10:01:00+00:00",
                    "metadata": {"project_id": "proj-1"},
                }
            ]

        def list_bots(self):
            return []

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/tasks")

    assert resp.status_code == 200
    assert b"Retry Failed Branch" in resp.data
    assert b"Edit Payload &amp; Rerun" in resp.data


def test_tasks_api_cancel_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def cancel_task(self, task_id):
            return {"id": task_id, "status": "cancelled"}

        def last_error(self):
            return {}

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post("/api/tasks/task-1/cancel")

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "cancelled"


def test_tasks_page_shows_stop_action_for_running_tasks(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return [
                {
                    "id": "task-running-1",
                    "bot_id": "course-lesson-writer",
                    "status": "running",
                    "payload": {"instruction": "go"},
                    "result": None,
                    "error": None,
                    "created_at": "2026-03-08T10:00:00+00:00",
                    "updated_at": "2026-03-08T10:01:00+00:00",
                    "metadata": {"project_id": "proj-1"},
                }
            ]

        def list_bots(self):
            return []

    with patch("dashboard.routes.tasks.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/tasks")

    assert resp.status_code == 200
    assert b"Stop Task" in resp.data


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


def test_chat_messages_api_proxies_control_plane_messages(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_messages(self, conversation_id):
            return [{"id": "m1", "role": "assistant", "content": "hello"}]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/conversations/c1/messages")

    assert resp.status_code == 200
    assert resp.get_json()[0]["content"] == "hello"


def test_chat_messages_api_surfaces_control_plane_error(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_messages(self, conversation_id):
            return None

        def last_error(self):
            return {"status_code": 404, "detail": "conversation missing"}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/conversations/c1/messages")

    assert resp.status_code == 404
    assert b"conversation missing" in resp.data


def test_chat_delete_conversation_api_surfaces_success(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def delete_conversation(self, conversation_id):
            return True

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.delete("/api/chat/conversations/c1")

    assert resp.status_code == 204


def test_chat_archive_restore_conversation_apis_surface_success(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def archive_conversation(self, conversation_id):
            return {"id": conversation_id, "archived_at": "2026-03-07T00:00:00+00:00"}

        def restore_conversation(self, conversation_id):
            return {"id": conversation_id, "archived_at": None}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        archive_resp = dashboard_client.post("/api/chat/conversations/c1/archive")
        restore_resp = dashboard_client.post("/api/chat/conversations/c1/restore")

    assert archive_resp.status_code == 200
    assert archive_resp.get_json()["archived_at"] is not None
    assert restore_resp.status_code == 200
    assert restore_resp.get_json()["archived_at"] is None


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
        api_token = "cp-token"

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
    assert captured["headers"]["Authorization"] == "Bearer cp-token"


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


def test_project_github_context_sync_api_forwards_full_ingestion_fields(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.kwargs = None

        def sync_project_github_context(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "ok", "ingested_count": 3, "counts": {"files": 1, "commits": 1, "pull_requests": 1}}

    fake_cp = FakeCP()
    with patch("dashboard.routes.projects.get_cp_client", return_value=fake_cp):
        resp = dashboard_client.post(
            "/api/projects/proj-x/github/context/sync",
            json={
                "sync_mode": "full",
                "branch": "main",
                "namespace": "project:test",
            },
        )

    assert resp.status_code == 200
    assert fake_cp.kwargs is not None
    assert fake_cp.kwargs["project_id"] == "proj-x"
    assert fake_cp.kwargs["sync_mode"] == "full"
    assert fake_cp.kwargs["branch"] == "main"
    assert fake_cp.kwargs["namespace"] == "project:test"


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
    assert b'id="form-api-key"' in resp.data
    assert b'autocomplete="off"' in resp.data
    assert b'fake_username' in resp.data
    assert b'autocomplete="new-password"' in resp.data
    assert b"Export/Import" in resp.data
    assert b"Audit Log" in resp.data
    assert b"Deploy" in resp.data
    assert b"Bot Trigger Max Depth" in resp.data
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


def test_worker_model_pull_proxy_returns_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_worker(self, worker_id):
            return {"id": worker_id, "host": "127.0.0.1", "port": 8011}

    class FakeResponse:
        status_code = 200
        text = '{"model":"llama3.1:8b","status":"success"}'

        def json(self):
            return {"model": "llama3.1:8b", "status": "success"}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.workers.requests.post", return_value=FakeResponse()):
        resp = dashboard_client.post(
            "/api/workers/nasa1-windows/models/pull",
            json={"model": "llama3.1:8b", "provider": "ollama"},
        )

    assert resp.status_code == 200
    assert resp.get_json()["model"] == "llama3.1:8b"


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
    assert b"Check items off to hide them" in resp.data
    assert b"Show Hidden" in resp.data
    assert b"Control Plane Checks" in resp.data
    assert b"Control plane health and auth" in resp.data
    assert b"/v1/projects" in resp.data
    assert b"Required complete" in resp.data
    assert b"System Alerts" in resp.data
    assert b"Recent Activity" in resp.data
    assert b"Worker Health" in resp.data
    assert b"Quick Links" in resp.data
    assert b"Workflow Launch" in resp.data


def test_overview_page_shows_saved_launch_profiles(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def health(self):
            return True

        def list_workers(self):
            return []

        def list_bots(self):
            return [
                {
                    "id": "course-intake",
                    "name": "Course Intake",
                    "role": "assistant",
                    "enabled": True,
                    "routing_rules": {
                        "launch_profile": {
                            "enabled": True,
                            "label": "Run Course Pipeline",
                            "payload": {"topic": "AP World History"},
                            "show_on_overview": True,
                        }
                    },
                }
            ]

        def list_projects(self):
            return []

        def list_tasks(self):
            return []

        def probe_paths(self, paths):
            return [{"path": p, "ok": True, "status_code": 200, "detail": "ok"} for p in paths]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/")

    assert resp.status_code == 200
    assert b"Run Course Pipeline" in resp.data


def test_pipelines_pages_render_grouped_pipeline_runs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200):
            rows = [
                {
                    "id": "task-root",
                    "bot_id": "course-intake",
                    "status": "completed",
                    "payload": {"topic": "AP World History"},
                    "result": {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
                    "error": None,
                    "created_at": "2026-03-09T10:00:00+00:00",
                    "updated_at": "2026-03-09T10:01:00+00:00",
                    "metadata": {
                        "source": "saved_launch_pipeline",
                        "orchestration_id": "orch-1",
                        "workflow_root_task_id": "task-root",
                        "pipeline_name": "Course Generation Pipeline",
                        "pipeline_entry_bot_id": "course-intake",
                    },
                },
                {
                    "id": "task-child",
                    "bot_id": "course-outline",
                    "status": "running",
                    "payload": {},
                    "result": {"usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}},
                    "error": None,
                    "created_at": "2026-03-09T10:02:00+00:00",
                    "updated_at": "2026-03-09T10:03:00+00:00",
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-1",
                        "workflow_root_task_id": "task-root",
                        "pipeline_name": "Course Generation Pipeline",
                        "pipeline_entry_bot_id": "course-intake",
                    },
                },
            ]
            if orchestration_id:
                return [row for row in rows if (row.get("metadata") or {}).get("orchestration_id") == orchestration_id]
            return rows

        def list_bot_artifacts(self, bot_id, limit=100, task_id=None, include_content=False):
            rows = [
                {"id": "art-1", "task_id": "task-root", "bot_id": "course-intake", "kind": "note", "label": "Run Report", "content": None, "path": None, "metadata": {}, "created_at": "2026-03-09T10:01:00+00:00"},
                {"id": "art-2", "task_id": "task-child", "bot_id": "course-outline", "kind": "note", "label": "Execution Report", "content": None, "path": None, "metadata": {}, "created_at": "2026-03-09T10:03:00+00:00"},
            ]
            return [row for row in rows if row["bot_id"] == bot_id]

    with patch("dashboard.routes.pipelines.get_cp_client", return_value=FakeCP()):
        list_resp = dashboard_client.get("/pipelines")
        detail_resp = dashboard_client.get("/pipelines/orch-1")

    assert list_resp.status_code == 200
    assert b"Pipelines" in list_resp.data
    assert b"Course Generation Pipeline" in list_resp.data
    assert detail_resp.status_code == 200
    assert b"Artifacts and Reports" in detail_resp.data
    assert b"Execution Report" in detail_resp.data
