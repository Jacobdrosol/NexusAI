import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.bot_blueprints import (
    SpecialistBlueprintRequest,
    build_specialist_bot,
    list_specialist_blueprints,
)
from shared.bot_policy import validate_bot_configuration
from shared.models import BackendConfig, BotContextAccess, BotExecutionPolicy


def _backend() -> BackendConfig:
    return BackendConfig(
        type="cloud_api",
        provider="ollama_cloud",
        model="qwen3-coder:480b-cloud",
        api_key_ref="ollama-cloud",
    )


def test_specialist_catalog_exposes_specialized_roles_without_secrets():
    catalog = list_specialist_blueprints()

    researcher = next(item for item in catalog if item["kind"] == "researcher")
    implementer = next(item for item in catalog if item["kind"] == "code_implementer")

    assert researcher["risk_level"] == "read_only"
    assert implementer["supports_repo_writes"] is True
    assert all("api_key" not in item for item in catalog)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "researcher",
            "name": "Unsafe Credential Field",
            "api_key": "secret-value",
            "backends": [_backend().model_dump()],
        },
        {
            "kind": "researcher",
            "name": "Unsafe Backend Credential Field",
            "backends": [{**_backend().model_dump(), "access_token": "secret-value"}],
        },
        {
            "kind": "researcher",
            "name": "Unsafe Credential Reference",
            "backends": [{**_backend().model_dump(), "api_key_ref": "sk-live-secret"}],
        },
    ],
)
def test_specialist_blueprint_rejects_raw_credential_material(payload):
    with pytest.raises(ValueError, match="credential|api_key_ref"):
        SpecialistBlueprintRequest.model_validate(payload)


def test_specialist_blueprint_rejects_raw_credential_reference_from_backend_model():
    with pytest.raises(ValueError, match="api_key_ref"):
        SpecialistBlueprintRequest(
            kind="researcher",
            name="Unsafe Backend Object",
            backends=[
                BackendConfig(
                    type="cloud_api",
                    provider="ollama_cloud",
                    model="qwen3.5:cloud",
                    api_key_ref="sk-live-secret",
                )
            ],
        )


def test_assessment_and_lesson_specialists_are_bounded_by_default():
    catalog = list_specialist_blueprints()
    catalog_by_kind = {item["kind"]: item for item in catalog}

    assert catalog_by_kind["question_bank_reviewer"]["risk_level"] == "read_only"
    assert catalog_by_kind["question_bank_writer"]["risk_level"] == "draft_only"
    assert catalog_by_kind["lesson_block_reviewer"]["risk_level"] == "read_only"
    assert catalog_by_kind["lesson_block_builder"]["risk_level"] == "draft_only"

    bot = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="question_bank_writer",
            name="Question Draft Writer",
            backends=[_backend()],
        )
    )
    assert bot.enabled is False
    assert bot.execution_policy.repo_output_mode == "deny"
    assert "semantic novelty" in bot.system_prompt.lower()
    assert "admin ui" in bot.system_prompt.lower()


def test_content_writer_blueprint_is_disabled_and_draft_only_by_default():
    bot = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="content_writer",
            name="Course Lesson Writer",
            mission="Draft lessons for review.",
            project_id="globeiq",
            backends=[_backend()],
        )
    )

    assert bot.id == "course-lesson-writer"
    assert bot.enabled is False
    assert bot.execution_policy.repo_output_mode == "deny"
    assert bot.routing_rules["specialist"]["risk_level"] == "draft_only"
    assert bot.routing_rules["input_contract"]["required_fields"] == ["instruction"]
    assert bot.routing_rules["output_contract"]["non_empty_fields"] == ["status", "draft"]
    assert bot.routing_rules["output_contract"]["allow_blocked_status"] is True
    assert "draft" in bot.workflow.required_output_fields
    assert "never publish" in bot.system_prompt.lower()


def test_operations_manager_blueprint_stays_read_only_and_requests_operator_decisions():
    bot = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="operations_manager",
            name="GlobeIQ Operations Manager",
            project_id="globeiq",
            backends=[_backend()],
        )
    )

    assert bot.enabled is False
    assert bot.execution_policy.repo_output_mode == "deny"
    assert bot.routing_rules["specialist"]["risk_level"] == "read_only"
    assert bot.routing_rules["output_contract"]["required_fields"] == [
        "status",
        "operational_summary",
        "worker_reports",
        "risks",
        "decisions_needed",
        "handoff_notes",
    ]
    assert "do not enable workers" in bot.system_prompt.lower()


def test_code_implementer_requires_explicit_write_escalation():
    base_request = {
        "kind": "code_implementer",
        "name": "Scoped Implementer",
        "backends": [_backend()],
        "activate": True,
    }

    read_only_bot = build_specialist_bot(SpecialistBlueprintRequest(**base_request))
    writer_bot = build_specialist_bot(
        SpecialistBlueprintRequest(**base_request, allow_repo_writes=True)
    )

    assert read_only_bot.enabled is True
    assert read_only_bot.execution_policy.workspace_context_injection is True
    assert read_only_bot.execution_policy.repo_output_mode == "deny"
    assert writer_bot.execution_policy.repo_output_mode == "allow"
    assert writer_bot.execution_policy.inline_coding_default is True
    assert writer_bot.routing_rules["specialist"]["repo_write_granted"] is True
    assert writer_bot.routing_rules["specialist"]["operator_review_required"] is True


def test_specialist_policy_preserves_template_tool_and_write_boundaries():
    writer = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="content_writer",
            name="Draft-Only Writer",
            backends=[_backend()],
        )
    )
    unsafe_writer = writer.model_copy(
        update={
            "execution_policy": writer.execution_policy.model_copy(
                update={"repo_output_mode": "allow"}
            )
        }
    )

    writer_errors = validate_bot_configuration(unsafe_writer)
    assert any("guarded_write code_implementer" in error for error in writer_errors)
    assert any("repository-write grant" in error for error in writer_errors)

    implementer = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="code_implementer",
            name="Scoped Implementer",
            allow_repo_writes=True,
            backends=[_backend()],
        )
    )
    unsafe_routing_rules = dict(implementer.routing_rules)
    unsafe_routing_rules["specialist"] = {
        **unsafe_routing_rules["specialist"],
        "operator_review_required": False,
    }
    unsafe_implementer = implementer.model_copy(
        update={
            "context_access": BotContextAccess(receives=["instruction"], can_self_serve=[]),
            "routing_rules": unsafe_routing_rules,
            "execution_policy": BotExecutionPolicy(
                repo_output_mode="allow",
                workspace_context_injection=True,
                inline_coding_default=True,
            ),
        }
    )

    implementer_errors = validate_bot_configuration(unsafe_implementer)
    assert any("does not declare repo self-service access" in error for error in implementer_errors)
    assert any("requires injected workspace context and repo self-service access" in error for error in implementer_errors)
    assert any("repository-write grant and operator review marker" in error for error in implementer_errors)


def test_cli_specialist_uses_an_approved_claude_ollama_profile():
    bot = build_specialist_bot(
        SpecialistBlueprintRequest(
            kind="code_reviewer",
            name="Claude Review Worker",
            backends=[
                BackendConfig(
                    type="cli",
                    worker_id="coding-worker",
                    provider="cli",
                    model="claude",
                )
            ],
            cli_command_profile="claude_ollama_json",
            cli_runtime_model="glm-5.2:cloud",
        )
    )

    assert bot.backends[0].command == "claude -p --model glm-5.2:cloud --output-format json"


def test_cli_specialist_rejects_unapproved_commands_and_models():
    request = SpecialistBlueprintRequest(
        kind="code_reviewer",
        name="Unsafe Claude Worker",
        backends=[
            BackendConfig(
                type="cli",
                worker_id="coding-worker",
                provider="cli",
                model="claude",
                command="claude -p --unsafe",
            )
        ],
        cli_command_profile="claude_ollama_json",
        cli_runtime_model="glm-5.2:cloud",
    )

    with pytest.raises(ValueError, match="generated from an approved profile"):
        build_specialist_bot(request)


@pytest.mark.anyio
async def test_specialist_blueprint_api_previews_and_registers_disabled_bot(cp_app):
    payload = {
        "kind": "researcher",
        "name": "Release Researcher",
        "backends": [
            {
                "type": "cloud_api",
                "provider": "ollama_cloud",
                "model": "qwen3.5:cloud",
                "api_key_ref": "ollama-cloud",
            }
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        catalog_response = await client.get("/v1/bot-blueprints")
        preview_response = await client.post("/v1/bot-blueprints/preview", json=payload)
        create_response = await client.post("/v1/bot-blueprints/create", json=payload)
        duplicate_response = await client.post("/v1/bot-blueprints/create", json=payload)

    assert catalog_response.status_code == 200
    assert any(item["kind"] == "researcher" for item in catalog_response.json()["blueprints"])
    assert preview_response.status_code == 200
    assert preview_response.json()["bot"]["enabled"] is False
    assert create_response.status_code == 200
    assert create_response.json()["bot"]["id"] == "release-researcher"
    assert duplicate_response.status_code == 409


@pytest.mark.anyio
async def test_specialist_blueprint_api_rejects_unready_activation(cp_app):
    payload = {
        "kind": "website_monitor",
        "name": "Unready Website Monitor",
        "activate": True,
        "backends": [
            {
                "type": "remote_llm",
                "provider": "ollama_cloud",
                "model": "qwen3.5:cloud",
                "worker_id": "missing-worker",
            }
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        response = await client.post("/v1/bot-blueprints/create", json=payload)
        stored = await client.get("/v1/bots/unready-website-monitor")

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "bot_not_ready"
    assert response.json()["detail"]["readiness"]["ready"] is False
    assert stored.status_code == 404


@pytest.mark.anyio
async def test_specialist_blueprint_api_allows_ready_activation(cp_app):
    payload = {
        "kind": "researcher",
        "name": "Ready Researcher",
        "activate": True,
        "backends": [
            {
                "type": "cloud_api",
                "provider": "ollama_cloud",
                "model": "qwen3.5:cloud",
                "api_key_ref": "ollama-cloud",
            }
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        response = await client.post("/v1/bot-blueprints/create", json=payload)

    assert response.status_code == 200
    assert response.json()["bot"]["enabled"] is True
