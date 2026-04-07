import asyncio
import pytest
import sys
from contextlib import suppress

from control_plane.platform_ai.runtime import PlatformAISessionRuntime
from control_plane.platform_ai.session_store import PlatformAISessionStore
from control_plane.registry.bot_registry import BotRegistry
from shared.models import Bot


@pytest.mark.anyio
async def test_pipeline_tuner_terminal_failure_stops_session(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "pipeline_name": "Coding Pipeline",
            "autonomous_state": "needs_refinement",
            "autonomous_last_eval_signature": "sig-1",
            "autonomous_last_refine_signature": "sig-1",
        },
    )

    snapshot = {
        "orchestration_id": "orch-1",
        "status_counts": {"completed": 8, "failed": 7},
        "active_tasks": [],
        "runtime_state": {"task_total": 15},
    }

    updated = await runtime._finalize_autonomous_session_if_terminal(
        session["id"],
        session=session,
        snapshot=snapshot,
    )

    assert updated is not None
    assert str(updated.get("status") or "") == "running"
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert str(metadata.get("autonomous_terminal_reason") or "") == "autonomous_stalled_after_evaluation"

    events = await store.list_events(session["id"], limit=20)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "autonomous_session_terminalized"
        for event in events
    )
    messages = await store.list_messages(session["id"], limit=20)
    assert any("stalled refinement path" in str(message.get("content") or "") for message in messages)


@pytest.mark.anyio
async def test_pipeline_tuner_converged_session_completes(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "pipeline_name": "Coding Pipeline",
            "autonomous_state": "converged",
            "autonomous_last_eval_score": 0.94,
        },
    )

    snapshot = {
        "orchestration_id": "orch-2",
        "status_counts": {"completed": 12},
        "active_tasks": [],
        "runtime_state": {"task_total": 12},
    }

    updated = await runtime._finalize_autonomous_session_if_terminal(
        session["id"],
        session=session,
        snapshot=snapshot,
    )

    assert updated is not None
    assert str(updated.get("status") or "") == "running"
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert str(metadata.get("autonomous_terminal_reason") or "") == "autonomous_converged"


@pytest.mark.anyio
async def test_process_operator_message_upserts_bot_from_json_block(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running", metadata={"bot_name_seed": "Designer Created Bot"})
    message = """
Please create this bot config.
```json
{
  "platform_ai_action": "upsert_bot",
  "bot": {
    "id": "designer-created-bot",
    "name": "Designer Created Bot",
    "role": "assistant",
    "enabled": true,
    "backends": [
      {"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}
    ]
  }
}
```
"""
    await store.append_message(session["id"], role="operator", content=message, metadata={})
    await runtime._process_operator_messages(session["id"])
    created = await bot_registry.get("designer-created-bot")
    assert created.id == "designer-created-bot"
    assert str(created.name) == "Designer Created Bot"


@pytest.mark.anyio
async def test_process_operator_message_applies_tuning_overrides(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={"pipeline_bot_id": "pm-orchestrator"},
    )
    await store.append_message(
        session["id"],
        role="operator",
        content="Set target score to 0.96 and max iterations to 9 for this run.",
        metadata={},
    )
    await runtime._process_operator_messages(session["id"])
    updated = await store.get_session(session["id"])
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert float(metadata.get("autonomous_target_score") or 0.0) == pytest.approx(0.96, rel=1e-6)
    assert int(metadata.get("autonomous_max_iterations") or 0) == 9
    assert bool(metadata.get("autonomous_enabled")) is True


@pytest.mark.anyio
async def test_repo_edit_runner_executes_command_and_reports_terminal_event(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="tester@example.com",
        privileged=True,
        metadata={},
    )
    cmd = f"\"{sys.executable}\" -c \"print('repo-edit-ok')\""
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "tester@example.com")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD", cmd)
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_CWD", str(tmp_path))
    result = await runtime.start_repo_edit_run(
        session["id"],
        requested_by="tester@example.com",
        instruction="Apply patch and commit",
        external=False,
    )
    assert str(result.get("status") or "") == "started"

    for _ in range(100):
        events = await store.list_events(session["id"], limit=200)
        if any(
            str((event.get("payload") or {}).get("action") or "") == "repo_edit_finished"
            and str((event.get("payload") or {}).get("state") or "") == "succeeded"
            for event in events
            if isinstance(event, dict)
        ):
            break
        await asyncio.sleep(0.05)
    events = await store.list_events(session["id"], limit=400)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "repo_edit_finished"
        and str((event.get("payload") or {}).get("state") or "") == "succeeded"
        for event in events
        if isinstance(event, dict)
    )


@pytest.mark.anyio
async def test_repo_edit_runner_denies_non_allowlisted_operator(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="other@example.com",
        privileged=True,
        metadata={},
    )
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "owner@example.com")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD", "echo should-not-run")
    denied = await runtime.start_repo_edit_run(
        session["id"],
        requested_by="other@example.com",
        instruction="Apply change",
        external=False,
    )
    assert str(denied.get("status") or "") == "denied"


@pytest.mark.anyio
async def test_repo_edit_runner_denies_when_platform_project_scope_mismatch(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="owner@example.com",
        privileged=True,
        metadata={"project_id": "wrong-project"},
    )
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "owner@example.com")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_ENFORCE_PROJECT_ID", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ID", "globalagent-platform")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD", "echo should-not-run")
    denied = await runtime.start_repo_edit_run(
        session["id"],
        requested_by="owner@example.com",
        instruction="Apply change",
        external=False,
    )
    assert str(denied.get("status") or "") == "denied"
    assert "allowlist" in str(denied.get("detail") or "").lower() or "project_id" in str(denied.get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_edit_runner_denies_missing_project_id_when_required(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="owner@example.com",
        privileged=False,
        metadata={},
    )
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_RUN_CMD", "echo should-not-run")
    denied = await runtime.start_project_edit_run(
        session["id"],
        requested_by="owner@example.com",
        instruction="Apply patch and run tests",
    )
    assert str(denied.get("status") or "") == "denied"
    assert "project_id" in str(denied.get("detail") or "")


@pytest.mark.anyio
async def test_project_edit_runner_completes_and_checkpoints_ready(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="owner@example.com",
        privileged=False,
        metadata={},
    )
    cmd = f"\"{sys.executable}\" -c \"print('project-edit-ok')\""
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_RUN_CMD", cmd)
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_CWD", str(tmp_path))
    result = await runtime.start_project_edit_run(
        session["id"],
        requested_by="owner@example.com",
        instruction="Apply patch and run tests",
    )
    assert str(result.get("status") or "") == "started"

    for _ in range(120):
        events = await store.list_events(session["id"], limit=400)
        if any(
            str((event.get("payload") or {}).get("action") or "") == "project_edit_finished"
            for event in events
            if isinstance(event, dict)
        ):
            break
        await asyncio.sleep(0.05)
    updated = await store.get_session(session["id"])
    assert str((updated or {}).get("status") or "") == "ready"
    metadata = (updated or {}).get("metadata") if isinstance((updated or {}).get("metadata"), dict) else {}
    report = metadata.get("project_edit_report") if isinstance(metadata.get("project_edit_report"), dict) else {}
    assert "suggested_commit_message" in report
    assert isinstance(report.get("alternatives"), list)
    messages = await store.list_messages(session["id"], limit=50)
    assert any("No commit/push was performed by Platform AI" in str(row.get("content") or "") for row in messages)


@pytest.mark.anyio
async def test_reference_scope_bot_is_read_only_for_mutation(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "pipeline_bot_id": "pipeline-bot",
            "reference_bot_ids": ["reference-bot"],
            "mutable_bot_ids": ["pipeline-bot"],
        },
    )
    await bot_registry.register(
        Bot.model_validate(
            {
                "id": "reference-bot",
                "name": "Reference Bot",
                "role": "assistant",
                "enabled": True,
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
            }
        )
    )
    result = await runtime._upsert_bot_payload(
        {
            "id": "reference-bot",
            "name": "Reference Bot Updated",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
        },
        session_id=session["id"],
        session=session,
        allow_scope_expansion=True,
    )
    assert bool(result.get("ok")) is False
    assert str(result.get("detail") or "") == "reference_scope_read_only"


@pytest.mark.anyio
async def test_ensure_session_loop_is_singleton_under_concurrency(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(mode="bot_creator", status="running", metadata={})

    call_counter = {"count": 0}

    async def fake_loop(_: str) -> None:
        call_counter["count"] += 1
        await asyncio.sleep(0.2)

    runtime._session_loop = fake_loop  # type: ignore[method-assign]

    await asyncio.gather(*[runtime.ensure_session_loop(session["id"]) for _ in range(25)])
    assert len(runtime._session_tasks) == 1
    await asyncio.sleep(0.25)
    assert call_counter["count"] == 1


@pytest.mark.anyio
async def test_session_loop_does_not_halt_as_stalled_while_orchestration_running(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": False,
            "pipeline_bot_id": "pm-orchestrator",
            "runtime_state": {
                "orchestration_id": "orch-live",
                "task_total": 1,
                "status_counts": {"running": 1},
                "active_tasks": [{"task_id": "task-live", "status": "running"}],
            },
        },
        orchestration_id="orch-live",
    )

    check_calls = {"count": 0}
    halt_calls = {"count": 0}

    async def fake_check(_: str, **__: object) -> str | None:
        check_calls["count"] += 1
        return "stalled_duplicate_actions"

    async def fake_snapshot(_: dict) -> dict:
        return {
            "tick": 1,
            "phase": "evaluate",
            "active_action": "monitor_orchestration",
            "detail": "waiting",
            "heartbeat_detail": "waiting",
            "signature": "sig-live",
            "orchestration_id": "orch-live",
            "status_counts": {"running": 1},
            "active_tasks": [{"task_id": "task-live", "status": "running"}],
            "runtime_state": {
                "orchestration_id": "orch-live",
                "task_total": 1,
                "status_counts": {"running": 1},
                "active_tasks": [{"task_id": "task-live", "status": "running"}],
            },
        }

    async def fake_halt(session_id: str, *, reason: str, message: str) -> None:
        _ = (reason, message)
        halt_calls["count"] += 1
        await store.update_session(session_id, status="stopped")

    runtime._check_should_halt_as_stalled = fake_check  # type: ignore[method-assign]
    runtime._build_progress_snapshot = fake_snapshot  # type: ignore[method-assign]
    runtime._halt_session = fake_halt  # type: ignore[method-assign]

    loop_task = asyncio.create_task(runtime._session_loop(session["id"]))
    await asyncio.sleep(0.2)
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task

    assert check_calls["count"] == 0
    assert halt_calls["count"] == 0


@pytest.mark.anyio
async def test_session_loop_finalizes_terminal_state_before_stall_guard(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={"autonomous_enabled": True, "pipeline_bot_id": "pm-orchestrator"},
        orchestration_id="orch-terminal",
    )

    check_calls = {"count": 0}
    halt_calls = {"count": 0}
    finalize_calls = {"count": 0}

    async def fake_check(_: str, **__: object) -> str | None:
        check_calls["count"] += 1
        return "stalled_duplicate_actions"

    async def fake_snapshot(_: dict) -> dict:
        return {
            "tick": 1,
            "phase": "evaluate",
            "active_action": "monitor_orchestration",
            "detail": "terminal",
            "heartbeat_detail": "terminal",
            "signature": "sig-terminal",
            "orchestration_id": "orch-terminal",
            "status_counts": {"completed": 1, "running": 0, "failed": 0},
            "active_tasks": [],
            "runtime_state": {
                "orchestration_id": "orch-terminal",
                "task_total": 1,
                "status_counts": {"completed": 1},
                "active_tasks": [],
            },
        }

    async def fake_tuner(session_id: str, *, session: dict, snapshot: dict) -> None:
        _ = (session_id, session, snapshot)
        return None

    async def fake_finalize(
        session_id: str,
        *,
        session: dict,
        snapshot: dict,
    ) -> dict | None:
        _ = (session, snapshot)
        finalize_calls["count"] += 1
        await store.update_session(session_id, status="ready")
        return {"status": "ready"}

    async def fake_halt(session_id: str, *, reason: str, message: str) -> None:
        _ = (reason, message)
        halt_calls["count"] += 1
        await store.update_session(session_id, status="stopped")

    runtime._check_should_halt_as_stalled = fake_check  # type: ignore[method-assign]
    runtime._build_progress_snapshot = fake_snapshot  # type: ignore[method-assign]
    runtime._run_autonomous_pipeline_tuner = fake_tuner  # type: ignore[method-assign]
    runtime._finalize_autonomous_session_if_terminal = fake_finalize  # type: ignore[method-assign]
    runtime._halt_session = fake_halt  # type: ignore[method-assign]

    loop_task = asyncio.create_task(runtime._session_loop(session["id"]))
    await asyncio.sleep(0.2)
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task

    assert finalize_calls["count"] >= 1
    assert check_calls["count"] == 0
    assert halt_calls["count"] == 0
