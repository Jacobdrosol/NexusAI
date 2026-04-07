import asyncio
import pytest
import sys

from control_plane.platform_ai.runtime import PlatformAISessionRuntime
from control_plane.platform_ai.session_store import PlatformAISessionStore
from control_plane.registry.bot_registry import BotRegistry


@pytest.mark.anyio
async def test_pipeline_tuner_terminal_failure_stops_session(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="active",
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
    assert str(updated.get("status") or "") == "failed"
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert str(metadata.get("autonomous_terminal_reason") or "") == "autonomous_stalled_after_evaluation"

    events = await store.list_events(session["id"], limit=20)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "autonomous_session_terminalized"
        for event in events
    )
    messages = await store.list_messages(session["id"], limit=20)
    assert any("no new remediation iteration was launched" in str(message.get("content") or "") for message in messages)


@pytest.mark.anyio
async def test_pipeline_tuner_converged_session_completes(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="active",
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
    assert str(updated.get("status") or "") == "completed"
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert str(metadata.get("autonomous_terminal_reason") or "") == "autonomous_converged"


@pytest.mark.anyio
async def test_process_operator_message_upserts_bot_from_json_block(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_designer", status="active", metadata={})
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
        status="active",
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
        mode="copilot",
        status="active",
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
        mode="copilot",
        status="active",
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
async def test_ensure_session_loop_is_singleton_under_concurrency(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(mode="copilot", status="active", metadata={})

    call_counter = {"count": 0}

    async def fake_loop(_: str) -> None:
        call_counter["count"] += 1
        await asyncio.sleep(0.2)

    runtime._session_loop = fake_loop  # type: ignore[method-assign]

    await asyncio.gather(*[runtime.ensure_session_loop(session["id"]) for _ in range(25)])
    assert len(runtime._session_tasks) == 1
    await asyncio.sleep(0.25)
    assert call_counter["count"] == 1
