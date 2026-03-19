#!/usr/bin/env python3
"""Generate and optionally apply a PM/coding bot pack using Ollama Cloud backends.

Usage examples:
  py scripts/setup_pm_bot_pack.py --export-dir "C:\\tmp\\pm-pack"
  py scripts/setup_pm_bot_pack.py --export-dir "C:\\tmp\\pm-pack" --chat-tools-mode repo_and_filesystem
  py scripts/setup_pm_bot_pack.py --apply --base-url http://127.0.0.1:8000 --api-token <token>
  py scripts/setup_pm_bot_pack.py --export-dir "C:\\tmp\\pm-pack" --apply
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY_REF = "Ollama_Cloud1"
DEFAULT_CHAT_TOOLS_MODE = "off"


@dataclass(frozen=True)
class BotSpec:
    bot_id: str
    name: str
    role: str
    model: str
    priority: int
    system_prompt: str
    max_tokens: int = 65000
    temperature: float = 0.1


def _pm_specs() -> List[BotSpec]:
    return [
        BotSpec(
            bot_id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            model="gpt-oss:120b-cloud",
            priority=100,
            max_tokens=65000,
            temperature=0.05,
            system_prompt=(
                "You are a deterministic project manager orchestrator.\n"
                "Given an implementation request, return JSON only with:\n"
                "{\n"
                '  "global_acceptance_criteria": [],\n'
                '  "global_quality_gates": [],\n'
                '  "risks": [],\n'
                '  "steps": [\n'
                "    {\n"
                '      "id": "step_1",\n'
                '      "title": "",\n'
                '      "instruction": "",\n'
                '      "bot_id": "pm-research-analyst|pm-engineer|pm-coder|pm-tester|pm-security-reviewer|pm-database-engineer|pm-ui-tester|pm-final-qc",\n'
                '      "role_hint": "researcher|engineer|coder|tester|reviewer|security|dba|ui|final-qc",\n'
                '      "step_kind": "specification|planning|repo_change|test_execution|review|release",\n'
                '      "evidence_requirements": [],\n'
                '      "depends_on": [],\n'
                '      "acceptance_criteria": [],\n'
                '      "deliverables": [],\n'
                '      "quality_gates": []\n'
                "    }\n"
                "  ]\n"
                "}\n"
                "Use only the listed bot_id values. Every step must include bot_id. "
                "Use pm-ui-tester only when the task includes real UI deliverables or user-facing behavior changes. "
                "Use pm-final-qc only as the terminal evidence-backed delivery gate. "
                "No markdown. No prose outside JSON."
            ),
        ),
        BotSpec(
            bot_id="pm-research-analyst",
            name="PM Research Analyst",
            role="researcher",
            model="qwen3.5:397b-cloud",
            priority=70,
            system_prompt=(
                "You are a deterministic research and requirements analyst.\n"
                "Return JSON only with keys: status, summary, requirements, assumptions, artifacts, risks, handoff_notes.\n"
                "If the deliverables are repo files or docs, include each full file in artifacts using {path, content}.\n"
                "Focus on clarifying requirements, constraints, dependencies, edge cases, and testability.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-engineer",
            name="PM Engineer",
            role="engineer",
            model="gpt-oss:120b-cloud",
            priority=82,
            system_prompt=(
                "You are a deterministic implementation planner and systems engineer.\n"
                "Return JSON only with keys: status, architecture, implementation_plan, artifacts, risks, handoff_notes.\n"
                "If the deliverables are repo files or planning docs, include each full file in artifacts using {path, content}.\n"
                "Translate requirements into a concrete implementation plan with sequencing and constraints.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-coder",
            name="PM Coder",
            role="coder",
            model="qwen3.5:397b-cloud",
            priority=85,
            system_prompt=(
                "You are a deterministic coding bot.\n"
                "Return JSON only with keys: status, change_summary, files_touched, artifacts, risks, handoff_notes.\n"
                "For every created or modified deliverable file, include the FULL file content in artifacts using {path, content}.\n"
                "Do not only summarize files_touched; provide the actual code or document content in artifacts.\n"
                "Use acceptance criteria and quality gates from payload as non-negotiable constraints.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-tester",
            name="PM Tester",
            role="tester",
            model="gpt-oss:120b-cloud",
            priority=80,
            system_prompt=(
                "You are a deterministic QA/testing bot.\n"
                "Return JSON only with keys: outcome, failure_type, findings, evidence, artifacts, handoff_notes.\n"
                "Allowed failure_type values: pass, implementation_issue.\n"
                "If the step generates a report or log artifact, include it in artifacts using {path, content}.\n"
                "Use failure_type=pass only when validation passes cleanly; otherwise use implementation_issue.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-security-reviewer",
            name="PM Security Reviewer",
            role="security-reviewer",
            model="gpt-oss:120b-cloud",
            priority=78,
            system_prompt=(
                "You are a deterministic security reviewer.\n"
                "Return JSON only with keys: outcome, failure_type, findings, evidence, artifacts, handoff_notes.\n"
                "Allowed failure_type values: pass, security_fix_required, architecture_issue.\n"
                "If the step generates a report artifact, include it in artifacts using {path, content}.\n"
                "Use architecture_issue only when the fix belongs with planning/engineering rather than coding.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-database-engineer",
            name="PM Database Engineer",
            role="dba-sql",
            model="qwen3.5:397b-cloud",
            priority=76,
            system_prompt=(
                "You are a deterministic database engineer.\n"
                "Return JSON only with keys: outcome, failure_type, findings, evidence, artifacts, handoff_notes.\n"
                "Allowed failure_type values: pass, schema_fix_required, data_architecture_issue.\n"
                "If the step creates migration scripts or schema docs, include them in artifacts using {path, content}.\n"
                "Use data_architecture_issue only when the fix belongs with engineering/planning.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-ui-tester",
            name="PM UI Tester",
            role="ui-tester",
            model="gpt-oss:120b-cloud",
            priority=77,
            system_prompt=(
                "You are a deterministic UI tester.\n"
                "Return JSON only with keys: outcome, failure_type, findings, evidence, artifacts, handoff_notes.\n"
                "Allowed failure_type values: pass, skip, ui_render_issue, ui_data_issue, ui_config_issue.\n"
                "If there are no UI deliverables or user-facing behavior changes, return outcome=skip and failure_type=skip.\n"
                "If the step generates a UI validation report, include it in artifacts using {path, content}.\n"
                "Use ui_data_issue or ui_config_issue when the problem should route to the database engineer.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
        BotSpec(
            bot_id="pm-final-qc",
            name="PM Final QC",
            role="final-qc",
            model="gpt-oss:120b-cloud",
            priority=90,
            temperature=0.05,
            system_prompt=(
                "You are the terminal final quality-control gate.\n"
                "Return JSON only with keys: outcome, failure_type, findings, evidence, commit_message, artifacts, handoff_notes.\n"
                "Allowed failure_type values: pass, incomplete, test_failure, security_issue, missing_requirements.\n"
                "On pass, provide a concise commit_message and final delivery summary. On fail, classify to the specific failure_type.\n"
                "If the step generates a final report, include it in artifacts using {path, content}.\n"
                "Do not write prose outside the JSON object."
            ),
        ),
    ]


def _backend_payload(spec: BotSpec, api_key_ref: str) -> Dict[str, Any]:
    return {
        "type": "cloud_api",
        "provider": "ollama_cloud",
        "model": spec.model,
        "api_key_ref": api_key_ref,
        "params": {
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
        },
    }


def _chat_tool_access_payload(mode: str) -> Dict[str, Any]:
    mode_clean = str(mode or "").strip().lower() or DEFAULT_CHAT_TOOLS_MODE
    if mode_clean == "repo_search":
        return {"enabled": True, "filesystem": False, "repo_search": True}
    if mode_clean == "repo_and_filesystem":
        return {"enabled": True, "filesystem": True, "repo_search": True}
    return {"enabled": False, "filesystem": False, "repo_search": False}


def _output_contract_payload(spec: BotSpec) -> Dict[str, Any]:
    if spec.bot_id == "pm-orchestrator":
        return {
            "enabled": True,
            "description": "Deterministic PM orchestration plan with explicit bot IDs.",
            "mode": "model_output",
            "format": "json_object",
            "required_fields": ["global_acceptance_criteria", "global_quality_gates", "risks", "steps"],
            "non_empty_fields": ["steps"],
            "template": {},
            "defaults_template": {},
            "fallback_mode": "disabled",
            "example_output": {
                "global_acceptance_criteria": ["Implementation is verifiable."],
                "global_quality_gates": ["Evidence is attached."],
                "risks": ["Scope ambiguity"],
                "steps": [
                    {
                        "id": "step_1",
                        "title": "Clarify scope",
                        "instruction": "Capture implementation-ready requirements.",
                        "bot_id": "pm-research-analyst",
                        "role_hint": "researcher",
                        "step_kind": "specification",
                        "evidence_requirements": ["Requirements summary"],
                        "depends_on": [],
                        "acceptance_criteria": ["Requirements are testable."],
                        "deliverables": ["Requirements summary"],
                        "quality_gates": ["No open ambiguity"],
                    }
                ],
            },
        }

    contracts: Dict[str, Dict[str, Any]] = {
        "pm-research-analyst": {
            "description": "Deterministic requirements handoff for engineering.",
            "required_fields": ["status", "summary", "requirements", "assumptions", "artifacts", "risks", "handoff_notes"],
            "non_empty_fields": ["status", "summary", "handoff_notes"],
            "example_output": {
                "status": "complete",
                "summary": "Requirements clarified and scoped.",
                "requirements": ["Support deterministic retry routing."],
                "assumptions": ["Existing workflow engine remains in use."],
                "artifacts": [
                    {"path": "docs/requirements.md", "content": "# Requirements\n"}
                ],
                "risks": ["Missing edge-case coverage."],
                "handoff_notes": "Proceed to engineering with explicit route constraints.",
            },
        },
        "pm-engineer": {
            "description": "Deterministic implementation plan and system design handoff.",
            "required_fields": ["status", "architecture", "implementation_plan", "artifacts", "risks", "handoff_notes"],
            "non_empty_fields": ["status", "architecture", "implementation_plan", "handoff_notes"],
            "example_output": {
                "status": "complete",
                "architecture": ["Use explicit workflow triggers already supported by the platform."],
                "implementation_plan": ["Update configs before adding new schema fields."],
                "artifacts": [
                    {"path": "docs/implementation_plan.md", "content": "# Plan\n"}
                ],
                "risks": ["Inconsistent exported bot packs."],
                "handoff_notes": "Coder should implement within existing workflow fields.",
            },
        },
        "pm-coder": {
            "description": "Deterministic implementation handoff for downstream validation.",
            "required_fields": ["status", "change_summary", "files_touched", "artifacts", "risks", "handoff_notes"],
            "non_empty_fields": ["status", "change_summary", "artifacts", "handoff_notes"],
            "example_output": {
                "status": "complete",
                "change_summary": ["Implemented the requested repo changes."],
                "files_touched": ["control_plane/chat/pm_orchestrator.py"],
                "artifacts": [
                    {"path": "control_plane/chat/pm_orchestrator.py", "content": "import json\n"}
                ],
                "risks": ["Follow-up validation still required."],
                "handoff_notes": "Send to tester for execution-backed verification.",
            },
        },
        "pm-tester": {
            "description": "Deterministic QA result used to route either forward or back to coding.",
            "required_fields": ["outcome", "failure_type", "findings", "evidence", "artifacts", "handoff_notes"],
            "non_empty_fields": ["outcome", "failure_type", "handoff_notes"],
            "example_output": {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": [],
                "evidence": ["Targeted tests passed."],
                "artifacts": [{"path": "reports/test_results.txt", "content": "Tests passed"}],
                "handoff_notes": "Proceed to security review.",
            },
        },
        "pm-security-reviewer": {
            "description": "Deterministic security review result used to route to coding or engineering.",
            "required_fields": ["outcome", "failure_type", "findings", "evidence", "artifacts", "handoff_notes"],
            "non_empty_fields": ["outcome", "failure_type", "handoff_notes"],
            "example_output": {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": [],
                "evidence": ["Reviewed changed surfaces for auth and exposure issues."],
                "artifacts": [],
                "handoff_notes": "Proceed to database review.",
            },
        },
        "pm-database-engineer": {
            "description": "Deterministic database review result used to route to coding or engineering.",
            "required_fields": ["outcome", "failure_type", "findings", "evidence", "artifacts", "handoff_notes"],
            "non_empty_fields": ["outcome", "failure_type", "handoff_notes"],
            "example_output": {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": [],
                "evidence": ["Validated schema and data flow assumptions."],
                "artifacts": [],
                "handoff_notes": "Proceed to UI validation.",
            },
        },
        "pm-ui-tester": {
            "description": "Deterministic UI validation result used to close the workflow or route to the correct fix owner.",
            "required_fields": ["outcome", "failure_type", "findings", "evidence", "artifacts", "handoff_notes"],
            "non_empty_fields": ["outcome", "handoff_notes"],
            "example_output": {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": [],
                "evidence": ["UI flow validated against current data behavior."],
                "artifacts": [{"path": "reports/ui_validation.txt", "content": "UI validation completed"}],
                "handoff_notes": "Proceed to final QC.",
            },
        },
        "pm-final-qc": {
            "description": "Final QC validation output with delivery report.",
            "required_fields": ["outcome", "failure_type", "findings", "evidence", "commit_message", "artifacts", "handoff_notes"],
            "non_empty_fields": ["outcome", "handoff_notes"],
            "example_output": {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": [],
                "evidence": ["All required deliverables and checks were verified."],
                "commit_message": "Implement lesson block workflow and validation pack",
                "artifacts": [{"path": "reports/final_qc.md", "content": "# Final QC\n"}],
                "handoff_notes": "Ready for operator review.",
            },
        },
    }
    contract = contracts[spec.bot_id]
    return {
        "enabled": True,
        "description": contract["description"],
        "mode": "model_output",
        "format": "json_object",
        "required_fields": contract["required_fields"],
        "non_empty_fields": contract["non_empty_fields"],
        "template": {},
        "defaults_template": {},
        "fallback_mode": "disabled",
        "example_output": contract["example_output"],
    }


def _workflow_payload(spec: BotSpec) -> Dict[str, Any]:
    triggers: Dict[str, List[Dict[str, Any]]] = {
        "pm-research-analyst": [
            {
                "id": "research-to-engineer",
                "title": "Research To Engineer",
                "event": "task_completed",
                "target_bot_id": "pm-engineer",
                "condition": "has_result",
            }
        ],
        "pm-engineer": [
            {
                "id": "engineer-to-coder",
                "title": "Engineer To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
            }
        ],
        "pm-coder": [
            {
                "id": "coder-to-tester",
                "title": "Coder To Tester",
                "event": "task_completed",
                "target_bot_id": "pm-tester",
                "condition": "has_result",
            }
        ],
        "pm-tester": [
            {
                "id": "tester-pass-forward",
                "title": "Tester Pass Forward",
                "event": "task_completed",
                "target_bot_id": "pm-security-reviewer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "pass",
            },
            {
                "id": "tester-fix-coder",
                "title": "Tester Back To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "implementation_issue",
            },
            {
                "id": "tester-hard-fail-coder",
                "title": "Tester Hard Failure To Coder",
                "event": "task_failed",
                "target_bot_id": "pm-coder",
                "condition": "has_error",
            },
        ],
        "pm-security-reviewer": [
            {
                "id": "security-pass-forward",
                "title": "Security Pass Forward",
                "event": "task_completed",
                "target_bot_id": "pm-database-engineer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "pass",
            },
            {
                "id": "security-fix-coder",
                "title": "Security Back To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "security_fix_required",
            },
            {
                "id": "security-back-engineer",
                "title": "Security Back To Engineer",
                "event": "task_completed",
                "target_bot_id": "pm-engineer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "architecture_issue",
            },
            {
                "id": "security-hard-fail-coder",
                "title": "Security Hard Failure To Coder",
                "event": "task_failed",
                "target_bot_id": "pm-coder",
                "condition": "has_error",
            },
        ],
        "pm-database-engineer": [
            {
                "id": "database-pass-forward",
                "title": "Database Pass Forward",
                "event": "task_completed",
                "target_bot_id": "pm-ui-tester",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "pass",
            },
            {
                "id": "database-fix-coder",
                "title": "Database Back To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "schema_fix_required",
            },
            {
                "id": "database-back-engineer",
                "title": "Database Back To Engineer",
                "event": "task_completed",
                "target_bot_id": "pm-engineer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "data_architecture_issue",
            },
            {
                "id": "database-hard-fail-coder",
                "title": "Database Hard Failure To Coder",
                "event": "task_failed",
                "target_bot_id": "pm-coder",
                "condition": "has_error",
            },
        ],
        "pm-ui-tester": [
            {
                "id": "ui-pass-final-qc",
                "title": "UI Pass To Final QC",
                "event": "task_completed",
                "target_bot_id": "pm-final-qc",
                "condition": "has_result",
                "result_field": "outcome",
                "result_equals": "pass",
            },
            {
                "id": "ui-skip-final-qc",
                "title": "UI Skip To Final QC",
                "event": "task_completed",
                "target_bot_id": "pm-final-qc",
                "condition": "has_result",
                "result_field": "outcome",
                "result_equals": "skip",
            },
            {
                "id": "ui-back-coder",
                "title": "UI Back To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "ui_render_issue",
            },
            {
                "id": "ui-back-database-data",
                "title": "UI Back To Database",
                "event": "task_completed",
                "target_bot_id": "pm-database-engineer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "ui_data_issue",
            },
            {
                "id": "ui-back-database-config",
                "title": "UI Config Back To Database",
                "event": "task_completed",
                "target_bot_id": "pm-database-engineer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "ui_config_issue",
            },
            {
                "id": "ui-hard-fail-coder",
                "title": "UI Hard Failure To Coder",
                "event": "task_failed",
                "target_bot_id": "pm-coder",
                "condition": "has_error",
            },
        ],
        "pm-final-qc": [
            {
                "id": "final-qc-back-coder-incomplete",
                "title": "Final QC Back To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "incomplete",
            },
            {
                "id": "final-qc-back-coder-tests",
                "title": "Final QC Test Failure To Coder",
                "event": "task_completed",
                "target_bot_id": "pm-coder",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "test_failure",
            },
            {
                "id": "final-qc-back-security",
                "title": "Final QC Back To Security",
                "event": "task_completed",
                "target_bot_id": "pm-security-reviewer",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "security_issue",
            },
            {
                "id": "final-qc-back-research",
                "title": "Final QC Back To Research",
                "event": "task_completed",
                "target_bot_id": "pm-research-analyst",
                "condition": "has_result",
                "result_field": "failure_type",
                "result_equals": "missing_requirements",
            },
            {
                "id": "final-qc-hard-fail-coder",
                "title": "Final QC Hard Failure To Coder",
                "event": "task_failed",
                "target_bot_id": "pm-coder",
                "condition": "has_error",
            },
        ],
    }
    notes: Dict[str, str] = {
        "pm-orchestrator": "Assignment planning only. No downstream workflow triggers.",
        "pm-research-analyst": "Always hands off to engineering after producing structured requirements.",
        "pm-engineer": "Always hands off to coding after producing the implementation plan.",
        "pm-coder": "Always hands off to testing after implementation output is produced.",
        "pm-tester": "Routes forward only on pass; otherwise routes back to coder.",
        "pm-security-reviewer": "Routes forward only on pass; failures go to coder or engineer.",
        "pm-database-engineer": "Routes forward only on pass; failures go to coder or engineer.",
        "pm-ui-tester": "UI validation routes to final QC on pass or skip, and back only to explicitly allowed fix owners on failure.",
        "pm-final-qc": "Terminal final delivery gate on pass; routes back to the appropriate bot on fail.",
    }
    return {
        "notes": notes.get(spec.bot_id, ""),
        "triggers": triggers.get(spec.bot_id, []),
    }


def _routing_rules_payload(spec: BotSpec, chat_tools_mode: str) -> Dict[str, Any]:
    workflow = _workflow_payload(spec)
    return {
        "input_contract": {
            "description": "",
            "default_payload": {},
            "form_fields": [],
        },
        "input_transform": {
            "enabled": False,
            "description": "",
            "template": {},
        },
        "launch_profile": {
            "enabled": True,
            "label": spec.name,
            "description": "",
            "payload": {},
            "priority": None,
            "project_id": None,
            "show_on_overview": True,
            "show_on_tasks": True,
        },
        "output_contract": _output_contract_payload(spec),
        "workflow": workflow,
        "chat_tool_access": _chat_tool_access_payload(chat_tools_mode),
    }


def _bot_payload(spec: BotSpec, api_key_ref: str, chat_tools_mode: str) -> Dict[str, Any]:
    workflow = _workflow_payload(spec)
    return {
        "id": spec.bot_id,
        "name": spec.name,
        "role": spec.role,
        "system_prompt": spec.system_prompt,
        "priority": spec.priority,
        "enabled": True,
        "backends": [_backend_payload(spec, api_key_ref)],
        "routing_rules": _routing_rules_payload(spec, chat_tools_mode),
        "workflow": workflow,
    }


def _bundle_payload(spec: BotSpec, api_key_ref: str, chat_tools_mode: str) -> Dict[str, Any]:
    return {
        "schema_version": "nexusai.bot-export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "bot": _bot_payload(spec, api_key_ref, chat_tools_mode),
        "connections": [],
    }


def _http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: Dict[str, Any] | None = None,
) -> Tuple[int, Dict[str, Any] | List[Any] | None]:
    headers = {"Content-Type": "application/json"}
    token_clean = token.strip()
    if token_clean:
        headers["X-Nexus-API-Key"] = token_clean
        headers["Authorization"] = f"Bearer {token_clean}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url, method=method.upper(), headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            payload = json.loads(raw) if raw else None
            return int(resp.status), payload
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace").strip()
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"detail": raw}
        return int(exc.code), payload


def _upsert_bot(base_url: str, token: str, bot_payload: Dict[str, Any], dry_run: bool) -> str:
    bot_id = str(bot_payload.get("id") or "").strip()
    if not bot_id:
        raise ValueError("bot payload missing id")
    get_url = f"{base_url.rstrip('/')}/v1/bots/{bot_id}"
    create_url = f"{base_url.rstrip('/')}/v1/bots"
    update_url = f"{base_url.rstrip('/')}/v1/bots/{bot_id}"

    if dry_run:
        return "dry-run"

    status, _ = _http_json("GET", get_url, token=token)
    if status == 200:
        put_status, put_payload = _http_json("PUT", update_url, token=token, body=bot_payload)
        if put_status != 200:
            raise RuntimeError(f"PUT {update_url} failed ({put_status}): {put_payload}")
        return "updated"
    if status not in {404}:
        raise RuntimeError(f"GET {get_url} failed ({status})")

    post_status, post_payload = _http_json("POST", create_url, token=token, body=bot_payload)
    if post_status != 200:
        raise RuntimeError(f"POST {create_url} failed ({post_status}): {post_payload}")
    return "created"


def _write_exports(bundles: Iterable[Tuple[BotSpec, Dict[str, Any]]], export_dir: Path) -> List[Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    for spec, bundle in bundles:
        target = export_dir / f"{spec.bot_id}.bot.json"
        target.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        outputs.append(target)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup PM bot pack using Ollama Cloud models.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Control plane base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CONTROL_PLANE_API_TOKEN", ""),
        help="Control plane API token (or set CONTROL_PLANE_API_TOKEN).",
    )
    parser.add_argument(
        "--api-key-ref",
        default=DEFAULT_API_KEY_REF,
        help=f"Ollama Cloud API key reference to store on each bot backend (default: {DEFAULT_API_KEY_REF}).",
    )
    parser.add_argument(
        "--chat-tools-mode",
        choices=["off", "repo_search", "repo_and_filesystem"],
        default=DEFAULT_CHAT_TOOLS_MODE,
        help=(
            "Bot-level chat workspace tool policy for generated PM bots: "
            "off, repo_search, or repo_and_filesystem."
        ),
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Optional directory to write importable *.bot.json bundles.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply pack to control plane via /v1/bots create/update.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="When used with --apply, print actions without mutating bots.",
    )
    args = parser.parse_args()

    specs = _pm_specs()
    bundles = [(spec, _bundle_payload(spec, args.api_key_ref, args.chat_tools_mode)) for spec in specs]

    if args.export_dir is not None:
        outputs = _write_exports(bundles, args.export_dir)
        print(f"Wrote {len(outputs)} bot export file(s) to: {args.export_dir}")
        for path in outputs:
            print(f" - {path}")

    if args.apply:
        if not args.base_url.strip():
            print("ERROR: --base-url is required when --apply is used.")
            return 2
        for spec, bundle in bundles:
            action = _upsert_bot(
                base_url=args.base_url,
                token=args.api_token,
                bot_payload=bundle["bot"],
                dry_run=args.dry_run,
            )
            print(f"{spec.bot_id}: {action}")
        print("PM bot pack apply complete.")
    elif args.export_dir is None:
        print("No action taken. Use --export-dir and/or --apply.")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
