import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import sys
from contextlib import suppress

from control_plane.api.platform_ai import _apply_cli_backend_profile
from control_plane.agent_scheduler.engine import AgentScheduleEngine
from control_plane.platform_ai.runtime import PlatformAISessionRuntime
from control_plane.platform_ai.session_store import PlatformAISessionStore
from control_plane.registry.bot_registry import BotRegistry
from control_plane.registry.project_registry import ProjectRegistry
from control_plane.registry.worker_registry import WorkerRegistry
from control_plane.connections.resolver import ConnectionResolver
from control_plane.worker_probe_store import WorkerProbeStore
from shared.exceptions import BotNotFoundError
from shared.models import Bot, Worker


def test_platform_ai_cli_backend_uses_approved_ollama_profile():
    config = {
        "backend_type": "cli",
        "provider": "cli",
        "model": "claude",
        "worker_id": "globeiq-coding-sandbox-01",
    }

    _apply_cli_backend_profile(
        config,
        cli_command_profile="claude_ollama_json",
        cli_runtime_model="glm-5.2:cloud",
    )

    assert config["command"] == "claude -p --model glm-5.2:cloud --output-format json"
    assert config["cli_command_profile"] == "claude_ollama_json"


def test_platform_ai_cli_backend_rejects_unsafe_runtime_model():
    config = {
        "backend_type": "cli",
        "provider": "cli",
        "model": "claude",
        "worker_id": "globeiq-coding-sandbox-01",
    }

    with pytest.raises(Exception, match="valid Ollama model name"):
        _apply_cli_backend_profile(
            config,
            cli_command_profile="claude_ollama_json",
            cli_runtime_model="glm-5.2:cloud; rm -rf /",
        )


@pytest.mark.anyio
async def test_platform_ai_capabilities_expose_nonsecret_feature_flags(cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_CLOUD_CONTEXT_POLICY", "redact")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED", "0")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_DEPLOY_ENABLED", "0")

    response = await cp_client.get("/v1/platform-ai/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cloud_context_policy"] == "redact"
    assert payload["actions"] == {
        "privileged_mode": False,
        "configuration_mutations": False,
        "autonomous_pipeline_runs": False,
        "project_repo_edits": False,
        "external_repo_edits": False,
        "repository_edits": False,
        "deployments": False,
    }


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
async def test_process_operator_message_creates_proposal_from_json_block(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
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
    with pytest.raises(BotNotFoundError):
        await bot_registry.get("designer-created-bot")
    proposals = await store.list_patch_proposals(session["id"])
    assert len(proposals) == 1
    assert str((proposals[0].get("after_state") or {}).get("proposal_kind") or "") == "bot_configuration"


@pytest.mark.anyio
async def test_process_operator_message_applies_tuning_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED", "true")
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
async def test_process_operator_message_invokes_platform_brain_backend(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))

    class FakeScheduler:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            self.calls.append({"backend": backend, "payload": payload, "task": task})
            return {
                "output": "{\"assistant_reply\":\"Platform brain online.\",\"actions\":[]}",
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    scheduler = FakeScheduler()
    runtime = PlatformAISessionRuntime(store, scheduler=scheduler)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "pipeline_bot_id": "pm-orchestrator",
            "backend": {
                "provider": "vertex",
                "model": "claude-opus-4-6",
                "backend_type": "cloud_api",
                "credential_ref": "VERTEX_SERVICE_ACCOUNT_JSON",
                "params": {"max_tokens": 2048, "temperature": 0.1},
                "vertex_project_id": "nexusai-prod",
                "vertex_location": "global",
            },
        },
    )
    await store.append_message(session["id"], role="operator", content="Tune this pipeline carefully.", metadata={})
    await runtime._process_operator_messages(session["id"])

    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    backend = call["backend"]
    payload = call["payload"]
    assert str(getattr(backend, "provider", "") or "") == "vertex"
    assert str(getattr(backend, "model", "") or "") == "claude-opus-4-6"
    assert isinstance(payload, dict)
    assert str(payload.get("vertex_project_id") or "") == "nexusai-prod"
    assert str(payload.get("vertex_location") or "") == "global"
    assert isinstance(payload.get("messages"), list)

    messages = await store.list_messages(session["id"], limit=50)
    assert any(
        str((row.get("metadata") or {}).get("source") or "") == "platform_brain"
        and "Platform brain online." in str(row.get("content") or "")
        for row in messages
    )
    events = await store.list_events(session["id"], limit=100)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_invoked"
        for event in events
    )


@pytest.mark.anyio
async def test_platform_brain_specialist_catalog_exposes_only_ready_nonsecret_worker_metadata(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    workers = WorkerRegistry(db_path=str(tmp_path / "workers.db"))
    probes = WorkerProbeStore(db_path=str(tmp_path / "probes.db"))
    await workers.register(
        Worker.model_validate(
            {
                "id": "ready-catalog-worker",
                "name": "Private Worker Name",
                "host": "10.24.8.19",
                "port": 9911,
                "status": "online",
                "enabled": True,
                "request_token_env": "PRIVATE_REQUEST_TOKEN",
                "capabilities": [
                    {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]}
                ],
            }
        )
    )
    await workers.register(
        Worker.model_validate(
            {
                "id": "unready-catalog-worker",
                "name": "Unready Worker",
                "host": "10.24.8.20",
                "port": 9912,
                "status": "online",
                "enabled": True,
                "capabilities": [
                    {"type": "llm", "provider": "ollama_cloud", "models": ["qwen3.5:cloud"]}
                ],
            }
        )
    )
    await probes.record(
        {
            "worker_id": "ready-catalog-worker",
            "probe_status": "ready",
            "capability_attestation": {
                "enabled_cli_tools": ["claude"],
                "browser": {"configured": True, "ready": True, "reason": ""},
                "provider_credentials": {"ollama_cloud": "do-not-disclose"},
            },
        }
    )
    await probes.record({"worker_id": "unready-catalog-worker", "probe_status": "unreachable"})

    class FakeScheduler:
        def __init__(self) -> None:
            self.payload = None

        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            _ = (backend, task)
            self.payload = payload
            return {"output": "{\"assistant_reply\":\"Catalog received.\",\"actions\":[]}"}

    scheduler = FakeScheduler()
    runtime = PlatformAISessionRuntime(
        store,
        scheduler=scheduler,
        worker_registry=workers,
        worker_probe_store=probes,
    )
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={
            "backend": {
                "provider": "ollama_cloud",
                "model": "glm-5.2:cloud",
                "backend_type": "remote_llm",
                "worker_id": "ready-catalog-worker",
            }
        },
    )
    await store.append_message(session["id"], role="operator", content="Create a researcher.", metadata={})
    await runtime._process_operator_messages(session["id"])

    assert isinstance(scheduler.payload, list)
    system_prompt = str(scheduler.payload[0]["content"])
    assert "Non-secret execution catalog" in system_prompt
    assert "ready-catalog-worker" in system_prompt
    assert "glm-5.2:cloud" in system_prompt
    assert "claude" in system_prompt
    assert "unready-catalog-worker" not in system_prompt
    assert "10.24.8.19" not in system_prompt
    assert "9911" not in system_prompt
    assert "Private Worker Name" not in system_prompt
    assert "PRIVATE_REQUEST_TOKEN" not in system_prompt
    assert "do-not-disclose" not in system_prompt


@pytest.mark.anyio
async def test_platform_brain_actions_create_bot_proposals_within_mode_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))

    class FakeScheduler:
        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            _ = (backend, payload, task)
            return {
                "output": (
                    "{"
                    "\"assistant_reply\":\"Creating requested bot.\","
                    "\"actions\":["
                    "{"
                    "\"platform_ai_action\":\"upsert_bot\","
                    "\"bot\":{"
                    "\"id\":\"platform-brain-bot\","
                    "\"name\":\"Platform Brain Bot\","
                    "\"role\":\"assistant\","
                    "\"enabled\":true,"
                    "\"backends\":[{\"type\":\"cloud_api\",\"provider\":\"openai\",\"model\":\"gpt-4o-mini\"}]"
                    "}"
                    "}"
                    "]"
                    "}"
                ),
                "usage": {"prompt_tokens": 20, "completion_tokens": 14},
            }

    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry, scheduler=FakeScheduler())
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={
            "bot_name_seed": "Platform Brain Bot",
            "backend": {
                "provider": "openai",
                "model": "gpt-4.1",
                "backend_type": "cloud_api",
                "credential_ref": "OPENAI_API_KEY",
            },
        },
    )
    await store.append_message(session["id"], role="operator", content="Please create a new helper bot.", metadata={})
    await runtime._process_operator_messages(session["id"])

    with pytest.raises(BotNotFoundError):
        await bot_registry.get("platform-brain-bot")
    proposals = await store.list_patch_proposals(session["id"])
    assert len(proposals) == 1
    assert str(((proposals[0].get("after_state") or {}).get("bot") or {}).get("id") or "") == "platform-brain-bot"


@pytest.mark.anyio
async def test_platform_ai_keeps_model_actions_proposal_only_without_configuration_grant(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running", metadata={"bot_name_seed": "Draft Only Bot"})
    message = """
```json
{"platform_ai_action":"upsert_bot","bot":{"id":"draft-only-bot","name":"Draft Only Bot","role":"assistant","backends":[{"type":"cloud_api","provider":"openai","model":"gpt-4o-mini"}]}}
```
"""

    await store.append_message(session["id"], role="operator", content=message, metadata={})
    await runtime._process_operator_messages(session["id"])

    with pytest.raises(BotNotFoundError):
        await bot_registry.get("draft-only-bot")
    messages = await store.list_messages(session["id"], limit=20)
    assert any(
        str((row.get("metadata") or {}).get("source") or "") == "operator_directive_proposal"
        and "configuration_proposal_required" in str(row.get("content") or "")
        for row in messages
    )


@pytest.mark.anyio
async def test_pipeline_tuner_message_does_not_enable_autonomy_without_runtime_grant(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(mode="pipeline_tuner", status="running", metadata={"pipeline_bot_id": "safe-pipeline"})

    await store.append_message(session["id"], role="operator", content="Improve the pipeline quality.", metadata={})
    await runtime._process_operator_messages(session["id"])

    updated = await store.get_session(session["id"])
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    assert metadata.get("autonomous_enabled") is not True
    events = await store.list_events(session["id"], limit=30)
    assert any(str((event.get("payload") or {}).get("action") or "") == "autonomous_goal_proposed" for event in events)


@pytest.mark.anyio
async def test_platform_ai_keeps_bot_activation_manual_unless_both_grants_are_present(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={"bot_name_seed": "Approved Bot", "allow_bot_activation": True},
    )
    payload = {
        "id": "approved-bot",
        "name": "Approved Bot",
        "role": "assistant",
        "enabled": True,
        "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
    }

    denied = await runtime._upsert_bot_payload(
        payload,
        session_id=session["id"],
        session=session,
        allow_scope_expansion=True,
    )

    assert denied["activation_change"] == "created_disabled"
    assert (await bot_registry.get("approved-bot")).enabled is False

    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTO_ACTIVATE_BOTS", "true")
    granted_session = await store.update_session(session["id"], metadata={"allow_bot_activation": True})
    assert granted_session is not None
    granted = await runtime._upsert_bot_payload(
        payload,
        session_id=session["id"],
        session=granted_session,
        allow_scope_expansion=True,
    )

    assert granted["activation_change"] == "auto_activation_allowed"
    assert (await bot_registry.get("approved-bot")).enabled is True


@pytest.mark.anyio
async def test_platform_brain_uses_catalog_fallback_when_model_not_registered(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))

    class FakeScheduler:
        def __init__(self) -> None:
            self.vertex_calls = 0

        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            _ = (backend, payload, task)
            raise Exception("Model 'claude-opus-4-6' (provider 'vertex') is not present/enabled in the model catalog.")

        async def _call_vertex(self, backend, payload):  # noqa: ANN001
            _ = (backend, payload)
            self.vertex_calls += 1
            return {"output": "{\"assistant_reply\":\"fallback worked\",\"actions\":[]}", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    scheduler = FakeScheduler()
    runtime = PlatformAISessionRuntime(store, scheduler=scheduler)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "pipeline_bot_id": "pm-orchestrator",
            "backend": {
                "provider": "vertex",
                "model": "claude-opus-4-6",
                "backend_type": "cloud_api",
                "credential_ref": "VERTEX_SERVICE_ACCOUNT_JSON",
                "vertex_project_id": "nexusai-prod",
                "vertex_location": "global",
            },
        },
    )
    await store.append_message(session["id"], role="operator", content="Run fallback check.", metadata={})
    await runtime._process_operator_messages(session["id"])

    assert scheduler.vertex_calls == 1
    events = await store.list_events(session["id"], limit=100)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_catalog_fallback"
        for event in events
    )
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_invoked"
        and bool((event.get("payload") or {}).get("catalog_fallback_used")) is True
        for event in events
    )


@pytest.mark.anyio
async def test_platform_brain_auto_registers_catalog_model_before_fallback(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))

    class FakeModelRegistry:
        def __init__(self) -> None:
            self.registered = False
            self.register_calls = 0

        async def exists(self, provider, name):  # noqa: ANN001
            _ = (provider, name)
            return self.registered

        async def register(self, model):  # noqa: ANN001
            _ = model
            self.register_calls += 1
            self.registered = True

    class FakeScheduler:
        def __init__(self) -> None:
            self.model_registry = FakeModelRegistry()
            self.dispatch_calls = 0
            self.vertex_calls = 0

        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            _ = (backend, payload, task)
            self.dispatch_calls += 1
            if not self.model_registry.registered:
                raise Exception("Model 'gemini-2.5-pro' (provider 'vertex') is not present/enabled in the model catalog.")
            return {
                "output": "{\"assistant_reply\":\"catalog auto-register worked\",\"actions\":[]}",
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }

        async def _call_vertex(self, backend, payload):  # noqa: ANN001
            _ = (backend, payload)
            self.vertex_calls += 1
            return {"output": "{\"assistant_reply\":\"fallback path\",\"actions\":[]}"}

    scheduler = FakeScheduler()
    runtime = PlatformAISessionRuntime(store, scheduler=scheduler)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "pipeline_bot_id": "pm-orchestrator",
            "backend": {
                "provider": "vertex",
                "model": "gemini-2.5-pro",
                "backend_type": "cloud_api",
                "credential_ref": "VERTEX_SERVICE_ACCOUNT_JSON",
                "vertex_project_id": "nexusai-prod",
                "vertex_location": "global",
            },
        },
    )
    await store.append_message(session["id"], role="operator", content="Run catalog auto-register check.", metadata={})
    await runtime._process_operator_messages(session["id"])

    assert scheduler.model_registry.register_calls == 1
    assert scheduler.dispatch_calls >= 2
    assert scheduler.vertex_calls == 0
    events = await store.list_events(session["id"], limit=120)
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_catalog_autoregistered"
        for event in events
    )
    assert not any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_catalog_fallback"
        for event in events
    )
    assert any(
        str((event.get("payload") or {}).get("action") or "") == "platform_brain_invoked"
        and bool((event.get("payload") or {}).get("catalog_fallback_used")) is False
        for event in events
    )


@pytest.mark.anyio
async def test_operator_message_in_ready_state_is_processed_without_resume(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))

    class FakeScheduler:
        async def _dispatch_backend(self, backend, payload, task=None):  # noqa: ANN001
            _ = (backend, payload, task)
            return {
                "output": "{\"assistant_reply\":\"Ready-state instruction processed.\",\"actions\":[]}",
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }

    runtime = PlatformAISessionRuntime(store, scheduler=FakeScheduler())
    session = await store.create_session(
        mode="bot_creator",
        status="ready",
        metadata={
            "bot_name_seed": "ready-bot",
            "backend": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "backend_type": "cloud_api",
                "credential_ref": "OPENAI_API_KEY",
            },
        },
    )

    await runtime.post_message(
        session["id"],
        role="operator",
        content="Please inspect and suggest next actions.",
        metadata={},
    )

    for _ in range(60):
        rows = await store.list_messages(session["id"], limit=50)
        if any(str((row.get("metadata") or {}).get("source") or "") == "platform_brain" for row in rows):
            break
        await asyncio.sleep(0.05)

    rows = await store.list_messages(session["id"], limit=80)
    assert any(str((row.get("metadata") or {}).get("source") or "") == "runtime_ack" for row in rows)
    assert any(
        str((row.get("metadata") or {}).get("source") or "") == "platform_brain"
        and "Ready-state instruction processed." in str(row.get("content") or "")
        for row in rows
    )
    assert not any(
        "Resume/start the session" in str(row.get("content") or "")
        for row in rows
        if str(row.get("role") or "").strip().lower() == "assistant"
    )


@pytest.mark.anyio
async def test_autonomous_tuner_pauses_when_platform_brain_unavailable(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "pipeline_bot_id": "pm-orchestrator",
            "pipeline_name": "Coding Pipeline",
            "backend": {
                "provider": "vertex",
                "model": "claude-opus-4-6",
                "backend_type": "cloud_api",
                "credential_ref": "Vertex-cocopepia",
                "vertex_project_id": "nexusai-audit",
                "vertex_location": "global",
            },
        },
        assignment_id="assign-1",
        run_id="run-1",
        orchestration_id="orch-1",
    )

    async def fake_resolve_context(_session):  # noqa: ANN001
        return {
            "assignment_id": "assign-1",
            "run_id": "run-1",
            "orchestration_id": "orch-1",
            "graph": {"nodes": [{"id": "pm-orchestrator", "title": "PM Orchestrator"}], "edges": []},
            "tasks": [
                {
                    "id": "task-1",
                    "bot_id": "pm-orchestrator",
                    "status": "failed",
                    "updated_at": "2026-04-08T14:00:00+00:00",
                    "result": {"errors": ["failure"]},
                }
            ],
        }

    async def fake_backfill(session_id, *, context, session_metadata):  # noqa: ANN001
        _ = (session_id, context)
        return dict(session_metadata)

    async def fake_pipeline_name(bot_id):  # noqa: ANN001
        return "Coding Pipeline"

    async def fake_invoke(session_id, *, session, operator_message, recent_messages):  # noqa: ANN001
        _ = (session_id, session, operator_message, recent_messages)
        return {"ok": False, "error": "vertex 404", "hint": "Platform brain backend failed."}

    launch_calls = {"count": 0}

    async def fake_launch(**kwargs):  # noqa: ANN001
        _ = kwargs
        launch_calls["count"] += 1
        return "orch-next"

    runtime._resolve_context = fake_resolve_context  # type: ignore[method-assign]
    runtime._backfill_seed_binding_from_context = fake_backfill  # type: ignore[method-assign]
    runtime._pipeline_name_for_bot_id = fake_pipeline_name  # type: ignore[method-assign]
    runtime._invoke_platform_brain = fake_invoke  # type: ignore[method-assign]
    runtime._launch_autonomous_orchestration = fake_launch  # type: ignore[method-assign]
    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REQUIRE_BRAIN_FOR_AUTONOMY", "1")

    await runtime._run_autonomous_pipeline_tuner(
        session["id"],
        session=session,
        snapshot={
            "orchestration_id": "orch-1",
            "status_counts": {"failed": 1},
            "active_tasks": [],
            "runtime_state": {"task_total": 1},
        },
    )

    updated = await store.get_session(session["id"])
    assert str((updated or {}).get("status") or "") == "ready"
    metadata = (updated or {}).get("metadata") if isinstance((updated or {}).get("metadata"), dict) else {}
    assert str(metadata.get("checkpoint_reason") or "") == "platform_brain_unavailable"
    assert launch_calls["count"] == 0


@pytest.mark.anyio
async def test_autonomous_tuner_requires_configuration_approval_before_refining_live_bot(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    original_prompt = "Preserve this live prompt."
    await bot_registry.register(
        Bot.model_validate(
            {
                "id": "pm-orchestrator",
                "name": "PM Orchestrator",
                "role": "assistant",
                "enabled": True,
                "system_prompt": original_prompt,
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
            }
        )
    )
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "pipeline_bot_id": "pm-orchestrator",
            "pipeline_name": "Coding Pipeline",
            "autonomous_goal": "Produce a complete validated artifact.",
        },
        assignment_id="assign-1",
        run_id="run-1",
        orchestration_id="orch-1",
    )

    async def fake_resolve_context(_session):  # noqa: ANN001
        return {
            "assignment_id": "assign-1",
            "run_id": "run-1",
            "orchestration_id": "orch-1",
            "graph": {"nodes": [{"id": "pm-orchestrator", "title": "PM Orchestrator"}], "edges": []},
            "tasks": [
                {
                    "id": "task-1",
                    "bot_id": "pm-orchestrator",
                    "status": "failed",
                    "updated_at": "2026-04-08T14:00:00+00:00",
                    "result": {"errors": ["failure"]},
                }
            ],
        }

    async def fake_backfill(session_id, *, context, session_metadata):  # noqa: ANN001
        _ = (session_id, context)
        return dict(session_metadata)

    async def fake_pipeline_name(_bot_id):  # noqa: ANN001
        return "Coding Pipeline"

    async def fake_invoke(session_id, *, session, operator_message, recent_messages):  # noqa: ANN001
        _ = (session_id, session, operator_message, recent_messages)
        return {"ok": True, "reply": "Evaluation reviewed.", "actions": []}

    launch_calls = {"count": 0}

    async def fake_launch(**kwargs):  # noqa: ANN001
        _ = kwargs
        launch_calls["count"] += 1
        return "orch-next"

    runtime._resolve_context = fake_resolve_context  # type: ignore[method-assign]
    runtime._backfill_seed_binding_from_context = fake_backfill  # type: ignore[method-assign]
    runtime._pipeline_name_for_bot_id = fake_pipeline_name  # type: ignore[method-assign]
    runtime._invoke_platform_brain = fake_invoke  # type: ignore[method-assign]
    runtime._launch_autonomous_orchestration = fake_launch  # type: ignore[method-assign]
    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED", "1")
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)

    await runtime._run_autonomous_pipeline_tuner(
        session["id"],
        session=session,
        snapshot={
            "orchestration_id": "orch-1",
            "status_counts": {"failed": 1},
            "active_tasks": [],
            "runtime_state": {"task_total": 1},
        },
    )

    unchanged = await bot_registry.get("pm-orchestrator")
    assert unchanged.system_prompt == original_prompt
    assert launch_calls["count"] == 0
    suites = await store.list_test_suites(session_id=session["id"], pipeline_bot_id="pm-orchestrator", limit=10)
    assert len(suites) == 1
    proposals = await store.list_patch_proposals(session["id"])
    assert len(proposals) == 1
    assert proposals[0]["after_state"]["proposal_kind"] == "bot_system_prompt_refinement"
    assert proposals[0]["after_state"]["requires_direct_operator_edit"] is True
    assert "[[NEXUS_PLATFORM_AI_AUTOTUNE_START]]" in proposals[0]["after_state"]["suggested_autotune_block"]
    updated = await store.get_session(session["id"])
    assert str((updated or {}).get("status") or "") == "ready"
    metadata = (updated or {}).get("metadata") if isinstance((updated or {}).get("metadata"), dict) else {}
    assert metadata.get("checkpoint_reason") == "configuration_proposal_pending"
    approval = await runtime.approve_patch_proposal(session["id"], proposals[0]["id"], operator_id="operator")
    assert approval["status"] == "approved"
    assert approval["detail"] == "approved_direct_operator_edit_required"
    assert (await bot_registry.get("pm-orchestrator")).system_prompt == original_prompt


@pytest.mark.anyio
async def test_bot_refinement_write_boundary_requires_configuration_mutation_grant(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    await bot_registry.register(
        Bot.model_validate(
            {
                "id": "pm-orchestrator",
                "name": "PM Orchestrator",
                "role": "assistant",
                "enabled": True,
                "system_prompt": "Keep unchanged.",
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
            }
        )
    )
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)

    result = await runtime._apply_bot_refinement(
        session_id="session-1",
        pipeline_bot_id="pm-orchestrator",
        iteration=1,
        goal="Improve quality.",
        evaluation={"status": "failed", "score": 0.0, "tests": []},
    )

    assert result == {
        "updated": False,
        "reason": "configuration_mutations_disabled",
        "proposal_only": True,
    }
    assert (await bot_registry.get("pm-orchestrator")).system_prompt == "Keep unchanged."


@pytest.mark.anyio
async def test_handle_stall_without_halt_checkpoints_ready_when_brain_unavailable(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "pipeline_bot_id": "pm-orchestrator",
            "autonomous_last_brain_error": "vertex 404",
        },
        orchestration_id="orch-1",
    )
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REQUIRE_BRAIN_FOR_AUTONOMY", "1")
    await runtime._handle_stall_without_halt(
        session["id"],
        session=session,
        snapshot={"orchestration_id": "orch-1", "runtime_state": {"task_total": 1}, "status_counts": {"failed": 1}},
        reason="stalled_duplicate_actions",
    )
    updated = await store.get_session(session["id"])
    assert str((updated or {}).get("status") or "") == "ready"
    events = await store.list_events(session["id"], limit=100)
    actions = [str((row.get("payload") or {}).get("action") or "") for row in events if isinstance(row, dict)]
    assert "session_checkpoint_ready" in actions
    assert "autonomous_replan" not in actions


@pytest.mark.anyio
async def test_session_loop_skips_stall_logic_after_autonomous_step_moves_ready(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={"autonomous_enabled": True, "pipeline_bot_id": "pm-orchestrator"},
        orchestration_id="orch-1",
    )

    check_calls = {"count": 0}

    async def fake_process(_sid):  # noqa: ANN001
        return None

    async def fake_snapshot(_session):  # noqa: ANN001
        return {
            "tick": 1,
            "phase": "evaluate",
            "active_action": "terminal_failure_detected",
            "detail": "terminal failure",
            "heartbeat_detail": "terminal failure",
            "signature": "sig-1",
            "orchestration_id": "orch-1",
            "status_counts": {"failed": 1},
            "active_tasks": [],
            "runtime_state": {"orchestration_id": "orch-1", "task_total": 1, "status_counts": {"failed": 1}, "active_tasks": []},
        }

    async def fake_run_tuner(session_id, *, session, snapshot):  # noqa: ANN001
        _ = (session, snapshot)
        await store.update_session(session_id, status="ready", metadata={"checkpoint_reason": "platform_brain_unavailable"})

    async def fake_finalize(**kwargs):  # noqa: ANN001
        _ = kwargs
        return None

    async def fake_check(_sid, **kwargs):  # noqa: ANN001
        _ = kwargs
        check_calls["count"] += 1
        return "stalled_duplicate_actions"

    runtime._process_operator_messages = fake_process  # type: ignore[method-assign]
    runtime._build_progress_snapshot = fake_snapshot  # type: ignore[method-assign]
    runtime._run_autonomous_pipeline_tuner = fake_run_tuner  # type: ignore[method-assign]
    runtime._finalize_autonomous_session_if_terminal = fake_finalize  # type: ignore[method-assign]
    runtime._check_should_halt_as_stalled = fake_check  # type: ignore[method-assign]

    loop_task = asyncio.create_task(runtime._session_loop(session["id"]))
    await asyncio.sleep(0.25)
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task

    assert check_calls["count"] == 0

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
    monkeypatch.setenv("NEXUS_PLATFORM_AI_LOCAL_SUBPROCESS_RUNNERS_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_RUNNER_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("NEXUS_PLATFORM_AI_RUNNER_EXECUTABLE_ALLOWLIST", sys.executable)
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
async def test_repo_edit_runner_requires_isolated_worker_when_local_runner_is_not_explicitly_enabled(tmp_path, monkeypatch):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="tester@example.com",
        privileged=True,
        metadata={},
    )
    monkeypatch.setenv("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "tester@example.com")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD", f'"{sys.executable}" -c "raise SystemExit(99)"')
    result = await runtime.start_repo_edit_run(
        session["id"],
        requested_by="tester@example.com",
        instruction="This must not run locally",
        external=False,
    )
    assert str(result.get("status") or "") == "disabled"
    assert "isolated worker runner" in str(result.get("detail") or "")


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
    monkeypatch.setenv("NEXUS_PLATFORM_AI_LOCAL_SUBPROCESS_RUNNERS_ENABLED", "1")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_RUNNER_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("NEXUS_PLATFORM_AI_RUNNER_EXECUTABLE_ALLOWLIST", sys.executable)
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


@pytest.mark.anyio
async def test_resolve_context_prefers_explicit_orchestration_over_stale_run_id(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        assignment_id="assign-old",
        run_id="run-old",
        orchestration_id="orch-new",
        metadata={"pipeline_bot_id": "pm-orchestrator"},
    )

    calls = {"by_orch": 0, "by_run": 0, "by_assignment": 0}

    class FakeRunStore:
        async def get_run_by_orchestration(self, orchestration_id: str):
            _ = orchestration_id
            calls["by_orch"] += 1
            return None

        async def get_run(self, run_id: str):
            _ = run_id
            calls["by_run"] += 1
            return {
                "id": "run-old",
                "assignment_id": "assign-old",
                "orchestration_id": "orch-old",
            }

        async def get_latest_run_for_assignment(self, assignment_id: str):
            _ = assignment_id
            calls["by_assignment"] += 1
            return None

    runtime._run_store = FakeRunStore()  # type: ignore[assignment]
    context = await runtime._resolve_context(session)
    assert str(context.get("orchestration_id") or "") == "orch-new"
    assert context.get("run_id") is None
    assert calls["by_orch"] == 1
    assert calls["by_run"] == 0


@pytest.mark.anyio
async def test_launch_autonomous_orchestration_propagates_project_and_conversation_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED", "true")
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "pipeline_bot_id": "pm-orchestrator",
            "project_id": "globeiq",
            "conversation_id": "conv-123",
            "seed_binding": {
                "seed_project_id": "globeiq",
                "seed_conversation_id": "conv-123",
            },
        },
    )

    captured = {"metadata": None}

    class _Created:
        id = "task-created-1"

    class FakeTaskManager:
        async def create_task(self, *, bot_id, payload, metadata):  # noqa: ANN001
            _ = (bot_id, payload)
            captured["metadata"] = metadata
            return _Created()

    runtime._task_manager = FakeTaskManager()  # type: ignore[assignment]
    runtime._bot_registry = None  # force fallback payload path

    launched = await runtime._launch_autonomous_orchestration(
        session_id=session["id"],
        pipeline_bot_id="pm-orchestrator",
        pipeline_name="Coding Pipeline",
        goal="Run and refine.",
        reason="refinement_iteration",
        iteration=1,
    )
    assert launched
    metadata = captured.get("metadata")
    assert metadata is not None
    assert str(getattr(metadata, "project_id", "") or "") == "globeiq"
    assert str(getattr(metadata, "conversation_id", "") or "") == "conv-123"


@pytest.mark.anyio
async def test_derive_seed_binding_from_context_extracts_lineage_instruction(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    context = {
        "assignment_id": "assign-1",
        "run_id": "run-1",
        "orchestration_id": "orch-1",
        "tasks": [
            {
                "id": "task-root",
                "created_at": "2026-04-08T01:00:00+00:00",
                "metadata": {
                    "workflow_root_task_id": "task-root",
                    "source": "chat_assign",
                    "project_id": "globeiq",
                    "conversation_id": "conv-root",
                },
                "payload": {
                    "instruction": "please go the repo as context and go through everything we have built...",
                    "node_overrides": {"pm-ui-tester": {"skip": False}},
                },
            }
        ],
    }
    derived = runtime._derive_seed_binding_from_context(context=context, session_metadata={})
    assert str(derived.get("seed_assignment_id") or "") == "assign-1"
    assert str(derived.get("seed_run_id") or "") == "run-1"
    assert str(derived.get("seed_orchestration_id") or "") == "orch-1"
    assert str(derived.get("seed_project_id") or "") == "globeiq"
    assert str(derived.get("seed_conversation_id") or "") == "conv-root"
    assert "please go the repo as context" in str(derived.get("instruction") or "").lower()
    assert isinstance(derived.get("node_overrides"), dict)
    assert str(derived.get("trigger_source") or "") == "chat_assign"


@pytest.mark.anyio
async def test_backfill_seed_binding_merges_missing_fields_only(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    runtime = PlatformAISessionRuntime(store)
    session = await store.create_session(
        mode="pipeline_tuner",
        status="running",
        metadata={
            "autonomous_enabled": True,
            "seed_binding": {
                "instruction": "keep this original seed instruction",
                "seed_assignment_id": "assign-existing",
            },
        },
    )
    context = {
        "assignment_id": "assign-new",
        "run_id": "run-new",
        "orchestration_id": "orch-new",
        "tasks": [
            {
                "id": "task-root",
                "created_at": "2026-04-08T01:00:00+00:00",
                "metadata": {
                    "workflow_root_task_id": "task-root",
                    "source": "chat_assign",
                    "project_id": "globeiq",
                    "conversation_id": "conv-root",
                },
                "payload": {"instruction": "new instruction should not overwrite existing"},
            }
        ],
    }
    merged = await runtime._backfill_seed_binding_from_context(
        session["id"],
        context=context,
        session_metadata=session.get("metadata") if isinstance(session.get("metadata"), dict) else {},
    )
    binding = merged.get("seed_binding") if isinstance(merged.get("seed_binding"), dict) else {}
    assert str(binding.get("instruction") or "") == "keep this original seed instruction"
    assert str(binding.get("seed_assignment_id") or "") == "assign-existing"
    assert str(binding.get("seed_run_id") or "") == "run-new"
    assert str(binding.get("seed_orchestration_id") or "") == "orch-new"
    assert str(binding.get("seed_project_id") or "") == "globeiq"
    assert str(binding.get("seed_conversation_id") or "") == "conv-root"


@pytest.mark.anyio
async def test_disabled_configuration_mutation_creates_disabled_bot_proposal(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "proposal-only-bot",
            "name": "Proposal Only Bot",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert len(actions) == 1
    proposal_id = str((actions[0].get("result") or {}).get("proposal_id") or "")
    assert proposal_id
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
    assert after_state.get("proposal_kind") == "bot_configuration"
    assert bool((after_state.get("bot") or {}).get("enabled")) is False
    with pytest.raises(BotNotFoundError):
        await bot_registry.get("proposal-only-bot")


@pytest.mark.anyio
async def test_operator_approval_applies_proposed_bot_without_auto_activation(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "operator")
    monkeypatch.delenv("NEXUS_PLATFORM_AI_AUTO_ACTIVATE_BOTS", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        worker_registry=WorkerRegistry(db_path=str(tmp_path / "workers.db")),
        connection_resolver=ConnectionResolver(db_path=str(tmp_path / "connections.db")),
    )
    session = await store.create_session(mode="bot_creator", status="running", operator_id="operator")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "approved-disabled-bot",
            "name": "Approved Disabled Bot",
            "role": "assistant",
            "enabled": True,
            "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
        },
    }
    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )
    proposal_id = str((((result.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    blocked = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert blocked.get("detail") == "proposal_preflight_required"
    preflight = await runtime.preflight_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert preflight.get("status") == "ready"
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    stale_after_state = dict(proposal["after_state"])
    stale_preflight = dict(stale_after_state["preflight"])
    stale_preflight["checked_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    stale_after_state["preflight"] = stale_preflight
    await store.update_patch_proposal_after_state(proposal_id, stale_after_state)
    stale = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert stale.get("detail") == "proposal_preflight_stale"
    refreshed = await runtime.preflight_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert refreshed.get("status") == "ready"
    approval = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="operator")

    assert approval.get("status") == "applied"
    created = await bot_registry.get("approved-disabled-bot")
    assert created.enabled is False
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    assert proposal.get("status") == "applied"


@pytest.mark.anyio
async def test_operator_approval_requires_matching_allowlisted_session_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "owner@example.com")
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="owner@example.com",
    )
    created = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(
            {
                "platform_ai_action": "upsert_bot",
                "bot": {
                    "id": "owner-gated-bot",
                    "name": "Owner Gated Bot",
                    "role": "assistant",
                    "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
                },
            }
        ),
    )
    proposal_id = str((((created.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    denied = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="other@example.com")

    assert denied.get("status") == "blocked"
    assert denied.get("detail") == "session_owner_mismatch"
    with pytest.raises(BotNotFoundError):
        await bot_registry.get("owner-gated-bot")


@pytest.mark.anyio
async def test_preflight_validates_staged_bot_without_registering_or_enabling_it(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    worker_registry = WorkerRegistry(db_path=str(tmp_path / "workers.db"))
    await worker_registry.register(
        Worker.model_validate(
            {
                "id": "preflight-worker",
                "name": "Preflight Worker",
                "host": "127.0.0.1",
                "port": 9911,
                "status": "online",
                "capabilities": [
                    {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]}
                ],
            }
        )
    )
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        worker_registry=worker_registry,
        connection_resolver=ConnectionResolver(db_path=str(tmp_path / "connections.db")),
        worker_probe_store=WorkerProbeStore(db_path=str(tmp_path / "probes.db")),
    )
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "preflight-bot",
            "name": "Preflight Bot",
            "role": "assistant",
            "enabled": True,
            "backends": [
                {
                    "type": "remote_llm",
                    "provider": "ollama_cloud",
                    "model": "glm-5.2:cloud",
                    "worker_id": "preflight-worker",
                }
            ],
        },
    }
    created = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )
    proposal_id = str((((created.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    result = await runtime.preflight_patch_proposal(session["id"], proposal_id, operator_id="operator")

    assert result.get("status") == "ready"
    assert result["preflight"]["ready_for_operator_review"] is True
    assert result["preflight"]["manual_activation_required"] is True
    with pytest.raises(BotNotFoundError):
        await bot_registry.get("preflight-bot")
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    assert proposal.get("status") == "proposed"
    assert bool(((proposal.get("after_state") or {}).get("bot") or {}).get("enabled")) is False


@pytest.mark.anyio
async def test_preflight_blocks_unavailable_worker_without_mutating_proposal(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        worker_registry=WorkerRegistry(db_path=str(tmp_path / "workers.db")),
        connection_resolver=ConnectionResolver(db_path=str(tmp_path / "connections.db")),
    )
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "blocked-preflight-bot",
            "name": "Blocked Preflight Bot",
            "role": "assistant",
            "backends": [
                {
                    "type": "remote_llm",
                    "provider": "ollama_cloud",
                    "model": "glm-5.2:cloud",
                    "worker_id": "missing-worker",
                }
            ],
        },
    }
    created = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )
    proposal_id = str((((created.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    result = await runtime.preflight_patch_proposal(session["id"], proposal_id)

    assert result.get("status") == "blocked"
    assert result["preflight"]["ready_for_operator_review"] is False
    assert result["preflight"]["readiness"]["ready"] is False
    assert any("not registered" in str(item.get("message") or "") for item in result["preflight"]["readiness"]["checks"])
    assert await store.get_patch_proposal(proposal_id) is not None


@pytest.mark.anyio
async def test_platform_ai_preflight_api_keeps_proposal_disabled(cp_client, cp_app, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    runtime = PlatformAISessionRuntime(
        cp_app.state.platform_ai_session_store,
        bot_registry=cp_app.state.bot_registry,
        worker_registry=cp_app.state.worker_registry,
        connection_resolver=cp_app.state.connection_resolver,
        worker_probe_store=cp_app.state.worker_probe_store,
    )
    cp_app.state.platform_ai_runtime = runtime
    session = await cp_app.state.platform_ai_session_store.create_session(mode="bot_creator", status="running")
    created = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(
            {
                "platform_ai_action": "upsert_bot",
                "bot": {
                    "id": "api-preflight-bot",
                    "name": "API Preflight Bot",
                    "role": "assistant",
                    "backends": [
                        {"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}
                    ],
                },
            }
        ),
    )
    proposal_id = str((((created.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    response = await cp_client.post(
        f"/v1/platform-ai/sessions/{session['id']}/proposals/{proposal_id}/preflight",
        json={},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "ready"
    assert result["preflight"]["ready_for_operator_review"] is True
    with pytest.raises(BotNotFoundError):
        await cp_app.state.bot_registry.get("api-preflight-bot")


@pytest.mark.anyio
async def test_operator_approval_refuses_to_change_active_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "operator")
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    await bot_registry.register(
        Bot.model_validate(
            {
                "id": "active-bot",
                "name": "Active Bot",
                "role": "assistant",
                "enabled": True,
                "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4o-mini"}],
            }
        )
    )
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        worker_registry=WorkerRegistry(db_path=str(tmp_path / "workers.db")),
        connection_resolver=ConnectionResolver(db_path=str(tmp_path / "connections.db")),
    )
    session = await store.create_session(mode="bot_creator", status="running", operator_id="operator")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "active-bot",
            "name": "Changed Active Bot",
            "role": "assistant",
            "backends": [{"type": "cloud_api", "provider": "openai", "model": "gpt-4.1"}],
        },
    }
    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )
    proposal_id = str((((result.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))

    preflight = await runtime.preflight_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert preflight.get("status") == "ready"
    approval = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="operator")

    assert approval.get("status") == "blocked"
    assert approval.get("detail") == "active_bot_update_requires_direct_operator_edit"
    unchanged = await bot_registry.get("active-bot")
    assert unchanged.name == "Active Bot"


@pytest.mark.anyio
async def test_configuration_proposal_rejects_cli_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "cli-proposal-bot",
            "name": "CLI Proposal Bot",
            "role": "assistant",
            "backends": [{"type": "cli", "provider": "cli", "model": "claude", "command": "claude -p"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert len(actions) == 1
    detail = str((actions[0].get("result") or {}).get("detail") or "")
    assert detail == "proposal_backend_type_not_approvable:cli"
    assert await store.list_patch_proposals(session["id"]) == []


@pytest.mark.anyio
async def test_specialist_proposal_uses_approved_claude_ollama_profile_and_stays_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "propose_specialist_bot",
        "specialist": {
            "kind": "code_reviewer",
            "name": "Claude via Ollama Reviewer",
            "activate": True,
            "allow_repo_writes": True,
            "backends": [
                {
                    "type": "cli",
                    "worker_id": "coding-worker",
                    "provider": "cli",
                    "model": "claude",
                }
            ],
            "cli_command_profile": "claude_ollama_json",
            "cli_runtime_model": "glm-5.2:cloud",
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert len(actions) == 1
    proposal_id = str((actions[0].get("result") or {}).get("proposal_id") or "")
    assert proposal_id
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
    bot = after_state.get("bot") if isinstance(after_state.get("bot"), dict) else {}
    specialist_request = after_state.get("specialist_request") if isinstance(after_state.get("specialist_request"), dict) else {}
    assert bot["enabled"] is False
    assert bot["execution_policy"]["repo_output_mode"] == "deny"
    assert bot["backends"][0]["command"] == "claude -p --model glm-5.2:cloud --output-format json"
    assert specialist_request["activate"] is False
    assert specialist_request["allow_repo_writes"] is False
    preflight = await runtime.preflight_patch_proposal(session["id"], proposal_id)
    assert preflight["preflight"]["safety_error"] is None
    with pytest.raises(BotNotFoundError):
        await bot_registry.get("claude-via-ollama-reviewer")


@pytest.mark.anyio
async def test_specialist_proposal_is_limited_to_bot_creator_sessions(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="bot_tuner",
        status="running",
        metadata={"target_bot_id": "existing-bot"},
    )
    directive = {
        "platform_ai_action": "propose_specialist_bot",
        "specialist": {
            "kind": "researcher",
            "name": "Out Of Scope Researcher",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "glm-5.2:cloud"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert len(actions) == 1
    assert (actions[0].get("result") or {}).get("detail") == "specialist_proposal_requires_bot_creator_mode"
    assert await store.list_patch_proposals(session["id"]) == []


@pytest.mark.anyio
async def test_specialist_proposal_is_bound_to_its_creator_session_project(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={"project_id": "globeiq"},
    )
    directive = {
        "platform_ai_action": "propose_specialist_bot",
        "specialist": {
            "kind": "researcher",
            "name": "Project Bound Researcher",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "glm-5.2:cloud"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    proposal_id = str((actions[0].get("result") or {}).get("proposal_id") or "")
    assert proposal_id
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
    specialist_request = after_state.get("specialist_request") if isinstance(after_state.get("specialist_request"), dict) else {}
    bot = after_state.get("bot") if isinstance(after_state.get("bot"), dict) else {}

    assert specialist_request["project_id"] == "globeiq"
    assert bot["routing_rules"]["specialist"]["project_id"] == "globeiq"


@pytest.mark.anyio
async def test_specialist_proposal_rejects_project_scope_mismatch(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={"project_id": "globeiq"},
    )
    directive = {
        "platform_ai_action": "propose_specialist_bot",
        "specialist": {
            "kind": "researcher",
            "name": "Cross Project Researcher",
            "project_id": "another-project",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "glm-5.2:cloud"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert (actions[0].get("result") or {}).get("detail") == "specialist_project_scope_mismatch"
    assert await store.list_patch_proposals(session["id"]) == []


@pytest.mark.anyio
async def test_specialist_proposal_rejects_unknown_project_binding(tmp_path):
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    project_registry = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        project_registry=project_registry,
    )
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        metadata={"project_id": "missing-project"},
    )
    directive = {
        "platform_ai_action": "propose_specialist_bot",
        "specialist": {
            "kind": "researcher",
            "name": "Unknown Project Researcher",
            "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "glm-5.2:cloud"}],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert (actions[0].get("result") or {}).get("detail") == "project_not_found"
    assert await store.list_patch_proposals(session["id"]) == []


@pytest.mark.anyio
async def test_schedule_proposal_is_paused_project_bound_and_owner_approved(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "operator")
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    await bot_registry.register(
        Bot(
            id="project-monitor",
            name="Project Monitor",
            role="monitor",
            enabled=False,
            backends=[{"type": "cloud_api", "provider": "ollama_cloud", "model": "glm-5.2:cloud"}],
            routing_rules={
                "specialist": {"project_id": "globeiq", "kind": "monitoring", "risk_level": "read_only"},
                "worker_profile": {"role": "monitor", "task_scope": "read-only-monitoring", "can_edit": False},
            },
        )
    )
    schedule_engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=object(),
        db_path=str(tmp_path / "schedules.db"),
    )
    runtime = PlatformAISessionRuntime(
        store,
        bot_registry=bot_registry,
        agent_schedule_engine=schedule_engine,
    )
    session = await store.create_session(
        mode="bot_creator",
        status="running",
        operator_id="operator",
        metadata={"project_id": "globeiq"},
    )

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(
            {
                "platform_ai_action": "propose_schedule",
                "schedule": {
                    "name": "Nightly project monitor",
                    "cron_expression": "0 2 * * *",
                    "timezone": "America/Chicago",
                    "prompt": "Prepare a read-only operations summary.",
                    "target_bot_id": "project-monitor",
                    "status": "paused",
                },
            }
        ),
    )

    proposal_id = str((((result.get("actions") or [])[0].get("result") or {}).get("proposal_id") or ""))
    assert proposal_id
    proposal = await store.get_patch_proposal(proposal_id)
    assert proposal is not None
    after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
    schedule_payload = after_state.get("schedule") if isinstance(after_state.get("schedule"), dict) else {}
    assert schedule_payload["status"] == "paused"
    assert schedule_payload["project_id"] == "globeiq"
    assert schedule_payload["metadata"]["mutation_safe"] is True

    preflight = await runtime.preflight_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert preflight["status"] == "ready"
    assert preflight["preflight"]["runtime_readiness"]["deferred"] is True

    approval = await runtime.approve_patch_proposal(session["id"], proposal_id, operator_id="operator")
    assert approval["status"] == "applied"
    created = await schedule_engine.get_schedule(str(approval["schedule"]["id"]))
    assert created is not None
    assert created["status"] == "paused"
    assert created["target_bot_id"] == "project-monitor"
    assert created["project_id"] == "globeiq"


@pytest.mark.anyio
async def test_configuration_proposal_rejects_direct_credential_value(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED", raising=False)
    store = PlatformAISessionStore(db_path=str(tmp_path / "platform_ai.db"))
    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    runtime = PlatformAISessionRuntime(store, bot_registry=bot_registry)
    session = await store.create_session(mode="bot_creator", status="running")
    directive = {
        "platform_ai_action": "upsert_bot",
        "bot": {
            "id": "credential-proposal-bot",
            "name": "Credential Proposal Bot",
            "role": "assistant",
            "backends": [
                {
                    "type": "cloud_api",
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "api_key_ref": "sk-live-secret",
                }
            ],
        },
    }

    result = await runtime._apply_operator_directives(
        session["id"],
        session=session,
        content=json.dumps(directive),
    )

    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    assert len(actions) == 1
    assert (actions[0].get("result") or {}).get("detail") == "proposal_direct_credential_not_allowed"
    assert await store.list_patch_proposals(session["id"]) == []
