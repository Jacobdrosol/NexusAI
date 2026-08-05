"""Smoke tests for new Phase 4 dashboard pages."""

import bcrypt
import io
import json
from datetime import datetime, timezone
from pathlib import Path
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
    assert b"Memory" in resp.data


def test_projects_page_shows_configured_bot_and_schedule_coverage(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "mode": "isolated", "enabled": True, "memory_profiles_enabled": True, "bot_ids": []}]

        def list_bots(self):
            return [
                {
                    "id": "writer",
                    "project_id": "globeiq",
                    "enabled": True,
                    "execution_policy": {
                        "required_worker_tools": ["browser-ui"],
                        "connection_action_allowlist": ["globeiq-agent-api.updateLesson"],
                    },
                },
                {"id": "reviewer", "project_id": "globeiq", "enabled": False},
                {
                    "id": "researcher",
                    "project_id": "globeiq",
                    "enabled": True,
                    "execution_policy": {
                        "browser_action_allowlist": ["lesson_preview.read"],
                        "repo_output_mode": "allow",
                    },
                },
                {"id": "other", "project_id": "other-project", "enabled": True},
            ]

        def list_schedules(self):
            return {
                "schedules": [
                    {
                        "project_id": "globeiq",
                        "target_bot_id": "writer",
                        "status": "active",
                        "last_run_status": "completed",
                    },
                    {"project_id": "globeiq", "target_bot_id": "reviewer", "status": "paused"},
                ]
            }

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {"bot_id": "writer", "ready": True},
                    {"bot_id": "researcher", "ready": True},
                    {"bot_id": "other", "ready": True},
                ]
            }

        def get_project_chat_tool_access(self, project_id):
            assert project_id == "globeiq"
            return {
                "enabled": True,
                "filesystem": True,
                "repo_search": True,
                "workspace_root": "/srv/repos/globeiq",
            }

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/projects")

    assert resp.status_code == 200
    assert b"Chat Tools" in resp.data
    assert b"filesystem / repo search" in resp.data
    assert b"/srv/repos/globeiq" in resp.data
    assert b"Configured Bots" in resp.data
    assert b"2 enabled" in resp.data
    assert b"3 configured" in resp.data
    assert b"1 tool-backed" in resp.data
    assert b"1 site/API" in resp.data
    assert b"1 browser" in resp.data
    assert b"1 repo-edit" in resp.data
    assert b"1 active schedule" in resp.data
    assert b"1 latest run complete" in resp.data
    assert b"1 ready but unscheduled" in resp.data


def test_schedule_bot_readiness_api_proxies_non_secret_readiness(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot_readiness(self, bot_id):
            return {
                "bot_id": bot_id,
                "ready": False,
                "summary": {"checks": 1, "failed": 1, "warnings": 0},
                "checks": [{"component": "worker", "status": "failed", "message": "no healthy worker"}],
            }

    with patch("dashboard.routes.schedules.get_cp_client", return_value=FakeCP()):
        response = dashboard_client.get("/api/schedules/bots/example-bot/readiness")

    assert response.status_code == 200
    assert response.get_json()["bot_id"] == "example-bot"
    assert response.get_json()["ready"] is False


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
            return [
                {
                    "id": "globeiq-reviewer",
                    "name": "GlobeIQ Reviewer",
                    "role": "reviewer",
                    "project_id": "globeiq",
                }
            ]

        def list_bot_artifacts(self, bot_id, limit=20):
            assert bot_id == "globeiq-reviewer"
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
    assert b"GlobeIQ Reviewer" in resp.data
    assert b"Project Data Vault" in resp.data
    assert b"Chat Workspace Tools" in resp.data
    assert b"Repository Workspace" in resp.data
    assert b"Project Database Context" in resp.data
    assert b"GitHub Integration (PAT)" in resp.data
    assert b"Connection Flags" in resp.data
    assert b"Run Data Ingest" in resp.data
    assert b"Show File Status" in resp.data


def test_project_detail_page_surfaces_ai_workspace_readiness(dashboard_client):
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
                "bot_ids": ["bot-1"],
                "memory_profiles_enabled": True,
            }

        def list_projects(self):
            return [
                {
                    "id": "globeiq",
                    "name": "GlobeIQ",
                    "mode": "isolated",
                    "enabled": True,
                    "bridge_project_ids": [],
                    "bot_ids": ["bot-1"],
                }
            ]

        def list_bots(self):
            return [
                {
                    "id": "bot-1",
                    "name": "Research Bot",
                    "role": "researcher",
                    "project_id": "globeiq",
                    "enabled": True,
                    "backends": [
                        {
                            "type": "remote_llm",
                            "provider": "ollama_cloud",
                            "model": "qwen3.5:cloud",
                            "worker_id": "globeiq-reader",
                            "api_key_ref": "OLLAMA_CLOUD_KEY",
                        }
                    ],
                    "execution_policy": {
                        "required_worker_tools": ["browser-ui"],
                        "connection_action_allowlist": ["globeiq-agent-api.updateLesson"],
                        "connection_action_owner_approval_required": ["globeiq-agent-api.updateLesson"],
                        "browser_action_allowlist": ["lesson_preview.read"],
                        "browser_action_owner_approval_required": ["lesson_preview.read"],
                        "repo_output_mode": "allow",
                    },
                }
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "bot-1",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "Browser session expired"}],
                    }
                ]
            }

        def list_bot_artifacts(self, bot_id, limit=20):
            return []

        def list_tasks(self):
            return []

        def list_vault_items(self, **kwargs):
            return [{"id": "vault-1", "title": "Project Notes"}]

        def get_project_github_status(self, project_id):
            return {
                "connected": True,
                "context_sync": {"namespace": "project:globeiq:github"},
            }

        def list_project_github_webhook_events(self, project_id, limit=30):
            return {"events": []}

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": True, "filesystem": True, "repo_search": True}

        def get_project_repo_workspace(self, project_id):
            return {
                "enabled": True,
                "default_branch": "main",
                "allow_command_execution": True,
                "allow_push": False,
            }

        def list_workers(self):
            return [{"id": "globeiq-reader", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {"probes": [{"worker_id": "globeiq-reader", "probe_status": "expired_browser_session"}]}

        def list_keys(self):
            return [{"name": "OLLAMA_CLOUD_KEY"}]

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/projects/globeiq")

    assert resp.status_code == 200
    assert b"AI Workspace Readiness" in resp.data
    assert b"1 setup item(s) need attention" in resp.data
    assert b"1 enabled assigned bot(s) are blocked." in resp.data
    assert b"1 enabled / 1 total" in resp.data
    assert b"Assigned Bot Scope" in resp.data
    assert b"Project Bot Tooling Risks" in resp.data
    assert b"restore browser session" in resp.data
    assert b"Authenticated browser session" in resp.data
    assert b"Research Bot" in resp.data
    assert b"Browser session expired" in resp.data
    assert b"globeiq-reader" in resp.data
    assert b"expired_browser_session" in resp.data
    assert b"Routes: ollama_cloud / qwen3.5:cloud" in resp.data
    assert b"Tools: browser-ui" in resp.data
    assert b"Site/API actions: globeiq-agent-api.updateLesson" in resp.data
    assert b"Browser actions: lesson_preview.read" in resp.data
    assert b"Credential refs: OLLAMA_CLOUD_KEY" in resp.data
    assert b"Repo output: allow" in resp.data
    assert b"2 approval gates" in resp.data
    assert b"Enabled for filesystem, repo search." in resp.data
    assert b"managed workspace enabled; default branch main; command runner allowed." in resp.data
    assert b"context namespace project:globeiq:github" in resp.data


def test_project_git_status_api_reports_uncommitted_files(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_project(self, project_id):
            return {"id": project_id, "name": project_id}

    def _fake_run(args, cwd=None, capture_output=None, text=None, check=None):
        class Result:
            def __init__(self, stdout):
                self.stdout = stdout
                self.stderr = ""

        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return Result("main\n")
        if args == ["git", "status", "--short", "--untracked-files=all"]:
            return Result(" M dashboard/templates/project_detail.html\n?? tests/test_dashboard_phase4_pages.py\n")
        raise AssertionError(f"Unexpected git command: {args}")

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.projects.subprocess.run", side_effect=_fake_run):
        resp = dashboard_client.get("/api/projects/proj-git/git/status")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["branch"] == "main"
    assert body["has_changes"] is True
    assert body["count"] == 2
    assert body["entries"][0]["path"] == "dashboard/templates/project_detail.html"
    assert body["entries"][1]["code"] == "??"


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


def test_projects_api_lists_control_plane_projects(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "memory_profiles_enabled": True}]

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/projects")

    assert resp.status_code == 200
    assert resp.get_json()[0]["id"] == "globeiq"
    assert resp.get_json()[0]["memory_profiles_enabled"] is True


def test_memory_page_loads_user_scoped_items(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_memory_profile_items(self, *, user_id, profile_id="default", limit=200, query=None):
            assert user_id == "admin@test.com"
            assert profile_id == "default"
            return [
                {
                    "id": "memory-1",
                    "profile_id": "default",
                    "message_id": "manual:memory-1",
                    "conversation_id": "manual",
                    "role": "user",
                    "content": "Use direct answers.",
                    "metadata": {"source": "manual"},
                    "created_at": "2026-08-04T00:00:00+00:00",
                    "updated_at": "2026-08-04T00:00:00+00:00",
                },
                {
                    "id": "memory-2",
                    "profile_id": "default",
                    "message_id": "msg-1",
                    "conversation_id": "chat-1",
                    "role": "assistant",
                    "content": "User is working on NexusAI chat migration.",
                    "metadata": {"source": "generated_chat"},
                    "created_at": "2026-08-04T00:01:00+00:00",
                    "updated_at": "2026-08-04T00:01:00+00:00",
                },
                {
                    "id": "memory-3",
                    "profile_id": "default",
                    "message_id": "",
                    "conversation_id": "manual",
                    "role": "user",
                    "content": "Legacy memory without metadata.",
                    "created_at": "2026-08-04T00:02:00+00:00",
                    "updated_at": "2026-08-04T00:02:00+00:00",
                }
            ]

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/memory")

    assert resp.status_code == 200
    assert b"Use direct answers." in resp.data
    assert b"User is working on NexusAI chat migration." in resp.data
    assert b"Legacy memory without metadata." in resp.data
    assert b"Add Memory" in resp.data
    assert b"manual" in resp.data
    assert b"generated chat" in resp.data
    assert b"profile default" in resp.data
    assert b"chat-1" in resp.data
    assert b"message msg-1" in resp.data
    assert b"function memorySourceLabel" in resp.data


def test_memory_api_create_forces_current_user(dashboard_client):
    _login_admin(dashboard_client)
    captured = {}

    class FakeCP:
        def create_memory_profile_item(self, body):
            captured.update(body)
            return {
                "id": "memory-2",
                "profile_id": body["profile_id"],
                "message_id": "manual:memory-2",
                "conversation_id": "manual",
                "role": body["role"],
                "content": body["content"],
                "created_at": "2026-08-04T00:00:00+00:00",
                "updated_at": "2026-08-04T00:00:00+00:00",
            }

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/memory/items",
            json={"user_id": "other@test.com", "content": "Keep responses short.", "role": "assistant"},
        )

    assert resp.status_code == 201
    assert captured["user_id"] == "admin@test.com"
    assert captured["profile_id"] == "default"
    assert captured["content"] == "Keep responses short."


def test_memory_api_list_forces_current_user_and_bounds_limit(dashboard_client):
    _login_admin(dashboard_client)
    captured: dict[str, object] = {}

    class FakeCP:
        def list_memory_profile_items(self, *, user_id, profile_id="default", limit=200, query=None):
            captured.update({"user_id": user_id, "profile_id": profile_id, "limit": limit, "query": query})
            return [{"id": "memory-1", "role": "user", "content": "Use direct answers."}]

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/memory/items?query=direct&limit=9999")

    assert resp.status_code == 200
    assert captured == {"user_id": "admin@test.com", "profile_id": "default", "limit": 500, "query": "direct"}
    assert resp.get_json()["items"][0]["id"] == "memory-1"


def test_memory_api_update_forces_current_user_and_manual_metadata(dashboard_client):
    _login_admin(dashboard_client)
    captured: dict[str, object] = {}

    class FakeCP:
        def update_memory_profile_item(self, item_id, body):
            captured["item_id"] = item_id
            captured["body"] = body
            return {"id": item_id, **body}

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/memory/items/memory-1",
            json={"user_id": "other@test.com", "profile_id": "other", "content": "Prefer concise replies.", "role": "assistant"},
        )

    assert resp.status_code == 200
    assert captured["item_id"] == "memory-1"
    body = captured["body"]
    assert body["user_id"] == "admin@test.com"
    assert body["profile_id"] == "default"
    assert body["role"] == "assistant"
    assert body["metadata"] == {"source": "manual"}


def test_memory_api_delete_forces_current_user(dashboard_client):
    _login_admin(dashboard_client)
    captured: dict[str, object] = {}

    class FakeCP:
        def delete_memory_profile_item(self, item_id, *, user_id):
            captured["item_id"] = item_id
            captured["user_id"] = user_id
            return True

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.delete("/api/memory/items/memory-1")

    assert resp.status_code == 204
    assert captured == {"item_id": "memory-1", "user_id": "admin@test.com"}


def test_memory_api_delete_surfaces_control_plane_error(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def delete_memory_profile_item(self, item_id, *, user_id):
            return False

        def last_error(self):
            return {"status_code": 403, "detail": "memory item belongs to another user"}

    with patch("dashboard.routes.memory.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.delete("/api/memory/items/memory-1")

    assert resp.status_code == 403
    assert b"belongs to another user" in resp.data


def test_project_memory_toggle_updates_project(dashboard_client):
    _login_admin(dashboard_client)
    updated_payload = {}

    class FakeCP:
        def get_project(self, project_id):
            assert project_id == "globeiq"
            return {"id": "globeiq", "name": "GlobeIQ", "memory_profiles_enabled": False}

        def update_project(self, project_id, project):
            updated_payload.update(project)
            return dict(project)

    with patch("dashboard.routes.projects.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put("/api/projects/globeiq/memory-profile", json={"enabled": True})

    assert resp.status_code == 200
    assert updated_payload["memory_profiles_enabled"] is True


def test_bot_create_can_enable_memory(dashboard_client):
    _login_admin(dashboard_client)
    created_payload = {}

    class FakeCP:
        def list_bots(self):
            return []

        def create_bot(self, bot):
            created_payload.update(bot)
            return dict(bot)

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/bots",
            json={"name": "Memory Chat", "role": "assistant", "memory_profiles_enabled": True},
        )

    assert resp.status_code == 201
    assert created_payload["memory_profiles_enabled"] is True


def test_bots_api_prefers_control_plane_bots(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return [{"id": "personal-general-chat", "name": "Personal General Chat", "memory_profiles_enabled": True}]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/bots")

    assert resp.status_code == 200
    assert resp.get_json()[0]["id"] == "personal-general-chat"
    assert resp.get_json()[0]["memory_profiles_enabled"] is True


def test_chat_mobile_layout_prioritizes_active_conversation():
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert ".page-chat .chat-panel-main" in css
    assert "grid-row: 1;" in css
    assert ".page-chat .chat-panel-side" in css
    assert "grid-row: 2;" in css
    assert "min-height: calc(100dvh - 11rem);" in css


def test_chat_page_handles_legacy_selected_conversation_shapes(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-legacy",
                    "title": "Legacy Active",
                    "project_id": "globeiq",
                    "bridge_project_ids": 1,
                    "updated_at": "2026-03-10T12:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                },
                {
                    "id": "c-archived",
                    "title": "Archived",
                    "project_id": None,
                    "bridge_project_ids": "[]",
                    "updated_at": "2026-03-01T12:00:00+00:00",
                    "archived_at": "2026-03-05T00:00:00+00:00",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
            ]

        def list_messages(self, conversation_id):
            if conversation_id == "c-legacy":
                return [
                    {
                        "id": "m-1",
                        "role": "assistant",
                        "content": "hello",
                        "metadata": "not-json",
                    }
                ]
            return []

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

        def get_project_github_context_sync_status(self, project_id):
            return {}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-legacy")

    assert resp.status_code == 200
    assert b"Legacy Active" in resp.data
    assert b"No vault items available" in resp.data
    assert b"Message context and tools" in resp.data
    assert b"chat-workspace-context" in resp.data


def test_chat_page_renders_project_filter_metadata_on_conversations(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-proj",
                    "title": "Project Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": ["bridge-a", "bridge-b"],
                    "default_bot_id": "personal-general-chat",
                    "default_model_id": "ollama-cloud/gpt-oss-120b",
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
                {
                    "id": "c-archived",
                    "title": "Archived Project Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "default_bot_id": "personal-research-chat",
                    "default_model_id": "ollama-cloud/kimi-k2",
                    "updated_at": "2026-03-11T00:00:00+00:00",
                    "archived_at": "2026-03-11T01:00:00+00:00",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {"id": "personal-general-chat", "name": "Personal General Chat"},
                {"id": "personal-research-chat", "name": "Personal Research Chat"},
            ]

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "enabled": True}]

        def list_models(self):
            return [
                {"id": "ollama-cloud/gpt-oss-120b", "name": "gpt-oss:120b", "provider": "ollama_cloud", "enabled": True},
                {"id": "ollama-cloud/kimi-k2", "name": "kimi-k2", "provider": "ollama_cloud", "enabled": True},
            ]

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-proj")

    assert resp.status_code == 200
    assert b'data-project-id="globeiq"' in resp.data
    assert b'data-bridge-project-ids="bridge-a,bridge-b"' in resp.data
    assert b"bridgeProjectIds.includes(projectFilter)" in resp.data
    assert b"row.hidden = !matches" in resp.data
    assert b"All projects" in resp.data
    assert b"Project globeiq" in resp.data
    assert b'Bot Personal General Chat (personal-general-chat)' in resp.data
    assert b'title="Bot personal-general-chat"' in resp.data
    assert b"Model ollama_cloud / gpt-oss:120b" in resp.data
    assert b'title="Model ollama-cloud/gpt-oss-120b"' in resp.data
    assert b"Bot Personal Research Chat (personal-research-chat)" in resp.data
    assert b"Model ollama_cloud / kimi-k2" in resp.data
    page_html = resp.data.decode("utf-8")
    active_row_start = page_html.index('data-project-id="globeiq"')
    active_row = page_html[active_row_start:page_html.index("</div>", active_row_start)]
    archived_row_start = page_html.index('data-search-text="archived project chat')
    archived_row = page_html[archived_row_start:page_html.index("</div>", archived_row_start)]
    assert "personal general chat (personal-general-chat)" in active_row
    assert "ollama_cloud / gpt-oss:120b" in active_row
    assert "personal research chat (personal-research-chat)" in archived_row
    assert "ollama_cloud / kimi-k2" in archived_row
    assert b'id="chat-usage-pressure-banner"' in resp.data
    assert b"loadChatUsagePressureBanner" in resp.data
    assert b"chatHealthMessage" in resp.data
    assert b"Chat usage telemetry" in resp.data
    assert b"Chat provider/model attribution" in resp.data
    assert b"/api/work/brief" in resp.data


def test_chat_create_modal_surfaces_default_bot_capability_summary(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return []

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "personal-vision-math-tutor",
                    "name": "Vision Math Tutor",
                    "role": "assistant",
                    "enabled": True,
                    "memory_profiles_enabled": True,
                    "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                    "routing_rules": {
                        "operator_profile": {"autonomy": "chat"},
                        "chat_profile": {"label": "Tutor / Reasoning", "use_label": "Homework and engineering help", "tool_label": "off"},
                        "chat_tool_access": {"enabled": False, "filesystem": False, "repo_search": False},
                    },
                }
            ]

        def list_bot_readiness(self):
            return {"bots": [{"bot_id": "personal-vision-math-tutor", "state": "ready", "detail": ""}]}

        def list_projects(self):
            return []

        def list_models(self):
            return [
                {"id": "ollama-cloud-gpt-oss-120b", "name": "gpt-oss:120b", "provider": "ollama_cloud", "capabilities": ["text"], "enabled": True},
                {"id": "openai-gpt-5", "name": "gpt-5", "provider": "openai", "capabilities": ["text"], "enabled": True},
            ]

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat")

    assert resp.status_code == 200
    assert b'id="create-convo-default-bot-id"' in resp.data
    assert b'id="create-convo-default-model-id"' in resp.data
    assert b'id="create-convo-bot-summary"' in resp.data
    assert b'id="create-convo-tool-summary"' in resp.data
    assert b"Vision Math Tutor (personal-vision-math-tutor)" in resp.data
    assert b"function botCapabilitySummaryText" in resp.data
    assert b"updateCreateConversationBotSummary" in resp.data
    assert b"Homework and engineering help" in resp.data
    assert b"route warning" in resp.data
    assert b"Default model provider" in resp.data
    assert b"Workspace tools require a project-scoped chat." in resp.data
    assert b"scopedToolAccessAllowed" in resp.data
    assert b"tool_access_enabled: scopedToolAccessAllowed" in resp.data
    assert b"Select a default bot or leave blank to use the platform default." in resp.data


def test_chat_page_project_filter_limits_conversation_list(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-primary",
                    "title": "Primary Project Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
                {
                    "id": "c-bridged",
                    "title": "Bridged Project Chat",
                    "project_id": "nexusai",
                    "bridge_project_ids": ["globeiq"],
                    "updated_at": "2026-03-11T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
                {
                    "id": "c-other",
                    "title": "Other Project Chat",
                    "project_id": "other",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-10T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return []

        def list_projects(self):
            return [
                {"id": "globeiq", "name": "GlobeIQ", "enabled": True},
                {"id": "nexusai", "name": "NexusAI", "enabled": True},
                {"id": "other", "name": "Other", "enabled": True},
            ]

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?project_id=globeiq")

    assert resp.status_code == 200
    assert b"Primary Project Chat" in resp.data
    assert b"Bridged Project Chat" in resp.data
    assert b"Other Project Chat" not in resp.data
    assert b'option value="globeiq" selected' in resp.data
    assert b"Unscoped chats" in resp.data
    assert b"This chat will be scoped to the selected project." in resp.data
    assert b"Select a project before creating this scoped chat." in resp.data
    assert b"create-convo-project-id" in resp.data
    assert b"addEventListener('change', syncConversationScopeFields)" in resp.data
    assert b"conversation_id=c-primary" in resp.data
    assert b"project_id=globeiq" in resp.data


def test_chat_page_surfaces_selected_project_work_snapshot(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-project-work",
                    "title": "Project Work Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": ["nexusai"],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return []

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_projects(self):
            return [
                {"id": "globeiq", "name": "GlobeIQ", "enabled": True, "memory_profiles_enabled": True},
                {"id": "nexusai", "name": "NexusAI", "enabled": True, "memory_profiles_enabled": True},
            ]

        def list_models(self):
            return []

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": False, "filesystem": False, "repo_search": False}

        def list_vault_items(self, **kwargs):
            return []

        def list_tasks(self, **kwargs):
            seen.update(kwargs)
            return [
                {
                    "id": "task-running",
                    "bot_id": "writer",
                    "status": "running",
                    "metadata": {"project_id": "globeiq", "manager_bot_id": "content-manager"},
                    "updated_at": "2026-03-12T10:00:00+00:00",
                },
                {
                    "id": "task-queued",
                    "bot_id": "reviewer",
                    "status": "queued",
                    "metadata": {"project_id": "nexusai", "manager_bot_id": "platform-manager"},
                    "updated_at": "2026-03-12T09:00:00+00:00",
                },
                {
                    "id": "task-blocked",
                    "bot_id": "browser",
                    "status": "blocked",
                    "metadata": {"project_id": "globeiq", "manager_bot_id": "content-manager"},
                    "updated_at": "2026-03-12T08:00:00+00:00",
                },
                {
                    "id": "task-other",
                    "bot_id": "other",
                    "status": "running",
                    "metadata": {"project_id": "other", "manager_bot_id": "other-manager"},
                    "updated_at": "2026-03-12T07:00:00+00:00",
                },
            ]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-project-work")

    assert resp.status_code == 200
    assert seen["statuses"] == ["queued", "blocked", "running", "failed"]
    assert seen["include_content"] is False
    assert b"Project Work" in resp.data
    assert b"Projects: globeiq, nexusai" in resp.data
    assert b"content-manager" in resp.data
    assert b"platform-manager" in resp.data
    assert b"task-running" in resp.data
    assert b"task-queued" in resp.data
    assert b"task-blocked" in resp.data
    assert b"task-other" not in resp.data


def test_chat_page_limits_normal_bot_selectors_to_chat_bots(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-chat",
                    "title": "Chat",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-general-chat",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "personal-general-chat",
                    "name": "Personal General Chat",
                    "role": "assistant",
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "chat", "label": "General Chat"},
                    },
                    "assignment_capabilities": None,
                },
                {
                    "id": "personal-blocked-chat",
                    "name": "Personal Blocked Chat",
                    "role": "assistant",
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "chat", "label": "Blocked Chat"},
                    },
                    "assignment_capabilities": None,
                },
                {
                    "id": "personal-missing-key-chat",
                    "name": "Personal Missing Key Chat",
                    "role": "assistant",
                    "backends": [
                        {
                            "type": "cloud_api",
                            "provider": "ollama_cloud",
                            "model": "gpt-oss:120b",
                            "api_key_ref": "MISSING_OLLAMA_KEY",
                        }
                    ],
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "chat", "label": "Missing Key Chat"},
                    },
                    "assignment_capabilities": None,
                },
                {
                    "id": "globeiq-live-audit-qc-02-bot",
                    "name": "GlobeIQ Live Audit QC 02",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                    "assignment_capabilities": None,
                },
                {
                    "id": "pm-orchestrator",
                    "name": "PM Orchestrator",
                    "role": "pm",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                    "assignment_capabilities": {"is_project_manager": True},
                },
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "personal-general-chat",
                        "state": "ready",
                        "ready": True,
                        "checks": [
                            {
                                "status": "ready",
                                "message": "backend ready for chat",
                            }
                        ],
                    },
                    {
                        "bot_id": "personal-blocked-chat",
                        "state": "blocked",
                        "ready": False,
                        "checks": [
                            {
                                "status": "failed",
                                "message": "model credential missing",
                            }
                        ],
                    },
                    {
                        "bot_id": "personal-missing-key-chat",
                        "state": "ready",
                        "ready": True,
                        "checks": [],
                    },
                    {
                        "bot_id": "pm-orchestrator",
                        "state": "blocked",
                        "ready": False,
                        "checks": [
                            {
                                "status": "failed",
                                "message": "PM worker route missing",
                            }
                        ],
                    },
                ]
            }

        def list_projects(self):
            return []

        def list_models(self):
            return [
                {"id": "ollama-cloud-gpt-oss-120b", "name": "gpt-oss:120b", "provider": "ollama_cloud", "enabled": True},
                {"id": "ollama-cloud-disabled", "name": "old-model", "provider": "ollama_cloud", "enabled": False},
            ]

        def list_vault_items(self, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-chat")

    assert resp.status_code == 200
    page_html = resp.data.decode("utf-8")
    selector_start = page_html.index('id="chat-bot-selector"')
    chat_selector = page_html[selector_start:page_html.index("</select>", selector_start)]
    assert b"Personal General Chat" in resp.data
    assert b"Personal General Chat - General Chat" in resp.data
    assert "GlobeIQ Live Audit QC 02" not in chat_selector
    assert 'value="personal-blocked-chat"  disabled title="model credential missing"' in chat_selector
    assert "Personal Missing Key Chat - Missing Key Chat - blocked" in chat_selector
    assert 'value="personal-missing-key-chat"  disabled title="Missing key-vault credential reference(s): MISSING_OLLAMA_KEY"' in chat_selector
    assert b"PM Orchestrator" in resp.data
    assert b"Select a project manager bot" in resp.data
    assert b"PM Orchestrator (pm) - blocked: PM worker route missing" in resp.data
    assert b'value="pm-orchestrator" disabled title="PM worker route missing"' in resp.data
    assert b"chat-bot-capability-summary" in resp.data
    assert b"function updateChatBotCapabilitySummary" in resp.data
    assert b"backend ready for chat" in resp.data
    assert b"readinessLabel" in resp.data
    assert b"Personal Blocked Chat - Blocked Chat - blocked" in resp.data
    assert b"Personal Blocked Chat (personal-blocked-chat) - blocked: model credential missing" in resp.data
    assert b'value="personal-blocked-chat" disabled title="model credential missing"' in resp.data
    assert b"Personal Missing Key Chat (personal-missing-key-chat) - blocked: Missing key-vault credential reference(s): MISSING_OLLAMA_KEY" in resp.data
    assert b'value="personal-missing-key-chat" disabled title="Missing key-vault credential reference(s): MISSING_OLLAMA_KEY"' in resp.data
    assert b"model credential missing" in resp.data
    assert b"function activeBotReadinessBlocker" in resp.data
    assert b"function chatBotReadinessBlocker" in resp.data
    assert b'name="default_model_id"' in resp.data
    assert b'value="ollama-cloud-gpt-oss-120b"' in resp.data
    assert b"ollama_cloud / gpt-oss:120b" in resp.data
    assert b'value="ollama-cloud-disabled" disabled' in resp.data
    assert b"old-model" in resp.data
    assert b'id="chat-default-model-selector"' in resp.data
    assert b"Save Defaults" in resp.data
    assert b"function saveConversationRouteDefaults" in resp.data
    assert b"function routeDefaultCompatibilityBlocker" in resp.data
    assert b"function chatBotHasProviderBackend" in resp.data
    assert b"Default route is unavailable: ${routeBlocker}" in resp.data


def test_chat_page_warns_when_conversation_default_bot_is_not_chat_selectable(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-worker-default",
                    "title": "Worker Default",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "globeiq-live-audit-qc-02-bot",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "personal-general-chat",
                    "name": "Personal General Chat",
                    "role": "assistant",
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "chat", "label": "General Chat"},
                    },
                },
                {
                    "id": "globeiq-live-audit-qc-02-bot",
                    "name": "GlobeIQ Live Audit QC 02",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                },
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {"bot_id": "personal-general-chat", "state": "ready", "ready": True, "checks": []},
                    {"bot_id": "globeiq-live-audit-qc-02-bot", "state": "ready", "ready": True, "checks": []},
                ]
            }

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-worker-default")

    assert resp.status_code == 200
    assert b'id="chat-default-bot-route-warning"' in resp.data
    assert b"Default bot unavailable: GlobeIQ Live Audit QC 02 (globeiq-live-audit-qc-02-bot)." in resp.data
    assert b"not configured for manual chat use" in resp.data
    assert b"Default bot unavailable</span>" in resp.data
    page_html = resp.data.decode("utf-8")
    selector_start = page_html.index('id="chat-bot-selector"')
    chat_selector = page_html[selector_start:page_html.index("</select>", selector_start)]
    assert "GlobeIQ Live Audit QC 02" not in chat_selector


def test_chat_page_does_not_warn_for_plain_model_backed_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-model-default",
                    "title": "Model Default",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "text-bot",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "text-bot",
                    "name": "Text Bot",
                    "backends": [{"provider": "openai", "model": "gpt-4o-mini"}],
                }
            ]

        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "text-bot", "state": "ready", "ready": True, "checks": []}]}

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-model-default")

    assert resp.status_code == 200
    assert b'id="chat-default-bot-route-warning"' not in resp.data
    assert b"Default bot unavailable" not in resp.data


def test_chat_page_warns_when_conversation_default_model_is_unavailable(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-disabled-model",
                    "title": "Disabled Model",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "text-bot",
                    "default_model_id": "disabled-model",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [{"id": "text-bot", "name": "Text Bot", "backends": [{"provider": "openai", "model": "gpt-4o-mini"}]}]

        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "text-bot", "state": "ready", "ready": True, "checks": []}]}

        def list_projects(self):
            return []

        def list_models(self):
            return [{"id": "disabled-model", "name": "old-model", "provider": "ollama_cloud", "enabled": False}]

        def list_vault_items(self, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-disabled-model")

    assert resp.status_code == 200
    assert b'id="chat-default-model-route-warning"' in resp.data
    assert b"Default model unavailable: ollama_cloud / old-model." in resp.data
    assert b"This conversation default model is disabled." in resp.data
    assert b"Default model unavailable</span>" in resp.data


def test_chat_page_does_not_warn_for_default_model_when_catalog_unavailable(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-model-catalog-down",
                    "title": "Catalog Down",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "text-bot",
                    "default_model_id": "possibly-valid-model",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [{"id": "text-bot", "name": "Text Bot", "backends": [{"provider": "openai", "model": "gpt-4o-mini"}]}]

        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "text-bot", "state": "ready", "ready": True, "checks": []}]}

        def list_projects(self):
            return []

        def list_models(self):
            raise RuntimeError("catalog unavailable")

        def list_vault_items(self, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-model-catalog-down")

    assert resp.status_code == 200
    assert b'id="chat-default-model-route-warning"' not in resp.data
    assert b"Default model unavailable" not in resp.data


def test_chat_page_does_not_block_chat_bot_when_readiness_is_unreported(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-chat",
                    "title": "Chat",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-general-chat",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "personal-general-chat",
                    "name": "Personal General Chat",
                    "role": "assistant",
                    "backends": [
                        {
                            "type": "cloud_api",
                            "provider": "ollama_cloud",
                            "model": "gpt-oss:120b",
                            "api_key_ref": "OLLAMA_CLOUD_KEY",
                        }
                    ],
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "chat", "label": "General Chat"},
                    },
                }
            ]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return [{"name": "OLLAMA_CLOUD_KEY"}]

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-chat")

    assert resp.status_code == 200
    page_html = resp.data.decode("utf-8")
    selector_start = page_html.index('id="chat-bot-selector"')
    chat_selector = page_html[selector_start:page_html.index("</select>", selector_start)]
    assert "Personal General Chat - General Chat" in chat_selector
    assert 'value="personal-general-chat" selected  disabled' not in chat_selector
    assert "Readiness not reported." in page_html


def test_chat_page_embeds_effective_context_gate_inputs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-project-chat",
                    "title": "Project Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-research-chat",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "personal-research-chat",
                    "name": "Personal Research Chat",
                    "role": "assistant",
                    "memory_profiles_enabled": True,
                    "routing_rules": {
                        "operator_profile": {"autonomy": "manual_chat_only"},
                        "chat_profile": {"mode": "research", "label": "Research Chat"},
                        "chat_tool_access": {"enabled": True, "filesystem": True, "repo_search": True},
                    },
                    "backends": [{"provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                }
            ]

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "enabled": True, "memory_profiles_enabled": True}]

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": True, "filesystem": True, "repo_search": True}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-project-chat")

    assert resp.status_code == 200
    assert b"chat-effective-context-summary" in resp.data
    assert b"function renderChatEffectiveContextSummary" in resp.data
    assert b"function workspaceToolRequestBlocker" in resp.data
    assert b"function inlineCodingRequestBlocker" in resp.data
    assert b"function serverChatEffectiveContextBlocker" in resp.data
    assert b"/effective-context?" in resp.data
    assert b"hasImageAttachment" in resp.data
    assert b"image_attachments_supported" in resp.data
    assert b"refreshServerEffectiveContextSummary" in resp.data
    assert b"effective model:" in resp.data
    assert b"bot context:" in resp.data
    assert b"connection action" in resp.data
    assert b"browser action" in resp.data
    assert b"owner approval" in resp.data
    assert b"browser owner approval" in resp.data
    assert b"HTTP connection backend" in resp.data
    assert b"function routeDefaultDraftState" in resp.data
    assert b"function chatBotDisplayLabel" in resp.data
    assert b"function chatModelDisplayLabel" in resp.data
    assert b"route draft unsaved:" in resp.data
    assert b"route saved:" in resp.data
    assert b"Workspace tools are not available:" in resp.data
    assert b"Inline coding is not available:" in resp.data
    assert b"inline coding inactive: message toggle off" in resp.data
    assert b"const selectedConversationProjectId = \"globeiq\";" in resp.data
    assert b"const selectedProjectMemoryProfilesEnabled = true;" in resp.data
    assert b"\"repo_search\": true" in resp.data
    assert b"const assignmentContext = selectedContextItems();" in resp.data
    assert b"context_items: assignmentContext.contexts" in resp.data
    assert b"context_item_ids: assignmentContext.contextItemIds" in resp.data
    assert b"Context items: " in resp.data
    assert b"Assignments include selected conversation context and selected vault items." in resp.data


def test_chat_effective_context_api_reports_active_memory_tools_and_coding(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c-project-chat",
                    "title": "Project Chat",
                    "project_id": "globeiq",
                    "default_bot_id": "coding-chat",
                    "default_model_id": "ollama-cloud-gpt-oss-120b",
                    "memory_profiles_enabled": True,
                    "memory_profile_id": "default",
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                }
            ]

        def list_bots(self):
            return [
                {
                    "id": "coding-chat",
                    "name": "Coding Chat",
                    "memory_profiles_enabled": True,
                    "backends": [
                        {"provider": "ollama_cloud", "model": "gpt-oss:120b"},
                        {"type": "custom", "provider": "http_connection", "model": "attached-http"},
                    ],
                    "routing_rules": {
                        "chat_profile": {"mode": "coding", "label": "Coding"},
                        "chat_tool_access": {"enabled": True, "filesystem": True, "repo_search": True}
                    },
                    "execution_policy": {
                        "repo_output_mode": "allow",
                        "connection_action_allowlist": ["globeiq-agent-api.updateLesson"],
                        "connection_action_owner_approval_required": ["globeiq-agent-api.updateLesson"],
                        "browser_action_allowlist": ["lesson_preview.read"],
                        "browser_action_owner_approval_required": ["lesson_preview.read"],
                    },
                }
            ]

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "memory_profiles_enabled": True}]

        def list_models(self):
            return [
                {
                    "id": "ollama-cloud-gpt-oss-120b",
                    "name": "qwen3.5:cloud",
                    "provider": "ollama_cloud",
                    "capabilities": ["vision"],
                    "enabled": True,
                }
            ]

        def get_project_chat_tool_access(self, project_id):
            assert project_id == "globeiq"
            return {"enabled": True, "filesystem": True, "repo_search": False}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get(
            "/api/chat/conversations/c-project-chat/effective-context"
            "?use_workspace_tools=true&inline_coding_enabled=true"
        )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["conversation_id"] == "c-project-chat"
    assert payload["bot"]["id"] == "coding-chat"
    assert payload["bot"]["chat_profile"]["label"] == "Coding"
    assert payload["bot"]["chat_profile"]["use_label"] == "Tool-enabled chat"
    assert payload["bot"]["connection_actions"] == ["globeiq-agent-api.updateLesson"]
    assert payload["bot"]["owner_approval_actions"] == ["globeiq-agent-api.updateLesson"]
    assert payload["bot"]["browser_actions"] == ["lesson_preview.read"]
    assert payload["bot"]["browser_owner_approval_actions"] == ["lesson_preview.read"]
    assert payload["bot"]["http_connection_backend_count"] == 1
    assert payload["route"]["default_model_id"] == "ollama-cloud-gpt-oss-120b"
    assert payload["model"]["source"] == "conversation_default_model"
    assert payload["model"]["model"] == "qwen3.5:cloud"
    assert payload["model"]["capabilities"] == ["vision"]
    assert payload["model"]["image_attachments_supported"] is True
    assert payload["memory"]["active"] is True
    assert payload["memory"]["reasons"] == []
    assert payload["workspace_tools"]["requested"] is True
    assert payload["workspace_tools"]["available"] is True
    assert payload["workspace_tools"]["modes"] == ["filesystem"]
    assert payload["workspace_tools"]["request_allowed"] is True
    assert payload["inline_coding"]["requested"] is True
    assert payload["inline_coding"]["available"] is True
    assert payload["inline_coding"]["blocker"] == ""


def test_chat_effective_context_api_uses_explicit_bot_backend_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c-project-chat",
                    "title": "Project Chat",
                    "project_id": "globeiq",
                    "default_bot_id": "default-chat",
                    "default_model_id": "vision-default",
                    "memory_profiles_enabled": True,
                    "tool_access_enabled": False,
                }
            ]

        def list_bots(self):
            return [
                {
                    "id": "default-chat",
                    "name": "Default Chat",
                    "memory_profiles_enabled": True,
                    "backends": [{"provider": "ollama_cloud", "model": "qwen3.5:cloud"}],
                },
                {
                    "id": "explicit-chat",
                    "name": "Explicit Chat",
                    "memory_profiles_enabled": True,
                    "backends": [{"provider": "openai", "model": "gpt-4o-mini"}],
                },
            ]

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "memory_profiles_enabled": True}]

        def list_models(self):
            return [
                {
                    "id": "vision-default",
                    "name": "qwen3.5:cloud",
                    "provider": "ollama_cloud",
                    "capabilities": ["vision"],
                    "enabled": True,
                }
            ]

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": False, "filesystem": False, "repo_search": False}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get(
            "/api/chat/conversations/c-project-chat/effective-context?bot_id=explicit-chat"
        )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["bot"]["id"] == "explicit-chat"
    assert payload["route"]["requested_bot_id"] == "explicit-chat"
    assert payload["model"]["source"] == "bot_backend"
    assert payload["model"]["provider"] == "openai"
    assert payload["model"]["model"] == "gpt-4o-mini"
    assert payload["model"]["image_attachments_supported"] is True


def test_chat_effective_context_api_explains_blocked_gates(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c-unscoped",
                    "title": "One-off",
                    "project_id": None,
                    "default_bot_id": "chat-only",
                    "memory_profiles_enabled": True,
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": False,
                }
            ]

        def list_bots(self):
            return [
                {
                    "id": "chat-only",
                    "name": "Chat Only",
                    "memory_profiles_enabled": False,
                    "routing_rules": {
                        "chat_tool_access": {"enabled": False, "filesystem": False, "repo_search": False}
                    },
                    "execution_policy": {"repo_output_mode": "deny"},
                }
            ]

        def list_projects(self):
            raise AssertionError("unscoped chats should not require project memory lookup")

        def get_project_chat_tool_access(self, project_id):
            raise AssertionError("unscoped chats should not require project tool lookup")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get(
            "/api/chat/conversations/c-unscoped/effective-context"
            "?use_workspace_tools=true&inline_coding_enabled=true"
        )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["memory"]["active"] is False
    assert payload["memory"]["project_enabled"] is None
    assert payload["memory"]["reasons"] == ["bot off"]
    assert payload["workspace_tools"]["available"] is False
    assert payload["workspace_tools"]["request_allowed"] is False
    assert "bot off" in payload["workspace_tools"]["reasons"]
    assert "no scoped project" in payload["workspace_tools"]["reasons"]
    assert payload["workspace_tools"]["project_access"] is None
    assert payload["inline_coding"]["available"] is False
    assert payload["inline_coding"]["request_allowed"] is False
    assert payload["inline_coding"]["blocker"] == "no scoped project"


def test_chat_page_unscoped_filter_limits_conversation_list(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-unscoped",
                    "title": "One-off Chat",
                    "scope": "global",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-general-chat",
                    "default_model_id": "ollama-qwen",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
                {
                    "id": "c-project",
                    "title": "Project Chat",
                    "scope": "project",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-11T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
                {
                    "id": "c-bridged",
                    "title": "Bridged Chat",
                    "scope": "bridged",
                    "project_id": "nexusai",
                    "bridge_project_ids": ["globeiq"],
                    "updated_at": "2026-03-10T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                },
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return []

        def list_projects(self):
            return [
                {"id": "globeiq", "name": "GlobeIQ", "enabled": True},
                {"id": "nexusai", "name": "NexusAI", "enabled": True},
            ]

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?project_id=__unscoped__")

    assert resp.status_code == 200
    assert b"One-off Chat" in resp.data
    assert b"Project Chat" not in resp.data
    assert b"Bridged Chat" not in resp.data
    assert b'option value="__unscoped__" selected' in resp.data
    assert b"All projects (3 active / 0 archived)" in resp.data
    assert b"Unscoped chats (1 active / 0 archived)" in resp.data
    assert b"GlobeIQ (globeiq) - 2 active / 0 archived" in resp.data
    assert b"NexusAI (nexusai) - 1 active / 0 archived" in resp.data
    assert b'option value="global" selected' in resp.data
    assert b"Unscoped chat" in resp.data
    assert b"Unscoped" in resp.data
    assert b"project_id=__unscoped__" in resp.data
    assert b"targetProjectFilter = scope === 'global' ? unscopedProjectFilter" in resp.data
    assert b'id="chat-conversation-search"' in resp.data
    assert b'placeholder="Title, project, bot, or model"' in resp.data
    assert b'data-search-text="one-off chat c-unscoped global' in resp.data
    assert b"personal-general-chat" in resp.data
    assert b"ollama-qwen" in resp.data
    assert b"applyConversationProjectFilter" in resp.data
    assert b'id="chat-conversation-filter-summary"' in resp.data
    assert b"Active 1 / Archived 0" in resp.data
    assert b"Showing ${activeVisible} of ${activeTotal} active" in resp.data


def test_chat_page_surfaces_assistant_bot_and_model_provenance(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-chat",
                    "title": "Chat",
                    "scope": "global",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-general-chat",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return [
                {
                    "id": "m-user",
                    "role": "user",
                    "content": "User prompt",
                    "created_at": "2026-08-04T12:33:00+00:00",
                    "metadata": {},
                },
                {
                    "id": "m-assistant",
                    "role": "assistant",
                    "content": "Assistant reply",
                    "created_at": "2026-08-04T12:34:00+00:00",
                    "bot_id": "personal-general-chat",
                    "provider": "ollama_cloud",
                    "model": "qwen3.5:397b",
                    "metadata": {
                        "bot": {
                            "id": "personal-general-chat",
                            "name": "Personal General Chat",
                            "updated_at": "2026-08-04T12:34:56Z",
                        },
                        "model": {
                            "provider": "ollama_cloud",
                            "model": "qwen3.5:397b",
                            "source": "bot_config",
                        },
                        "usage": {
                            "prompt_tokens": 1234,
                            "completion_tokens": 456,
                        },
                    },
                }
            ]

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-chat")

    assert resp.status_code == 200
    assert b"message-provenance" in resp.data
    assert b"Personal General Chat" in resp.data
    assert b"ollama_cloud / qwen3.5:397b" in resp.data
    assert b"1,690 tokens (1,234 in / 456 out)" in resp.data
    assert b"bot updated 2026-08-04 12:34:56" in resp.data
    assert b"message-timestamp" in resp.data
    assert b"2026-08-04 12:33:00" in resp.data
    assert b"2026-08-04 12:34:00" in resp.data
    assert b"function renderMessageTimestampHtml" in resp.data
    assert b"timestampEl.insertAdjacentHTML('afterend', provenanceHtml)" in resp.data
    assert b"function formatMessageUsageLabel" in resp.data


def test_chat_page_surfaces_assignment_explicit_context_metadata(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-chat",
                    "title": "Chat",
                    "scope": "global",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "pm-bot",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return [
                {
                    "id": "m-assign",
                    "role": "user",
                    "content": "@assign Fix the bug",
                    "metadata": {
                        "mode": "assign_request",
                        "assignment_context_strategy": "semantic_excerpt",
                        "assignment_context_message_count": 2,
                        "assignment_explicit_context_item_count": 2,
                        "assignment_explicit_context_sources": ["vault:vault-1 Architecture Note"],
                    },
                }
            ]

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-chat")

    assert resp.status_code == 200
    assert b"2 explicit items" in resp.data
    assert b"Explicit sources: vault:vault-1 Architecture Note" in resp.data
    assert b"explicitCount" in resp.data
    assert b"assignment_explicit_context_sources" in resp.data


def test_chat_page_resolves_id_only_message_provenance_labels(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-chat",
                    "title": "Chat",
                    "scope": "global",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "default_bot_id": "personal-general-chat",
                    "default_model_id": "ollama-qwen",
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return [
                {
                    "id": "m-assistant",
                    "role": "assistant",
                    "content": "Assistant reply",
                    "bot_id": "personal-general-chat",
                    "provider": "ollama_cloud",
                    "model": "ollama-qwen",
                    "metadata": {
                        "bot": {"id": "personal-general-chat"},
                        "model": {"id": "ollama-qwen", "provider": "ollama_cloud"},
                    },
                }
            ]

        def list_bots(self):
            return [{"id": "personal-general-chat", "name": "Personal General Chat"}]

        def list_projects(self):
            return []

        def list_models(self):
            return [
                {
                    "id": "ollama-qwen",
                    "name": "qwen3.5:397b",
                    "provider": "ollama_cloud",
                    "enabled": True,
                }
            ]

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-chat")

    assert resp.status_code == 200
    assert b'title="Bot personal-general-chat">Personal General Chat (personal-general-chat)</span>' in resp.data
    assert b'title="Model ollama-qwen">ollama_cloud / qwen3.5:397b</span>' in resp.data
    assert b"const botDisplayLabels" in resp.data
    assert b"const modelDisplayLabels" in resp.data


def test_chat_page_handles_conversation_list_error_gracefully(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            raise RuntimeError("cp conversation list failed")

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat")

    assert resp.status_code == 200
    assert b"Conversation list is temporarily unavailable." in resp.data
    assert b"No conversations yet" in resp.data


def test_chat_page_handles_non_json_serializable_message_fields(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-proj",
                    "title": "Project Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                }
            ]

        def list_messages(self, conversation_id):
            return [
                {
                    "id": "m-weird",
                    "role": "assistant",
                    "content": "hello",
                    "created_at": datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
                    "metadata": {"seen_at": datetime(2026, 3, 12, 10, 1, tzinfo=timezone.utc)},
                }
            ]

        def list_bots(self):
            return []

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_vault_items(self, **kwargs):
            return []

        def get_project_github_context_sync_status(self, project_id):
            return {}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-proj")

    assert resp.status_code == 200
    assert b"Project Chat" in resp.data
    assert b"hello" in resp.data


def test_chat_page_requests_full_history_for_selected_conversation(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-full",
                    "title": "Full History",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            seen["conversation_id"] = conversation_id
            seen["limit"] = limit
            return [{"id": "m1", "role": "assistant", "content": "older history"}]

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-full")

    assert resp.status_code == 200
    assert seen == {"conversation_id": "c-full", "limit": None}
    assert b"older history" in resp.data


def test_chat_page_preserves_raw_markdown_content_for_selected_conversation(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-md",
                    "title": "Markdown Chat",
                    "project_id": None,
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": False,
                    "tool_access_filesystem": False,
                    "tool_access_repo_search": False,
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return [{"id": "m-md", "role": "assistant", "content": "# Heading\n\n- Item"}]

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-md")

    assert resp.status_code == 200
    assert b'data-content="# Heading' in resp.data
    assert b'data-raw-content="# Heading' in resp.data


def test_chat_page_handles_wrapped_vault_item_responses(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return [
                {
                    "id": "c-proj-vault",
                    "title": "Project Vault Chat",
                    "project_id": "globeiq",
                    "bridge_project_ids": [],
                    "updated_at": "2026-03-12T00:00:00+00:00",
                    "archived_at": None,
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                }
            ]

        def list_messages(self, conversation_id):
            return [{"id": "m-1", "role": "assistant", "content": "ok"}]

        def list_bots(self):
            return []

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ"}]

        def list_vault_items(self, **kwargs):
            if kwargs.get("namespace"):
                return {
                    "items": [
                        {
                            "id": "v-proj-1",
                            "title": "README.md",
                            "metadata": {"path": "README.md"},
                        }
                    ]
                }
            return {"items": [{"id": "v-global-1", "title": "General Doc"}]}

        def get_project_github_context_sync_status(self, project_id):
            return {}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-proj-vault")

    assert resp.status_code == 200
    assert b"Project Vault Chat" in resp.data
    assert b"README.md" in resp.data
    assert b"General Doc" in resp.data
    assert b"Chat view is temporarily unavailable" not in resp.data


def test_chat_page_unexpected_error_falls_back_to_safe_shell(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all", project_id=None):
            return []

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), patch(
        "dashboard.routes.chat._normalize_conversation_rows",
        return_value=[None],
    ):
        resp = dashboard_client.get("/chat")

    assert resp.status_code == 200
    assert b"Chat view is temporarily unavailable. Start a new chat or refresh." in resp.data
    assert b"No conversations yet" in resp.data


def test_vault_page_loads_when_logged_in(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/vault")
    assert resp.status_code == 200
    assert b"Vault" in resp.data
    assert b"Upload / Ingest" in resp.data


def test_events_stream_emits_dashboard_counts(dashboard_client):
    _login_admin(dashboard_client)

    resp = dashboard_client.get("/events", buffered=False)

    assert resp.status_code == 200
    first_frame = next(resp.response).decode()
    assert first_frame.startswith("data: ")
    payload = json.loads(first_frame.removeprefix("data: ").strip())
    assert "workers" in payload
    assert "bots" in payload
    assert "tasks" in payload


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
    assert b"Connection Context" in resp.data
    assert b"Saved Launch Profile" in resp.data
    assert b"Backlog" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"qwen3.5:397b" in resp.data
    assert b"Auto: 1024 for local Ollama chat" in resp.data
    assert b"Context Window" in resp.data
    assert b"GPU Layers" in resp.data


def test_bot_detail_page_renders_chat_profile_controls(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Coding Helper",
                "role": "coder",
                "priority": 1,
                "enabled": True,
                "memory_profiles_enabled": True,
                "backends": [
                    {
                        "type": "remote_llm",
                        "provider": "ollama_cloud",
                        "model": "qwen3.5:397b",
                        "worker_id": "coding-worker",
                        "api_key_ref": "OLLAMA_CLOUD_KEY",
                    }
                ],
                "routing_rules": {
                    "chat_profile": {
                        "mode": "coding",
                        "label": "Coding",
                        "description": "Repo-scoped coding chat.",
                        "attachments": True,
                        "diagrams": False,
                        "image_understanding": False,
                    },
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": True,
                    },
                    "operator_profile": {
                        "autonomy": "manual_chat_only",
                    },
                },
                "execution_policy": {
                    "required_worker_tools": ["repo-search"],
                    "repo_output_mode": "allow",
                    "inline_coding_default": True,
                    "connection_action_allowlist": ["globeiq-agent-api.updateLesson"],
                    "connection_action_owner_approval_required": ["globeiq-agent-api.updateLesson"],
                    "browser_action_allowlist": ["lesson_preview.read"],
                    "browser_action_owner_approval_required": ["lesson_preview.read"],
                },
            }

        def get_bot_readiness(self, bot_id):
            return {"bot_id": bot_id, "ready": True, "summary": {"checks": 0, "blocking": 0, "warnings": 0}, "checks": []}

        def get_bot_dependencies(self, bot_id):
            return {
                "schedule_references": [
                    {
                        "id": "coding-helper-hourly",
                        "name": "Coding Helper Hourly",
                        "relation": "target_bot",
                        "project_id": "nexusai",
                        "status": "active",
                    }
                ],
                "workflow_references": [],
                "can_disable": False,
                "can_delete": False,
            }

        def list_tasks(self, **kwargs):
            return []

        def list_bot_runs(self, bot_id, **kwargs):
            return [
                {
                    "task_id": "task-test-run-123",
                    "bot_id": bot_id,
                    "status": "completed",
                    "started_at": "2026-08-05T12:00:00Z",
                    "trigger_rule_id": None,
                    "metadata": {
                        "source": "bot_test",
                        "tooling_preflight": {
                            "tooling_state": "ready",
                            "recommended_action": {"label": "continue"},
                            "missing_credential_refs": [],
                        },
                    },
                }
            ]

        def list_bot_artifacts(self, bot_id, **kwargs):
            return []

        def list_workers(self):
            return [{"id": "coding-worker", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {"probes": [{"worker_id": "coding-worker", "probe_status": "ready"}]}

        def list_models(self):
            return []

        def list_keys(self):
            return [{"name": "OLLAMA_CLOUD_KEY"}]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/bots/coding-helper")

    assert resp.status_code == 200
    assert b"Chat Profile" in resp.data
    assert b"Operating Summary" in resp.data
    assert b"Tooling Readiness" in resp.data
    assert b"Preflight" in resp.data
    assert b"bot_test" in resp.data
    assert b"Action: continue" in resp.data
    assert b"Recommended action:" in resp.data
    assert b"continue" in resp.data
    assert b"No blocking tooling readiness issue is currently reported for this bot." in resp.data
    assert b"scheduled" in resp.data
    assert b"ready" in resp.data
    assert b"1 active / 0 paused" in resp.data
    assert b"Coding" in resp.data
    assert b"Backend Routes" in resp.data
    assert b"1 configured" in resp.data
    assert b"Route: ollama_cloud / qwen3.5:397b on coding-worker" in resp.data
    assert b"Use:" in resp.data
    assert b"Tool-enabled chat" in resp.data
    assert b"Autonomy:" in resp.data
    assert b"manual_chat_only" in resp.data
    assert b"Chat Tools:" in resp.data
    assert b"filesystem, repo_search" in resp.data
    assert b"Personal Memory" in resp.data
    assert b"enabled" in resp.data
    assert b"repo_search" in resp.data
    assert b"Action Scope" in resp.data
    assert b"site/API actions" in resp.data
    assert b"browser actions" in resp.data
    assert b"repo edits" in resp.data
    assert b"owner approval gates" in resp.data
    assert b"Connection Actions" in resp.data
    assert b"Owner Approval Actions" in resp.data
    assert b"globeiq-agent-api.updateLesson" in resp.data
    assert b"Browser Actions" in resp.data
    assert b"Browser Owner Approval Actions" in resp.data
    assert b"lesson_preview.read" in resp.data
    assert b"Required Worker Tools" in resp.data
    assert b"Credential References" in resp.data
    assert b"OLLAMA_CLOUD_KEY" in resp.data
    assert b"repo-search" in resp.data
    assert b"coding-worker" in resp.data
    assert b"No worker binding declared" not in resp.data
    assert b"repo search" in resp.data
    assert b"filesystem" in resp.data
    assert b'id="bot-chat-profile-mode"' in resp.data
    assert b'id="bot-chat-profile-description"' in resp.data
    assert b'id="bot-chat-profile-diagrams"' in resp.data


def test_bot_detail_page_still_loads_when_history_endpoints_fail(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "History Fault Bot",
                "role": "assistant",
                "priority": 1,
                "enabled": True,
                "backends": [],
                "routing_rules": {},
                "execution_policy": {},
            }

        def get_bot_readiness(self, bot_id):
            return None

        def get_bot_dependencies(self, bot_id):
            return None

        def list_tasks(self, **kwargs):
            return None

        def list_bot_runs(self, bot_id, **kwargs):
            return None

        def list_bot_artifacts(self, bot_id, **kwargs):
            return None

        def list_workers(self):
            return []

        def list_models(self):
            return []

        def list_keys(self):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/bots/history-fault-bot")

    assert resp.status_code == 200
    assert b"History Fault Bot" in resp.data
    assert b"Chat Profile" in resp.data


def test_bot_detail_page_redacts_raw_backend_credential_refs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Raw Secret Bot",
                "role": "assistant",
                "enabled": True,
                "backends": [
                    {
                        "type": "cloud_api",
                        "provider": "openai",
                        "model": "gpt-5",
                        "api_key_ref": "sk-live-secret",
                    }
                ],
            }

        def get_bot_readiness(self, bot_id):
            return {
                "bot_id": bot_id,
                "state": "ready",
                "ready": True,
                "summary": {"checks": 0, "blocking": 0, "warnings": 0},
                "checks": [],
            }

        def get_bot_dependencies(self, bot_id):
            return {"schedule_references": [], "workflow_references": [], "can_disable": True, "can_delete": True}

        def list_tasks(self, **kwargs):
            return []

        def list_bot_runs(self, bot_id, **kwargs):
            return []

        def list_bot_artifacts(self, bot_id, **kwargs):
            return []

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_models(self):
            return []

        def list_keys(self):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/bots/raw-secret-bot")

    assert resp.status_code == 200
    assert b"Raw Secret Bot" in resp.data
    assert b"[redacted raw credential]" in resp.data
    assert b"Replace with a vault key reference before saving." in resp.data
    assert b"sk-live-secret" not in resp.data
    assert b"api_key_ref_raw_detected" in resp.data
    assert b"configure vault key" in resp.data


def test_bot_test_run_api_proxies_to_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Ready Bot",
                "enabled": True,
                "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5", "api_key_ref": "OLLAMA_CLOUD_KEY"}],
            }

        def get_bot_readiness(self, bot_id):
            return {"bot_id": bot_id, "state": "ready", "ready": True, "checks": []}

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return [{"name": "OLLAMA_CLOUD_KEY"}]

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            return {"id": "task-123", "bot_id": bot_id, "payload": payload, "metadata": metadata}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/bots/bot-1/test-run",
            json={"payload": {"instruction": "hello"}},
        )

    assert resp.status_code == 201
    assert resp.get_json()["id"] == "task-123"
    assert resp.get_json()["metadata"]["execution_mode"] == "test"
    assert resp.get_json()["metadata"]["tooling_preflight"]["tooling_state"] == "ready"
    assert resp.get_json()["metadata"]["tooling_preflight"]["credential_refs"] == ["OLLAMA_CLOUD_KEY"]


def test_bot_test_run_api_blocks_tooling_preflight_failures(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Blocked Bot",
                "enabled": True,
                "backends": [{"type": "custom", "provider": "http_connection", "model": "attached-http", "api_key_ref": "MISSING_SITE_TOKEN"}],
                "execution_policy": {"connection_action_allowlist": ["site.update"]},
            }

        def get_bot_readiness(self, bot_id):
            return {"bot_id": bot_id, "state": "ready", "ready": True, "checks": []}

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return [{"name": "OTHER_TOKEN"}]

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            raise AssertionError("blocked bot test runs must not create tasks")

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/bots/blocked-bot/test-run",
            json={"payload": {"instruction": "hello"}},
        )

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "bot test run blocked by tooling readiness"
    assert body["tooling"]["tooling_state"] == "blocked"
    assert body["tooling"]["blocking_category"] == "credential"
    assert body["tooling"]["missing_credential_refs"] == ["MISSING_SITE_TOKEN"]
    assert body["tooling"]["recommended_action"]["label"] == "configure vault key"


def test_bot_test_run_api_blocks_raw_credential_refs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_bot(self, bot_id):
            return {
                "id": bot_id,
                "name": "Raw Secret Bot",
                "enabled": True,
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-5", "api_key_ref": "sk-live-secret"}],
            }

        def get_bot_readiness(self, bot_id):
            return {"bot_id": bot_id, "state": "ready", "ready": True, "checks": []}

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            raise AssertionError("raw credential bot test runs must not create tasks")

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/bots/raw-secret-bot/test-run",
            json={"payload": {"instruction": "hello"}},
        )

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "bot test run blocked by tooling readiness"
    assert body["tooling"]["tooling_state"] == "blocked"
    assert body["tooling"]["blocking_category"] == "credential"
    assert body["tooling"]["raw_credential_ref_detected"] is True
    assert body["tooling"]["credential_refs"] == ["[redacted raw credential]"]
    assert body["tooling"]["recommended_action"]["label"] == "configure vault key"


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
                        "concurrency_limit": 2,
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
    assert body["metadata"]["orchestration_concurrency_limit"] == 2
    assert body["pipeline_id"]


def test_bot_launch_api_applies_deterministic_launch_transform(dashboard_client):
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
                        "payload": {"topic": "AP World History", "allowed_lesson_blocks_json": "[\"AdvancedParagraph\"]"},
                    },
                    "output_contract": {
                        "enabled": True,
                        "mode": "payload_transform",
                        "template": {
                            "workflow_type": "course_generation",
                            "course_brief": {
                                "topic": "{{payload.topic}}",
                            },
                            "generation_settings": {
                                "allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}",
                            },
                        },
                    },
                },
            }

        def create_task_full(self, bot_id, payload, metadata=None, depends_on=None):
            return {"id": "task-launch-3", "bot_id": bot_id, "payload": payload, "metadata": metadata}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post("/api/bots/course-intake/launch", json={})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["payload"]["workflow_type"] == "course_generation"
    assert body["payload"]["course_brief"]["topic"] == "AP World History"
    assert body["payload"]["generation_settings"]["allowed_lesson_blocks"] == ["AdvancedParagraph"]


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
    assert b"Executed by" in resp.data


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


def test_chat_message_to_vault_blocks_failed_pm_reports(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def ingest_vault_item(self, body):
            raise AssertionError("failed PM run report should not be ingested")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/message-to-vault",
            json={
                "conversation_id": "c1",
                "message": {
                    "id": "m-failed",
                    "role": "assistant",
                    "content": "failed output",
                    "metadata": {"mode": "pm_run_report", "run_status": "failed"},
                },
            },
        )

    assert resp.status_code == 400
    assert b"failed PM run reports cannot be ingested" in resp.data


def test_chat_ingest_api_excludes_failed_pm_reports(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_conversations(self):
            return [{"id": "c1", "title": "Project Chat"}]

        def list_messages(self, conversation_id):
            assert conversation_id == "c1"
            return [
                {"id": "m1", "role": "user", "content": "build this"},
                {
                    "id": "m2",
                    "role": "assistant",
                    "content": "failed implementation output",
                    "metadata": {"mode": "pm_run_report", "run_status": "failed"},
                },
                {"id": "m3", "role": "assistant", "content": "safe summary"},
            ]

        def ingest_vault_item(self, body):
            seen["body"] = body
            return {"id": "vault-1", **body}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/ingest",
            json={"conversation_id": "c1", "namespace": "project:globeiq"},
        )

    assert resp.status_code == 201
    body = seen["body"]
    assert body["title"] == "Chat: Project Chat"
    assert body["namespace"] == "project:globeiq"
    assert "build this" in body["content"]
    assert "safe summary" in body["content"]
    assert "failed implementation output" not in body["content"]


def test_chat_stream_api_validates_required_fields(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/chat/stream", json={})
    assert resp.status_code == 400


def test_chat_page_supports_attachment_picker(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "title": "Chat 1", "scope": "global"}]

        def list_bots(self):
            return [{"id": "bot-vision", "name": "Vision Bot", "backends": [{"provider": "openai", "model": "gpt-4o-mini"}]}]

        def list_projects(self):
            return []

        def list_models(self):
            return [{"id": "openai-gpt-4o-mini", "name": "gpt-4o-mini", "provider": "openai", "capabilities": ["vision"], "enabled": True}]

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c1")

    assert resp.status_code == 200
    assert b'id="chat-attachment-input"' in resp.data
    assert b'Attach Files' in resp.data
    assert b'id="chat-composer-status"' in resp.data
    assert b"chatSendInFlight" in resp.data
    assert b"showChatComposerStatus(data.error || 'Stream send failed')" in resp.data
    assert b"persistChatComposerStatus(data.memory_effective_warning, 'info')" in resp.data
    assert b"replayPersistedChatComposerStatus()" in resp.data
    assert b"clearAcceptedComposerDraft(form)" in resp.data
    assert b"Response stream finished without a saved assistant message." in resp.data


def test_chat_page_formats_saved_attachment_sizes(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "title": "Chat 1", "scope": "global"}]

        def list_messages(self, conversation_id, limit=None):
            return [
                {
                    "id": "m1",
                    "role": "user",
                    "content": "Use these files.",
                    "metadata": {
                        "attachments": [
                            {
                                "name": "notes.md",
                                "mime_type": "text/markdown",
                                "kind": "text",
                                "size_bytes": 1536,
                            },
                            {
                                "name": "diagram.png",
                                "mime_type": "image/png",
                                "kind": "image",
                                "size_bytes": 2_097_152,
                                "data_url": "data:image/png;base64,aGVsbG8=",
                            },
                        ]
                    },
                }
            ]

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c1")

    assert resp.status_code == 200
    assert b"notes.md" in resp.data
    assert b"text/markdown" in resp.data
    assert b"1.5 KB" in resp.data
    assert b"diagram.png" in resp.data
    assert b"image/png" in resp.data
    assert b"2.0 MB" in resp.data
    assert b"2097152 bytes" not in resp.data


def test_chat_page_image_preflight_uses_effective_default_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c-default-vision",
                    "title": "Default Vision",
                    "scope": "global",
                    "default_bot_id": "bot-text",
                    "default_model_id": "vision-model",
                }
            ]

        def list_messages(self, conversation_id, limit=None):
            return []

        def list_bots(self):
            return [
                {
                    "id": "bot-text",
                    "name": "Text Bot",
                    "backends": [{"provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                }
            ]

        def list_projects(self):
            return []

        def list_models(self):
            return [
                {
                    "id": "vision-model",
                    "name": "qwen2.5-vl",
                    "provider": "ollama_cloud",
                    "capabilities": ["vision"],
                    "enabled": True,
                },
                {
                    "id": "text-model",
                    "name": "gpt-oss:120b",
                    "provider": "ollama_cloud",
                    "capabilities": ["chat"],
                    "enabled": True,
                },
            ]

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c-default-vision")

    assert resp.status_code == 200
    assert b"function effectiveChatModelInfo" in resp.data
    assert b"function routeUsesConversationDefaultModel" in resp.data
    assert b"function modelCatalogEntryById" in resp.data
    assert b"conversation model" in resp.data
    assert b"The newly selected conversation model does not support image attachments" in resp.data
    assert b'value="vision-model" selected' in resp.data


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


def test_chat_message_api_proxies_attachments(dashboard_client):
    _login_admin(dashboard_client)
    seen = {}

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": "globeiq", "default_bot_id": "coding-bot"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "coding-bot", "execution_policy": {"repo_output_mode": "allow"}}]

        def post_message(self, conversation_id, body):
            seen["conversation_id"] = conversation_id
            seen["body"] = body
            return {"user_message": {"id": "u1", "content": body.get("content", "")}, "assistant_message": {"id": "a1", "content": "ok"}}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={
                "conversation_id": "c1",
                "content": "review this",
                "inline_coding_enabled": True,
                "attachments": [{"name": "notes.md", "mime_type": "text/markdown", "kind": "text", "text_content": "# Notes"}],
            },
        )

    assert resp.status_code == 200
    assert seen["conversation_id"] == "c1"
    assert seen["body"]["inline_coding_enabled"] is True
    assert seen["body"]["attachments"][0]["name"] == "notes.md"


def test_chat_message_api_blocks_invalid_attachment_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            raise AssertionError("invalid attachments should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "attachments": {"name": "bad.txt"}},
        )

    assert resp.status_code == 400
    assert b"Invalid attachments" in resp.data
    assert b"attachments must be a list" in resp.data


def test_chat_message_api_blocks_attachment_count_limit(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            raise AssertionError("too many attachments should not reach control plane message send")

    attachments = [{"name": f"file-{idx}.txt", "kind": "text", "size_bytes": 1} for idx in range(16)]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "attachments": attachments},
        )

    assert resp.status_code == 400
    assert b"Invalid attachments" in resp.data
    assert b"maximum is 15 files per message" in resp.data


def test_chat_message_api_blocks_oversized_content(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            raise AssertionError("oversized content should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "x" * 120001},
        )

    assert resp.status_code == 400
    assert b"Invalid message content" in resp.data
    assert b"limited to 120000 characters" in resp.data


def test_chat_page_embeds_message_content_client_guard(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "title": "Chat", "project_id": None, "archived": False}]

        def get_conversation(self, conversation_id):
            return {"id": conversation_id, "title": "Chat", "project_id": None, "archived": False}

        def list_messages(self, conversation_id):
            return []

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_models(self):
            return []

        def list_vault_items(self, **kwargs):
            return []

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/chat?conversation_id=c1")

    assert resp.status_code == 200
    assert b"CHAT_MESSAGE_CONTENT_MAX_CHARS = 120000" in resp.data
    assert b"function chatMessageContentBlocker" in resp.data
    assert b"Messages are limited to" in resp.data


def test_chat_message_api_blocks_invalid_context_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            raise AssertionError("invalid context payload should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "context_items": {"id": "vault-1"}},
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_items must be a list" in resp.data


def test_chat_message_api_proxies_string_context_items(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def post_message(self, conversation_id, body):
            seen["conversation_id"] = conversation_id
            seen["body"] = body
            return {"assistant_message": {"id": "a1", "content": "ok"}}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "context_items": ["Chat: prior notes"]},
        )

    assert resp.status_code == 200
    assert seen["conversation_id"] == "c1"
    assert seen["body"]["context_items"] == ["Chat: prior notes"]


def test_chat_message_api_blocks_object_context_items(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_message(self, conversation_id, body):
            raise AssertionError("object context payload should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "context_items": [{"id": "vault-1"}]},
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_items must contain strings" in resp.data


def test_chat_stream_api_blocks_oversized_context_item_id(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

    def _fake_post(*args, **kwargs):
        raise AssertionError("oversized context id should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello", "context_item_ids": ["x" * 257]},
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_item_ids entries are limited to 256 characters" in resp.data


def test_chat_stream_api_proxies_string_context_and_vault_ids(dashboard_client):
    _login_admin(dashboard_client)
    captured: dict[str, object] = {}

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

        def list_bot_readiness(self):
            return {"readiness": []}

    def _fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeStreamResponse()

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={
                "conversation_id": "c1",
                "content": "hello",
                "context_items": ["Chat: prior notes"],
                "context_item_ids": ["vault-1"],
                "include_project_context": True,
            },
        )

    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/chat/conversations/c1/stream")
    assert captured["json"]["context_items"] == ["Chat: prior notes"]
    assert captured["json"]["context_item_ids"] == ["vault-1"]
    assert captured["json"]["include_project_context"] is True


def test_chat_message_api_blocks_image_attachment_for_text_only_effective_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-3.5-turbo"}]}]

        def post_message(self, conversation_id, body):
            raise AssertionError("unsupported image attachment should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 409
    assert b"Image attachments are not available" in resp.data
    assert b"selected chat bot model does not support image attachments" in resp.data


def test_chat_message_api_blocks_image_attachment_when_effective_model_unavailable(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot", "default_model_id": "missing-model"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-4o-mini"}]}]

        def list_models(self):
            return []

        def post_message(self, conversation_id, body):
            raise AssertionError("unavailable effective model should not reach control plane message send")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 409
    assert b"Image attachments are not available" in resp.data
    assert b"effective model unavailable" in resp.data
    assert b"missing-model is not in the enabled model catalog" in resp.data


def test_chat_message_api_allows_image_attachment_for_vision_default_model(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot", "default_model_id": "vision-default"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-3.5-turbo"}]}]

        def list_models(self):
            return [{"id": "vision-default", "name": "gpt-4o-mini", "provider": "openai", "enabled": True, "capabilities": ["vision"]}]

        def post_message(self, conversation_id, body):
            seen["conversation_id"] = conversation_id
            seen["body"] = body
            return {"assistant_message": {"id": "a1", "content": "ok"}}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 200
    assert seen["conversation_id"] == "c1"
    assert seen["body"]["attachments"][0]["kind"] == "image"


def test_chat_stream_api_blocks_image_attachment_for_text_only_effective_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-3.5-turbo"}]}]

    def _fake_post(*args, **kwargs):
        raise AssertionError("unsupported image attachment should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 409
    assert b"Image attachments are not available" in resp.data
    assert b"selected chat bot model does not support image attachments" in resp.data


def test_chat_stream_api_blocks_image_attachment_when_effective_model_unavailable(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot", "default_model_id": "missing-model"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-4o-mini"}]}]

        def list_models(self):
            return []

    def _fake_post(*args, **kwargs):
        raise AssertionError("unavailable effective model should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 409
    assert b"Image attachments are not available" in resp.data
    assert b"effective model unavailable" in resp.data
    assert b"missing-model is not in the enabled model catalog" in resp.data


def test_chat_stream_api_allows_image_attachment_for_vision_default_model(dashboard_client):
    _login_admin(dashboard_client)
    captured: dict[str, object] = {}

    class FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=True):
            yield "event: done"
            yield 'data: {"ok": true}'
            yield ""

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "text-bot", "default_model_id": "vision-default"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "text-bot", "backends": [{"provider": "openai", "model": "gpt-3.5-turbo"}]}]

        def list_models(self):
            return [{"id": "vision-default", "name": "gpt-4o-mini", "provider": "openai", "enabled": True, "capabilities": ["vision"]}]

    def _fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeStreamResponse()

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={
                "conversation_id": "c1",
                "content": "read this screenshot",
                "attachments": [{"name": "screen.png", "kind": "image", "mime_type": "image/png", "size_bytes": 42}],
            },
        )

    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/chat/conversations/c1/stream")
    assert captured["json"]["attachments"][0]["kind"] == "image"


def test_chat_stream_api_blocks_attachment_total_size_limit(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_bot_readiness(self):
            return {"readiness": []}

    def _fake_post(*args, **kwargs):
        raise AssertionError("oversized attachments should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={
                "conversation_id": "c1",
                "content": "hello",
                "attachments": [{"name": "huge.bin", "kind": "file", "size_bytes": 1_073_741_825}],
            },
        )

    assert resp.status_code == 400
    assert b"Invalid attachments" in resp.data
    assert b"attachments exceed 1073741824 bytes total" in resp.data


def test_chat_stream_api_blocks_oversized_content(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

    def _fake_post(*args, **kwargs):
        raise AssertionError("oversized stream content should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "x" * 120001},
        )

    assert resp.status_code == 400
    assert b"Invalid message content" in resp.data
    assert b"limited to 120000 characters" in resp.data


def test_chat_stream_api_blocks_invalid_context_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

    def _fake_post(*args, **kwargs):
        raise AssertionError("invalid context payload should not open an upstream stream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello", "context_item_ids": [{"id": "vault-1"}]},
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_item_ids must contain strings" in resp.data


def test_chat_messages_api_proxies_control_plane_messages(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_messages(self, conversation_id, limit=None):
            seen["conversation_id"] = conversation_id
            seen["limit"] = limit
            return [{"id": "m1", "role": "assistant", "content": "hello"}]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/conversations/c1/messages")

    assert resp.status_code == 200
    assert seen == {"conversation_id": "c1", "limit": None}
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


def test_chat_apply_assignment_api_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def apply_project_assignment_to_repo_workspace(self, project_id, orchestration_id, overwrite=True):
            seen["overwrite"] = overwrite
            return {
                "status": "ok",
                "project_id": project_id,
                "orchestration_id": orchestration_id,
                "applied_files": [{"path": "src/demo.ts", "status": "created"}],
                "workspace": {"branch": "main", "porcelain": ["?? src/demo.ts"]},
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/apply",
            json={"project_id": "proj-1", "orchestration_id": "orch-1", "overwrite": "false"},
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["applied_files"][0]["path"] == "src/demo.ts"
    assert seen["overwrite"] is False


def test_chat_review_assignment_api_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def review_project_assignment_files(
            self,
            project_id,
            orchestration_id,
            include_content=True,
            max_content_chars=20000,
            diff_context_lines=3,
        ):
            seen["include_content"] = include_content
            seen["max_content_chars"] = max_content_chars
            seen["diff_context_lines"] = diff_context_lines
            return {
                "status": "ok",
                "project_id": project_id,
                "orchestration_id": orchestration_id,
                "file_count": 1,
                "apply_eligible_count": 1,
                "status_counts": {"new": 1},
                "review_files": [
                    {
                        "path": "docs/demo.md",
                        "status": "new",
                        "apply_eligible": True,
                        "diff": "--- /dev/null\n+++ b/docs/demo.md\n",
                        "generated_content": "# demo\n",
                    }
                ],
                "workspace": {"branch": "main", "porcelain": []},
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/review",
            json={
                "project_id": "proj-1",
                "orchestration_id": "orch-1",
                "include_content": "false",
                "max_content_chars": "500",
                "diff_context_lines": "99",
            },
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["review_files"][0]["path"] == "docs/demo.md"
    assert seen == {"include_content": False, "max_content_chars": 1000, "diff_context_lines": 20}


def test_chat_review_assignment_api_rejects_invalid_numeric_options(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def review_project_assignment_files(self, **kwargs):
            raise AssertionError("invalid review options should not reach control plane")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/review",
            json={"project_id": "proj-1", "orchestration_id": "orch-1", "max_content_chars": "many"},
        )

    assert resp.status_code == 400
    assert b"must be integers" in resp.data


def test_chat_orchestration_recap_api_builds_full_recap(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200, include_content=True):
            rows = [
                {
                    "id": "task-1",
                    "bot_id": "pm-coder",
                    "status": "completed",
                    "payload": [
                        {"role": "system", "content": "Context:\n[workspace:file] src/main.py"},
                        {"role": "user", "content": "Please code this feature in the repo."},
                    ],
                    "result": {
                        "output": "full implementation output",
                        "tool_calls": [{"id": "call-1", "name": "read_file", "arguments": {"path": "src/main.py"}}],
                        "tool_calls_executed": [{"id": "call-1", "name": "read_file", "arguments": {"path": "src/main.py"}}],
                        "usage": {"prompt_tokens": 1234, "completion_tokens": 456},
                        "finish_reason": "stop",
                    },
                    "error": None,
                    "created_at": "2026-03-17T00:00:00+00:00",
                    "updated_at": "2026-03-17T00:05:00+00:00",
                    "metadata": {"orchestration_id": "orch-1"},
                }
            ]
            if orchestration_id:
                return [row for row in rows if (row.get("metadata") or {}).get("orchestration_id") == orchestration_id]
            return rows

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/orchestrations/orch-1/recap")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["orchestration_id"] == "orch-1"
    assert "Assignment Full Recap" in body["recap"]
    assert "full implementation output" in body["recap"]
    assert "Task JSON" in body["recap"]
    assert "Payload JSON" in body["recap"]
    assert "Prompt Messages: 2" in body["recap"]
    assert "Please code this feature in the repo." in body["recap"]
    assert "Metadata JSON" in body["recap"]
    assert "Result JSON" in body["recap"]
    assert "Tool Calls Executed" in body["recap"]


def test_chat_create_conversation_api_blocks_unavailable_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-chat",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "model credential missing"}],
                    }
                ]
            }

        def create_conversation(self, body):
            raise AssertionError("blocked default bot should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={"title": "Blocked Default", "default_bot_id": "blocked-chat"},
        )

    assert resp.status_code == 409
    assert b"Default bot is unavailable" in resp.data
    assert b"model credential missing" in resp.data


def test_chat_create_conversation_api_blocks_missing_default_bot_credential_ref(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "missing-key-chat", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "missing-key-chat",
                    "name": "Missing Key Chat",
                    "role": "assistant",
                    "backends": [
                        {
                            "type": "cloud_api",
                            "provider": "ollama_cloud",
                            "model": "gpt-oss:120b",
                            "api_key_ref": "MISSING_OLLAMA_KEY",
                        }
                    ],
                    "routing_rules": {"operator_profile": {"autonomy": "manual_chat_only"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return [{"name": "OTHER_KEY"}]

        def create_conversation(self, body):
            raise AssertionError("missing default bot credential should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={"title": "Missing Key Default", "default_bot_id": "missing-key-chat"},
        )

    assert resp.status_code == 409
    assert b"Default bot is unavailable" in resp.data
    assert b"Missing key-vault credential reference(s): MISSING_OLLAMA_KEY" in resp.data


def test_chat_create_conversation_api_blocks_non_chat_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "worker-qc", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "worker-qc",
                    "name": "Worker QC",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

        def create_conversation(self, body):
            raise AssertionError("non-chat default bot should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={"title": "Worker Default", "default_bot_id": "worker-qc"},
        )

    assert resp.status_code == 409
    assert b"Default bot is unavailable" in resp.data
    assert b"worker-qc is not configured for manual chat use" in resp.data


def test_chat_create_conversation_api_proxies_default_model(dashboard_client):
    _login_admin(dashboard_client)
    captured = {}

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def create_conversation(self, body):
            captured.update(body)
            return {"id": "c-model", "title": body["title"]}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={
                "title": "Model Scoped",
                "scope": "global",
                "default_model_id": "ollama-cloud-gpt-oss-120b",
            },
        )

    assert resp.status_code == 201
    assert captured["default_model_id"] == "ollama-cloud-gpt-oss-120b"
    assert captured["owner_user_id"] == "admin@test.com"


def test_chat_create_conversation_api_blocks_unscoped_workspace_tools(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def create_conversation(self, body):
            raise AssertionError("unscoped workspace tool request should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={
                "title": "Unsafe Tools",
                "scope": "global",
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )

    assert resp.status_code == 400
    assert b"workspace tools require a project-scoped or bridged conversation" in resp.data


def test_chat_create_conversation_api_blocks_invalid_scope(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def create_conversation(self, body):
            raise AssertionError("invalid conversation scope should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={"title": "Bad Scope", "scope": "site-admin"},
        )

    assert resp.status_code == 400
    assert b"scope must be one of: global, project, bridged" in resp.data


def test_chat_create_conversation_api_blocks_disabled_default_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def list_models(self):
            return [
                {
                    "id": "disabled-chat-model",
                    "name": "disabled-model",
                    "provider": "ollama_cloud",
                    "enabled": False,
                }
            ]

        def create_conversation(self, body):
            raise AssertionError("disabled default model should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={"title": "Disabled Model", "default_model_id": "disabled-chat-model"},
        )

    assert resp.status_code == 409
    assert b"Default model is unavailable" in resp.data
    assert b"disabled-model is disabled" in resp.data


def test_chat_create_conversation_api_blocks_default_route_provider_mismatch(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def list_models(self):
            return [{"id": "openai-chat-model", "name": "gpt-4o-mini", "provider": "openai", "enabled": True}]

        def list_bots(self):
            return [
                {
                    "id": "ollama-chat",
                    "name": "Ollama Chat",
                    "backends": [{"provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                }
            ]

        def create_conversation(self, body):
            raise AssertionError("incompatible default route should not reach control plane create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/conversations",
            json={
                "title": "Provider Mismatch",
                "default_bot_id": "ollama-chat",
                "default_model_id": "openai-chat-model",
            },
        )

    assert resp.status_code == 409
    assert b"Default route is unavailable" in resp.data
    assert b"default_model_id provider 'openai' is not available on default_bot_id 'ollama-chat'" in resp.data


def test_chat_assignment_preview_api_blocks_unavailable_pm_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-pm",
                        "state": "disabled",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "PM schedule disabled"}],
                    }
                ]
            }

        def preview_assignment(self, body):
            raise AssertionError("blocked PM should not reach control plane preview")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/preview",
            json={"conversation_id": "c1", "instruction": "Do work", "pm_bot_id": "blocked-pm"},
        )

    assert resp.status_code == 409
    assert b"Project manager bot is unavailable" in resp.data
    assert b"PM schedule disabled" in resp.data


def test_chat_assignment_preview_blocks_invalid_context_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def preview_assignment(self, body):
            raise AssertionError("invalid assignment preview context should not reach control plane")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/preview",
            json={
                "conversation_id": "c1",
                "instruction": "Do work",
                "pm_bot_id": "pm-bot",
                "context_items": [{"id": "vault-1"}],
            },
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_items must contain strings" in resp.data


def test_chat_assignment_preview_proxies_string_context_items(dashboard_client):
    _login_admin(dashboard_client)
    seen = {}

    class FakeCP:
        def preview_assignment(self, body):
            seen["body"] = body
            return {
                "run_id": "run-1",
                "context_item_count": len(body.get("context_items") or []) + len(body.get("context_item_ids") or []),
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/preview",
            json={
                "conversation_id": "c1",
                "instruction": "Do work",
                "pm_bot_id": "pm-bot",
                "context_items": ["Chat: prior notes"],
                "context_item_ids": ["vault-1"],
            },
        )

    assert resp.status_code == 200
    assert seen["body"]["context_items"] == ["Chat: prior notes"]
    assert seen["body"]["context_item_ids"] == ["vault-1"]
    assert resp.get_json()["context_item_count"] == 2


def test_chat_assignment_create_blocks_invalid_context_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def create_assignment(self, body):
            raise AssertionError("invalid assignment context should not reach control plane")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments",
            json={
                "conversation_id": "c1",
                "instruction": "Do work",
                "pm_bot_id": "pm-bot",
                "context_items": [{"id": "vault-1"}],
            },
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_items must contain strings" in resp.data


def test_chat_assignment_splice_blocks_invalid_context_payload(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def splice_assignment(self, *args, **kwargs):
            raise AssertionError("invalid splice context should not reach control plane")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments/assignment-1/splice",
            json={"from_node_id": "node-1", "context_items": {"id": "vault-1"}},
        )

    assert resp.status_code == 400
    assert b"Invalid context payload" in resp.data
    assert b"context_items must be a list" in resp.data


def test_chat_message_api_blocks_unavailable_explicit_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-chat",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "chat model credential missing"}],
                    }
                ]
            }

        def post_message(self, conversation_id, body):
            raise AssertionError("blocked explicit chat bot should not reach control plane post")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "bot_id": "blocked-chat"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"chat model credential missing" in resp.data


def test_chat_message_api_blocks_missing_explicit_bot_credential_ref(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "missing-key-chat", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "missing-key-chat",
                    "name": "Missing Key Chat",
                    "role": "assistant",
                    "backends": [
                        {
                            "type": "cloud_api",
                            "provider": "ollama_cloud",
                            "model": "gpt-oss:120b",
                            "api_key_ref": "MISSING_OLLAMA_KEY",
                        }
                    ],
                    "routing_rules": {"operator_profile": {"autonomy": "manual_chat_only"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return [{"name": "OTHER_KEY"}]

        def post_message(self, conversation_id, body):
            raise AssertionError("missing explicit bot credential should not reach control plane post")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "bot_id": "missing-key-chat"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"Missing key-vault credential reference(s): MISSING_OLLAMA_KEY" in resp.data


def test_chat_message_api_blocks_unavailable_conversation_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "default_bot_id": "blocked-default"}]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-default",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "default bot disabled after chat creation"}],
                    }
                ]
            }

        def post_message(self, conversation_id, body):
            raise AssertionError("blocked default chat bot should not reach control plane post")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"default bot disabled after chat creation" in resp.data


def test_chat_message_api_blocks_non_chat_conversation_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "default_bot_id": "worker-qc"}]

        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "worker-qc", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "worker-qc",
                    "name": "Worker QC",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

        def post_message(self, conversation_id, body):
            raise AssertionError("non-chat default bot should not reach control plane post")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"worker-qc is not configured for manual chat use" in resp.data


def test_chat_message_api_blocks_workspace_tools_without_project_policy(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c1",
                    "project_id": "globeiq",
                    "default_bot_id": "tool-bot",
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": True,
                }
            ]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [
                {
                    "id": "tool-bot",
                    "routing_rules": {
                        "chat_tool_access": {"enabled": True, "filesystem": True, "repo_search": True}
                    },
                }
            ]

        def get_project_chat_tool_access(self, project_id):
            assert project_id == "globeiq"
            return {"enabled": False, "filesystem": True, "repo_search": True}

        def post_message(self, conversation_id, body):
            raise AssertionError("workspace tool request should not dispatch when project policy is off")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "use_workspace_tools": True},
        )

    assert resp.status_code == 409
    assert b"Workspace tools are not available" in resp.data
    assert b"project off" in resp.data


def test_chat_message_api_allows_workspace_tools_when_all_gates_overlap(dashboard_client):
    _login_admin(dashboard_client)
    seen: dict[str, object] = {}

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c1",
                    "project_id": "globeiq",
                    "default_bot_id": "tool-bot",
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": False,
                }
            ]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [
                {
                    "id": "tool-bot",
                    "routing_rules": {
                        "chat_tool_access": {"enabled": True, "filesystem": True, "repo_search": True}
                    },
                }
            ]

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": True, "filesystem": True, "repo_search": False}

        def post_message(self, conversation_id, body):
            seen["conversation_id"] = conversation_id
            seen["body"] = body
            return {"assistant_message": {"id": "a1", "content": "ok"}}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "hello", "use_workspace_tools": True},
        )

    assert resp.status_code == 200
    assert seen["conversation_id"] == "c1"
    assert seen["body"]["use_workspace_tools"] is True


def test_chat_message_api_blocks_inline_coding_for_repo_output_denied_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": "globeiq", "default_bot_id": "read-only-bot"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "read-only-bot", "execution_policy": {"repo_output_mode": "deny"}}]

        def post_message(self, conversation_id, body):
            raise AssertionError("inline coding should not dispatch for repo-output denied bot")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/messages",
            json={"conversation_id": "c1", "content": "change the repo", "inline_coding_enabled": True},
        )

    assert resp.status_code == 409
    assert b"Inline coding is not available" in resp.data
    assert b"repo output is deny" in resp.data


def test_chat_assignment_create_api_blocks_unavailable_pm_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-pm",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "PM browser session expired"}],
                    }
                ]
            }

        def create_assignment(self, body):
            raise AssertionError("blocked PM should not reach control plane assignment create")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/chat/assignments",
            json={"conversation_id": "c1", "instruction": "Do work", "pm_bot_id": "blocked-pm"},
        )

    assert resp.status_code == 409
    assert b"Project manager bot is unavailable" in resp.data
    assert b"PM browser session expired" in resp.data


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


def test_chat_conversation_tool_access_api_surfaces_control_plane_error(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c1", "scope": "project", "project_id": "globeiq"}]

        def update_conversation_tool_access(self, conversation_id, enabled, filesystem, repo_search):
            return None

        def last_error(self):
            return {"status_code": 400, "detail": "tool access update blocked"}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/tool-access",
            json={"enabled": True, "filesystem": True, "repo_search": True},
        )

    assert resp.status_code == 400
    assert b"tool access update blocked" in resp.data


def test_chat_conversation_tool_access_api_blocks_unscoped_enablement(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_conversations(self, archived="all"):
            return [{"id": "c-global", "scope": "global", "project_id": None}]

        def update_conversation_tool_access(self, conversation_id, enabled, filesystem, repo_search):
            raise AssertionError("unscoped tool enablement should not reach control plane")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c-global/tool-access",
            json={"enabled": True, "filesystem": True, "repo_search": False},
        )

    assert resp.status_code == 400
    assert b"workspace tools require a project-scoped or bridged conversation" in resp.data


def test_chat_conversation_tool_access_api_allows_unscoped_disablement(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def update_conversation_tool_access(self, conversation_id, enabled, filesystem, repo_search):
            return {
                "id": conversation_id,
                "scope": "global",
                "tool_access_enabled": enabled,
                "tool_access_filesystem": filesystem,
                "tool_access_repo_search": repo_search,
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c-global/tool-access",
            json={"enabled": False, "filesystem": False, "repo_search": False},
        )

    assert resp.status_code == 200
    assert resp.get_json()["tool_access_enabled"] is False


def test_chat_conversation_memory_profile_api_warns_when_project_gate_blocks_memory(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def update_conversation_memory_profile(self, conversation_id, enabled, profile_id):
            return {
                "id": conversation_id,
                "project_id": "globeiq",
                "memory_profiles_enabled": enabled,
                "memory_profile_id": profile_id,
            }

        def list_conversations(self, archived="all"):
            return [{"id": "c-project", "scope": "project", "project_id": "globeiq"}]

        def list_projects(self):
            return [{"id": "globeiq", "memory_profiles_enabled": False}]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c-project/memory-profile",
            json={"enabled": True, "profile_id": "default"},
        )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["memory_profiles_enabled"] is True
    assert "project memory gate is off" in payload["memory_effective_warning"]


def test_chat_conversation_memory_profile_api_allows_unscoped_memory_without_project_warning(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def update_conversation_memory_profile(self, conversation_id, enabled, profile_id):
            return {
                "id": conversation_id,
                "project_id": None,
                "memory_profiles_enabled": enabled,
                "memory_profile_id": profile_id,
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c-global/memory-profile",
            json={"enabled": True, "profile_id": "default"},
        )

    assert resp.status_code == 200
    assert "memory_effective_warning" not in resp.get_json()


def test_chat_conversation_route_defaults_api_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)
    captured = {}

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def update_conversation_route_defaults(self, conversation_id, default_bot_id=None, default_model_id=None):
            captured.update(
                {
                    "conversation_id": conversation_id,
                    "default_bot_id": default_bot_id,
                    "default_model_id": default_model_id,
                }
            )
            return {
                "id": conversation_id,
                "default_bot_id": default_bot_id,
                "default_model_id": default_model_id,
            }

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/route-defaults",
            json={"default_bot_id": "personal-research-chat", "default_model_id": "ollama-cloud-gpt-oss-120b"},
        )

    assert resp.status_code == 200
    assert captured == {
        "conversation_id": "c1",
        "default_bot_id": "personal-research-chat",
        "default_model_id": "ollama-cloud-gpt-oss-120b",
    }


def test_chat_conversation_route_defaults_api_blocks_unavailable_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-chat",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "backend missing"}],
                    }
                ]
            }

        def update_conversation_route_defaults(self, *args, **kwargs):
            raise AssertionError("blocked bot should not reach control plane route defaults update")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/route-defaults",
            json={"default_bot_id": "blocked-chat", "default_model_id": "ollama-cloud-gpt-oss-120b"},
        )

    assert resp.status_code == 409
    assert b"Default bot is unavailable" in resp.data
    assert b"backend missing" in resp.data


def test_chat_conversation_route_defaults_api_blocks_non_chat_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "worker-qc", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "worker-qc",
                    "name": "Worker QC",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

        def update_conversation_route_defaults(self, *args, **kwargs):
            raise AssertionError("non-chat bot should not reach control plane route defaults update")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/route-defaults",
            json={"default_bot_id": "worker-qc"},
        )

    assert resp.status_code == 409
    assert b"Default bot is unavailable" in resp.data
    assert b"worker-qc is not configured for manual chat use" in resp.data


def test_chat_conversation_route_defaults_api_blocks_unknown_default_model(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def list_models(self):
            return [
                {
                    "id": "known-chat-model",
                    "name": "known-model",
                    "provider": "ollama_cloud",
                    "enabled": True,
                }
            ]

        def update_conversation_route_defaults(self, *args, **kwargs):
            raise AssertionError("unknown default model should not reach control plane route defaults update")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/route-defaults",
            json={"default_bot_id": "", "default_model_id": "missing-chat-model"},
        )

    assert resp.status_code == 409
    assert b"Default model is unavailable" in resp.data
    assert b"missing-chat-model is not in the enabled model catalog" in resp.data


def test_chat_conversation_route_defaults_api_blocks_default_route_provider_mismatch(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bot_readiness(self):
            return {"readiness": []}

        def list_models(self):
            return [{"id": "openai-chat-model", "name": "gpt-4o-mini", "provider": "openai", "enabled": True}]

        def list_bots(self):
            return [
                {
                    "id": "ollama-chat",
                    "name": "Ollama Chat",
                    "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "gpt-oss:120b"}],
                }
            ]

        def update_conversation_route_defaults(self, *args, **kwargs):
            raise AssertionError("incompatible default route should not reach control plane route defaults update")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/chat/conversations/c1/route-defaults",
            json={"default_bot_id": "ollama-chat", "default_model_id": "openai-chat-model"},
        )

    assert resp.status_code == 409
    assert b"Default route is unavailable" in resp.data
    assert b"default_model_id provider 'openai' is not available on default_bot_id 'ollama-chat'" in resp.data


def test_chat_stream_api_blocks_unavailable_explicit_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-chat",
                        "state": "disabled",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "stream model disabled"}],
                    }
                ]
            }

    def _fake_post(*args, **kwargs):
        raise AssertionError("blocked stream bot should not open an upstream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello", "bot_id": "blocked-chat"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"stream model disabled" in resp.data


def test_chat_stream_api_blocks_unavailable_conversation_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "default_bot_id": "blocked-default"}]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "blocked-default",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "default stream bot blocked"}],
                    }
                ]
            }

    def _fake_post(*args, **kwargs):
        raise AssertionError("blocked default stream bot should not open an upstream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"default stream bot blocked" in resp.data


def test_chat_stream_api_blocks_non_chat_conversation_default_bot(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "default_bot_id": "worker-qc"}]

        def list_bot_readiness(self):
            return {"readiness": [{"bot_id": "worker-qc", "state": "ready", "ready": True, "checks": []}]}

        def list_bots(self):
            return [
                {
                    "id": "worker-qc",
                    "name": "Worker QC",
                    "role": "qc",
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                }
            ]

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_keys(self):
            return []

    def _fake_post(*args, **kwargs):
        raise AssertionError("non-chat default stream bot should not open an upstream request")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello"},
        )

    assert resp.status_code == 409
    assert b"Selected bot is unavailable" in resp.data
    assert b"worker-qc is not configured for manual chat use" in resp.data


def test_chat_stream_api_blocks_workspace_tools_without_shared_mode(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [
                {
                    "id": "c1",
                    "project_id": "globeiq",
                    "default_bot_id": "tool-bot",
                    "tool_access_enabled": True,
                    "tool_access_filesystem": True,
                    "tool_access_repo_search": False,
                }
            ]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [
                {
                    "id": "tool-bot",
                    "routing_rules": {
                        "chat_tool_access": {"enabled": True, "filesystem": False, "repo_search": True}
                    },
                }
            ]

        def get_project_chat_tool_access(self, project_id):
            return {"enabled": True, "filesystem": True, "repo_search": False}

    def _fake_post(*args, **kwargs):
        raise AssertionError("workspace tool stream request should not dispatch without shared mode")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "hello", "use_workspace_tools": True},
        )

    assert resp.status_code == 409
    assert b"Workspace tools are not available" in resp.data
    assert b"no shared tool mode" in resp.data


def test_chat_stream_api_blocks_inline_coding_for_unscoped_chat(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        base_url = "http://100.81.64.82:8000"

        def list_conversations(self, archived="all"):
            return [{"id": "c1", "project_id": None, "default_bot_id": "coding-bot"}]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_bots(self):
            return [{"id": "coding-bot", "execution_policy": {"repo_output_mode": "allow"}}]

    def _fake_post(*args, **kwargs):
        raise AssertionError("inline coding stream should not dispatch for unscoped chat")

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()), \
         patch("dashboard.routes.chat.requests.post", side_effect=_fake_post):
        resp = dashboard_client.post(
            "/api/chat/stream",
            json={"conversation_id": "c1", "content": "change the repo", "inline_coding_enabled": True},
        )

    assert resp.status_code == 409
    assert b"Inline coding is not available" in resp.data
    assert b"no scoped project" in resp.data


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


def test_chat_orchestration_graph_api_uses_explicit_entry_task_and_trigger_parent_edges(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200, include_content=False):
            return [
                {
                    "id": "task-step-1",
                    "bot_id": "pm-research-analyst",
                    "status": "completed",
                    "payload": {"title": "Research repo implementation patterns"},
                    "depends_on": [],
                    "metadata": {
                        "source": "chat_assign",
                        "orchestration_id": "orch-graph-1",
                        "step_id": "step_1_repo",
                    },
                },
                {
                    "id": "task-trigger-coder",
                    "bot_id": "pm-coder",
                    "status": "running",
                    "payload": {"title": "Fix generated code"},
                    "depends_on": [],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-graph-1",
                        "parent_task_id": "task-step-4",
                    },
                },
                {
                    "id": "task-step-4",
                    "bot_id": "pm-tester",
                    "status": "failed",
                    "payload": {"title": "Execute automated tests and validate behavior"},
                    "depends_on": ["task-step-3"],
                    "metadata": {
                        "source": "chat_assign",
                        "orchestration_id": "orch-graph-1",
                        "step_id": "step_4",
                    },
                },
            ]

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/orchestrations/orch-graph-1/graph")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body is not None
    nodes = body["nodes"]
    by_id = {node["id"]: node for node in nodes}
    assert "orchestrator::orch-graph-1" not in by_id
    assert by_id["task-step-1"]["depends_on"] == []
    assert by_id["task-step-1"]["title"] == "Research repo implementation patterns"
    assert by_id["task-step-4"]["step_id"] == "step_4"
    assert by_id["task-trigger-coder"]["depends_on"] == ["task-step-4"]
    assert {"from": "task-step-4", "to": "task-trigger-coder"} in body["edges"]
    assert by_id["task-step-1"]["display_name"] == "PM Research Analyst"


def test_chat_orchestration_graph_api_includes_reference_graph_stage_order(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200, include_content=False):
            return [
                {
                    "id": "pm_assignment_entry",
                    "bot_id": "pm-orchestrator",
                    "status": "completed",
                    "payload": {"title": "PM assignment intake"},
                    "depends_on": [],
                    "metadata": {
                        "source": "chat_assign",
                        "orchestration_id": "orch-stage-order",
                    },
                },
                {
                    "id": "research-1",
                    "bot_id": "pm-research-analyst",
                    "status": "completed",
                    "payload": {"title": "Repo research", "research_step_index": 0},
                    "depends_on": ["pm_assignment_entry"],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-stage-order",
                        "parent_task_id": "pm_assignment_entry",
                    },
                },
            ]

        def get_bot(self, bot_id):
            if bot_id == "pm-orchestrator":
                return {
                    "id": "pm-orchestrator",
                    "name": "PM Orchestrator",
                    "workflow": {
                        "reference_graph": {
                            "graph_id": "pm-pipeline",
                            "entry_bot_id": "pm-orchestrator",
                            "current_bot_id": "pm-orchestrator",
                            "nodes": [
                                {"bot_id": "pm-orchestrator", "title": "PM Orchestrator", "stage_kind": "entry"},
                                {"bot_id": "pm-research-analyst", "title": "PM Research Analyst", "stage_kind": "research"},
                                {"bot_id": "pm-engineer", "title": "PM Engineer", "stage_kind": "engineering"},
                            ],
                            "edges": [
                                {"source_bot_id": "pm-orchestrator", "target_bot_id": "pm-research-analyst", "route_kind": "forward"},
                                {"source_bot_id": "pm-research-analyst", "target_bot_id": "pm-engineer", "route_kind": "forward"},
                            ],
                        }
                    },
                }
            if bot_id == "pm-research-analyst":
                return {"id": "pm-research-analyst", "name": "PM Research Analyst"}
            return {"id": bot_id, "name": bot_id}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/orchestrations/orch-stage-order/graph")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["stage_order"][:3] == ["pm-orchestrator", "pm-research-analyst", "pm-engineer"]
    assert body["reference_graph"]["graph_id"] == "pm-pipeline"
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes["pm_assignment_entry"]["display_name"] == "PM Orchestrator"
    assert nodes["research-1"]["stage_key"] == "pm-research-analyst"


def test_chat_orchestration_graph_api_uses_pipeline_entry_graph_for_pm_docs(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200, include_content=False):
            return [
                {
                    "id": "pm-docs-entry",
                    "bot_id": "pm-docs",
                    "status": "completed",
                    "payload": {"title": "Docs assignment intake"},
                    "depends_on": [],
                    "metadata": {
                        "source": "chat_assign",
                        "orchestration_id": "orch-pm-docs",
                        "pipeline_entry_bot_id": "pm-docs",
                        "pm_bot_id": "pm-docs",
                        "root_pm_bot_id": "pm-docs",
                    },
                },
                {
                    "id": "research-0",
                    "bot_id": "pm-docs-research",
                    "status": "completed",
                    "payload": {"title": "Repo research", "research_step_index": 0},
                    "depends_on": ["pm-docs-entry"],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-pm-docs",
                        "parent_task_id": "pm-docs-entry",
                        "pipeline_entry_bot_id": "pm-docs",
                    },
                },
                {
                    "id": "engineer-1",
                    "bot_id": "pm-docs-engineer",
                    "status": "queued",
                    "payload": {"title": "Build the docs plan"},
                    "depends_on": ["research-0"],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-pm-docs",
                        "parent_task_id": "research-0",
                        "pipeline_entry_bot_id": "pm-docs",
                    },
                },
            ]

        def get_bot(self, bot_id):
            if bot_id == "pm-docs":
                return {
                    "id": "pm-docs",
                    "name": "PM Docs",
                    "workflow": {
                        "reference_graph": {
                            "graph_id": "pm-docs-pipeline-v1",
                            "entry_bot_id": "pm-docs",
                            "current_bot_id": "pm-docs",
                            "nodes": [
                                {"bot_id": "pm-docs", "title": "PM Docs", "stage_kind": "entry"},
                                {"bot_id": "pm-docs-research", "title": "PM Docs Research", "stage_kind": "research"},
                                {"bot_id": "pm-docs-engineer", "title": "PM Docs Engineer", "stage_kind": "planning"},
                                {"bot_id": "pm-docs-writer", "title": "PM Docs Writer", "stage_kind": "implementation"},
                                {"bot_id": "pm-docs-validator", "title": "PM Docs Validator", "stage_kind": "validation"},
                                {"bot_id": "pm-docs-final-qc", "title": "PM Docs Final QC", "stage_kind": "final_qc"},
                            ],
                            "edges": [
                                {"source_bot_id": "pm-docs", "target_bot_id": "pm-docs-research", "route_kind": "forward"},
                                {"source_bot_id": "pm-docs-research", "target_bot_id": "pm-docs-engineer", "route_kind": "forward"},
                                {"source_bot_id": "pm-docs-engineer", "target_bot_id": "pm-docs-writer", "route_kind": "forward"},
                                {"source_bot_id": "pm-docs-writer", "target_bot_id": "pm-docs-validator", "route_kind": "forward"},
                                {"source_bot_id": "pm-docs-validator", "target_bot_id": "pm-docs-final-qc", "route_kind": "forward"},
                                {"source_bot_id": "pm-docs-validator", "target_bot_id": "pm-docs-writer", "route_kind": "backward"},
                                {"source_bot_id": "pm-docs-final-qc", "target_bot_id": "pm-docs", "route_kind": "backward"},
                            ],
                        }
                    },
                }
            if bot_id == "pm-docs-engineer":
                return {
                    "id": "pm-docs-engineer",
                    "name": "PM Docs Engineer",
                    "workflow": {
                        "reference_graph": {
                            "graph_id": "wrong-local-view",
                            "entry_bot_id": "pm-docs-engineer",
                            "current_bot_id": "pm-docs-engineer",
                            "nodes": [
                                {"bot_id": "pm-docs-engineer", "title": "PM Docs Engineer", "stage_kind": "planning"},
                                {"bot_id": "pm-docs-research", "title": "PM Docs Research", "stage_kind": "research"},
                                {"bot_id": "pm-docs-writer", "title": "PM Docs Writer", "stage_kind": "implementation"},
                            ],
                            "edges": [],
                        }
                    },
                }
            return {"id": bot_id, "name": bot_id}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/orchestrations/orch-pm-docs/graph")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reference_graph"]["graph_id"] == "pm-docs-pipeline-v1"
    assert body["stage_order"][:6] == [
        "pm-docs",
        "pm-docs-research",
        "pm-docs-engineer",
        "pm-docs-writer",
        "pm-docs-validator",
        "pm-docs-final-qc",
    ]
    nodes = {node["id"]: node for node in body["nodes"]}
    assert "orchestrator::orch-pm-docs" not in nodes
    assert nodes["pm-docs-entry"]["bot_id"] == "pm-docs"
    assert nodes["research-0"]["depends_on"] == ["pm-docs-entry"]


def test_chat_orchestration_graph_api_includes_branch_lane_keys(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, orchestration_id=None, statuses=None, bot_id=None, limit=200, include_content=False):
            return [
                {
                    "id": "coder-1",
                    "bot_id": "pm-coder",
                    "status": "completed",
                    "payload": {
                        "title": "Geometry Block Documentation",
                        "workstream_index": 2,
                        "fanout_branch_key": "2",
                    },
                    "depends_on": ["engineer-1"],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-lanes",
                        "parent_task_id": "engineer-1",
                    },
                    "created_at": "2026-03-20T00:00:03+00:00",
                },
                {
                    "id": "tester-1",
                    "bot_id": "pm-tester",
                    "status": "running",
                    "payload": {
                        "title": "Geometry Block Documentation",
                        "workstream_index": 2,
                        "fanout_branch_key": "2",
                    },
                    "depends_on": ["coder-1"],
                    "metadata": {
                        "source": "bot_trigger",
                        "orchestration_id": "orch-lanes",
                        "parent_task_id": "coder-1",
                    },
                    "created_at": "2026-03-20T00:00:04+00:00",
                },
            ]

        def get_bot(self, bot_id):
            return {"id": bot_id, "name": bot_id}

    with patch("dashboard.routes.chat.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/chat/orchestrations/orch-lanes/graph")

    assert resp.status_code == 200
    body = resp.get_json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes["coder-1"]["lane_key"] == "2"
    assert nodes["tester-1"]["lane_key"] == "2"
    assert nodes["tester-1"]["details"]["created_at"] == "2026-03-20T00:00:04+00:00"


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


def test_project_chat_tool_access_api_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.updated = None

        def get_project_chat_tool_access(self, project_id):
            return {
                "project_id": project_id,
                "enabled": True,
                "filesystem": True,
                "repo_search": False,
                "workspace_root": "C:\\repo\\demo",
            }

        def update_project_chat_tool_access(self, **kwargs):
            self.updated = kwargs
            return {"status": "ok", **kwargs}

        def last_error(self):
            return {}

    fake_cp = FakeCP()
    with patch("dashboard.routes.projects.get_cp_client", return_value=fake_cp):
        get_resp = dashboard_client.get("/api/projects/proj-1/chat-tool-access")
        put_resp = dashboard_client.put(
            "/api/projects/proj-1/chat-tool-access",
            json={
                "enabled": True,
                "filesystem": True,
                "repo_search": True,
                "workspace_root": "C:\\repo\\demo",
            },
        )

    assert get_resp.status_code == 200
    assert get_resp.get_json()["filesystem"] is True
    assert put_resp.status_code == 200
    assert fake_cp.updated is not None
    assert fake_cp.updated["project_id"] == "proj-1"
    assert fake_cp.updated["enabled"] is True
    assert fake_cp.updated["repo_search"] is True


def test_project_repo_workspace_api_proxies_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.updated = None
            self.clone_called = None
            self.run_called = None
            self.discard_called = None

        def get_project_repo_workspace(self, project_id):
            return {
                "project_id": project_id,
                "enabled": True,
                "managed_path_mode": True,
                "workspace_binding": "managed",
                "root_path": None,
                "clone_url": "https://github.com/org/demo.git",
                "default_branch": "main",
                "allow_push": True,
                "allow_command_execution": True,
            }

        def update_project_repo_workspace(self, **kwargs):
            self.updated = kwargs
            return {"status": "ok", **kwargs}

        def get_project_repo_workspace_status(self, project_id):
            return {
                "project_id": project_id,
                "enabled": True,
                "workspace_exists": True,
                "is_repo": True,
                "branch": "main",
            }

        def discard_project_repo_workspace_untracked(self, **kwargs):
            self.discard_called = kwargs
            return {"status": "ok", "removed_paths": kwargs.get("paths") or []}

        def clone_project_repo_workspace(self, **kwargs):
            self.clone_called = kwargs
            return {"status": "ok", **kwargs}

        def pull_project_repo_workspace(self, **kwargs):
            return {"status": "ok", **kwargs}

        def commit_project_repo_workspace(self, **kwargs):
            return {"status": "ok", **kwargs}

        def push_project_repo_workspace(self, **kwargs):
            return {"status": "ok", **kwargs}

        def run_project_repo_workspace_command(self, **kwargs):
            self.run_called = kwargs
            return {"status": "ok", "result": {"ok": True}, **kwargs}

        def list_project_repo_workspace_runs(self, **kwargs):
            return {"project_id": kwargs.get("project_id"), "runs": [{"id": "run-1", "action": "run", "status": "ok"}]}

        def summarize_project_repo_workspace_runs(self, **kwargs):
            return {
                "project_id": kwargs.get("project_id"),
                "since_hours": kwargs.get("since_hours"),
                "totals": {"total_runs": 1, "success_runs": 1, "failed_runs": 0},
                "by_action": [{"action": "run", "runs": 1}],
            }

        def last_error(self):
            return {}

    fake_cp = FakeCP()
    with patch("dashboard.routes.projects.get_cp_client", return_value=fake_cp):
        get_resp = dashboard_client.get("/api/projects/proj-1/repo/workspace")
        put_resp = dashboard_client.put(
            "/api/projects/proj-1/repo/workspace",
            json={
                "enabled": True,
                "managed_path_mode": True,
                "root_path": None,
                "clone_url": "https://github.com/org/demo.git",
                "default_branch": "main",
                "allow_push": True,
                "allow_command_execution": True,
            },
        )
        status_resp = dashboard_client.get("/api/projects/proj-1/repo/workspace/status")
        discard_resp = dashboard_client.post(
            "/api/projects/proj-1/repo/workspace/discard-untracked",
            json={"paths": ["src/demo.py", "tests/test_demo.py"]},
        )
        clone_resp = dashboard_client.post(
            "/api/projects/proj-1/repo/workspace/clone",
            json={"clone_url": "https://github.com/org/demo.git", "branch": "main", "depth": 1},
        )
        run_resp = dashboard_client.post(
            "/api/projects/proj-1/repo/workspace/run",
            json={
                "command": ["py", "-m", "pytest", "-q"],
                "timeout_seconds": 90,
                "use_temp_workspace": True,
                "temp_ref": "main",
                "bootstrap": True,
                "bootstrap_languages": ["python", "node"],
                "keep_temp_workspace": False,
            },
        )
        runs_resp = dashboard_client.get("/api/projects/proj-1/repo/workspace/runs?limit=10")
        summary_resp = dashboard_client.get("/api/projects/proj-1/repo/workspace/runs/summary?since_hours=24")

    assert get_resp.status_code == 200
    assert get_resp.get_json()["root_path"] is None
    assert get_resp.get_json()["managed_path_mode"] is True
    assert put_resp.status_code == 200
    assert fake_cp.updated is not None
    assert fake_cp.updated["project_id"] == "proj-1"
    assert fake_cp.updated["allow_push"] is True
    assert status_resp.status_code == 200
    assert status_resp.get_json()["is_repo"] is True
    assert discard_resp.status_code == 200
    assert fake_cp.discard_called is not None
    assert fake_cp.discard_called["paths"] == ["src/demo.py", "tests/test_demo.py"]
    assert clone_resp.status_code == 200
    assert fake_cp.clone_called is not None
    assert fake_cp.clone_called["depth"] == 1
    assert run_resp.status_code == 200
    assert fake_cp.run_called is not None
    assert fake_cp.run_called["command"] == ["py", "-m", "pytest", "-q"]
    assert fake_cp.run_called["use_temp_workspace"] is True
    assert fake_cp.run_called["bootstrap"] is True
    assert fake_cp.run_called["bootstrap_languages"] == ["python", "node"]
    assert runs_resp.status_code == 200
    assert runs_resp.get_json()["runs"][0]["id"] == "run-1"
    assert summary_resp.status_code == 200
    assert summary_resp.get_json()["totals"]["total_runs"] == 1


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
    assert b"Last Heartbeat" in resp.data


def test_worker_detail_page_surfaces_dependent_bot_worker_scope(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_worker(self, worker_id):
            assert worker_id == "globeiq-worker"
            return {
                "id": "globeiq-worker",
                "name": "GlobeIQ Worker",
                "host": "100.81.64.82",
                "port": 8080,
                "status": "online",
                "enabled": True,
                "runtime_limits": {},
                "last_heartbeat_at": "2026-08-04T21:10:00+00:00",
                "capabilities": [],
                "metrics": {},
            }

        def get_worker_probe(self, worker_id):
            return {"worker_id": worker_id, "probe_status": "ready", "checks": []}

        def get_worker_dependencies(self, worker_id):
            return {
                "can_disable": False,
                "can_delete": False,
                "dependent_bots": [
                    {
                        "id": "course-repair-bot",
                        "name": "Course Repair Bot",
                        "project_id": "globeiq",
                        "enabled": True,
                        "backends": [{"type": "browser", "provider": "browser", "model": "browser-ui", "worker_id": "globeiq-worker"}],
                        "routing_rules": {
                            "worker_profile": {
                                "can_edit": False,
                                "task_scope": "published-lesson-quality-audit",
                                "site_scope": "GlobeIQ",
                                "course_scope": ["101", "102"],
                            }
                        },
                        "execution_policy": {
                            "required_worker_tools": ["browser-ui"],
                            "connection_action_allowlist": ["globeiq-agent-api.updateLesson"],
                            "connection_action_owner_approval_required": ["globeiq-agent-api.updateLesson"],
                            "browser_action_allowlist": ["lesson_preview.read"],
                            "browser_action_owner_approval_required": ["lesson_preview.read"],
                        },
                    }
                ],
                "active_schedules": [],
            }

        def list_tasks(self, **kwargs):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/workers/globeiq-worker")

    assert resp.status_code == 200
    assert b"Worker Scope" in resp.data
    assert b"Routes: browser / browser-ui on globeiq-worker" in resp.data
    assert b"published-lesson-quality-audit" in resp.data
    assert b"Site: GlobeIQ" in resp.data
    assert b"Courses: 101, 102" in resp.data
    assert b"read only" in resp.data
    assert b"Action Policy" in resp.data
    assert b"Tools: browser-ui" in resp.data
    assert b"Site/API actions: globeiq-agent-api.updateLesson" in resp.data
    assert b"Browser actions: lesson_preview.read" in resp.data
    assert b"Owner approvals: 2" in resp.data


def test_workers_page_surfaces_runtime_tool_evidence(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_workers(self):
            return [
                {
                    "id": "nexus-browser-worker",
                    "name": "Nexus Browser Worker",
                    "host": "100.81.64.82",
                    "port": 8080,
                    "status": "online",
                    "enabled": True,
                    "runtime_limits": {"cpus": 4, "memory_limit": "8g", "pids_limit": 512},
                    "last_heartbeat_at": "2026-08-04T21:10:00+00:00",
                    "capabilities": [],
                }
            ]

        def list_bots(self):
            return [
                {
                    "id": "globeiq-browser-auditor",
                    "name": "GlobeIQ Browser Auditor",
                    "enabled": True,
                    "backends": [
                        {
                            "type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                            "worker_id": "nexus-browser-worker",
                        }
                    ],
                },
                {
                    "id": "parked-helper",
                    "name": "Parked Helper",
                    "enabled": False,
                    "backends": [
                        {
                            "type": "llm",
                            "provider": "ollama_cloud",
                            "model": "qwen3.5:cloud",
                            "worker_id": "nexus-browser-worker",
                        }
                    ],
                },
                {
                    "id": "other-worker-bot",
                    "name": "Other Worker Bot",
                    "enabled": True,
                    "backends": [
                        {
                            "type": "browser",
                            "provider": "browser",
                            "model": "browser-ui",
                            "worker_id": "separate-worker",
                        }
                    ],
                },
            ]

        def list_worker_probes(self):
            return {
                "probes": [
                    {
                        "worker_id": "nexus-browser-worker",
                        "probe_status": "ready",
                        "checked_at": "2026-08-04T21:11:00+00:00",
                        "capability_attestation": {
                            "provider_credentials": {
                                "brave_search": False,
                                "ollama_cloud": True,
                            },
                            "installed_cli_tools": ["git", "node"],
                            "enabled_cli_tools": ["git"],
                            "unauthenticated_cli_tools": ["claude"],
                            "browser": {
                                "configured": True,
                                "ready": False,
                                "reason": "browser_session_check_failed",
                            },
                        },
                    }
                ]
            }

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/workers")

    assert resp.status_code == 200
    assert b"Nexus Browser Worker" in resp.data
    assert b"browser_session_check_failed" in resp.data
    assert b"auth needed for claude" in resp.data
    assert b"brave_search missing" in resp.data
    assert b"ollama_cloud ok" in resp.data
    assert b"1 enabled bot(s), 1 disabled" in resp.data
    assert b"Routes: browser / browser-ui on nexus-browser-worker, ollama_cloud / qwen3.5:cloud on nexus-browser-worker" in resp.data
    assert b"GlobeIQ Browser Auditor" in resp.data
    assert b"Parked Helper" in resp.data
    assert b"Other Worker Bot" not in resp.data
    assert b"Open worker detail and reassign or disable dependent bots before disabling this worker." in resp.data
    assert b"Open worker detail and clear dependent bots before deleting this worker." in resp.data
    assert b"secret" not in resp.data.lower()


def test_worker_probe_view_exposes_nonsecret_cli_authentication_blockers():
    from dashboard.routes.workers import _worker_probe_view

    view = _worker_probe_view(
        {
            "probe_status": "ready",
            "checked_at": "2026-07-18T18:37:38+00:00",
            "checks": [],
            "capability_attestation": {"unauthenticated_cli_tools": ["codex", "claude"]},
        }
    )

    assert view is not None
    assert view["status"] == "ready"
    assert view["detail"] == "CLI authentication required: codex, claude"


def test_worker_probe_view_exposes_attested_runtime_tool_evidence():
    from dashboard.routes.workers import _worker_probe_view

    view = _worker_probe_view(
        {
            "probe_status": "ready",
            "checked_at": "2026-07-18T18:37:38+00:00",
            "checks": [],
            "capability_attestation": {
                "provider_credentials": {"ollama_cloud": True, "other": False},
                "installed_cli_tools": ["claude", "git"],
                "enabled_cli_tools": ["claude"],
                "auth_required_cli_tools": ["claude"],
                "browser": {"configured": True, "ready": True, "browser": "chromium"},
            },
        }
    )

    assert view is not None
    evidence = view["runtime_evidence"]
    assert evidence["provider_status"] == [
        {"provider": "ollama_cloud", "configured": True},
        {"provider": "other", "configured": False},
    ]
    assert evidence["installed_cli_tools"] == ["claude", "git"]
    assert evidence["enabled_cli_tools"] == ["claude"]
    assert evidence["browser"] == {
        "configured": True,
        "ready": True,
        "name": "chromium",
        "reason": "",
    }


def test_worker_probe_view_marks_unavailable_browser_session_degraded():
    from dashboard.routes.workers import _worker_probe_view

    view = _worker_probe_view(
        {
            "probe_status": "ready",
            "checked_at": "2026-07-18T18:37:38+00:00",
            "checks": [],
            "capability_attestation": {
                "browser": {"configured": True, "ready": False, "reason": "browser_session_check_failed"}
            },
        }
    )

    assert view is not None
    assert view["status"] == "degraded"
    assert view["detail"] == "Browser session unavailable: browser_session_check_failed"


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
    assert b"Test Enabled Tools" in resp.data
    assert b"Task Provider Concurrency Limits" in resp.data
    assert b"Token Governor" in resp.data
    assert b'data-target="section-token-governor"' in resp.data
    assert b"Token Governor Project Hourly Limit" in resp.data
    assert b"Token Governor Bot Hourly Limits" in resp.data
    assert b"Token Governor Chat Global Hourly Limit" in resp.data
    assert b"Chat State" in resp.data
    assert b"Chat Hourly Remaining" in resp.data
    assert b"Reserve / Chat" in resp.data


def test_settings_token_governor_api_reports_settings_and_live_status(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def task_usage(self, hours=24, limit_bots=25, timeout=None):
            return {
                "token_governor": {
                    "enabled": True,
                    "limits": {
                        "global_hourly_tokens": 1000,
                        "project_hourly_tokens": 500,
                        "manager_hourly_tokens": 250,
                        "llm_concurrency": 3,
                        "estimated_tokens_per_task": 50,
                    },
                    "current": {
                        "global_hourly_remaining": 800,
                        "running_llm_tasks": 1,
                    },
                }
            }

        def chat_usage(self, hours=24, limit_conversations=25, timeout=None):
            return {
                "chat_token_governor": {
                    "enabled": True,
                    "limits": {
                        "global_hourly_tokens": 90000,
                        "bot_hourly_tokens": 12000,
                        "estimated_tokens_per_message": 3500,
                    },
                    "current": {
                        "global_hourly_remaining": 85000,
                    },
                }
            }

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/api/settings/token-governor")

    assert resp.status_code == 200
    data = resp.get_json()
    keys = {item["key"] for item in data["settings"]}
    assert "token_governor_project_hourly_limit" in keys
    assert "token_governor_manager_hourly_limit" in keys
    assert "token_governor_bot_hourly_limits" in keys
    assert "token_governor_chat_global_hourly_limit" in keys
    assert "token_governor_chat_bot_hourly_limit" in keys
    assert "token_governor_chat_bot_hourly_limits" in keys
    assert "token_governor_estimated_tokens_per_chat_message" in keys
    assert data["status"]["limits"]["project_hourly_tokens"] == 500
    assert data["status"]["current"]["running_llm_tasks"] == 1
    assert data["status"]["chat"]["limits"]["bot_hourly_tokens"] == 12000
    assert data["status"]["chat"]["current"]["global_hourly_remaining"] == 85000


def test_settings_token_governor_api_updates_whitelisted_values(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def task_usage(self, hours=24, limit_bots=25, timeout=None):
            return {"token_governor": {"enabled": True, "limits": {}, "current": {}}}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.put(
            "/api/settings/token-governor",
            json={
                "token_governor_enabled": True,
                "token_governor_project_hourly_limit": "120000",
                "token_governor_manager_hourly_limit": 30000,
                "token_governor_bot_hourly_limits": {"audit-reader": "50000"},
                "token_governor_chat_global_hourly_limit": "90000",
                "token_governor_chat_bot_hourly_limit": 12000,
                "token_governor_chat_bot_hourly_limits": {"general-chat": "8000"},
                "token_governor_estimated_tokens_per_chat_message": "3500",
                "token_governor_bot_estimates": {"audit-reader": "2500"},
            },
        )

    assert resp.status_code == 200
    data = resp.get_json()
    settings = {item["key"]: item["value"] for item in data["settings"]}
    assert settings["token_governor_enabled"] == "true"
    assert settings["token_governor_project_hourly_limit"] == "120000"
    assert settings["token_governor_manager_hourly_limit"] == "30000"
    assert json.loads(settings["token_governor_bot_hourly_limits"]) == {"audit-reader": 50000}
    assert settings["token_governor_chat_global_hourly_limit"] == "90000"
    assert settings["token_governor_chat_bot_hourly_limit"] == "12000"
    assert json.loads(settings["token_governor_chat_bot_hourly_limits"]) == {"general-chat": 8000}
    assert settings["token_governor_estimated_tokens_per_chat_message"] == "3500"
    assert json.loads(settings["token_governor_bot_estimates"]) == {"audit-reader": 2500}


def test_settings_token_governor_api_rejects_unknown_or_invalid_values(dashboard_client):
    _login_admin(dashboard_client)

    unknown = dashboard_client.put(
        "/api/settings/token-governor",
        json={"task_provider_concurrency_limits": "{}"},
    )
    assert unknown.status_code == 400
    assert "task_provider_concurrency_limits" in unknown.get_data(as_text=True)

    negative = dashboard_client.put(
        "/api/settings/token-governor",
        json={"token_governor_project_hourly_limit": -1},
    )
    assert negative.status_code == 400
    assert "non-negative integer" in negative.get_data(as_text=True)


def test_settings_page_handles_noncanonical_cp_payloads(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def health(self):
            return True

        def list_keys(self):
            return [{"name": "vertex-main", "provider": "vertex", "updated_at": 1234567890}]

        def list_models(self):
            return []

        def list_projects(self):
            return [
                {
                    "id": "globeiq",
                    "name": "GlobeIQ",
                    "mode": "isolated",
                    "enabled": True,
                    "bridge_project_ids": None,
                }
            ]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/settings")

    assert resp.status_code == 200
    assert b"Projects" in resp.data
    assert b"GlobeIQ" in resp.data


def test_settings_tools_api_reports_install_support(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/settings/tools")
    assert resp.status_code == 200
    data = resp.get_json()
    dotnet = next(tool for tool in data["tools"] if tool["id"] == "code_exec_dotnet")
    git = next(tool for tool in data["tools"] if tool["id"] == "devops_git")
    assert dotnet["install_supported"] is True
    assert git["install_supported"] is True


def test_settings_tool_status_checks_enabled_tools(dashboard_client):
    _login_admin(dashboard_client)
    bulk_resp = dashboard_client.put(
        "/api/settings/tools",
        json={"enabled_tools": ["code_exec_python", "code_exec_dotnet"]},
    )
    assert bulk_resp.status_code == 200

    def _fake_run(command, capture_output=None, text=None, shell=None, timeout=None, check=None, env=None):
        class Result:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if command == "python --version":
            return Result(0, "Python 3.12.2\n", "")
        if command == "dotnet --version":
            return Result(1, "", "'dotnet' is not recognized as an internal or external command")
        raise AssertionError(f"Unexpected tool check command: {command}")

    with patch("dashboard.settings.subprocess.run", side_effect=_fake_run):
        resp = dashboard_client.post("/api/settings/tools/test", json={"scope": "enabled"})

    assert resp.status_code == 200
    data = resp.get_json()
    statuses = {item["id"]: item for item in data["statuses"]}
    assert statuses["code_exec_python"]["status"] == "installed"
    assert statuses["code_exec_dotnet"]["status"] == "missing"


def test_settings_tool_install_enables_tool_after_success(dashboard_client):
    _login_admin(dashboard_client)
    dashboard_client.put("/api/settings/tools", json={"enabled_tools": []})

    class FakeInstallManager:
        def __init__(self):
            self.started = False

        def start(self, tool, plan, enable_callback):
            self.started = True
            enable_callback(tool.id)
            return True, {
                "run_id": "run-1",
                "tool_id": tool.id,
                "state": "running",
                "current_step": 0,
                "total_steps": 1,
                "command_log": [],
            }

        def latest_for_tool(self, tool_id):
            return {
                "run_id": "run-1",
                "tool_id": tool_id,
                "state": "succeeded",
                "current_step": 1,
                "total_steps": 1,
                "command_log": [],
                "tool_status": {"status": "installed", "summary": "8.0.203"},
                "enabled": True,
            }

    fake_manager = FakeInstallManager()

    with patch("dashboard.settings.platform.system", return_value="Windows"), \
         patch("dashboard.settings.ToolInstallManager.instance", return_value=fake_manager):
        resp = dashboard_client.post("/api/settings/tools/install/code_exec_dotnet")
        status_resp = dashboard_client.get("/api/settings/tools/install/code_exec_dotnet/status")

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["state"] == "running"
    assert status_resp.status_code == 200
    assert status_resp.get_json()["state"] == "succeeded"

    list_resp = dashboard_client.get("/api/settings/tools")
    assert list_resp.status_code == 200
    tools = {tool["id"]: tool for tool in list_resp.get_json()["tools"]}
    assert tools["code_exec_dotnet"]["enabled"] is True


def test_settings_tools_api_reports_linux_install_support_for_playwright(dashboard_client):
    _login_admin(dashboard_client)
    with patch("dashboard.settings.platform.system", return_value="Linux"):
        resp = dashboard_client.get("/api/settings/tools")
    assert resp.status_code == 200
    data = resp.get_json()
    browser = next(tool for tool in data["tools"] if tool["id"] == "ui_browser")
    dotnet = next(tool for tool in data["tools"] if tool["id"] == "code_exec_dotnet")
    assert browser["install_supported"] is True
    assert dotnet["install_supported"] is True
    assert browser["install_mode"] == "runtime_deploy"
    assert dotnet["install_mode"] == "runtime_deploy"

    from dashboard import settings as settings_module

    with patch("dashboard.settings.platform.system", return_value="Linux"):
        plan = settings_module._tool_install_plan("code_exec_dotnet")
        assert plan is not None
        assert any(
            isinstance(command, list) and "curl" in " ".join(command)
            for command in plan["commands"]
        )


def test_settings_runtime_tool_install_configures_env_and_requires_deploy(dashboard_client, tmp_path):
    _login_admin(dashboard_client)
    env_path = tmp_path / ".env"
    env_path.write_text("NEXUSAI_REPO_RUNTIME_TOOLCHAINS=node\n", encoding="utf-8")

    with patch("dashboard.settings.platform.system", return_value="Linux"), \
         patch("dashboard.settings._env_file_path", return_value=env_path):
        resp = dashboard_client.post("/api/settings/tools/install/code_exec_dotnet")
        status_resp = dashboard_client.get("/api/settings/tools/install/code_exec_dotnet/status")

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["state"] == "configured"
    assert data["deploy_required"] is True
    assert "dotnet" in data["configured_toolchains"]
    assert "node,dotnet" in env_path.read_text(encoding="utf-8")

    assert status_resp.status_code == 200
    status_data = status_resp.get_json()
    assert status_data["state"] == "configured"


def test_settings_browser_runtime_install_configures_playwright_and_requires_deploy(dashboard_client, tmp_path):
    _login_admin(dashboard_client)
    env_path = tmp_path / ".env"
    env_path.write_text("NEXUSAI_REPO_RUNTIME_TOOLCHAINS=dotnet\n", encoding="utf-8")

    with patch("dashboard.settings.platform.system", return_value="Linux"), \
         patch("dashboard.settings._env_file_path", return_value=env_path):
        resp = dashboard_client.post("/api/settings/tools/install/ui_browser")
        status_resp = dashboard_client.get("/api/settings/tools/install/ui_browser/status")

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["state"] == "configured"
    assert data["deploy_required"] is True
    assert "node" in data["configured_toolchains"]
    assert "playwright" in data["configured_toolchains"]
    env_text = env_path.read_text(encoding="utf-8")
    assert "dotnet,node,playwright" in env_text

    assert status_resp.status_code == 200
    status_data = status_resp.get_json()
    assert status_data["state"] == "configured"


def test_settings_tool_status_uses_user_profile_runtime_paths(dashboard_client):
    _login_admin(dashboard_client)
    dashboard_client.put("/api/settings/tools", json={"enabled_tools": ["code_exec_dotnet"]})

    captured = {}

    def _fake_run(command, capture_output=None, text=None, shell=None, timeout=None, check=None, env=None):
        captured["path"] = env.get("PATH", "")

        class Result:
            def __init__(self):
                self.returncode = 0
                self.stdout = "8.0.203\n"
                self.stderr = ""

        return Result()

    with patch("dashboard.settings.platform.system", return_value="Windows"), \
         patch("dashboard.settings.subprocess.run", side_effect=_fake_run):
        resp = dashboard_client.post("/api/settings/tools/test", json={"tool_id": "code_exec_dotnet"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["statuses"][0]["status"] == "installed"
    assert ".dotnet" in captured["path"]


def test_settings_persistent_runtime_status_uses_docker_exec(dashboard_client):
    _login_admin(dashboard_client)
    dashboard_client.put("/api/settings/tools", json={"enabled_tools": ["code_exec_dotnet"]})

    calls = []

    def _fake_run(command, cwd=None, capture_output=None, text=None, timeout=None, check=None, shell=None, env=None):
        calls.append(command)

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if isinstance(command, list) and command[:3] == ["docker", "ps", "-q"]:
            return Result(stdout="abc123\n")
        if isinstance(command, list) and command[:3] == ["docker", "exec", "abc123"]:
            return Result(stdout="8.0.419\n")
        raise AssertionError(f"Unexpected command: {command!r}")

    with patch("dashboard.settings.platform.system", return_value="Linux"), \
         patch("dashboard.settings._configured_runtime_toolchains", return_value=["dotnet"]), \
         patch("dashboard.settings.subprocess.run", side_effect=_fake_run):
        resp = dashboard_client.post("/api/settings/tools/test", json={"tool_id": "code_exec_dotnet"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["statuses"][0]["status"] == "installed"
    assert any(
        isinstance(command, list) and command[:3] == ["docker", "ps", "-q"]
        for command in calls
    )
    assert any(
        isinstance(command, list) and command[:3] == ["docker", "exec", "abc123"]
        for command in calls
    )


def test_bots_page_supports_multi_file_import(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/bots")
    assert resp.status_code == 200
    assert b'Import Bot(s)' in resp.data
    assert b'id="bot-import-file"' in resp.data
    assert b'multiple' in resp.data


def test_bots_page_identifies_scheduled_and_manual_dispatch_modes(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return [
                {"id": "scheduled-bot", "name": "Scheduled Bot", "role": "monitor", "enabled": True, "backends": []},
                {"id": "manual-bot", "name": "Manual Bot", "role": "researcher", "enabled": True, "backends": []},
                {"id": "paused-bot", "name": "Paused Bot", "role": "reviewer", "enabled": True, "backends": []},
                {"id": "disabled-bot", "name": "Disabled Bot", "role": "writer", "enabled": False, "backends": []},
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {"bot_id": "scheduled-bot", "ready": True, "checks": []},
                    {"bot_id": "manual-bot", "ready": True, "checks": []},
                    {"bot_id": "paused-bot", "ready": True, "checks": []},
                ]
            }

        def list_schedules(self, **kwargs):
            return {
                "schedules": [
                    {"id": "schedule-1", "status": "active", "target_bot_id": "scheduled-bot"},
                    {"id": "schedule-2", "status": "paused", "assignment_pm_bot_id": "paused-bot"},
                ]
            }

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/bots")

    assert resp.status_code == 200
    assert b"Dispatch mode" in resp.data
    assert b"scheduled" in resp.data
    assert b"manual" in resp.data
    assert b"paused" in resp.data
    assert b"disabled" in resp.data
    assert b"1 active / 0 paused schedule(s)" in resp.data
    assert b"0 active / 1 paused schedule(s)" in resp.data


def test_bots_page_surfaces_bot_scoped_chat_profiles(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return [
                {
                    "id": "chat-only",
                    "name": "Chat Only",
                    "role": "assistant",
                    "enabled": True,
                    "backends": [],
                    "routing_rules": {"operator_profile": {"autonomy": "manual_chat_only"}},
                    "execution_policy": {"repo_output_mode": "deny"},
                },
                {
                    "id": "repo-coder",
                    "name": "Repo Coder",
                    "role": "coder",
                    "enabled": True,
                    "backends": [],
                    "routing_rules": {
                        "chat_tool_access": {
                            "enabled": True,
                            "filesystem": True,
                            "repo_search": True,
                        },
                        "operator_profile": {"autonomy": "manual_chat_only"},
                    },
                    "execution_policy": {
                        "repo_output_mode": "allow",
                        "inline_coding_default": True,
                    },
                },
                {
                    "id": "scheduled-worker",
                    "name": "Scheduled Worker",
                    "role": "worker",
                    "enabled": True,
                    "backends": [],
                    "routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}},
                    "execution_policy": {"repo_output_mode": "deny"},
                },
                {
                    "id": "project-manager",
                    "name": "Project Manager",
                    "role": "project-manager",
                    "enabled": True,
                    "backends": [],
                    "assignment_capabilities": {"is_project_manager": True},
                },
            ]

        def list_bot_readiness(self):
            return {"readiness": []}

        def list_schedules(self, **kwargs):
            return {"schedules": []}

        def list_workers(self):
            return []

        def list_models(self):
            return []

        def list_keys(self):
            return []

        def list_projects(self):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/bots")

    assert resp.status_code == 200
    assert b"Chat profile" in resp.data
    assert b"Chat Only" in resp.data
    assert b"Coding" in resp.data
    assert b"attachments" in resp.data
    assert b"repo_search" in resp.data
    assert b"filesystem" in resp.data
    assert b"repo_output" in resp.data
    assert b"Use: Manual chat" in resp.data
    assert b"Use: Tool-enabled chat" in resp.data
    assert b"Use: Scheduled worker" in resp.data
    assert b"Use: Project manager" in resp.data
    assert b"Autonomy: manual_chat_only" in resp.data
    assert b"Chat tools: off" in resp.data
    assert b"Chat tools: filesystem, repo_search" in resp.data
    assert b"Repo output: allow" in resp.data


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


def test_worker_probe_endpoint_proxies_control_plane_result(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def probe_worker(self, worker_id):
            return {"worker_id": worker_id, "probe_status": "ready", "checks": []}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        response = dashboard_client.post("/api/workers/worker-1/probe")

    assert response.status_code == 200
    assert response.get_json()["probe_status"] == "ready"


def test_worker_creation_provisions_through_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.provisioned = None

        def list_workers(self):
            return []

        def provision_worker(self, body):
            self.provisioned = body
            return {**body, "status": "offline", "last_heartbeat_at": None}

    fake_cp = FakeCP()
    with patch("dashboard.cp_client.get_cp_client", return_value=fake_cp):
        response = dashboard_client.post(
            "/api/workers",
            json={"name": "New Worker", "host": "worker.internal", "port": 8011},
        )

    assert response.status_code == 201
    assert response.get_json()["id"] == "new-worker"
    assert response.get_json()["status"] == "offline"
    assert fake_cp.provisioned["id"] == "new-worker"
    assert fake_cp.provisioned["capabilities"] == []


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


def test_bots_page_and_proxy_support_specialist_creation(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return []

        def list_workers(self):
            return [{"id": "worker-1", "name": "Worker One"}]

        def list_models(self):
            return [{"name": "qwen3.5:cloud", "provider": "ollama_cloud"}]

        def list_keys(self):
            return [{"name": "ollama-cloud", "provider": "ollama_cloud"}]

        def list_projects(self):
            return [{"id": "project-1", "name": "Project One"}]

        def list_bot_blueprints(self):
            return {"blueprints": [{"kind": "researcher", "label": "Researcher"}]}

        def preview_bot_blueprint(self, body):
            return {"bot": {"id": "researcher", "name": body["name"]}}

        def preflight_bot_blueprint(self, body):
            return {
                "bot_id": body["id"],
                "ready_to_enable": True,
                "readiness": {"ready": True, "checks": []},
            }

        def create_bot_blueprint(self, body):
            return {"bot": {"id": "researcher", "name": body["name"]}}

    fake_cp = FakeCP()
    with patch("dashboard.cp_client.get_cp_client", return_value=fake_cp):
        page = dashboard_client.get("/bots")
        catalog = dashboard_client.get("/api/bot-blueprints")
        preview = dashboard_client.post(
            "/api/bot-blueprints/preview",
            json={"name": "Researcher"},
        )
        preflight = dashboard_client.post(
            "/api/bot-blueprints/preflight",
            json={"name": "Researcher"},
        )
        created = dashboard_client.post(
            "/api/bot-blueprints/create",
            json={"name": "Researcher"},
        )

    assert page.status_code == 200
    assert b"Create Specialist" in page.data
    assert b"specialist-model-options" in page.data
    assert catalog.get_json()["blueprints"][0]["kind"] == "researcher"
    assert preview.get_json()["bot"]["id"] == "researcher"
    assert preflight.get_json()["preflight"]["ready_to_enable"] is True
    assert created.status_code == 201


def test_specialist_create_returns_actionable_readiness_blocker(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def create_bot_blueprint(self, body):
            return None

        def last_error(self):
            return {
                "status_code": 409,
                "detail": json.dumps(
                    {
                        "detail": {
                            "reason_code": "bot_not_ready",
                            "message": "Bot cannot be enabled until its dispatch checks pass.",
                            "readiness": {"ready": False},
                        }
                    }
                ),
            }

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        response = dashboard_client.post("/api/bot-blueprints/create", json={"name": "Blocked"})

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "Bot cannot be enabled until its dispatch checks pass.",
        "reason_code": "bot_not_ready",
        "readiness": {"ready": False},
    }


def test_schedules_page_and_proxy_support_operational_schedule_management(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_schedules(self, **kwargs):
            return {
                "schedules": [
                    {
                        "id": "schedule-1",
                        "name": "Daily Review",
                        "status": "paused",
                        "cron_expression": "0 8 * * *",
                        "timezone": "UTC",
                        "target_bot_id": "reviewer",
                        "assignment_pm_bot_id": None,
                        "next_run_at": "2026-07-19T08:00:00+00:00",
                        "last_run_at": None,
                        "last_run_status": None,
                        "metadata": {"mutation_safe": True},
                    },
                    {
                        "id": "schedule-unsafe",
                        "name": "Unsafe Active",
                        "status": "active",
                        "cron_expression": "*/15 * * * *",
                        "timezone": "UTC",
                        "target_bot_id": "reviewer",
                        "assignment_pm_bot_id": None,
                        "next_run_at": "2026-07-19T08:15:00+00:00",
                        "last_run_at": "2026-07-19T08:00:00+00:00",
                        "last_run_status": "failed",
                        "metadata": {},
                    }
                ]
            }

        def list_bots(self):
            return [{"id": "reviewer", "name": "Reviewer"}]

        def list_projects(self):
            return []

        def list_schedule_queue_sources(self):
            return {
                "sources": [
                    {
                        "relative_path": "queues/draft-work.csv",
                        "headers": ["lesson_id", "instruction"],
                        "row_count": 1,
                        "available": True,
                        "issue": None,
                    }
                ]
            }

        def create_schedule(self, body):
            return {"schedule": {"id": "schedule-2", **body}}

        def update_schedule(self, schedule_id, body):
            return {"schedule": {"id": schedule_id, **body}}

        def trigger_schedule(self, schedule_id):
            return {"run": {"id": "run-1", "schedule_id": schedule_id}}

        def preview_schedule(self, schedule_id):
            return {"schedule": {"id": schedule_id}, "task_payload": {"revision_items": "preview"}}

        def list_schedule_runs(self, schedule_id, limit=50):
            return {"schedule_id": schedule_id, "runs": []}

    fake_cp = FakeCP()
    with patch("dashboard.routes.schedules.get_cp_client", return_value=fake_cp):
        page = dashboard_client.get("/schedules")
        listed = dashboard_client.get("/api/schedules")
        queue_sources = dashboard_client.get("/api/schedules/queue-sources")
        created = dashboard_client.post(
            "/api/schedules",
            json={"name": "Daily Review", "target_bot_id": "reviewer", "cron_expression": "0 8 * * *", "prompt": "Review"},
        )
        toggled = dashboard_client.patch("/api/schedules/schedule-1", json={"status": "active"})
        triggered = dashboard_client.post("/api/schedules/schedule-1/trigger")
        preview = dashboard_client.post("/api/schedules/schedule-1/preview")
        runs = dashboard_client.get("/api/schedules/schedule-1/runs")

    assert page.status_code == 200
    assert b"Create Schedule" in page.data
    assert b"Control-plane fleet health summary" in page.data
    assert b"Aggregate operational quality snapshot" in page.data
    assert b"csv_work_items_v1" in page.data
    assert b"schedule-csv-source" in page.data
    assert b"schedule-csv-payload-map" in page.data
    assert b"Daily Review" in page.data
    assert b"Unsafe Active" in page.data
    assert b"Active Unattested" in page.data
    assert b"Recent Failures" in page.data
    assert b"Origin" in page.data
    assert b"Retry After" in page.data
    assert b"retry_not_before" in page.data
    assert b"run.manual === true" in page.data
    assert b"schedule-retry-max" in page.data
    assert b"schedule-retry-backoff" in page.data
    assert listed.get_json()["schedules"][0]["id"] == "schedule-1"
    assert queue_sources.get_json()["sources"][0]["relative_path"] == "queues/draft-work.csv"
    assert created.status_code == 201
    assert toggled.get_json()["schedule"]["status"] == "active"
    assert triggered.get_json()["run"]["schedule_id"] == "schedule-1"
    assert preview.get_json()["task_payload"]["revision_items"] == "preview"
    assert runs.get_json()["runs"] == []


def test_work_page_surfaces_provider_model_usage(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_tasks(self, **kwargs):
            return [
                {
                    "id": "task-1",
                    "bot_id": "research-bot",
                    "status": "completed",
                    "metadata": {"project_id": "globeiq", "manager_id": "research-manager"},
                    "updated_at": "2026-03-12T00:00:00+00:00",
                }
            ]

        def list_projects(self):
            return [{"id": "globeiq", "name": "GlobeIQ", "enabled": True}]

        def list_bots(self):
            return [{"id": "research-bot", "name": "Research Bot", "enabled": True}]

        def list_workers(self):
            return []

        def list_work_dispatch_holds(self):
            return {"holds": []}

        def task_usage(self, **kwargs):
            return {
                "window": {"hours": 24},
                "totals": {
                    "total_tokens": 12345,
                    "tasks_with_usage": 1,
                    "tasks_without_usage": 0,
                },
                "by_project": [{"project_id": "globeiq", "total_tokens": 12345, "tasks_with_usage": 1, "tasks_without_usage": 0}],
                "by_manager": [{"project_id": "globeiq", "manager_id": "research-manager", "total_tokens": 12345, "tasks_with_usage": 1}],
                "by_bot": [{"bot_id": "research-bot", "total_tokens": 12345, "tasks_with_usage": 1, "tasks_without_usage": 0}],
                "by_provider_model": [
                    {
                        "provider": "ollama_cloud",
                        "model": "gpt-oss:120b",
                        "total_tokens": 12345,
                        "tasks_with_usage": 1,
                        "tasks_without_usage": 0,
                    }
                ],
                "token_governor": {"limits": {}},
            }

        def list_platform_ai_quality_suites_global(self, **kwargs):
            return {
                "suites": [
                    {
                        "id": "suite-1",
                        "name": "Research Bot Quality",
                        "pipeline_bot_id": "research-bot",
                        "suite": {"tests": [{"id": "checks-output"}, {"id": "checks-evidence"}]},
                    }
                ]
            }

        def list_platform_ai_quality_suite_runs(self, suite_id, **kwargs):
            assert suite_id == "suite-1"
            return {
                "runs": [
                    {
                        "id": "run-1",
                        "status": "passed",
                        "score": 0.92,
                        "completed_at": "2026-03-12T00:10:00+00:00",
                    }
                ]
            }

    with patch("dashboard.routes.work.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/work")

    assert resp.status_code == 200
    assert b"Usage By Project And Manager" in resp.data
    assert b"ollama_cloud" in resp.data
    assert b"gpt-oss:120b" in resp.data
    assert b"12,345" in resp.data
    assert b"Quality Gates" in resp.data
    assert b"Research Bot Quality" in resp.data
    assert b"research-bot" in resp.data
    assert b"0.92" in resp.data
    assert b"Recommended Action" in resp.data
    assert b"continue monitoring" in resp.data


def test_platform_ai_session_stream_error_payload_is_displayed(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def get_platform_ai_session(self, session_id):
            return {
                "id": session_id,
                "mode": "bot_creator",
                "status": "running",
                "archived": False,
                "metadata": {},
                "created_at": "2026-03-12T00:00:00+00:00",
                "updated_at": "2026-03-12T00:00:00+00:00",
            }

        def list_platform_ai_messages(self, session_id, limit=400):
            return {"messages": []}

        def list_platform_ai_events(self, session_id, limit=600):
            return {"events": []}

        def list_platform_ai_proposals(self, session_id, limit=100):
            return {"proposals": []}

        def list_projects(self):
            return []

        def list_bots(self):
            return []

        def list_workers(self):
            return []

    with patch("dashboard.routes.platform_ai.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/platform-ai/sessions/session-1")

    assert resp.status_code == 200
    assert b"source.addEventListener('error', (event)" in resp.data
    assert b"parsed.error || rawError" in resp.data
    assert b"operator-chat-status" in resp.data


def test_platform_ai_session_uses_server_upload_limits(dashboard_client, monkeypatch):
    _login_admin(dashboard_client)
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILES", "7")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_TOTAL_BYTES", "123456")

    class FakeCP:
        def get_platform_ai_session(self, session_id):
            return {
                "id": session_id,
                "mode": "bot_creator",
                "status": "running",
                "archived": False,
                "metadata": {},
                "created_at": "2026-03-12T00:00:00+00:00",
                "updated_at": "2026-03-12T00:00:00+00:00",
            }

        def list_platform_ai_messages(self, session_id, limit=400):
            return {"messages": []}

        def list_platform_ai_events(self, session_id, limit=600):
            return {"events": []}

        def list_platform_ai_proposals(self, session_id, limit=100):
            return {"proposals": []}

        def list_projects(self):
            return []

        def list_bots(self):
            return []

        def list_workers(self):
            return []

    with patch("dashboard.routes.platform_ai.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/platform-ai/sessions/session-1")

    assert resp.status_code == 200
    assert b'"max_files": 7' in resp.data
    assert b'"max_total_bytes": 123456' in resp.data
    assert b"Session upload limit: 7 files, 123456 bytes total" in resp.data
    assert b"SESSION_ATTACHMENT_MAX_FILES = Number(sessionUploadLimits?.max_files || 15)" in resp.data
    assert b"SESSION_ATTACHMENT_MAX_TOTAL_BYTES = Number(sessionUploadLimits?.max_total_bytes || 1024 * 1024 * 1024)" in resp.data
    assert b"SESSION_MESSAGE_CONTENT_MAX_CHARS = 120000" in resp.data
    assert b"function sessionMessageContentBlocker" in resp.data


def test_platform_ai_message_api_blocks_oversized_content(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def post_platform_ai_message(self, session_id, body):
            raise AssertionError("oversized platform ai message should not reach control plane")

    with patch("dashboard.routes.platform_ai.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.post(
            "/api/platform-ai/sessions/session-1/messages",
            json={"role": "operator", "content": "x" * 120001},
        )

    assert resp.status_code == 400
    assert b"message content is limited to 120000 characters" in resp.data


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
    assert b"Fleet Readiness" in resp.data
    assert b"Latest Fleet Health Analysis" in resp.data
    assert b"Worker runtime attention" in resp.data
    assert b"Active schedules" in resp.data
    assert b"Recent Activity" in resp.data
    assert b"Worker Health" in resp.data
    assert b"Quick Links" in resp.data
    assert b"Workflow Launch" in resp.data


def test_overview_reports_worker_capability_and_schedule_attention(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def health(self):
            return True

        def list_workers(self):
            return [
                {
                    "id": "browser-worker",
                    "name": "Browser Worker",
                    "status": "online",
                    "enabled": True,
                    "metrics": {"load": 0, "queue_depth": 0, "gpu_utilization": []},
                }
            ]

        def list_worker_probes(self):
            return {
                "probes": [
                    {
                        "worker_id": "browser-worker",
                        "probe_status": "ready",
                        "capability_attestation": {
                            "browser": {
                                "configured": True,
                                "ready": False,
                                "reason": "browser_session_check_failed",
                            }
                        },
                    }
                ]
            }

        def get_fleet_summary(self):
            return {
                "workers": {
                    "runtime_attention": [
                        {
                            "worker_id": "browser-worker",
                            "reason_codes": ["browser_session_unavailable"],
                        }
                    ]
                }
            }

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_tasks(self, **kwargs):
            return []

        def list_schedules(self, **kwargs):
            return {
                "schedules": [
                    {
                        "id": "schedule-1",
                        "status": "active",
                        "last_run_status": "failed",
                    }
                ]
            }

        def probe_paths(self, paths):
            return [{"path": path, "ok": True, "status_code": 200, "detail": "ok"} for path in paths]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/")

    assert resp.status_code == 200
    assert b"1 online worker(s) need capability attention" in resp.data
    assert b"runtime degraded" in resp.data
    assert b"browser_session_check_failed" in resp.data
    assert b"browser-worker</code>: browser_session_unavailable" in resp.data
    assert b"1 most-recent run(s) failed" in resp.data


def test_overview_shows_latest_bounded_fleet_health_report(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def health(self):
            return True

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_tasks(self, **kwargs):
            return []

        def list_schedules(self, **kwargs):
            return {
                "schedules": [
                    {
                        "id": "fleet-health",
                        "status": "active",
                        "last_run_status": "completed",
                        "metadata": {
                            "system_payload_source": {"type": "control_plane_fleet_summary_v1"}
                        },
                    }
                ]
            }

        def list_schedule_runs(self, schedule_id, limit=50):
            assert schedule_id == "fleet-health"
            assert limit == 1
            return {
                "runs": [
                    {
                        "status": "completed",
                        "finished_at": "2026-07-18T22:30:00+00:00",
                        "task_id": "fleet-task",
                    }
                ]
            }

        def get_task(self, task_id):
            assert task_id == "fleet-task"
            return {
                "id": task_id,
                "payload": {
                    "monitoring_events": json.dumps(
                        {
                            "tasks": {
                                "recent_failed_by_category": {
                                    "authentication": 2,
                                    "secret": 99,
                                }
                            }
                        }
                    )
                },
                "result": {
                    "status": "warning",
                    "severity": "warning",
                    "health_summary": "One worker requires runtime attention.",
                    "recommended_next_step": "Review the worker capability evidence.",
                },
            }

        def probe_paths(self, paths):
            return [{"path": path, "ok": True, "status_code": 200, "detail": "ok"} for path in paths]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/")

    assert resp.status_code == 200
    assert b"Latest Fleet Health Analysis" in resp.data
    assert b"One worker requires runtime attention." in resp.data
    assert b"Review the worker capability evidence." in resp.data
    assert b"Failure signals:" in resp.data
    assert b"authentication: 2" in resp.data
    assert b"secret: 99" not in resp.data


def test_overview_shows_bounded_operational_qc_report(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def health(self):
            return True

        def list_workers(self):
            return []

        def list_worker_probes(self):
            return {"probes": []}

        def list_bots(self):
            return []

        def list_projects(self):
            return []

        def list_tasks(self, **kwargs):
            return []

        def list_schedules(self, **kwargs):
            return {
                "schedules": [
                    {
                        "id": "operational-qc",
                        "status": "active",
                        "last_run_status": "completed",
                        "metadata": {
                            "system_payload_source": {
                                "type": "control_plane_operational_quality_v1"
                            }
                        },
                    }
                ]
            }

        def list_schedule_runs(self, schedule_id, limit=50):
            assert schedule_id == "operational-qc"
            assert limit == 1
            return {
                "runs": [
                    {
                        "status": "completed",
                        "finished_at": "2026-07-19T11:17:15+00:00",
                        "task_id": "qc-task",
                    }
                ]
            }

        def get_task(self, task_id):
            assert task_id == "qc-task"
            return {
                "id": task_id,
                "result": {
                    "status": "pass",
                    "acceptance_result": "pass",
                    "findings": ["No concrete operational risks."],
                    "evidence": ["aggregate-only"],
                    "recommended_next_step": "Continue routine monitoring.",
                    "handoff_notes": "This must not be rendered.",
                },
            }

        def probe_paths(self, paths):
            return [{"path": path, "ok": True, "status_code": 200, "detail": "ok"} for path in paths]

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        resp = dashboard_client.get("/")

    assert resp.status_code == 200
    assert b"Latest Operational QC" in resp.data
    assert b"Acceptance:</strong> pass" in resp.data
    assert b"Reported findings:</strong> 1" in resp.data
    assert b"Continue routine monitoring." in resp.data
    assert b"This must not be rendered." not in resp.data


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
                        "project_id": "proj-1",
                        "conversation_id": "conv-1",
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
                        "project_id": "proj-1",
                        "conversation_id": "conv-1",
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
    assert b"View DAG" in detail_resp.data
    assert b"Full Recap" in detail_resp.data
    assert b"Review Files" in detail_resp.data
    assert b"Download DAG JSON" in detail_resp.data
    assert b"Download Review JSON" in detail_resp.data
    assert b"Download Recap TXT" in detail_resp.data
    assert b"proj-1" in detail_resp.data
    assert b"Artifacts and Reports" in detail_resp.data
    assert b"Execution Report" in detail_resp.data
    assert b"Cancel Pipeline" in detail_resp.data


def test_pipeline_cancel_proxy_uses_control_plane(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self.request = None

        def cancel_orchestration(self, orchestration_id, reason=None):
            self.request = {"orchestration_id": orchestration_id, "reason": reason}
            return {"orchestration_id": orchestration_id, "cancelled_task_count": 2}

    fake_cp = FakeCP()
    with patch("dashboard.routes.pipelines.get_cp_client", return_value=fake_cp):
        response = dashboard_client.post(
            "/api/pipelines/orch-cancel/cancel",
            json={"reason": "operator_test"},
        )

    assert response.status_code == 200
    assert response.get_json()["cancelled_task_count"] == 2
    assert fake_cp.request == {"orchestration_id": "orch-cancel", "reason": "operator_test"}


def test_pipeline_status_reports_cancelled_when_prior_steps_completed():
    from dashboard.routes.pipelines import _pipeline_status

    assert _pipeline_status(
        [
            {"status": "completed"},
            {"status": "cancelled"},
        ]
    ) == "cancelled"
