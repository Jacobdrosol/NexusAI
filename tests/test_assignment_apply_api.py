import subprocess
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.models import TaskMetadata


@pytest.mark.anyio
async def test_apply_assignment_writes_extracted_files_into_repo_workspace(cp_app, tmp_path, monkeypatch):
    project_id = "proj-apply"
    orchestration_id = "orch-apply-1"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Apply Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-coder", "name": "PM Coder", "role": "coder", "backends": [], "enabled": True},
        )

        task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder",
            payload={"title": "Implement lesson block", "step_number": 2},
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-1",
            ),
        )
        await cp_app.state.task_manager.update_status(
            task.id,
            "completed",
            result={
                "output": (
                    "### Deliverable 1: src/lessonBlocks/MathBlock.tsx\n"
                    "```tsx\n"
                    "export const MathBlock = () => <div>ok</div>;\n"
                    "```\n"
                )
            },
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/apply-assignment",
            json={"orchestration_id": orchestration_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["applied_files"][0]["path"] == "src/lessonBlocks/MathBlock.tsx"
    assert (root / "src" / "lessonBlocks" / "MathBlock.tsx").read_text(encoding="utf-8").strip().startswith(
        "export const MathBlock"
    )
    assert body["workspace"]["is_repo"] is True


@pytest.mark.anyio
async def test_apply_assignment_rejects_in_progress_tasks(cp_app, tmp_path, monkeypatch):
    project_id = "proj-apply-pending"
    orchestration_id = "orch-apply-pending"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Apply Pending Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={"id": "pm-coder-pending", "name": "PM Coder", "role": "coder", "backends": [], "enabled": True},
        )

        await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-pending",
            payload={"title": "Still running", "step_number": 1},
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-2",
            ),
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/apply-assignment",
            json={"orchestration_id": orchestration_id},
        )

    assert resp.status_code == 409
    assert "still in progress" in (resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_completed_task_records_extracted_file_artifact(cp_app):
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post(
            "/v1/bots",
            json={"id": "pm-coder-artifacts", "name": "PM Coder", "role": "coder", "backends": [], "enabled": True},
        )
        task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-artifacts",
            payload={"title": "Emit file", "step_number": 1},
            metadata=TaskMetadata(source="chat_assign", project_id="proj-artifacts", orchestration_id="orch-artifacts"),
        )
        await cp_app.state.task_manager.update_status(
            task.id,
            "completed",
            result={
                "output": (
                    "File: src/generated/demo.ts\n"
                    "```ts\n"
                    "export const demo = true;\n"
                    "```\n"
                )
            },
        )

    artifacts = await cp_app.state.task_manager.list_bot_run_artifacts("pm-coder-artifacts", task_id=task.id)
    file_artifact = next((artifact for artifact in artifacts if artifact.kind == "file"), None)
    assert file_artifact is not None
    assert file_artifact.path == "src/generated/demo.ts"
    assert "export const demo = true" in str(file_artifact.content or "")
