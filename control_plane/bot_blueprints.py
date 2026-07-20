"""Typed, least-privilege templates for creating specialized NexusAI bots."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.models import (
    BackendConfig,
    Bot,
    BotContextAccess,
    BotExecutionPolicy,
    BotWorkflow,
)


SpecialistKind = Literal[
    "researcher",
    "content_planner",
    "content_writer",
    "content_reviewer",
    "question_bank_reviewer",
    "question_bank_writer",
    "lesson_block_reviewer",
    "lesson_block_builder",
    "quality_reviewer",
    "customer_service_triage",
    "marketing_analyst",
    "operations_manager",
    "website_monitor",
    "code_reviewer",
    "code_implementer",
    "deployment_reviewer",
]


_DIRECT_CREDENTIAL_PREFIXES = (
    "sk-",
    "sk_",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "akia",
    "aiza",
    "hf_",
)
_RAW_CREDENTIAL_FIELDS = frozenset(
    {
        "api_key",
        "api_token",
        "access_token",
        "secret",
        "password",
        "private_key",
    }
)
_PORTFOLIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_MANAGER_ACTIONS = ["pause_schedule", "hold_bot", "configuration_review"]


def is_safe_credential_reference(value: object) -> bool:
    """Return whether ``value`` is an opaque credential reference, never a secret."""
    if not isinstance(value, str):
        return False
    reference = value.strip()
    if not reference or reference != value or len(reference) > 128:
        return False
    normalized = reference.lower()
    return not (
        normalized.startswith(_DIRECT_CREDENTIAL_PREFIXES)
        or "=" in reference
        or any(character.isspace() for character in reference)
    )


def _normalize_portfolio_ids(values: list[str], *, field_name: str) -> list[str]:
    """Validate bounded control-plane identifiers without resolving them yet."""
    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        identifier = str(value or "").strip()
        if not identifier:
            continue
        if not _PORTFOLIO_ID_PATTERN.fullmatch(identifier):
            raise ValueError(f"{field_name} entries must be control-plane identifiers.")
        if identifier not in seen:
            seen.add(identifier)
            normalized_values.append(identifier)
    return normalized_values


class SpecialistBlueprintRequest(BaseModel):
    """The operator-controlled inputs used to compose a specialist bot.

    Credentials are intentionally referenced by name through ``BackendConfig`` and
    never accepted as raw secret values.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SpecialistKind
    name: str = Field(min_length=1, max_length=120)
    backends: list[BackendConfig] = Field(min_length=1, max_length=5)
    bot_id: str | None = Field(default=None, max_length=120)
    mission: str | None = Field(default=None, max_length=4_000)
    project_id: str | None = Field(default=None, max_length=120)
    activate: bool = False
    allow_repo_writes: bool = False
    cli_command_profile: Literal["claude_ollama_json"] | None = None
    cli_runtime_model: str | None = Field(default=None, max_length=128)
    portfolio_bot_ids: list[str] = Field(default_factory=list, max_length=100)
    portfolio_schedule_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def reject_raw_credential_material(cls, value: object) -> object:
        """Keep secret values out of public, exportable bot configuration."""
        if not isinstance(value, dict):
            return value
        unexpected_top_level = _RAW_CREDENTIAL_FIELDS.intersection(value)
        if unexpected_top_level:
            raise ValueError("Raw credential fields are not accepted; use api_key_ref.")

        backends = value.get("backends")
        if not isinstance(backends, list):
            return value
        for backend in backends:
            if not isinstance(backend, dict):
                continue
            unexpected_backend_fields = _RAW_CREDENTIAL_FIELDS.intersection(backend)
            if unexpected_backend_fields:
                raise ValueError("Raw backend credential fields are not accepted; use api_key_ref.")
            credential_reference = backend.get("api_key_ref")
            if credential_reference is not None and not is_safe_credential_reference(credential_reference):
                raise ValueError("api_key_ref must be a named vault or environment reference, not a secret value.")
        return value

    @model_validator(mode="after")
    def validate_credential_references(self) -> "SpecialistBlueprintRequest":
        """Apply the same secret guard to programmatic ``BackendConfig`` inputs."""
        for backend in self.backends:
            credential_reference = backend.api_key_ref
            if credential_reference is not None and not is_safe_credential_reference(credential_reference):
                raise ValueError("api_key_ref must be a named vault or environment reference, not a secret value.")
        self.portfolio_bot_ids = _normalize_portfolio_ids(
            self.portfolio_bot_ids,
            field_name="portfolio_bot_ids",
        )
        self.portfolio_schedule_ids = _normalize_portfolio_ids(
            self.portfolio_schedule_ids,
            field_name="portfolio_schedule_ids",
        )
        if self.kind == "operations_manager":
            if not (self.portfolio_bot_ids or self.portfolio_schedule_ids):
                raise ValueError(
                    "operations_manager requires at least one portfolio_bot_ids or portfolio_schedule_ids entry."
                )
            manager_bot_id = _normalize_bot_id(self.bot_id or self.name)
            if manager_bot_id in self.portfolio_bot_ids:
                raise ValueError("operations_manager cannot include itself in portfolio_bot_ids.")
        return self


_BLUEPRINTS: dict[str, dict[str, Any]] = {
    "researcher": {
        "label": "Researcher",
        "role": "researcher",
        "description": "Produces source-grounded research briefs and open questions.",
        "risk_level": "read_only",
        "outputs": ["status", "research_brief", "sources", "assumptions", "open_questions", "handoff_notes"],
        "receives": ["instruction", "research_scope", "existing_context"],
        "self_serve": ["vault", "web"],
        "rules": [
            "Separate verified facts from assumptions.",
            "Cite or identify the source for every material claim.",
        ],
    },
    "content_planner": {
        "label": "Content Planner",
        "role": "content_planner",
        "description": "Produces structured course, unit, or lesson outlines without publishing content.",
        "risk_level": "draft_only",
        "outputs": ["status", "outline", "learning_objectives", "coverage_notes", "source_gaps", "handoff_notes"],
        "receives": ["instruction", "audience", "standards", "research_context"],
        "self_serve": ["vault"],
        "rules": [
            "Create a plan only; do not edit, publish, or import content.",
            "Flag missing source material or requirements instead of inventing them.",
        ],
    },
    "content_writer": {
        "label": "Content Writer",
        "role": "content_writer",
        "description": "Creates lesson or documentation drafts for a separate review and publication workflow.",
        "risk_level": "draft_only",
        "outputs": ["status", "draft", "learning_objectives", "source_notes", "quality_checks", "handoff_notes"],
        "receives": ["instruction", "outline", "audience", "style_guide", "research_context"],
        "self_serve": ["vault"],
        "rules": [
            "Return a draft only; never publish, import, delete, reorder, or edit a live lesson.",
            "Do not create assessment questions unless the task explicitly includes an approved question-writing scope.",
        ],
    },
    "content_reviewer": {
        "label": "Content Reviewer",
        "role": "content_reviewer",
        "description": "Reviews existing drafts or lessons and returns evidence-backed revision recommendations.",
        "risk_level": "read_only",
        "outputs": ["status", "findings", "severity_summary", "recommended_changes", "evidence", "handoff_notes"],
        "receives": ["instruction", "content", "rubric", "standards"],
        "self_serve": ["vault"],
        "rules": [
            "Review only. Do not alter source material or publish changes.",
            "Distinguish blocking defects from optional improvements.",
        ],
    },
    "question_bank_reviewer": {
        "label": "Question Bank Reviewer",
        "role": "question_bank_reviewer",
        "description": "Audits a supplied question bank for correctness, coverage, difficulty balance, and semantic repetition.",
        "risk_level": "read_only",
        "outputs": ["status", "findings", "coverage_analysis", "duplicate_or_repetitive_questions", "difficulty_analysis", "recommended_actions", "handoff_notes"],
        "receives": ["instruction", "question_bank", "learning_objectives", "assessment_context"],
        "self_serve": ["vault"],
        "rules": [
            "Review only. Do not alter questions, test-builder settings, lesson blocks, or published content.",
            "Flag semantic duplicates, but allow distinct application scenarios that assess the same skill.",
            "Recommend additions only when the supplied bank cannot be corrected to cover a required objective.",
        ],
    },
    "question_bank_writer": {
        "label": "Question Bank Draft Writer",
        "role": "question_bank_writer",
        "description": "Produces targeted draft question patches and justified additions for a separate approval workflow.",
        "risk_level": "draft_only",
        "outputs": ["status", "question_patches", "new_question_drafts", "novelty_check", "coverage_notes", "handoff_notes"],
        "receives": ["instruction", "question_bank", "learning_objectives", "assessment_context", "review_findings"],
        "self_serve": ["vault"],
        "rules": [
            "Return draft patches only. Do not open an admin UI, modify a question bank, alter assessment configuration, or publish content.",
            "Prefer correcting an identified question; add a draft question only for a demonstrated coverage or count gap.",
            "Check semantic novelty so the draft does not repeat an existing question with superficial wording changes.",
        ],
    },
    "lesson_block_reviewer": {
        "label": "Lesson Block Reviewer",
        "role": "lesson_block_reviewer",
        "description": "Reviews a supplied lesson structure and returns evidence-backed block placement and revision recommendations.",
        "risk_level": "read_only",
        "outputs": ["status", "findings", "block_order_analysis", "recommended_changes", "placement_evidence", "handoff_notes"],
        "receives": ["instruction", "lesson_structure", "lesson_content", "learning_objectives"],
        "self_serve": ["vault"],
        "rules": [
            "Review only. Do not add, edit, delete, reorder, or publish lesson blocks.",
            "Identify the exact predecessor and successor for every recommended insertion point.",
        ],
    },
    "lesson_block_builder": {
        "label": "Lesson Block Draft Builder",
        "role": "lesson_block_builder",
        "description": "Produces a lesson-block draft and an ordered placement plan for a separate controlled editing workflow.",
        "risk_level": "draft_only",
        "outputs": ["status", "block_draft", "placement_plan", "reorder_plan", "verification_checklist", "handoff_notes"],
        "receives": ["instruction", "lesson_structure", "lesson_content", "learning_objectives", "review_findings"],
        "self_serve": ["vault"],
        "rules": [
            "Return a draft and placement plan only. Do not operate a lesson builder, mutate blocks, reorder content, or publish.",
            "When proposing an insertion, name the predecessor, list one-step reorder moves, and defer edits until placement is verified.",
        ],
    },
    "quality_reviewer": {
        "label": "Quality Reviewer",
        "role": "quality_reviewer",
        "description": "Evaluates a bounded artifact against a supplied checklist or acceptance criteria.",
        "risk_level": "read_only",
        "outputs": ["status", "findings", "evidence", "acceptance_result", "recommended_next_step", "handoff_notes"],
        "receives": ["instruction", "artifact", "acceptance_criteria", "prior_results"],
        "self_serve": ["vault"],
        "rules": [
            "Do not repair the artifact. Report verifiable findings and the exact acceptance criterion involved.",
        ],
    },
    "customer_service_triage": {
        "label": "Customer Service Triage",
        "role": "customer_service_triage",
        "description": "Classifies customer messages, urgency, and proposed next actions without sending a response.",
        "risk_level": "read_only",
        "outputs": ["status", "category", "priority", "summary", "proposed_response", "escalation_reason"],
        "receives": ["instruction", "customer_message", "account_context", "support_policy"],
        "self_serve": ["vault"],
        "rules": [
            "Prepare a response recommendation only. Do not send messages, alter accounts, or issue refunds.",
            "Escalate safety, privacy, billing, and account-access issues when policy requires it.",
        ],
    },
    "marketing_analyst": {
        "label": "Marketing Analyst",
        "role": "marketing_analyst",
        "description": "Turns supplied campaign and funnel data into an evidence-backed performance report.",
        "risk_level": "read_only",
        "outputs": ["status", "report", "observations", "recommendations", "data_gaps", "handoff_notes"],
        "receives": ["instruction", "marketing_data", "goals", "reporting_window"],
        "self_serve": ["vault"],
        "rules": [
            "Analyze supplied or explicitly attached data only.",
            "Do not launch, pause, or modify a campaign.",
        ],
    },
    "operations_manager": {
        "label": "Operations Manager",
        "role": "operations_manager",
        "description": "Supervises an explicit worker and schedule portfolio and produces approval-gated executive decisions.",
        "risk_level": "read_only",
        "outputs": [
            "executive_summary",
            "overall_status",
            "accomplishments",
            "risks",
            "decisions_needed",
            "portfolio",
            "action_proposals",
        ],
        "receives": ["instruction", "portfolio_snapshot"],
        "self_serve": [],
        "rules": [
            "Analyze only the supplied declared-portfolio snapshot and instruction.",
            "You may propose pause_schedule, hold_bot, or configuration_review actions for declared targets only; proposals never execute by themselves.",
            "Do not enable workers, dispatch tasks, modify configurations, restart services, deploy changes, or access other portfolios.",
            "Separate verified operating evidence from assumptions and identify each decision that needs an operator.",
        ],
    },
    "website_monitor": {
        "label": "Website Monitor",
        "role": "website_monitor",
        "description": "Classifies supplied uptime, API, and browser-monitor evidence into an actionable incident report.",
        "risk_level": "read_only",
        "outputs": ["status", "health_summary", "incidents", "severity", "recommended_next_step", "handoff_notes"],
        "receives": ["instruction", "monitoring_events", "service_inventory", "recent_changes"],
        "self_serve": [],
        "rules": [
            "Analyze supplied monitoring evidence only.",
            "Do not restart services, change infrastructure, or call external endpoints.",
        ],
    },
    "code_reviewer": {
        "label": "Code Reviewer",
        "role": "code_reviewer",
        "description": "Inspects a scoped repository workspace and produces review findings without changing files.",
        "risk_level": "read_only",
        "outputs": ["status", "findings", "evidence", "test_gaps", "recommended_changes", "handoff_notes"],
        "receives": ["instruction", "requirements", "changed_files", "test_context"],
        "self_serve": ["repo", "vault"],
        "workspace_context": True,
        "rules": [
            "Inspect and report only. Do not write, delete, commit, deploy, or alter repository files.",
            "Tie every finding to a concrete file, symbol, test, or missing acceptance criterion.",
        ],
    },
    "code_implementer": {
        "label": "Code Implementer",
        "role": "code_implementer",
        "description": "Implements a bounded repository change with tests when explicitly granted write access.",
        "risk_level": "guarded_write",
        "outputs": ["status", "change_summary", "files_touched", "tests_run", "risks", "handoff_notes"],
        "receives": ["instruction", "requirements", "acceptance_criteria", "test_context"],
        "self_serve": ["repo", "vault"],
        "workspace_context": True,
        "rules": [
            "Work only inside the injected workspace and task scope.",
            "Do not commit, push, deploy, alter infrastructure, or access secrets.",
            "When write access is not explicitly granted, return a patch plan instead of editing files.",
        ],
    },
    "deployment_reviewer": {
        "label": "Deployment Reviewer",
        "role": "deployment_reviewer",
        "description": "Reviews release evidence and returns a go/no-go recommendation without deploying.",
        "risk_level": "read_only",
        "outputs": ["status", "release_assessment", "blocking_issues", "verification_evidence", "recommendation", "handoff_notes"],
        "receives": ["instruction", "release_candidate", "test_results", "deployment_policy"],
        "self_serve": ["vault"],
        "rules": [
            "Do not trigger or approve a deployment. Return a recommendation with evidence.",
        ],
    },
}


def list_specialist_blueprints() -> list[dict[str, Any]]:
    """Return public metadata needed to present the specialist catalog."""
    return [
        {
            "kind": kind,
            "label": str(spec["label"]),
            "role": str(spec["role"]),
            "description": str(spec["description"]),
            "risk_level": str(spec["risk_level"]),
            "supports_repo_writes": kind == "code_implementer",
        }
        for kind, spec in _BLUEPRINTS.items()
    ]


def build_specialist_bot(request: SpecialistBlueprintRequest) -> Bot:
    """Compose a complete bot config from a safe specialist template."""
    spec = _BLUEPRINTS[request.kind]
    bot_id = _normalize_bot_id(request.bot_id or request.name)
    mission = str(request.mission or "").strip()
    allow_repo_writes = request.kind == "code_implementer" and bool(request.allow_repo_writes)
    workspace_context = bool(spec.get("workspace_context", False))
    output_fields = list(spec["outputs"])
    input_fields = ["instruction"]
    backends = _prepare_specialist_backends(request)
    non_empty_output_fields = (
        ["status", output_fields[1]]
        if output_fields[0] == "status"
        else output_fields[:2]
    )

    system_prompt = _system_prompt(
        label=str(spec["label"]),
        description=str(spec["description"]),
        mission=mission,
        outputs=output_fields,
        rules=list(spec["rules"]),
        allow_repo_writes=allow_repo_writes,
    )
    routing_rules: dict[str, Any] = {
        "specialist": {
            "schema_version": "nexusai.specialist.v1",
            "kind": request.kind,
            "risk_level": spec["risk_level"],
            "project_id": request.project_id,
            "mission": mission or None,
            "operator_review_required": not bool(request.activate) or allow_repo_writes,
            "repo_write_granted": allow_repo_writes,
        },
        "input_contract": {
            "enabled": True,
            "format": "json_object",
            "required_fields": input_fields,
            "description": f"Provide a bounded instruction for the {spec['label'].lower()}.",
        },
        "output_contract": {
            "enabled": True,
            "format": "json_object",
            "required_fields": output_fields,
            "non_empty_fields": non_empty_output_fields,
            "allow_blocked_status": True,
            "max_retries": 1,
        },
    }
    if request.project_id:
        routing_rules["launch_profile"] = {
            "enabled": False,
            "project_id": request.project_id,
        }
    if request.kind == "operations_manager":
        routing_rules["worker_profile"] = {
            "role": "operations-manager",
            "task_scope": "read-only-manager-review",
            "can_edit": False,
        }
        routing_rules["supervision_manager"] = {
            "enabled": True,
            "portfolio": {
                "project_id": str(request.project_id or "").strip() or None,
                "bot_ids": list(request.portfolio_bot_ids),
                "schedule_ids": list(request.portfolio_schedule_ids),
            },
            "action_policy": {
                "allow_actions": list(_MANAGER_ACTIONS),
            },
        }

    return Bot(
        id=bot_id,
        name=str(request.name).strip(),
        role=str(spec["role"]),
        project_id=str(request.project_id or "").strip() or None,
        system_prompt=system_prompt,
        priority=0,
        enabled=bool(request.activate),
        backends=backends,
        routing_rules=routing_rules,
        workflow=BotWorkflow(required_output_fields=output_fields),
        context_access=BotContextAccess(
            receives=list(spec["receives"]),
            can_self_serve=list(spec["self_serve"]),
        ),
        execution_policy=BotExecutionPolicy(
            repo_output_mode="allow" if allow_repo_writes else "deny",
            workspace_context_injection=workspace_context,
            inline_coding_default=allow_repo_writes,
            can_apply_db_actions=False,
            allow_run_result_ingest=True,
        ),
    )


def _prepare_specialist_backends(request: SpecialistBlueprintRequest) -> list[BackendConfig]:
    """Apply approved CLI profiles without accepting arbitrary shell commands."""
    cli_backends = [backend for backend in request.backends if backend.type == "cli"]
    profile = request.cli_command_profile
    runtime_model = str(request.cli_runtime_model or "").strip()

    if not cli_backends:
        if profile or runtime_model:
            raise ValueError("CLI profile settings require at least one CLI backend.")
        return list(request.backends)

    if profile != "claude_ollama_json":
        raise ValueError("CLI backends require the approved Claude via Ollama JSON profile.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", runtime_model):
        raise ValueError("CLI runtime model must be a valid Ollama model name.")

    configured: list[BackendConfig] = []
    for backend in request.backends:
        if backend.type != "cli":
            configured.append(backend)
            continue
        if backend.command:
            raise ValueError("Specialist CLI commands are generated from an approved profile.")
        if backend.provider != "cli" or backend.model != "claude":
            raise ValueError("The Claude via Ollama profile requires provider 'cli' and model 'claude'.")
        configured.append(
            backend.model_copy(
                update={
                    "command": f"claude -p --model {runtime_model} --output-format json",
                }
            )
        )
    return configured


def _normalize_bot_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not normalized:
        raise ValueError("bot_id or name must contain at least one letter or number")
    return normalized[:120]


def _system_prompt(
    *,
    label: str,
    description: str,
    mission: str,
    outputs: list[str],
    rules: list[str],
    allow_repo_writes: bool,
) -> str:
    lines = [
        f"You are the NexusAI {label}.",
        description,
    ]
    if mission:
        lines.extend(["Mission:", mission])
    lines.extend(
        [
            "Operating rules:",
            *[f"- {rule}" for rule in rules],
            "- Do not expand the task scope. If required context, access, or approval is missing, return status=blocked and explain what is needed.",
            "- Never disclose credentials, private keys, tokens, or sensitive personal data.",
            "- Never publish, send external communications, delete data, or change production systems.",
        ]
    )
    if allow_repo_writes:
        lines.append("- Repository writes are allowed only through the injected workspace tools for this task. Run relevant tests and report every changed file.")
    else:
        lines.append("- Repository writes are not permitted for this bot.")
    lines.extend(
        [
            "Return a single JSON object and include every required field:",
            ", ".join(outputs),
        ]
    )
    return "\n".join(lines)
