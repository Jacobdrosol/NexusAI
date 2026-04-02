import subprocess
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.models import TaskMetadata


async def _register_temp_workspace(cp_app, *, project_id: str, orchestration_id: str, root) -> None:
    temp_root = root.parent / "temp-workspaces" / orchestration_id
    temp_root.mkdir(parents=True, exist_ok=True)
    await cp_app.state.orchestration_workspace_store.register(
        project_id=project_id,
        orchestration_id=orchestration_id,
        source_root=str(root),
        temp_root=str(temp_root),
        mode="copy",
    )


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
            json={
                "id": "pm-coder",
                "name": "PM Coder",
                "role": "coder",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "allow"},
            },
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
        await _register_temp_workspace(cp_app, project_id=project_id, orchestration_id=orchestration_id, root=root)

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
    assert body["assignment_workspace"]["lifecycle_state"] == "applied"
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
async def test_apply_assignment_requires_retained_temp_workspace(cp_app, tmp_path, monkeypatch):
    project_id = "proj-apply-no-temp"
    orchestration_id = "orch-apply-no-temp"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Apply No Temp Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-coder-no-temp",
                "name": "PM Coder",
                "role": "coder",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "allow"},
            },
        )
        task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-no-temp",
            payload={"title": "Implement without temp"},
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-no-temp",
            ),
        )
        await cp_app.state.task_manager.update_status(
            task.id,
            "completed",
            result={"output": "File: docs/example.md\n```md\n# example\n```\n"},
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/apply-assignment",
            json={"orchestration_id": orchestration_id},
        )

    assert resp.status_code == 409
    assert "temp workspace" in (resp.json().get("detail") or "").lower()


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


@pytest.mark.anyio
async def test_apply_assignment_ignores_repo_files_from_deny_policy_bots(cp_app, tmp_path, monkeypatch):
    project_id = "proj-apply-policy"
    orchestration_id = "orch-apply-policy"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Apply Policy Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-coder-apply",
                "name": "PM Coder",
                "role": "coder",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "allow"},
            },
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-tester-apply",
                "name": "PM Tester",
                "role": "tester",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "deny"},
            },
        )

        coder_task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-apply",
            payload={"title": "Write doc"},
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-policy",
            ),
        )
        await cp_app.state.task_manager.update_status(
            coder_task.id,
            "completed",
            result={"output": "File: docs/allowed.md\n```md\n# allowed\n```\n"},
        )

        tester_task = await cp_app.state.task_manager.create_task(
            bot_id="pm-tester-apply",
            payload={"title": "Do not write files"},
            metadata=TaskMetadata(
                source="bot_trigger",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-policy",
            ),
        )
        await cp_app.state.task_manager.update_status(
            tester_task.id,
            "completed",
            result={"output": "File: docs/blocked.md\n```md\n# blocked\n```\n"},
        )
        await _register_temp_workspace(cp_app, project_id=project_id, orchestration_id=orchestration_id, root=root)

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/apply-assignment",
            json={"orchestration_id": orchestration_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    applied_paths = [item["path"] for item in body["applied_files"]]
    assert applied_paths == ["docs/allowed.md"]
    assert (root / "docs" / "allowed.md").exists()
    assert not (root / "docs" / "blocked.md").exists()


@pytest.mark.anyio
async def test_review_assignment_previews_generated_files_without_writing(cp_app, tmp_path, monkeypatch):
    project_id = "proj-review"
    orchestration_id = "orch-review-1"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "existing.md").write_text("# old\n", encoding="utf-8")
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Review Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-coder-review",
                "name": "PM Coder",
                "role": "coder",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "allow"},
            },
        )

        task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-review",
            payload={
                "title": "Preview docs",
                "step_number": 1,
                "deliverables": ["docs/existing.md", "docs/new.md"],
            },
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-review",
            ),
        )
        await cp_app.state.task_manager.update_status(
            task.id,
            "completed",
            result={
                "artifacts": [
                    {"path": "docs/existing.md", "content": "# new\n"},
                    {"path": "docs/new.md", "content": "# created\n"},
                ]
            },
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/review-assignment",
            json={"orchestration_id": orchestration_id, "include_content": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["file_count"] == 2
    assert body["assignment_workspace"]["workspace_source"] == "orchestration_temp"
    assert body["expected_deliverables"] == ["docs/existing.md", "docs/new.md"]
    assert body["generated_deliverables"] == ["docs/existing.md", "docs/new.md"]
    assert body["missing_deliverables"] == []
    assert body["canonical_suite_complete"] is True
    assert body["review_subset_complete"] is True
    review_files = {item["path"]: item for item in body["review_files"]}
    assert review_files["docs/existing.md"]["status"] == "modified"
    assert review_files["docs/new.md"]["status"] == "new"
    assert "--- a/docs/existing.md" in review_files["docs/existing.md"]["diff"]
    assert (root / "docs" / "existing.md").read_text(encoding="utf-8") == "# old\n"
    assert not (root / "docs" / "new.md").exists()


@pytest.mark.anyio
async def test_review_assignment_prefers_repo_output_bot_provenance_for_duplicate_paths(cp_app, tmp_path, monkeypatch):
    project_id = "proj-review-provenance"
    orchestration_id = "orch-review-provenance"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))
    cp_app.state.task_manager._schedule_ready_tasks = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Review Provenance Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-coder-review-provenance",
                "name": "PM Coder",
                "role": "coder",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "allow"},
            },
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-tester-review-provenance",
                "name": "PM Tester",
                "role": "tester",
                "backends": [],
                "enabled": True,
                "execution_policy": {"repo_output_mode": "deny"},
            },
        )

        coder_task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-review-provenance",
            payload={"title": "Write roadmap"},
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-review-provenance",
            ),
        )
        await cp_app.state.task_manager.update_status(
            coder_task.id,
            "completed",
            result={"artifacts": [{"path": "docs/blocks/roadmap.md", "content": "# Roadmap\n"}]},
        )

        tester_task = await cp_app.state.task_manager.create_task(
            bot_id="pm-tester-review-provenance",
            payload={"title": "Echo roadmap"},
            metadata=TaskMetadata(
                source="bot_trigger",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-review-provenance",
            ),
        )
        await cp_app.state.task_manager.update_status(
            tester_task.id,
            "completed",
            result={"artifacts": [{"path": "docs/blocks/roadmap.md", "content": "# Roadmap\n"}]},
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/review-assignment",
            json={"orchestration_id": orchestration_id, "include_content": False},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["file_count"] == 1
    item = body["review_files"][0]
    assert item["path"] == "docs/blocks/roadmap.md"
    assert item["bot_id"] == "pm-coder-review-provenance"
    assert item["apply_eligible"] is True
    assert body["generated_deliverables"] == ["docs/blocks/roadmap.md"]


@pytest.mark.anyio
async def test_review_assignment_extracts_expected_repo_path_from_deliverable_text(cp_app, tmp_path, monkeypatch):
    project_id = "proj-review-deliverable-text"
    orchestration_id = "orch-review-deliverable-text"
    base_root = tmp_path / "repo-workspaces"
    root = base_root / project_id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("NEXUSAI_REPO_WORKSPACE_ROOT", str(base_root))

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        await client.post("/v1/projects", json={"id": project_id, "name": "Deliverable Text Project", "mode": "isolated"})
        await client.put(
            f"/v1/projects/{project_id}/repo/workspace",
            json={"enabled": True, "managed_path_mode": True},
        )
        await client.post(
            "/v1/bots",
            json={
                "id": "pm-coder-deliverable-text",
                "name": "PM Coder Deliverable Text",
                "role": "coder",
                "enabled": True,
                "backends": [],
                "execution_policy": {"repo_output_mode": "allow"},
            },
        )

        task = await cp_app.state.task_manager.create_task(
            bot_id="pm-coder-deliverable-text",
            payload={
                "instruction": "create sql file",
                "deliverables": ["File: `temp/sql/ai_grading_migration.sql` containing the full migration script."],
            },
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                orchestration_id=orchestration_id,
                conversation_id="conv-review-deliverable-text",
            ),
        )
        await cp_app.state.task_manager.update_status(
            task.id,
            "completed",
            result={"artifacts": [{"path": "temp/sql/ai_grading_migration.sql", "content": "-- migration\nSELECT 1;\n"}]},
        )

        resp = await client.post(
            f"/v1/projects/{project_id}/repo/workspace/review-assignment",
            json={"orchestration_id": orchestration_id, "include_content": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["expected_deliverables"] == ["temp/sql/ai_grading_migration.sql"]
    assert body["generated_deliverables"] == ["temp/sql/ai_grading_migration.sql"]
    assert body["missing_deliverables"] == []
