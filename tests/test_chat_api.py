"""Integration tests for chat API routes."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_create_conversation_and_post_message(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat 1"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        # Register a bot so chat request can target it.
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-chat",
                "name": "Chat Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-chat"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["user_message"]["content"] == "hello"
        assert data["assistant_message"]["content"] == "assistant reply"


@pytest.mark.anyio
async def test_create_bridged_conversation_stores_bridge_projects(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Bridge Chat",
                "scope": "bridged",
                "project_id": "proj-a",
                "bridge_project_ids": ["proj-b", "proj-c", "proj-a"],
            },
        )
        assert create_resp.status_code == 200
        data = create_resp.json()
        assert data["project_id"] == "proj-a"
        assert data["bridge_project_ids"] == ["proj-b", "proj-c"]


@pytest.mark.anyio
async def test_delete_conversation_removes_messages(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "assistant reply"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Delete Me"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-delete",
                "name": "Delete Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )
        await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-delete"},
        )

        not_archived_delete = await client.delete(f"/v1/chat/conversations/{conversation_id}")
        assert not_archived_delete.status_code == 400

        archive_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/archive")
        assert archive_resp.status_code == 200
        assert archive_resp.json()["archived_at"] is not None

        delete_resp = await client.delete(f"/v1/chat/conversations/{conversation_id}")
        assert delete_resp.status_code == 204

        missing_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        assert missing_resp.status_code == 404


@pytest.mark.anyio
async def test_archive_and_restore_conversation_visibility(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Archive Me"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        active_resp = await client.get("/v1/chat/conversations")
        assert active_resp.status_code == 200
        assert len(active_resp.json()) == 1

        archive_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/archive")
        assert archive_resp.status_code == 200

        active_after_archive = await client.get("/v1/chat/conversations")
        assert active_after_archive.status_code == 200
        assert active_after_archive.json() == []

        archived_resp = await client.get("/v1/chat/conversations?archived=archived")
        assert archived_resp.status_code == 200
        assert len(archived_resp.json()) == 1

        restore_resp = await client.post(f"/v1/chat/conversations/{conversation_id}/restore")
        assert restore_resp.status_code == 200
        assert restore_resp.json()["archived_at"] is None

        active_after_restore = await client.get("/v1/chat/conversations")
        assert active_after_restore.status_code == 200
        assert len(active_after_restore.json()) == 1


@pytest.mark.anyio
async def test_stream_message_endpoint(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "stream "}
        yield {"event": "token", "text": "reply"}
        yield {"event": "final", "output": "stream reply", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Stream"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-stream",
                "name": "Stream Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-stream"},
        )
        assert stream_resp.status_code == 200
        assert "event: user_message" in stream_resp.text
        assert "event: status" in stream_resp.text
        assert "event: token" in stream_resp.text
        assert "event: assistant_message" in stream_resp.text
        assert "event: done" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        messages = messages_resp.json()
        assert len(messages) == 2
        assert messages[-1]["content"] == "stream reply"
        assert messages[-1]["model"] == "llama3.1:8b"
        assert messages[-1]["provider"] == "ollama"
        assert messages[-1]["metadata"]["streaming"] is False


@pytest.mark.anyio
async def test_stream_message_persists_partial_when_final_missing(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "partial "}
        yield {"event": "token", "text": "reply"}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Chat Partial"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-partial",
                "name": "Partial Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
            },
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "hello", "bot_id": "bot-partial"},
        )
        assert stream_resp.status_code == 200
        assert "event: assistant_message" in stream_resp.text

        messages_resp = await client.get(f"/v1/chat/conversations/{conversation_id}/messages")
        messages = messages_resp.json()
        assert len(messages) == 2
        assert messages[-1]["content"] == "partial reply"
        assert messages[-1]["model"] == "llama3.1:8b"
        assert messages[-1]["provider"] == "ollama"
        assert messages[-1]["metadata"]["partial"] is True
        assert messages[-1]["metadata"]["streaming"] is False


@pytest.mark.anyio
async def test_assign_message_creates_task_graph_and_summary(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Chat"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={"id": "bot-pm", "name": "PM Bot", "role": "pm", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-code", "name": "Code Bot", "role": "coder", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-test", "name": "Test Bot", "role": "tester", "backends": [], "enabled": True},
        )

        post_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "@assign Build authentication API and tests", "bot_id": "bot-pm"},
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["mode"] == "assign"
        assert len(data["assignment"]["tasks"]) >= 2
        assert "Assignment summary" in data["assistant_message"]["content"]

        tasks_resp = await client.get("/v1/tasks")
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        assert len(tasks) >= 2
        first_payload = tasks[0].get("payload") if isinstance(tasks[0], dict) else {}
        assert isinstance(first_payload, dict)
        assert "acceptance_criteria" in first_payload
        assert "quality_gates" in first_payload


@pytest.mark.anyio
async def test_stream_assign_emits_task_events(cp_app):
    async def _schedule(task):
        import asyncio

        if str(task.id).startswith("pm-plan-"):
            return {
                "output": (
                    '{"steps":['
                    '{"id":"step_1","title":"Design","instruction":"Design API","role_hint":"coder","depends_on":[]},'
                    '{"id":"step_2","title":"Implement","instruction":"Implement API","role_hint":"coder","depends_on":["step_1"]}'
                    "]} "
                )
            }
        await asyncio.sleep(0.05)
        return {"output": f"done:{task.id}"}

    cp_app.state.scheduler.schedule = AsyncMock(side_effect=_schedule)
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Assign Stream"})
        conversation_id = create_resp.json()["id"]

        await client.post(
            "/v1/bots",
            json={"id": "bot-pm", "name": "PM Bot", "role": "pm", "backends": [], "enabled": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "bot-code", "name": "Code Bot", "role": "coder", "backends": [], "enabled": True},
        )

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={"content": "@assign Build API", "bot_id": "bot-pm"},
        )
        assert stream_resp.status_code == 200
        text = stream_resp.text
        assert "event: task_graph" in text
        assert "event: task_status" in text
        assert "event: assistant_message" in text
        assert "event: done" in text


@pytest.mark.anyio
async def test_chat_context_item_ids_are_resolved_from_vault(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post("/v1/chat/conversations", json={"title": "Context IDs"})
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-context", "name": "Ctx Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        vault_item = await client.post(
            "/v1/vault/items",
            json={"title": "Doc", "content": "Secret architecture note", "namespace": "global"},
        )
        item_id = vault_item.json()["id"]

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Use context",
                "bot_id": "bot-context",
                "context_item_ids": [item_id],
            },
        )
        assert resp.status_code == 200
        # Ensure scheduler received a context system message.
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "Context:\n" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_project_repo_context_is_attached_when_requested(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-ctx"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Context Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Project Repo Context",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-ctx",
                "name": "Repo Ctx Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_CONTEXT_TOKEN architecture note for chat retrieval.",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "How is this repo structured?",
                "bot_id": "bot-repo-ctx",
                "include_project_context": True,
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[repo:proj-repo-ctx]" in payload[0]["content"]
        assert "PROJECT_REPO_CONTEXT_TOKEN" in payload[0]["content"]
        assert any(
            m.get("role") == "system" and "Repository Evidence Policy:" in str(m.get("content", ""))
            for m in payload
        )
        assert any("Files inspected" in str(m.get("content", "")) for m in payload if m.get("role") == "system")


@pytest.mark.anyio
async def test_chat_project_repo_context_is_not_attached_by_default(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-off"
        create_project = await client.post(
            "/v1/projects",
            json={"id": project_id, "name": "Repo Off Project"},
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={"title": "Project Repo Off", "project_id": project_id},
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-repo-off", "name": "Repo Off Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_CONTEXT_DISABLED_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Hello",
                "bot_id": "bot-repo-off",
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "user"
        assert "PROJECT_REPO_CONTEXT_DISABLED_TOKEN" not in str(payload)


@pytest.mark.anyio
async def test_chat_repo_intent_auto_attaches_project_context(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-auto"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Auto Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Auto Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-auto",
                "name": "Repo Auto Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "PROJECT_REPO_AUTO_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search the repository and explain auth hardening gaps.",
                "bot_id": "bot-repo-auto",
            },
        )
        assert resp.status_code == 200
        assert cp_app.state.scheduler.schedule.await_count == 1
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[repo:proj-repo-auto]" in payload[0]["content"]
        assert "PROJECT_REPO_AUTO_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_repo_context_search_uses_focused_query_terms(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    cp_app.state.vault_manager.search = AsyncMock(
        return_value=[
            {
                "chunk_id": "row-lesson-1",
                "title": "GlobeIQ.Server/Services/LessonBuilderService.cs",
                "content": "FOCUSED_LESSON_CONTEXT_TOKEN",
                "score": 0.72,
            }
        ]
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-focus-query"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Focus Query Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Focus Query Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-focus-query",
                "name": "Repo Focus Query Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": (
                    "Testing repo awareness and proper file searching. Can you look through everything "
                    "related to my lesson builder system and lesson blocks and tell me what is done?"
                ),
                "bot_id": "bot-repo-focus-query",
            },
        )
        assert resp.status_code == 200
        assert cp_app.state.vault_manager.search.await_count >= 1
        search_query = str(cp_app.state.vault_manager.search.await_args_list[0].kwargs.get("query") or "")
        assert "lesson" in search_query
        assert "builder" in search_query
        assert "blocks" in search_query
        assert "awareness" not in search_query
        assert "proper" not in search_query

        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert "FOCUSED_LESSON_CONTEXT_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_repo_intent_prefers_workspace_as_source_of_truth(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace-repo-truth"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "backend" / "auth").mkdir(parents=True, exist_ok=True)
    (workspace_root / "backend" / "auth" / "login.ts").write_text(
        "WORKSPACE_AUTH_TOKEN current login implementation",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-truth"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Truth Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": True,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Truth Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-truth",
                "name": "Repo Truth Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": True,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "docs/legacy-auth.md",
                "content": "INGESTED_AUTH_TOKEN historical note",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search the repository auth implementation and hardening opportunities",
                "bot_id": "bot-repo-truth",
            },
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        context_blob = payload[0]["content"]
        assert "[workspace:file]" in context_blob or "[workspace:search]" in context_blob
        assert "WORKSPACE_AUTH_TOKEN" in context_blob
        assert "INGESTED_AUTH_TOKEN" in context_blob
        assert context_blob.index("WORKSPACE_AUTH_TOKEN") < context_blob.index("INGESTED_AUTH_TOKEN")
        policy_blob = payload[1]["content"] if len(payload) > 1 else ""
        assert "Treat workspace snippets as source of truth" in policy_blob


@pytest.mark.anyio
async def test_chat_workspace_filesystem_context_requires_three_switches(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "WORKSPACE_FILESYSTEM_TOKEN architecture details",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-files"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Files",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Files Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-files",
                "name": "Workspace Files Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Please inspect README.md",
                "bot_id": "bot-workspace-files",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200

        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        payload = task_arg.payload
        assert isinstance(payload, list)
        assert payload[0]["role"] == "system"
        assert "[workspace:file]" in payload[0]["content"] or "[workspace:search]" in payload[0]["content"]
        assert "WORKSPACE_FILESYSTEM_TOKEN" in payload[0]["content"]


@pytest.mark.anyio
async def test_chat_workspace_tools_blocked_when_chat_switch_off(cp_app, tmp_path):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    workspace_root = tmp_path / "workspace-blocked"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(
        "WORKSPACE_BLOCKED_TOKEN should not appear",
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-workspace-blocked"
        project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Workspace Blocked",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                        "workspace_root": str(workspace_root),
                    }
                },
            },
        )
        assert project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Workspace Blocked Chat",
                "project_id": project_id,
                "tool_access_enabled": False,
                "tool_access_filesystem": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        bot = await client.post(
            "/v1/bots",
            json={
                "id": "bot-workspace-blocked",
                "name": "Workspace Blocked Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "filesystem": True,
                        "repo_search": False,
                    }
                },
            },
        )
        assert bot.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Please inspect README.md",
                "bot_id": "bot-workspace-blocked",
                "use_workspace_tools": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_message"]["content"].startswith("I could not retrieve repository context")
        assert cp_app.state.scheduler.schedule.await_count == 0


@pytest.mark.anyio
async def test_stream_message_emits_context_summary_event_when_repo_context_loaded(cp_app):
    async def _stream(_task):
        yield {"event": "backend_selected", "provider": "ollama", "model": "llama3.1:8b", "worker_id": "w1"}
        yield {"event": "token", "text": "ok"}
        yield {"event": "final", "output": "ok", "usage": {}}

    cp_app.state.scheduler.stream = _stream
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-context-stream"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Context Stream",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Context Stream",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-context-stream",
                "name": "Context Stream Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README",
                "content": "STREAM_CONTEXT_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        stream_resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/stream",
            json={
                "content": "Review auth hardening",
                "bot_id": "bot-context-stream",
                "include_project_context": True,
            },
        )
        assert stream_resp.status_code == 200
        assert "event: context_summary" in stream_resp.text
        assert "Files inspected (verified context)" in stream_resp.text
        assert "STREAM_CONTEXT_TOKEN" not in stream_resp.text
        assert "event: token" not in stream_resp.text


@pytest.mark.anyio
async def test_repo_grounded_output_sanitizes_unverifiable_action_lines(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "Let me search for authentication files.\n"
                "I'll read through the actual files in your repository to give you a proper review.\n"
                "Now let me read what I found:\n"
                "Now I have the actual file contents.\n"
                "GlobeIQ.Server/Models/Lesson.cs\n"
                "GlobeIQ.Server/Controllers/LessonsController.cs\n"
                "\"BlockType\" \"LessonBlock\" \"BlockSettings\"\n"
                "**/auth*.ts\n"
                "Based on verified context, auth is configured."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-sanitize"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Sanitize Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Sanitize Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-sanitize",
                "name": "Repo Sanitize Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_SANITIZE_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-sanitize",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Source-of-truth (workspace repo)" in content
        assert "Supporting context (ingested repo/docs/history)" in content
        assert "Let me search" not in content
        assert "I'll read through the actual files" not in content
        assert "Now let me read what I found" not in content
        assert "Now I have the actual file contents" not in content
        assert "GlobeIQ.Server/Models/Lesson.cs" not in content
        assert '"BlockType" "LessonBlock" "BlockSettings"' not in content
        assert "**/auth*.ts" not in content


@pytest.mark.anyio
async def test_repo_grounded_output_adds_grounding_note_when_citations_missing(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "Authentication is configured with modern defaults."})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-required"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation",
                "name": "Repo Citation Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Authentication is configured with modern defaults." in content
        assert "I can only provide a limited grounded response for this turn" not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." in content
        assert "[S1]" in content


@pytest.mark.anyio
async def test_repo_grounded_output_keeps_cited_claims(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "Authentication middleware exists in current setup [S1]."})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-kept"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Kept Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Kept Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation-kept",
                "name": "Repo Citation Kept Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_KEPT_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation-kept",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "Authentication middleware exists in current setup [S1]." in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." not in content


@pytest.mark.anyio
async def test_repo_grounded_output_rejects_weak_front_loaded_citations(cp_app):
    long_uncited_body = " ".join(["Detailed claim without citation."] * 220)
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={"output": f"Short cited opener [S1].\n\n{long_uncited_body}"}
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-citation-weak-density"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Citation Weak Density",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Citation Weak Density Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-citation-weak-density",
                "name": "Repo Citation Weak Density Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_CITATION_WEAK_DENSITY_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-citation-weak-density",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "I can only provide a limited grounded response for this turn" not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." in content
        assert len(content) < 3200


@pytest.mark.anyio
async def test_repo_grounded_output_ignores_model_generated_files_inspected_block(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(
        return_value={
            "output": (
                "Files inspected (verified context)\n"
                "Source-of-truth (workspace repo)\n"
                "- [S1] workspace:search fake/path1.cs\n"
                "- [S2] workspace:search fake/path2.cs\n"
                "Code Review: very long uncited analysis text."
            )
        }
    )
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        project_id = "proj-repo-model-files-inspected"
        create_project = await client.post(
            "/v1/projects",
            json={
                "id": project_id,
                "name": "Repo Model Header Project",
                "settings_overrides": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        assert create_project.status_code == 200

        convo = await client.post(
            "/v1/chat/conversations",
            json={
                "title": "Repo Model Header Chat",
                "project_id": project_id,
                "tool_access_enabled": True,
                "tool_access_repo_search": True,
            },
        )
        assert convo.status_code == 200
        conversation_id = convo.json()["id"]

        await client.post(
            "/v1/bots",
            json={
                "id": "bot-repo-model-header",
                "name": "Repo Model Header Bot",
                "role": "assistant",
                "backends": [],
                "enabled": True,
                "routing_rules": {
                    "chat_tool_access": {
                        "enabled": True,
                        "repo_search": True,
                        "filesystem": False,
                    }
                },
            },
        )
        ingest = await client.post(
            "/v1/vault/items",
            json={
                "title": "README.md",
                "content": "AUTH_MODEL_HEADER_TOKEN",
                "namespace": f"project:{project_id}:repo",
                "project_id": project_id,
            },
        )
        assert ingest.status_code == 200

        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={
                "content": "Search repository authentication setup",
                "bot_id": "bot-repo-model-header",
            },
        )
        assert resp.status_code == 200
        content = resp.json()["assistant_message"]["content"]
        assert content.startswith("Files inspected (verified context)")
        assert "workspace:search fake/path1.cs" not in content
        assert "workspace:search fake/path2.cs" not in content
        assert "Code Review: very long uncited analysis text." not in content
        assert "Grounding note: inline [S#] citations were not generated; response kept concise." in content


@pytest.mark.anyio
async def test_update_conversation_tool_access_endpoint(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post("/v1/chat/conversations", json={"title": "Tool Access Conversation"})
        assert create_resp.status_code == 200
        conversation_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/chat/conversations/{conversation_id}/tool-access",
            json={"enabled": True, "filesystem": True, "repo_search": True},
        )
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["tool_access_enabled"] is True
        assert body["tool_access_filesystem"] is True
        assert body["tool_access_repo_search"] is True


@pytest.mark.anyio
async def test_chat_task_metadata_includes_project_id(cp_app):
    cp_app.state.scheduler.schedule = AsyncMock(return_value={"output": "ok"})
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        convo = await client.post(
            "/v1/chat/conversations",
            json={"title": "Project Scoped", "project_id": "proj-meta-1"},
        )
        conversation_id = convo.json()["id"]
        await client.post(
            "/v1/bots",
            json={"id": "bot-meta", "name": "Meta Bot", "role": "assistant", "backends": [], "enabled": True},
        )
        resp = await client.post(
            f"/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "hello", "bot_id": "bot-meta"},
        )
        assert resp.status_code == 200
        task_arg = cp_app.state.scheduler.schedule.await_args[0][0]
        assert task_arg.metadata is not None
        assert task_arg.metadata.project_id == "proj-meta-1"
