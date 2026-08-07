import asyncio
import base64
import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from control_plane.browser_action_approvals import browser_action_payload_digest
from control_plane.connection_action_approvals import (
    connection_action_key,
    connection_action_payload_digest,
)
from control_plane.connections.resolver import ConnectionResolver
from control_plane.worker_probe import (
    WorkerProbeError,
    autonomous_worker_probe_max_age_seconds,
    worker_base_url,
)
from shared import connection_runtime, connection_secrets
from shared.bot_policy import bot_allows_repo_output, bot_execution_policy
from shared.exceptions import BackendError, BotNotFoundError, NoViableBackendError
from shared.worker_capabilities import required_worker_tools, worker_missing_tools

try:
    from control_plane.chat.workspace_tools import list_workspace_tree  # noqa: F401
except ImportError:
    list_workspace_tree = None  # type: ignore[assignment]
from shared.models import BackendConfig, BackendParams, Task, Worker
from shared.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

_PAYLOAD_CONTEXT_REDUCTION_TARGET_CHARS = 48000


class _OllamaModelNotFound(Exception):
    """Internal sentinel raised when Ollama Cloud returns 404 for a missing model.
    Caught by the caller to trigger an auto-pull before retrying."""
    def __init__(self, model: str) -> None:
        super().__init__(f"Ollama model not found: {model}")
        self.model = model


# ---------------------------------------------------------------------------
# Agent tool-calling helpers (module-level)
# ---------------------------------------------------------------------------

def _agent_workspace_context(bot: Any, task: "Task") -> tuple["Path | None", bool]:
    """Return (workspace_root, allow_writes) for agentic tool calling.

    Returns (None, False) when agentic mode should not be used.
    """
    execution_policy = getattr(bot, "execution_policy", None) or {}
    if isinstance(execution_policy, dict):
        ws_injection = execution_policy.get("workspace_context_injection", False)
        repo_output_mode = str(execution_policy.get("repo_output_mode", "deny")).lower()
    else:
        ws_injection = getattr(execution_policy, "workspace_context_injection", False)
        repo_output_mode = str(getattr(execution_policy, "repo_output_mode", "deny") or "deny").lower()

    if not ws_injection:
        return None, False

    workspace_root_str = ""
    payload = task.payload
    if isinstance(payload, dict):
        # The task_manager stores the resolved workspace root in the payload.
        workspace_root_str = str(payload.get("_injected_workspace_root") or "").strip()
        if not workspace_root_str:
            # Fallback: look in assignment_workspace.
            aw = payload.get("assignment_workspace") or {}
            if isinstance(aw, dict):
                workspace_root_str = str(aw.get("temp_root") or aw.get("root") or "").strip()
    elif isinstance(payload, list):
        # Chat payloads can carry a hidden marker message with _workspace_root.
        for item in payload:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("_workspace_root") or "").strip()
            if candidate:
                workspace_root_str = candidate
                break

    if not workspace_root_str:
        return None, False

    try:
        ws_root = Path(workspace_root_str)
        if not ws_root.exists():
            return None, False
        return ws_root, (repo_output_mode == "allow")
    except Exception:
        return None, False


def _is_non_mutating_test_task(task: "Task | None") -> bool:
    """Return whether a task is an interactive test that must not change state."""
    if task is None or task.metadata is None:
        return False
    mode = str(getattr(task.metadata, "execution_mode", "") or "").strip().lower()
    source = str(getattr(task.metadata, "source", "") or "").strip().lower()
    return mode == "test" or source == "bot_test"


def _is_autonomous_schedule_task(task: "Task | None") -> bool:
    """Return whether a task originated from an agent schedule."""
    if task is None or task.metadata is None:
        return False
    source = str(getattr(task.metadata, "source", "") or "").strip().lower()
    if source == "agent_schedule":
        return True
    # Task retries retain the original scheduler envelope in their persisted payload
    # while changing metadata.source to auto_retry. Keep that envelope out of strict
    # browser request schemas without broadening browser authorization.
    if source != "auto_retry" or not isinstance(task.payload, dict):
        return False
    return str(task.payload.get("source") or "").strip().lower() == "agent_schedule"


def _mark_test_payload(payload: Any) -> Any:
    guardrail = (
        "Execution mode is TEST. Analyze and report only. Do not claim to have changed external "
        "systems, sent requests, published content, or written files."
    )
    if isinstance(payload, list):
        return [*payload, {"role": "system", "content": guardrail}]
    if isinstance(payload, dict):
        marked = dict(payload)
        marked["execution_mode"] = "test"
        marked["test_constraints"] = guardrail
        return marked
    if isinstance(payload, str):
        return f"{guardrail}\n\n{payload}"
    return payload


def _backend_supports_tools(backend: "BackendConfig") -> bool:
    """Return True if the backend provider supports function/tool calling."""
    provider = str(getattr(backend, "provider", "") or "").lower()
    return provider in {"ollama_cloud", "openai", "claude"}


def _parse_ollama_tool_calls(raw: list) -> list[dict]:
    """Normalize Ollama tool_calls to our common format."""
    result = []
    for tc in (raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or tc.get("name") or "")
        raw_args = fn.get("arguments") or tc.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {}
        result.append({
            "id": str(tc.get("id") or ""),
            "name": name,
            "arguments": raw_args if isinstance(raw_args, dict) else {},
        })
    return result


def _parse_openai_tool_calls(raw: list) -> list[dict]:
    """Normalize OpenAI tool_calls to our common format."""
    result = []
    for tc in (raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {}
        result.append({
            "id": str(tc.get("id") or ""),
            "name": name,
            "arguments": raw_args if isinstance(raw_args, dict) else {},
        })
    return result


def _claude_payload_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split a messages list into (system_prompt, chat_messages) for Claude."""
    system_parts = []
    chat_msgs = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(str(m.get("content") or ""))
        else:
            chat_msgs.append(m)
    return "\n\n".join(system_parts), chat_msgs


def _convert_tools_for_claude(tools: list[dict]) -> list[dict]:
    """Convert OpenAI-format tool definitions to Anthropic format."""
    claude_tools = []
    for t in (tools or []):
        fn = t.get("function") or {}
        claude_tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return claude_tools


def _convert_tool_messages_for_claude(messages: list[dict]) -> list[dict]:
    """Convert our internal tool message format to Anthropic's expected format.

    Anthropic expects tool results as user messages with content type 'tool_result'.
    """
    converted = []
    for m in messages:
        role = m.get("role", "")
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": str(m.get("content") or ""),
                    }
                ],
            })
        elif role == "assistant" and m.get("tool_calls"):
            # Anthropic expects tool_use blocks in the assistant message content
            content_blocks = []
            text = m.get("content") or ""
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in (m.get("tool_calls") or []):
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": tc.get("arguments") or {},
                })
            converted.append({"role": "assistant", "content": content_blocks})
        else:
            converted.append(m)
    return converted


def _vertex_anthropic_model_ref(model_ref: str) -> str | None:
    """Return a Claude model id if model_ref targets Vertex Anthropic partner models."""
    raw = str(model_ref or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("claude-"):
        return raw
    marker = "publishers/anthropic/models/"
    if marker in lowered:
        idx = lowered.find(marker)
        return raw[idx + len(marker):].strip()
    if lowered.startswith("anthropic/claude-"):
        return raw.split("/", 1)[1].strip()
    return None
_ASSIGNMENT_TRANSCRIPT_REDUCTION_CHARS = 6000
_ARTIFACT_CONTENT_REDUCTION_CHARS = 1800
_LONG_STRING_REDUCTION_CHARS = 1200
_JOIN_RESULT_LIST_MAX_ITEMS = 12


def _backend_failure_message(task_id: str, last_error: Exception, attempts: list[str] | None = None) -> str:
    detail = str(last_error or "").strip()
    if not detail:
        detail = repr(last_error) if last_error is not None else ""
    attempt_detail = f" Attempts: {'; '.join(attempts)}." if attempts else ""
    if detail:
        return f"All backends failed for task {task_id}: {detail}.{attempt_detail}".strip()
    return f"All backends failed for task {task_id}.{attempt_detail}".strip()


def _ollama_options(params: dict[str, Any]) -> dict[str, Any]:
    options = dict(params or {})
    max_tokens = options.pop("max_tokens", None)
    # Only forward a positive num_predict; negative values (e.g. -1 meaning
    # "unlimited") must be omitted because the Ollama Cloud direct API rejects
    # non-positive values with a 400 error. Omitting num_predict lets the API
    # use the model's own output-length default (effectively unlimited).
    if max_tokens is not None and max_tokens > 0 and "num_predict" not in options:
        options["num_predict"] = max_tokens
    # Apply a platform default so local Ollama's low built-in cap (128 tokens
    # in older versions) does not silently truncate. Only set when positive;
    # override via settings key default_ollama_num_predict.
    if "num_predict" not in options:
        default_predict = _settings_int("default_ollama_num_predict", -1)
        if default_predict > 0:
            options["num_predict"] = default_predict
    return options


def _worker_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=None, write=120.0, pool=30.0)


def _cloud_timeout() -> float:
    env_raw = os.environ.get("NEXUSAI_CLOUD_API_TIMEOUT_SECONDS", "").strip()
    if env_raw:
        return float(env_raw)
    env_default = 900.0
    try:
        configured = SettingsManager.instance().get("cloud_backend_timeout_seconds", env_default)
        return float(configured)
    except Exception:
        return env_default


def _settings_int(name: str, default: int) -> int:
    try:
        return int(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _google_generation_config(params: dict[str, Any]) -> dict[str, Any]:
    """Map internal backend params to Gemini generationConfig keys."""
    raw = dict(params or {})
    config: dict[str, Any] = {}
    if raw.get("temperature") is not None:
        config["temperature"] = raw.get("temperature")
    if raw.get("top_p") is not None:
        config["topP"] = raw.get("top_p")
    if raw.get("max_tokens") is not None:
        try:
            config["maxOutputTokens"] = int(raw.get("max_tokens"))
        except Exception:
            pass
    return config


def _retry_incremented_value(value: Any, increment: int, retry_attempt: int) -> Any:
    try:
        return int(value) + (max(0, increment) * max(0, retry_attempt))
    except Exception:
        return value


def _backend_with_retry_params(backend: BackendConfig, task: Task | None = None) -> BackendConfig:
    if task is None or task.metadata is None:
        return backend
    retry_attempt = int(task.metadata.retry_attempt or 0)
    if retry_attempt <= 0:
        return backend

    params_model = backend.params
    params_dict = params_model.model_dump(exclude_none=True) if params_model else {}

    max_tokens_increment = _settings_int("task_retry_max_tokens_increment", 2048)
    num_width_increment = _settings_int("task_retry_num_width_increment", 2048)
    updates: dict[str, Any] = {}
    fallback_max_tokens = 1024
    fallback_num_ctx = 8192

    if max_tokens_increment > 0:
        if "max_tokens" in params_dict:
            current_max = params_dict["max_tokens"]
            # If max_tokens is -1 (unlimited), don't override it on retry —
            # incrementing -1 would produce a small positive cap (e.g. 2047)
            # which is worse than no limit at all.
            if current_max > 0:
                updates["max_tokens"] = _retry_incremented_value(
                    current_max,
                    max_tokens_increment,
                    retry_attempt,
                )
        else:
            updates["max_tokens"] = _retry_incremented_value(
                fallback_max_tokens,
                max_tokens_increment,
                retry_attempt,
            )

    width_key = None
    if "num_width" in params_dict:
        width_key = "num_width"
    elif "num_ctx" in params_dict:
        width_key = "num_ctx"
    if num_width_increment > 0:
        if width_key:
            updates[width_key] = _retry_incremented_value(
                params_dict[width_key],
                num_width_increment,
                retry_attempt,
            )
        elif backend.type == "local_llm":
            updates["num_ctx"] = _retry_incremented_value(
                fallback_num_ctx,
                num_width_increment,
                retry_attempt,
            )

    if not updates:
        return backend

    updated_params = params_model.model_copy(update=updates) if params_model else BackendParams(**updates)
    return backend.model_copy(update={"params": updated_params})


def _task_preferred_model_id(task: Task | None = None) -> str:
    if task is None or task.metadata is None:
        return ""
    return str(getattr(task.metadata, "preferred_model_id", "") or "").strip()


async def _backend_with_preferred_model(
    backend: BackendConfig,
    task: Task | None = None,
    model_registry: Any = None,
) -> BackendConfig:
    preferred_model_id = _task_preferred_model_id(task)
    if not preferred_model_id:
        return backend
    if backend.type not in {"cloud_api", "local_llm", "remote_llm"}:
        return backend

    catalog_model = None
    if model_registry is not None:
        try:
            catalog_model = await model_registry.get(preferred_model_id)
        except Exception:
            catalog_model = None

    if catalog_model is not None:
        if getattr(catalog_model, "enabled", True) is False:
            raise BackendError(f"Preferred model is disabled: {preferred_model_id}")
        catalog_provider = str(getattr(catalog_model, "provider", "") or "").strip()
        if catalog_provider and catalog_provider != backend.provider:
            raise BackendError(
                f"Preferred model provider '{catalog_provider}' does not match backend provider '{backend.provider}'"
            )
        model_name = str(getattr(catalog_model, "name", "") or "").strip()
        if model_name:
            return backend.model_copy(update={"model": model_name})

    return backend.model_copy(update={"model": preferred_model_id})


def _payload_to_messages(payload: Any) -> list[dict[str, Any]]:
    """Normalize a chat payload without flattening multipart content.

    Chat image attachments are represented as a list of content parts.  Serializing
    that list to JSON here turns the image data URL into ordinary prompt text, which
    both loses the image and can exceed upstream text limits.
    """
    if isinstance(payload, list):
        normalized: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                role = str(item.get("role") or "user")
                content = item.get("content")
                normalized.append({"role": role, "content": "" if content is None else content})
            else:
                normalized.append({"role": "user", "content": str(item)})
        return normalized
    if isinstance(payload, dict):
        return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    return [{"role": "user", "content": str(payload)}]


def _inject_system_prompt(system_prompt: str | None, payload: Any) -> Any:
    prompt = str(system_prompt or "").strip()
    if not prompt:
        return payload

    messages = _payload_to_messages(payload)
    if messages and str(messages[0].get("role") or "").lower() == "system":
        existing = str(messages[0].get("content") or "").strip()
        if existing == prompt:
            return messages
        if existing and existing in prompt:
            updated = [dict(message) for message in messages]
            updated[0]["content"] = prompt
            return updated
    return [{"role": "system", "content": prompt}, *messages]


def _payload_assignment_scope(payload: Any) -> dict[str, Any]:
    current: Any = payload
    seen: set[int] = set()
    for _ in range(8):
        if not isinstance(current, dict):
            return {}
        current_id = id(current)
        if current_id in seen:
            return {}
        seen.add(current_id)
        scope = current.get("assignment_scope")
        if isinstance(scope, dict):
            return scope
        current = current.get("source_payload")
    return {}


def _assignment_scope_prompt_suffix(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    scope = _payload_assignment_scope(payload)
    request_text = str(scope.get("request_text") or payload.get("assignment_request") or "").strip()
    conversation_brief = str(scope.get("conversation_brief") or "").strip()
    conversation_transcript = str(scope.get("conversation_transcript") or "").strip()
    conversation_message_count = int(scope.get("conversation_message_count") or 0)
    conversation_transcript_strategy = str(scope.get("conversation_transcript_strategy") or "").strip().lower()
    docs_only = bool(scope.get("docs_only", False))
    requested_output_paths = scope.get("requested_output_paths")
    prefer_in_house = bool(scope.get("prefer_in_house", False))
    avoid_external_apis = bool(scope.get("avoid_external_apis", False))
    prefer_client_side_execution = bool(scope.get("prefer_client_side_execution", False))
    minimize_server_load = bool(scope.get("minimize_server_load", False))
    minimize_bandwidth = bool(scope.get("minimize_bandwidth", False))
    requested_outcome_style = str(scope.get("requested_outcome_style") or "").strip().lower()
    focus_topics = scope.get("focus_topics")
    requested_artifact_hints = scope.get("requested_artifact_hints")
    constraint_hints = scope.get("constraint_hints")
    explicit_stage_exclusions = scope.get("explicit_stage_exclusions")
    explicit_stage_exclusion_reasons = scope.get("explicit_stage_exclusion_reasons")
    ui_test_mode = str(scope.get("ui_test_mode") or "").strip().lower()
    if (
        not docs_only
        and not request_text
        and not conversation_brief
        and not conversation_transcript
        and not requested_output_paths
        and not prefer_in_house
        and not avoid_external_apis
        and not prefer_client_side_execution
        and not minimize_server_load
        and not minimize_bandwidth
        and not requested_outcome_style
        and not focus_topics
        and not requested_artifact_hints
        and not constraint_hints
        and not explicit_stage_exclusions
        and not ui_test_mode
    ):
        return ""

    parts: list[str] = ["Assignment scope:"]
    if request_text:
        parts.append("Use the original assignment below as authoritative scope. Do not pivot to a different feature, file set, or recent unrelated workspace change.")
        parts.append(_truncate_text(request_text, 1200))
    if conversation_brief:
        parts.append("Conversation brief from earlier user messages that still constrains this assignment:")
        parts.append(_truncate_text(conversation_brief, 1200))
    if conversation_transcript:
        transcript_label = "Conversation transcript"
        if conversation_message_count > 0:
            transcript_label += f" ({conversation_message_count} prior message(s)"
            if conversation_transcript_strategy:
                transcript_label += f", {conversation_transcript_strategy}"
            transcript_label += ")"
        parts.append(transcript_label + ":")
        parts.append(_truncate_text(conversation_transcript, 12000))
    parts.append("If repo, vault, or workspace search surfaces unrelated files, ignore them. If relevant evidence is missing, say so explicitly instead of changing scope.")
    if isinstance(focus_topics, list):
        normalized_topics = [str(item).strip() for item in focus_topics if str(item).strip()]
        if normalized_topics:
            parts.append("Focus topics: " + ", ".join(normalized_topics[:12]))
    if isinstance(requested_artifact_hints, list):
        normalized_hints = [str(item).strip() for item in requested_artifact_hints if str(item).strip()]
        if normalized_hints:
            parts.append("Requested artifact shapes: " + ", ".join(normalized_hints[:12]))
    if requested_outcome_style == "roadmap":
        parts.append(
            "Requested output shape: a roadmap, block catalog, phased documentation plan, or comparable expansion map. "
            "Do not substitute only generic infrastructure guidance if the user asked what to build and how to expand."
        )
    elif requested_outcome_style == "documentation_plan":
        parts.append(
            "Requested output shape: documentation-first planning artifacts. Keep the output actionable for later implementation, "
            "but do not substitute source-code work for the requested documentation plan."
        )
    hard_constraints: list[str] = []
    if prefer_in_house:
        hard_constraints.append("Prefer in-house and locally owned solutions over outsourced provider workflows.")
    if avoid_external_apis:
        hard_constraints.append("Do not rely on external product APIs or paid third-party provider APIs unless the assignment explicitly re-authorizes them.")
    if prefer_client_side_execution:
        hard_constraints.append("Prefer client-side rendering and execution when possible.")
    if minimize_server_load:
        hard_constraints.append("Keep server CPU, memory, and infrastructure cost low.")
    if minimize_bandwidth:
        hard_constraints.append("Keep payloads and asset delivery bandwidth-light for end users.")
    if hard_constraints:
        parts.append("Non-negotiable constraints:")
        parts.extend(f"- {item}" for item in hard_constraints)
    if avoid_external_apis:
        parts.append(
            "You may mention external products or APIs only to reject them, compare against them, or explain why they are out of scope. "
            "Do not recommend, depend on, or instruct the user to integrate them."
        )
    if isinstance(constraint_hints, list):
        normalized_constraints = [str(item).strip() for item in constraint_hints if str(item).strip()]
        if normalized_constraints:
            parts.append("Interpreted scope constraints:")
            parts.extend(f"- {item}" for item in normalized_constraints[:12])
    if isinstance(explicit_stage_exclusions, list):
        normalized_exclusions = [str(item).strip() for item in explicit_stage_exclusions if str(item).strip()]
        if normalized_exclusions:
            parts.append("Explicitly excluded downstream stages for this run: " + ", ".join(normalized_exclusions[:8]))
            parts.append(
                "Do not invent deliverables, blockers, or required evidence for explicitly excluded stages. "
                "If an excluded stage is still invoked by workflow routing, return a skip/not_applicable outcome tied to assignment scope."
            )
            parts.append(
                "Final QC and other downstream validation stages must treat explicitly excluded stages as intentional omissions, "
                "not as missing verification, when the remaining required evidence is present."
            )
            if isinstance(explicit_stage_exclusion_reasons, dict):
                normalized_reasons = [
                    f"{str(stage).strip()}={str(reason).strip()}"
                    for stage, reason in explicit_stage_exclusion_reasons.items()
                    if str(stage).strip()
                ]
                if normalized_reasons:
                    parts.append("Excluded stage reasons: " + ", ".join(normalized_reasons[:8]))
    if ui_test_mode == "build_only":
        parts.append(
            "UI validation mode: build_only. Do not skip the pm-ui-tester stage. "
            "Run install/build/runtime validation and inspect startup or runtime errors, but do not require interactive browser automation."
        )
        parts.append(
            "Final QC must treat build_only UI validation as the intended validation mode for this run, not as a missing stage."
        )
    if docs_only:
        parts.append(
            "This is a documentation-only run. Allowed committed outputs are documentation files only, preferably markdown. "
            "Do not propose or produce source-code changes, tests, migrations, database work, UI implementation, configuration updates, or repo files outside the requested documentation scope."
        )
        parts.append(
            "For documentation-only coder branches, always return the repo-change contract JSON wrapper and place each generated markdown file under artifacts[path, content], "
            "along with status, change_summary, files_touched, risks, and handoff_notes."
        )
        parts.append(
            "Do not interpret documentation-only as an empty plan. Planning bots must still return a complete documentation architecture, "
            "implementation_plan, and implementation_workstreams for the requested docs deliverables. Those workstreams must stay documentation-only."
        )
        parts.append(
            "For this kind of run, coder branches should create only the requested documentation files, while tester/security/database/ui stages may return pass/skip/not_applicable based on branch applicability rather than inventing code or tests."
        )
        parts.append(
            "For tester and reviewer stages on documentation-only branches, treat upstream_artifacts (or source_result.artifacts when present) as the primary branch evidence. "
            "Do not fail solely because the live repo snapshot does not yet contain the proposed markdown files; assignment apply happens later."
        )
        parts.append(
            "When validating documentation-only branches, explicitly verify internal markdown links, referenced doc paths, and claimed evidence against the actual upstream_artifacts set. "
            "Do not claim 'no broken links', 'schema validation passed', or similar checks unless the available artifacts actually support that conclusion."
        )
        parts.append(
            "For documentation-only planning and coder stages, only cross-link to markdown docs that actually exist in the upstream_artifacts set, the current branch deliverables, or the live repository. "
            "Do not invent sibling folders, placeholder doc names, or guessed markdown paths just to make the docs feel complete. "
            "Links to real repository source files (for example .cs, .razor, .js) are allowed when they truly exist and support the documentation."
        )
        parts.append(
            "For final QC on documentation-only runs, prefer the strongest upstream tester evidence over later skip/not_applicable review signals. "
            "If a tester has already verified the requested markdown content and later UI/database/security stages skip because the branch has no applicable runtime work, treat those skips as acceptable rather than as missing verification."
        )
    parts.append(
        "Every downstream stage must validate its output against the original assignment scope above, not only the immediate upstream handoff. "
        "If the handoff drifts from the assignment, call that drift out explicitly and fail or send back the branch."
    )
    if isinstance(requested_output_paths, list) and requested_output_paths:
        normalized = [str(item).strip() for item in requested_output_paths if str(item).strip()]
        if normalized:
            parts.append("Requested output paths: " + ", ".join(normalized[:8]))
    return "\n\n" + "\n".join(parts)


def _pm_database_contract_prompt_suffix(bot_id: str | None, payload: Any) -> str:
    if str(bot_id or "").strip().lower() != "pm-database-engineer":
        return ""
    return (
        "\n\nDatabase stage contract:\n"
        "If the outcome is pass/completed, return exactly one canonical SQL migration script artifact for this stage.\n"
        "Do not emit duplicate migration variants, alternate SQL files, test_logs outputs, or unrelated top-level repo artifacts.\n"
        "Reject destructive SQL. Forbidden statements include DELETE, DROP, TRUNCATE, and destructive ALTER TABLE forms such as DROP COLUMN or DROP CONSTRAINT.\n"
        "If the branch has no applicable database change, return a structured skip/not_applicable outcome instead of inventing SQL."
    )


def _lookup_payload_path(payload: Any, path: str) -> Any:
    current: Any = payload
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
            continue
        if isinstance(current, list):
            if not key.isdigit():
                return None
            index = int(key)
            if index < 0 or index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _split_transform_expr_list(expr: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in str(expr or ""):
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_transform_literal(expr: str) -> tuple[bool, Any]:
    value = str(expr or "").strip()
    if value == "":
        return False, None
    lowered = value.lower()
    if lowered == "null":
        return True, None
    if lowered == "true":
        return True, True
    if lowered == "false":
        return True, False
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        inner = value[1:-1]
        inner = inner.replace("\\'", "'").replace("\\\\", "\\")
        return True, inner
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        try:
            return True, json.loads(value)
        except json.JSONDecodeError:
            return True, value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        try:
            return True, int(value)
        except ValueError:
            return False, None
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
        try:
            return True, float(value)
        except ValueError:
            return False, None
    if (value.startswith("[") and value.endswith("]")) or (value.startswith("{") and value.endswith("}")):
        try:
            return True, json.loads(value)
        except json.JSONDecodeError:
            return False, None
    return False, None


def _camelize_key(key: str) -> str:
    text = str(key or "")
    if "_" not in text:
        return text
    parts = [part for part in text.split("_") if part]
    if not parts:
        return text
    first = parts[0]
    rest = "".join(part[:1].upper() + part[1:] for part in parts[1:])
    return first + rest


def _camelize_json_keys(value: Any) -> Any:
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            converted[_camelize_key(str(key))] = _camelize_json_keys(item)
        return converted
    if isinstance(value, list):
        return [_camelize_json_keys(item) for item in value]
    return value


def _transform_template_value(template: Any, payload: Any) -> Any:
    if isinstance(template, dict):
        return {str(key): _transform_template_value(value, payload) for key, value in template.items()}
    if isinstance(template, list):
        return [_transform_template_value(item, payload) for item in template]
    if not isinstance(template, str):
        return template

    raw = template.strip()
    if raw.startswith("{{") and raw.endswith("}}"):
        expr = raw[2:-2].strip()
        mode = "value"
        path = expr
        if expr.startswith("json:"):
            mode = "json"
            path = expr[5:].strip()
        camelize = False
        while path.startswith("camelize:"):
            camelize = True
            path = path[len("camelize:") :].strip()
        if path.startswith("render:"):
            render_path = path[len("render:") :].strip()
            if render_path.startswith("payload."):
                render_path = render_path[8:].strip()
            rendered = _transform_template_value(_lookup_payload_path(payload, render_path), payload)
            if camelize:
                rendered = _camelize_json_keys(rendered)
            if mode == "json":
                return rendered
            return rendered
        if path.startswith("coalesce:"):
            candidates = _split_transform_expr_list(path[len("coalesce:") :])
            for candidate in candidates:
                literal_ok, literal_value = _parse_transform_literal(candidate)
                if literal_ok:
                    if literal_value is not None:
                        if camelize:
                            literal_value = _camelize_json_keys(literal_value)
                        return literal_value
                    continue
                nested_expr = candidate
                if camelize:
                    nested_expr = "camelize:" + nested_expr
                if mode == "json":
                    nested_expr = "json:" + nested_expr
                value = _transform_template_value("{{" + nested_expr + "}}", payload)
                if value not in (None, "", [], {}):
                    return value
            return None
        literal_ok, literal_value = _parse_transform_literal(path)
        if literal_ok:
            if camelize:
                literal_value = _camelize_json_keys(literal_value)
            return literal_value
        if path.startswith("payload."):
            path = path[8:].strip()
        value = _lookup_payload_path(payload, path)
        if mode == "json":
            if value in (None, ""):
                return None
            if isinstance(value, (dict, list)):
                if camelize:
                    return _camelize_json_keys(value)
                return value
            parsed_json = json.loads(str(value))
            if camelize:
                return _camelize_json_keys(parsed_json)
            return parsed_json
        if camelize:
            return _camelize_json_keys(value)
        return value
    return template


def _http_action_error_hint(op_id: str, action: dict[str, Any], result: dict[str, Any]) -> str:
    try:
        status = int(result.get("status"))
    except Exception:
        return ""
    if status != 404:
        return ""

    op = str(op_id or "").strip().lower()
    path = str(action.get("path") or "").strip().lower()
    url = str(result.get("url") or "").strip().lower()
    if op == "importcoursepackage" or "/api/agent/import/course-package" in path or "/api/agent/import/course-package" in url:
        return (
            " Endpoint /api/agent/import/course-package is not available on the target server. "
            "Deploy GlobeIQ build with agent bulk import support (commit 03f1270 or later) "
            "or update the connection base_url to the server that hosts the agent API."
        )
    if path.startswith("/api/agent/") or "/api/agent/" in url:
        return " Target server does not expose the requested /api/agent route. Verify base_url and deployed GlobeIQ API version."
    return ""


def _contract_prompt_suffix(bot: Any) -> str:
    routing_rules = getattr(bot, "routing_rules", None)
    if not isinstance(routing_rules, dict):
        return ""
    contract = routing_rules.get("output_contract")
    if not isinstance(contract, dict) or not bool(contract.get("enabled", False)):
        return ""
    if str(contract.get("mode") or "model_output").strip().lower() != "model_output":
        return ""
    parts: list[str] = []
    output_format = str(contract.get("format") or "any").strip().lower()
    required_fields = contract.get("required_fields")
    non_empty_fields = contract.get("non_empty_fields")
    description = str(contract.get("description") or "").strip()
    example_output = contract.get("example_output")
    fallback_mode = str(contract.get("fallback_mode") or "").strip().lower()

    if description:
        parts.append(description)
    if output_format == "json_object":
        parts.append("Return exactly one JSON object.")
    elif output_format == "json_array":
        parts.append("Return exactly one JSON array.")
    if isinstance(required_fields, list) and required_fields:
        parts.append(f"Required top-level fields: {', '.join(str(field) for field in required_fields)}.")
    if isinstance(non_empty_fields, list) and non_empty_fields:
        parts.append(f"Fields that must be populated: {', '.join(str(field) for field in non_empty_fields)}.")
    if fallback_mode == "disabled":
        parts.append("Do not omit required content. Missing or empty required fields will fail the run.")
    if isinstance(example_output, dict) and example_output:
        parts.append("Example output JSON:")
        parts.append(json.dumps(example_output, ensure_ascii=False, indent=2))
    if not parts:
        return ""
    return "\n\nOutput contract:\n" + "\n".join(parts)


def _connection_context_config(bot: Any) -> dict[str, Any]:
    routing_rules = getattr(bot, "routing_rules", None)
    config = routing_rules.get("connection_context") if isinstance(routing_rules, dict) else None
    return config if isinstance(config, dict) else {}


def _load_attached_connection_rows(bot_id: str) -> list[Any]:
    try:
        resolver = ConnectionResolver()
        return resolver.list_bot_connections(str(bot_id))
    except Exception as exc:
        logger.warning("Failed to load attached bot connections for %s: %s", bot_id, exc)
        return []


def _resolve_attached_connection(
    rows: list[Any],
    *,
    requested_name: str | None = None,
    requested_id: str | None = None,
) -> Any | None:
    if requested_id:
        match = next(
            (
                row
                for row in rows
                if str((row.get("id") if isinstance(row, dict) else getattr(row, "id", "")) or "") == str(requested_id)
            ),
            None,
        )
        if match is not None:
            return match
    if requested_name:
        match = next(
            (
                row
                for row in rows
                if str((row.get("name") if isinstance(row, dict) else getattr(row, "name", "")) or "").strip().lower()
                == str(requested_name).strip().lower()
            ),
            None,
        )
        if match is not None:
            return match
    if len(rows) == 1:
        return rows[0]
    return None


def _connection_row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _normalize_payload_path(path: str) -> str:
    cleaned = str(path or "").strip()
    if cleaned.startswith("payload."):
        cleaned = cleaned[8:].strip()
    return cleaned


def _render_loop_template(template: Any, *, item: Any, item_index: int) -> Any:
    if isinstance(template, dict):
        return {str(key): _render_loop_template(value, item=item, item_index=item_index) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_loop_template(value, item=item, item_index=item_index) for value in template]
    if not isinstance(template, str):
        return template

    raw = template.strip()
    if raw == "{{item_json}}":
        return item
    if raw == "{{item_index}}":
        return item_index
    if raw == "{{item}}":
        return item if isinstance(item, (dict, list, int, float, bool)) else str(item)

    rendered = template.replace("{{item_index}}", str(item_index))
    if "{{item_json}}" in rendered:
        rendered = rendered.replace("{{item_json}}", json.dumps(item, ensure_ascii=False))
    if "{{item}}" in rendered:
        rendered = rendered.replace("{{item}}", str(item))
    return rendered


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[TRUNCATED]"


def _serialized_payload_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except Exception:
        return len(str(value or ""))


def _compact_text_with_edges(
    value: Any,
    *,
    limit: int,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> str:
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    if head_chars is None:
        head_chars = max(120, int(limit * 0.65))
    if tail_chars is None:
        tail_chars = max(80, limit - head_chars - 64)
    head_chars = max(40, head_chars)
    tail_chars = max(24, tail_chars)
    if head_chars + tail_chars >= max(0, limit - 32):
        tail_chars = max(24, limit - head_chars - 32)
    omitted_chars = max(0, len(text) - head_chars - tail_chars)
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip() if tail_chars > 0 else ""
    omission = f"\n...[{omitted_chars} chars omitted for context]...\n"
    compacted = head + omission + tail
    if len(compacted) <= limit + 64:
        return compacted
    return compacted[: limit + 64].rstrip()


def _assignment_transcript_priority_terms(scope: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    raw_terms: list[str] = []
    for item in (
        scope.get("request_text"),
        payload.get("assignment_request"),
        scope.get("conversation_brief"),
    ):
        raw_terms.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{3,}", str(item or "").lower()))
    focus_topics = scope.get("focus_topics")
    if isinstance(focus_topics, list):
        raw_terms.extend(str(item or "").strip().lower() for item in focus_topics if str(item or "").strip())
    ordered: list[str] = []
    seen: set[str] = set()
    stop_words = {
        "assignment",
        "build",
        "chat",
        "docs",
        "documentation",
        "feature",
        "help",
        "implementation",
        "math",
        "message",
        "messages",
        "plan",
        "please",
        "project",
        "task",
        "that",
        "this",
        "user",
        "with",
    }
    for item in raw_terms:
        normalized = str(item or "").strip().lower()
        if len(normalized) < 4 or normalized in stop_words or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
        if len(ordered) >= 18:
            break
    return ordered


def _reduce_assignment_transcript_for_context(
    transcript: Any,
    *,
    scope: dict[str, Any],
    payload: dict[str, Any],
    max_chars: int = _ASSIGNMENT_TRANSCRIPT_REDUCTION_CHARS,
    max_lines: int = 24,
    head_lines: int = 4,
    tail_lines: int = 4,
) -> tuple[str, str]:
    text = str(transcript or "").strip()
    if not text:
        return "", ""
    if len(text) <= max_chars:
        return text, ""

    rendered_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not rendered_lines:
        return _compact_text_with_edges(text, limit=max_chars), "assignment_scope.conversation_transcript"

    priority_terms = _assignment_transcript_priority_terms(scope, payload)
    kept_indices: list[int] = []
    kept_index_set: set[int] = set()

    def _keep(index: int) -> None:
        if index < 0 or index >= len(rendered_lines) or index in kept_index_set:
            return
        kept_index_set.add(index)
        kept_indices.append(index)

    for index in range(min(head_lines, len(rendered_lines))):
        _keep(index)
    for index in range(max(0, len(rendered_lines) - tail_lines), len(rendered_lines)):
        _keep(index)

    ranked: list[tuple[int, int]] = []
    for index, line in enumerate(rendered_lines):
        if index in kept_index_set:
            continue
        lowered = line.lower()
        score = 0
        if lowered.startswith("user:"):
            score += 5
        elif lowered.startswith("assistant:"):
            score += 2
        if any(marker in lowered for marker in ("must", "should", "need", "avoid", "do not", "don't", "prefer", "required", "deliver")):
            score += 2
        score += min(4, sum(1 for term in priority_terms if term and term in lowered))
        if score > 0:
            ranked.append((score, index))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    for _, index in ranked:
        if len(kept_indices) >= max_lines:
            break
        projected = [rendered_lines[item] for item in sorted([*kept_indices, index])]
        candidate = "\n".join(projected)
        if len(candidate) > max_chars:
            continue
        _keep(index)

    compacted_lines = [rendered_lines[index] for index in sorted(kept_indices)]
    omitted_count = max(0, len(rendered_lines) - len(compacted_lines))
    if omitted_count > 0:
        insert_at = min(head_lines, len(compacted_lines))
        compacted_lines.insert(insert_at, f"... ({omitted_count} chat line(s) omitted for context) ...")
    compacted = "\n".join(compacted_lines)
    if len(compacted) > max_chars:
        compacted = _compact_text_with_edges(compacted, limit=max_chars)
    return compacted, "assignment_scope.conversation_transcript"


def _compact_string_fields_for_context(value: Any, *, string_limit: int = _LONG_STRING_REDUCTION_CHARS) -> Any:
    if isinstance(value, str):
        return _compact_text_with_edges(value, limit=string_limit)
    if isinstance(value, list):
        return [_compact_string_fields_for_context(item, string_limit=string_limit) for item in value]
    if isinstance(value, dict):
        return {
            key: _compact_string_fields_for_context(item, string_limit=string_limit)
            for key, item in value.items()
        }
    return value


def _reduce_artifact_entry_for_context(item: Any) -> Any:
    if not isinstance(item, dict):
        return _compact_string_fields_for_context(item, string_limit=_LONG_STRING_REDUCTION_CHARS)
    reduced = dict(item)
    content = reduced.get("content")
    if content is not None:
        reduced["content"] = _compact_text_with_edges(
            content,
            limit=_ARTIFACT_CONTENT_REDUCTION_CHARS,
            head_chars=1100,
            tail_chars=400,
        )
        if str(content or "").strip() != str(reduced["content"] or "").strip():
            reduced["content_truncated_for_context"] = True
    for key, value in list(reduced.items()):
        if key == "content":
            continue
        reduced[key] = _compact_string_fields_for_context(value, string_limit=400)
    return reduced


def _reduce_artifact_list_for_context(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, list):
        return value, False
    reduced = [_reduce_artifact_entry_for_context(item) for item in value]
    return reduced, reduced != value


def _summarize_result_dict_for_context(result: dict[str, Any]) -> dict[str, Any]:
    preferred_keys = [
        "status",
        "outcome",
        "failure_type",
        "summary",
        "findings",
        "evidence",
        "implementation_plan",
        "implementation_workstreams",
        "artifacts",
        "handoff_notes",
        "risks",
        "recommendations",
        "questions",
        "notes",
    ]
    reduced: dict[str, Any] = {}
    for key in preferred_keys:
        if key not in result:
            continue
        value = result.get(key)
        if key == "artifacts":
            reduced[key], _ = _reduce_artifact_list_for_context(value)
            continue
        if isinstance(value, list):
            items = value[:_JOIN_RESULT_LIST_MAX_ITEMS]
            reduced[key] = [_compact_string_fields_for_context(item, string_limit=500) for item in items]
            continue
        if isinstance(value, dict):
            reduced[key] = _compact_string_fields_for_context(value, string_limit=600)
            continue
        reduced[key] = _compact_string_fields_for_context(value, string_limit=600)
    if not reduced:
        return _compact_string_fields_for_context(result, string_limit=500)
    return reduced


def _looks_like_join_branch_payload(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return any(
        key in item
        for key in (
            "source_result",
            "source_task_id",
            "title",
            "instruction",
            "deliverables",
            "upstream_artifacts",
            "fanout_branch_key",
        )
    )


def _summarize_join_branch_payload_for_context(item: Any) -> Any:
    if not isinstance(item, dict):
        return _compact_string_fields_for_context(item, string_limit=500)
    preferred_keys = [
        "source_task_id",
        "source_bot_id",
        "title",
        "instruction",
        "role_hint",
        "step_kind",
        "path",
        "deliverables",
        "acceptance_criteria",
        "quality_gates",
        "evidence_requirements",
        "upstream_failure_type",
        "upstream_handoff_notes",
        "upstream_artifacts",
        "workstream",
        "fanout_branch_key",
        "source_result",
    ]
    reduced: dict[str, Any] = {}
    for key in preferred_keys:
        if key not in item:
            continue
        value = item.get(key)
        if key in {"upstream_artifacts"}:
            reduced[key], _ = _reduce_artifact_list_for_context(value)
            continue
        if key == "source_result" and isinstance(value, dict):
            reduced[key] = _summarize_result_dict_for_context(value)
            continue
        if isinstance(value, list):
            reduced[key] = [_compact_string_fields_for_context(entry, string_limit=400) for entry in value[:12]]
            continue
        if isinstance(value, dict):
            reduced[key] = _compact_string_fields_for_context(value, string_limit=450)
            continue
        reduced[key] = _compact_string_fields_for_context(value, string_limit=500)
    return reduced or _compact_string_fields_for_context(item, string_limit=500)


def _reduce_join_payload_fields_for_context(payload: dict[str, Any]) -> list[str]:
    reductions: list[str] = []
    join_count = int(payload.get("join_count") or 0)
    for key, value in list(payload.items()):
        if not isinstance(value, list) or not value:
            continue
        if key == "join_results":
            reduced = [
                _summarize_result_dict_for_context(item)
                if isinstance(item, dict)
                else _compact_string_fields_for_context(item, string_limit=500)
                for item in value[: max(_JOIN_RESULT_LIST_MAX_ITEMS, join_count)]
            ]
            if reduced != value:
                payload[key] = reduced
                reductions.append(key)
            continue
        if key == "join_task_ids":
            continue
        if join_count > 1 and len(value) == join_count and any(_looks_like_join_branch_payload(item) for item in value):
            reduced = [_summarize_join_branch_payload_for_context(item) for item in value]
            if reduced != value:
                payload[key] = reduced
                reductions.append(key)
    return reductions


def _looks_like_join_context_payload(payload: dict[str, Any]) -> bool:
    join_count = int(payload.get("join_count") or 0)
    if join_count > 1:
        return True
    for key in ("research_payloads", "research_branches", "join_results", "upstream_artifacts"):
        value = payload.get(key)
        if isinstance(value, list) and len(value) > 1:
            if key == "upstream_artifacts":
                return True
            if any(_looks_like_join_branch_payload(item) or isinstance(item, dict) for item in value):
                return True
    return False


def _reduce_payload_for_context_limits(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if not _looks_like_join_context_payload(payload):
        return payload

    original_chars = _serialized_payload_chars(payload)
    if original_chars <= _PAYLOAD_CONTEXT_REDUCTION_TARGET_CHARS:
        return payload

    reduced_payload = copy.deepcopy(payload)
    reduced_fields: list[str] = []

    scope = reduced_payload.get("assignment_scope")
    if isinstance(scope, dict):
        reduced_transcript, transcript_field = _reduce_assignment_transcript_for_context(
            scope.get("conversation_transcript"),
            scope=scope,
            payload=reduced_payload,
        )
        if transcript_field and reduced_transcript != scope.get("conversation_transcript"):
            scope["conversation_transcript"] = reduced_transcript
            strategy = str(scope.get("conversation_transcript_strategy") or "").strip().lower()
            if strategy in {"", "full"}:
                scope["conversation_transcript_strategy"] = "context_reduced_excerpt"
            else:
                scope["conversation_transcript_strategy"] = f"{strategy}+context_reduced"
            reduced_fields.append(transcript_field)

    upstream_artifacts, upstream_changed = _reduce_artifact_list_for_context(reduced_payload.get("upstream_artifacts"))
    if upstream_changed:
        reduced_payload["upstream_artifacts"] = upstream_artifacts
        reduced_fields.append("upstream_artifacts")

    source_result = reduced_payload.get("source_result")
    if isinstance(source_result, dict) and "artifacts" in source_result:
        reduced_artifacts, changed = _reduce_artifact_list_for_context(source_result.get("artifacts"))
        if changed:
            source_result["artifacts"] = reduced_artifacts
            reduced_fields.append("source_result.artifacts")

    reduced_fields.extend(_reduce_join_payload_fields_for_context(reduced_payload))

    final_chars = _serialized_payload_chars(reduced_payload)
    if final_chars >= original_chars:
        return payload

    reduced_payload["context_reduction"] = {
        "applied": True,
        "original_payload_chars": original_chars,
        "reduced_payload_chars": _serialized_payload_chars(reduced_payload),
        "reduced_fields": reduced_fields,
    }
    return reduced_payload


def _static_connection_context_prompt(rows: list[Any], config: dict[str, Any]) -> str:
    if not rows:
        return ""

    include_schema = bool(config.get("include_schema", True))
    include_actions = bool(config.get("include_actions", True))
    max_schema_chars = max(500, int(config.get("max_schema_chars") or 12000))
    max_total_chars = max(1000, int(config.get("max_total_chars") or 24000))
    max_actions = max(1, int(config.get("max_actions") or 24))
    requested_name = str(config.get("connection_name") or "").strip()

    target_rows = rows
    if requested_name:
        selected = _resolve_attached_connection(rows, requested_name=requested_name)
        target_rows = [selected] if selected is not None else []
    if not target_rows:
        return ""

    parts: list[str] = [
        "Attached connection schemas:",
        "Use these attached connection definitions as authoritative for field names, nesting, and allowed JSON shapes.",
        "Do not invent fields outside the attached schemas and examples.",
    ]
    remaining_chars = max_total_chars

    for row in target_rows:
        row_name = str(_connection_row_value(row, "name", "") or "").strip()
        row_id = str(_connection_row_value(row, "id", "") or "").strip()
        row_kind = str(_connection_row_value(row, "kind", "") or "").strip().lower()
        section: list[str] = [f"Connection: {row_name or row_id} ({row_kind or 'unknown'})"]
        description = str(_connection_row_value(row, "description", "") or "").strip()
        if description:
            section.append(f"Description: {description}")

        raw_config = _connection_row_value(row, "config", None)
        if isinstance(raw_config, dict):
            connection_config = raw_config
        else:
            try:
                connection_config = json.loads(str(_connection_row_value(row, "config_json", "{}") or "{}"))
            except Exception:
                connection_config = {}
        if isinstance(connection_config, dict):
            connection_config = connection_secrets.mask_connection_config(connection_config)
        if isinstance(connection_config, dict):
            if row_kind == "http":
                base_url = str(connection_config.get("base_url") or "").strip()
                if base_url:
                    section.append(f"Base URL: {base_url}")
            if row_kind == "database":
                readonly = bool(connection_config.get("readonly", False))
                section.append(f"Readonly: {'true' if readonly else 'false'}")

        schema_text = str(_connection_row_value(row, "schema_text", "") or "").strip()
        if include_actions and row_kind == "http" and schema_text:
            try:
                actions = connection_runtime.parse_openapi_actions(schema_text)
            except Exception:
                actions = []
            if actions:
                formatted_actions = []
                for action in actions[:max_actions]:
                    op = str(action.get("operation_id") or "").strip()
                    method = str(action.get("method") or "").strip().upper()
                    path = str(action.get("path") or "").strip()
                    formatted_actions.append(f"{op} [{method} {path}]".strip())
                section.append("Available actions: " + ", ".join(item for item in formatted_actions if item).strip())

        if include_schema and schema_text:
            section.append("Schema and examples:")
            section.append(_truncate_text(schema_text, max_schema_chars))

        rendered = "\n".join(item for item in section if str(item).strip()).strip()
        if not rendered:
            continue
        if len(rendered) > remaining_chars:
            rendered = _truncate_text(rendered, remaining_chars)
        if not rendered:
            break
        parts.append(rendered)
        remaining_chars -= len(rendered)
        if remaining_chars <= 0:
            break

    if len(parts) <= 3:
        return ""
    return "\n\n" + "\n\n".join(parts)


def _dynamic_connection_fetch_prompt(rows: list[Any], config: dict[str, Any], payload: Any) -> str:
    fetch_templates = config.get("fetch_actions")
    if isinstance(fetch_templates, dict):
        fetch_templates = [fetch_templates]
    if not isinstance(fetch_templates, list) or not fetch_templates:
        return ""

    connection = _resolve_attached_connection(
        rows,
        requested_name=str(config.get("fetch_connection_name") or config.get("connection_name") or "").strip() or None,
        requested_id=str(config.get("fetch_connection_id") or "").strip() or None,
    )
    if connection is None:
        return ""

    raw_config = _connection_row_value(connection, "config", None)
    if isinstance(raw_config, dict):
        connection_config = raw_config
    else:
        try:
            connection_config = json.loads(str(_connection_row_value(connection, "config_json", "{}") or "{}"))
        except Exception:
            connection_config = {}
    if isinstance(connection_config, dict):
        connection_config = connection_secrets.resolve_connection_config(connection_config)
    raw_auth = _connection_row_value(connection, "auth", None)
    if isinstance(raw_auth, dict):
        auth_payload = connection_secrets.resolve_auth_payload(raw_auth)
    else:
        try:
            auth_payload = connection_secrets.resolve_auth_payload(json.loads(str(_connection_row_value(connection, "auth_json", "{}") or "{}")))
        except Exception:
            auth_payload = {}
    schema_text = str(_connection_row_value(connection, "schema_text", "") or "")

    allow_mutating_fetch = bool(config.get("allow_mutating_fetch", False))
    response_chars = max(500, int(config.get("fetch_response_chars") or 5000))
    max_items = max(1, int(config.get("max_items") or 40))
    for_each_field = _normalize_payload_path(str(config.get("for_each_field") or ""))
    items: list[Any]
    if for_each_field:
        resolved = _lookup_payload_path(payload, for_each_field)
        if not isinstance(resolved, list) or not resolved:
            return ""
        items = list(resolved[:max_items])
    else:
        items = [None]

    actions: list[tuple[str, dict[str, Any]]] = []
    for item_index, item in enumerate(items):
        for template in fetch_templates:
            if not isinstance(template, dict):
                continue
            expanded = _render_loop_template(template, item=item, item_index=item_index) if item is not None else template
            action = _transform_template_value(expanded, payload)
            if not isinstance(action, dict):
                continue
            method = str(action.get("method") or "GET").strip().upper()
            if method not in {"GET", "HEAD", "OPTIONS"} and not allow_mutating_fetch:
                logger.warning("Skipping mutating connection-context fetch for bot payload because method %s is not allowed", method)
                continue
            label = str(action.get("operation_id") or action.get("path") or f"fetch_{len(actions) + 1}").strip()
            if item is not None:
                label = f"{label} [{item}]"
            actions.append((label, action))

    if not actions:
        return ""

    sections: list[str] = []
    for label, action in actions:
        result = connection_runtime.test_http_connection(
            config=connection_config if isinstance(connection_config, dict) else {},
            auth=auth_payload if isinstance(auth_payload, dict) else {},
            schema_text=schema_text,
            payload=action,
        )
        preview = str(result.get("body_preview") or "").strip()
        if preview:
            try:
                preview = json.dumps(json.loads(preview), ensure_ascii=False, indent=2)
            except Exception:
                pass
        rendered = "\n".join(
            part
            for part in [
                f"Fetch: {label}",
                f"Status: {result.get('status')}",
                f"URL: {result.get('url')}",
                "Response:",
                _truncate_text(preview or "{}", response_chars),
            ]
            if str(part).strip()
        ).strip()
        sections.append(rendered)

    if not sections:
        return ""
    return "\n\nDynamic connection fetch results:\n" + "\n\n".join(sections)


def _connection_context_prompt_suffix(bot_id: str, bot: Any, payload: Any) -> str:
    config = _connection_context_config(bot)
    if config and not bool(config.get("enabled", True)):
        return ""

    rows = _load_attached_connection_rows(bot_id)
    if not rows:
        return ""

    parts = [
        _static_connection_context_prompt(rows, config),
        _dynamic_connection_fetch_prompt(rows, config, payload),
    ]
    rendered = "\n".join(part for part in parts if str(part).strip()).strip()
    if not rendered:
        return ""
    return "\n\n" + rendered


def _retry_prompt_suffix(task: Task | None) -> str:
    if task is None or task.error is None:
        return ""
    metadata = task.metadata
    retry_attempt = int(metadata.retry_attempt or 0) if metadata is not None else 0
    if retry_attempt <= 0:
        return ""
    error_message = str(task.error.message or "").strip()
    if not error_message:
        return ""
    guidance = [
        f"Retry attempt: {retry_attempt}.",
        "Previous attempt failed with this error:",
        error_message,
        "Correct that exact issue on this retry while preserving the original scope and output contract.",
    ]
    lowered = error_message.lower()
    if "broken internal markdown links" in lowered:
        guidance.append(
            "For documentation files, resolve internal markdown links relative to the generated file path. "
            "Only link to markdown docs that actually exist in the upstream artifacts, the current deliverables, or the live repository."
        )
        available_docs = _payload_available_markdown_paths(task.payload if task is not None else None)
        if available_docs:
            guidance.append("Available markdown docs for this branch and upstream context:")
            guidance.extend(f"- {path}" for path in available_docs[:24])
            suggestions = _broken_link_retry_suggestions(error_message, available_docs)
            if suggestions:
                guidance.append("Likely link corrections:")
                guidance.extend(f"- {item}" for item in suggestions[:12])
    if "outside its assigned deliverables" in lowered:
        guidance.append(
            "Only emit the markdown files explicitly assigned in this workstream. "
            "Do not add extra documentation files outside the listed deliverables."
        )
    return "\n\nRetry guidance:\n" + "\n".join(guidance)


def _looks_like_markdown_repo_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/").strip("`")
    return bool(text) and "/" in text and text.lower().endswith(".md")


def _looks_like_repo_path_target(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/").strip("`")
    if not text:
        return False
    if "/" in text:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", text))


def _collect_markdown_paths(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        raw_path = ""
        if isinstance(item, dict):
            raw_path = str(item.get("path") or item.get("label") or "").strip()
        elif isinstance(item, str):
            raw_path = item.strip()
        normalized = raw_path.replace("\\", "/").strip("`")
        if not _looks_like_markdown_repo_path(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        paths.append(normalized)
    return paths


def _payload_available_markdown_paths(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()

    def _add(items: list[str]) -> None:
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            paths.append(item)

    _add(_collect_markdown_paths(payload.get("deliverables")))
    _add(_collect_markdown_paths(payload.get("upstream_artifacts")))
    source_result = payload.get("source_result")
    if isinstance(source_result, dict):
        _add(_collect_markdown_paths(source_result.get("artifacts")))
    workstream = payload.get("workstream")
    if isinstance(workstream, dict):
        _add(_collect_markdown_paths(workstream.get("deliverables")))
    return paths


def _broken_link_retry_suggestions(error_message: str, available_docs: list[str]) -> list[str]:
    suggestions: list[str] = []
    available_by_name = {PurePosixPath(path).name: path for path in available_docs}
    matches = re.findall(r"([A-Za-z0-9_./\\-]+\.md)\s*->\s*([A-Za-z0-9_./\\-]+\.md(?:#[^\s,]+)?)", str(error_message or ""))
    for source_path, broken_ref in matches:
        source = PurePosixPath(source_path.replace("\\", "/"))
        broken_target = PurePosixPath(broken_ref.split("#", 1)[0].replace("\\", "/"))
        candidate = available_by_name.get(broken_target.name)
        if not candidate:
            continue
        try:
            corrected = os.path.relpath(candidate, start=str(source.parent)).replace("\\", "/")
        except Exception:
            continue
        suggestions.append(f"{source_path}: replace `{broken_ref}` with `{corrected}`")
    return suggestions


def _workspace_context_prompt_suffix(payload: Any) -> str:
    """Build a system prompt suffix from pre-fetched workspace context items."""
    if not isinstance(payload, dict):
        return ""
    items = payload.get("workspace_context_items")
    if not isinstance(items, list) or not items:
        return ""
    tree = str(payload.get("workspace_context_tree") or "").strip()
    parts: list[str] = ["Workspace context (pre-fetched from the project repository):"]
    if tree:
        parts.append(f"Directory tree:\n```\n{tree}\n```")
    for item in items:
        text = str(item or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _prepare_system_prompt(bot: Any, *, bot_id: str | None = None, payload: Any = None, task: Task | None = None) -> str | None:
    base = str(getattr(bot, "system_prompt", None) or "").strip()
    suffix_parts: list[str] = []
    contract_suffix = _contract_prompt_suffix(bot).strip()
    if contract_suffix:
        suffix_parts.append(contract_suffix)
    repo_output_policy_suffix = _repo_output_policy_prompt_suffix(bot, payload=payload).strip()
    if repo_output_policy_suffix:
        suffix_parts.append(repo_output_policy_suffix)
    assignment_scope_suffix = _assignment_scope_prompt_suffix(payload).strip()
    if assignment_scope_suffix:
        suffix_parts.append(assignment_scope_suffix)
    database_contract_suffix = _pm_database_contract_prompt_suffix(bot_id, payload).strip()
    if database_contract_suffix:
        suffix_parts.append(database_contract_suffix)
    if bot_id:
        connection_suffix = _connection_context_prompt_suffix(bot_id, bot, payload).strip()
        if connection_suffix:
            suffix_parts.append(connection_suffix)
    workspace_context_suffix = _workspace_context_prompt_suffix(payload).strip()
    if workspace_context_suffix:
        suffix_parts.append(workspace_context_suffix)
    retry_suffix = _retry_prompt_suffix(task).strip()
    if retry_suffix:
        suffix_parts.append(retry_suffix)
    suffix = "\n".join(part for part in suffix_parts if part).strip()
    if not suffix:
        return base or None
    if not base:
        return suffix
    if suffix in base:
        return base
    return f"{base}\n{suffix}"


def _repo_output_policy_prompt_suffix(bot: Any, payload: Any = None) -> str:
    if bot_allows_repo_output(bot):
        return ""
    if not isinstance(payload, dict):
        return (
            "\n\nExecution policy:\n"
            "This bot is not allowed to emit repo file outputs. Do not create, modify, or return repo file artifacts."
        )

    deliverables = payload.get("deliverables")
    workstream = payload.get("workstream") if isinstance(payload.get("workstream"), dict) else {}
    repo_like_targets = []
    deliverable_items = deliverables if isinstance(deliverables, list) else [deliverables]
    for item in deliverable_items:
        if _looks_like_repo_path_target(item):
            repo_like_targets.append(item)
    workstream_deliverables = workstream.get("deliverables")
    workstream_items = workstream_deliverables if isinstance(workstream_deliverables, list) else [workstream_deliverables]
    for item in workstream_items:
        if _looks_like_repo_path_target(item):
            repo_like_targets.append(item)
    step_kind = str(payload.get("step_kind") or "").strip().lower()
    if not repo_like_targets and step_kind not in {"repo_change", "implementation", "coding"}:
        return (
            "\n\nExecution policy:\n"
            "This bot is validation-only or planning-only. Do not create, modify, or return repo file artifacts."
        )
    return (
        "\n\nExecution policy:\n"
        "This bot has execution_policy.repo_output_mode=deny.\n"
        "Do not create, modify, or return repo file artifacts, full file contents, or `artifacts` entries with repo-style `path` values.\n"
        "Treat any repo-style deliverables as read-only validation or planning targets only.\n"
        "If the task appears to require repo outputs, report that contract mismatch in findings/evidence/handoff_notes instead of attempting file generation."
    )


def _prepare_payload_for_backend(bot: Any, backend: BackendConfig, payload: Any, *, task: Task | None = None) -> Any:
    if backend.type in {"browser", "documentation"}:
        if _is_autonomous_schedule_task(task) and isinstance(payload, dict):
            schedule_envelope_fields = {"instruction", "source", "schedule_id", "project_id", "node_overrides"}
            return {key: value for key, value in payload.items() if key not in schedule_envelope_fields}
        return payload
    if backend.type == "custom":
        return payload
    payload = _reduce_payload_for_context_limits(payload)
    return _inject_system_prompt(
        _prepare_system_prompt(bot, bot_id=getattr(task, "bot_id", None), payload=payload, task=task),
        payload,
    )


def _parse_data_url(data_url: str) -> tuple[str, str] | None:
    text = str(data_url or "").strip()
    if not text.startswith("data:") or ";base64," not in text:
        return None
    header, encoded = text.split(",", 1)
    mime_type = header[len("data:"):].split(";", 1)[0].strip().lower() or "application/octet-stream"
    if not encoded:
        return None
    return mime_type, encoded


def _normalize_message_parts_for_provider(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        parts = [item for item in content if isinstance(item, dict)]
        if parts:
            return parts
    return [{"type": "text", "text": str(content or "")}]


def _messages_for_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        parts = _normalize_message_parts_for_provider(message.get("content"))
        content_parts: list[dict[str, Any]] = []
        for part in parts:
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                url = str(image.get("url") or "").strip()
                if url:
                    content_parts.append({"type": "image_url", "image_url": {"url": url}})
                continue
            content_parts.append({"type": "text", "text": str(part.get("text") or "")})
        if len(content_parts) == 1 and content_parts[0]["type"] == "text":
            entry: dict[str, Any] = {"role": role, "content": content_parts[0]["text"]}
        else:
            entry = {"role": role, "content": content_parts}
        if role == "assistant":
            tool_calls = _tool_calls_for_openai(message.get("tool_calls"))
            if tool_calls:
                entry["tool_calls"] = tool_calls
                if not str(entry.get("content") or "").strip():
                    entry["content"] = None
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            tool_name = str(message.get("name") or message.get("tool_name") or "").strip()
            if tool_name:
                entry["name"] = tool_name
        normalized.append(entry)
    return normalized


def _tool_calls_for_openai(raw_tool_calls: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tool_calls or []):
        if not isinstance(item, dict):
            continue
        function_obj = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(item.get("name") or function_obj.get("name") or "").strip()
        if not name:
            continue
        tool_call_id = str(item.get("id") or f"call_{index + 1}").strip()
        raw_arguments = function_obj.get("arguments", item.get("arguments", {}))
        arguments_payload = "{}"
        if isinstance(raw_arguments, str):
            arguments_payload = raw_arguments
        elif isinstance(raw_arguments, dict):
            arguments_payload = json.dumps(raw_arguments, ensure_ascii=False)
        normalized.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments_payload,
                },
            }
        )
    return normalized


def _messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        parts = _normalize_message_parts_for_provider(message.get("content"))
        text_parts: list[str] = []
        images: list[str] = []
        for part in parts:
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                parsed = _parse_data_url(str(image.get("url") or ""))
                if parsed is not None:
                    _, encoded = parsed
                    images.append(encoded)
                continue
            text = str(part.get("text") or "")
            if text:
                text_parts.append(text)
        entry: dict[str, Any] = {"role": role, "content": "\n\n".join(text_parts)}
        if images:
            entry["images"] = images
        if role == "assistant":
            tool_calls = _tool_calls_for_ollama(message.get("tool_calls"))
            if tool_calls:
                entry["tool_calls"] = tool_calls
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            tool_name = str(message.get("name") or message.get("tool_name") or "").strip()
            if tool_name:
                entry["name"] = tool_name
                entry["tool_name"] = tool_name
        normalized.append(entry)
    return normalized


def _tool_calls_for_ollama(raw_tool_calls: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tool_calls or []):
        if not isinstance(item, dict):
            continue
        function_obj = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(item.get("name") or function_obj.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function_obj.get("arguments", item.get("arguments", {}))
        arguments_payload: dict[str, Any]
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
                arguments_payload = parsed if isinstance(parsed, dict) else {}
            except Exception:
                arguments_payload = {}
        elif isinstance(raw_arguments, dict):
            arguments_payload = raw_arguments
        else:
            arguments_payload = {}
        tool_call: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments_payload,
            },
        }
        tool_call_id = str(item.get("id") or f"call_{index + 1}").strip()
        if tool_call_id:
            tool_call["id"] = tool_call_id
        normalized.append(tool_call)
    return normalized


def _claude_payload_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_chunks: list[str] = []
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        parts = _normalize_message_parts_for_provider(message.get("content"))
        content_parts: list[dict[str, Any]] = []
        for part in parts:
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                parsed = _parse_data_url(str(image.get("url") or ""))
                if parsed is None:
                    continue
                mime_type, encoded = parsed
                content_parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": encoded,
                        },
                    }
                )
                continue
            content_parts.append({"type": "text", "text": str(part.get("text") or "")})
        if role == "system":
            text_only = "\n\n".join(str(part.get("text") or "") for part in content_parts if part.get("type") == "text").strip()
            if text_only:
                system_chunks.append(text_only)
            continue
        normalized.append({"role": "assistant" if role == "assistant" else "user", "content": content_parts or [{"type": "text", "text": ""}]})
    system_prompt = "\n\n".join(chunk for chunk in system_chunks if chunk).strip() or None
    return system_prompt, normalized


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        parts = _normalize_message_parts_for_provider(message.get("content"))
        gemini_parts: list[dict[str, Any]] = []
        for part in parts:
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                parsed = _parse_data_url(str(image.get("url") or ""))
                if parsed is None:
                    continue
                mime_type, encoded = parsed
                gemini_parts.append({"inline_data": {"mime_type": mime_type, "data": encoded}})
                continue
            gemini_parts.append({"text": str(part.get("text") or "")})
        if gemini_parts:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": gemini_parts})
    return contents


class Scheduler:
    def __init__(
        self,
        bot_registry: Any,
        worker_registry: Any,
        key_vault: Any = None,
        model_registry: Any = None,
        project_registry: Any = None,
        connection_resolver: Any = None,
        worker_probe_store: Any = None,
        browser_action_approval_store: Any = None,
        connection_action_approval_store: Any = None,
    ) -> None:
        self.bot_registry = bot_registry
        self.worker_registry = worker_registry
        self.key_vault = key_vault
        self.model_registry = model_registry
        self.project_registry = project_registry
        self._connection_resolver = connection_resolver or ConnectionResolver()
        self._worker_probe_store = worker_probe_store
        self._browser_action_approval_store = browser_action_approval_store
        self._connection_action_approval_store = connection_action_approval_store
        self._inflight_by_worker: dict[str, int] = {}
        self._latency_ema_ms: dict[str, float] = {}
        self._latency_alpha = float(os.environ.get("NEXUSAI_WORKER_LATENCY_EMA_ALPHA", "0.30"))
        self._default_latency_ms = float(os.environ.get("NEXUSAI_WORKER_DEFAULT_LATENCY_MS", "800"))
        self._vertex_token_cache: dict[str, tuple[str, float]] = {}
        self._execution_provenance_by_task: dict[str, dict[str, Any]] = {}

    def _record_execution_provenance(
        self,
        task: "Task | None",
        backend: "BackendConfig",
        *,
        worker: "Worker | None" = None,
    ) -> None:
        """Store the selected execution route until the task manager persists it.

        This is operational metadata only. Keeping it separate from backend
        responses avoids weakening strict bot output contracts.
        """
        if task is None:
            return
        task_id = str(getattr(task, "id", "") or "").strip()
        if not task_id:
            return
        self._execution_provenance_by_task[task_id] = {
            "backend_type": str(getattr(backend, "type", "") or ""),
            "provider": str(getattr(backend, "provider", "") or ""),
            "model": str(getattr(backend, "model", "") or ""),
            "worker_id": str(getattr(worker, "id", "") or "") or None,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    def consume_task_execution_provenance(self, task_id: str) -> Optional[dict[str, Any]]:
        """Return and clear execution route metadata for a completed task attempt."""
        return self._execution_provenance_by_task.pop(str(task_id or ""), None)

    def _worker_capacity_limit(self, worker: Worker, backend: BackendConfig) -> int:
        if str(getattr(backend, "type", "") or "").strip().lower() in {
            "browser",
            "documentation",
            "local_llm",
        }:
            return 1
        return 2**31 - 1

    def _worker_has_capacity(self, worker: Worker, backend: BackendConfig) -> bool:
        limit = self._worker_capacity_limit(worker, backend)
        inflight = int(self._inflight_by_worker.get(worker.id, 0))
        return inflight < limit

    async def schedule(self, task: Task) -> Any:
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except BotNotFoundError:
            raise

        if not bot.enabled:
            raise NoViableBackendError(f"Bot {task.bot_id} is disabled")

        last_error: Exception = NoViableBackendError("No backends configured")
        attempts: list[str] = []
        is_test_task = _is_non_mutating_test_task(task)
        transformed_payload = self._apply_input_transform(bot, task.payload)
        if is_test_task:
            transformed_payload = _mark_test_payload(transformed_payload)

        # Determine if this bot should run in agentic tool-calling mode.
        workspace_root, allow_writes = _agent_workspace_context(bot, task)
        if is_test_task:
            allow_writes = False

        for backend in bot.backends:
            try:
                preferred_backend = await _backend_with_preferred_model(backend, task, self.model_registry)
                effective_backend = _backend_with_retry_params(preferred_backend, task)
                if is_test_task and effective_backend.type in {"cli", "custom"}:
                    raise BackendError(
                        "Test-mode tasks do not execute CLI or custom backends; configure an LLM backend for analysis."
                    )
                prepared_payload = _prepare_payload_for_backend(bot, effective_backend, transformed_payload, task=task)

                if workspace_root is not None and _backend_supports_tools(effective_backend):
                    result = await self._run_agent_loop(
                        effective_backend, prepared_payload, workspace_root,
                        allow_writes=allow_writes, task=task,
                    )
                else:
                    result = await self._dispatch_backend(effective_backend, prepared_payload, task=task)
                return result
            except Exception as e:
                attempts.append(f"{backend.provider}/{backend.model}: {str(e or '').strip() or repr(e)}")
                logger.warning(
                    "Backend %s/%s failed for task %s: %s",
                    backend.provider,
                    backend.model,
                    task.id,
                    e,
                )
                last_error = e
                continue

        raise NoViableBackendError(_backend_failure_message(task.id, last_error, attempts)) from last_error

    # ------------------------------------------------------------------
    # Agentic tool-calling loop
    # ------------------------------------------------------------------

    async def _run_agent_loop(
        self,
        backend: "BackendConfig",
        prepared_payload: Any,
        workspace_root: "Path",
        *,
        allow_writes: bool = False,
        task: "Task | None" = None,
        max_iterations: int = 25,
    ) -> Any:
        """Execute an agentic loop: call the LLM, run tool calls, feed results back.

        Iterates until the model returns a plain content response (no tool_calls)
        or until ``max_iterations`` is reached (circuit breaker).

        For writable runs (``allow_writes=True``), the loop performs forced follow-up
        retries when the model returns plain text before any tool call has occurred.
        This prevents "ask-for-clarification" non-actions from being accepted as
        completed coding runs.
        """
        from control_plane.scheduler.agent_workspace_tools import (
            execute_tool,
            get_tool_definitions,
            parse_tool_call_arguments,
        )
        from control_plane.chat.workspace_tools import normalize_workspace_root

        tools = get_tool_definitions(allow_writes=allow_writes)
        active_tools: list[dict] = list(tools)
        ws_root = normalize_workspace_root(str(workspace_root))

        messages: list[dict] = (
            list(prepared_payload)
            if isinstance(prepared_payload, list)
            else [{"role": "user", "content": str(prepared_payload)}]
        )

        accumulated_usage: dict = {}
        last_result: dict = {}
        observed_tool_call = False
        observed_write_tool_call = False
        observed_non_tree_tool_call = False
        executed_tool_calls: list[dict] = []
        forced_tool_followups = 0
        max_forced_tool_followups = max(
            1,
            int(os.environ.get("NEXUSAI_AGENT_FORCE_TOOL_FOLLOWUPS", "8") or "8"),
        )
        max_discovery_iterations_before_write = max(
            1,
            int(os.environ.get("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_WRITE", "6") or "6"),
        )
        max_discovery_iterations_before_strict_write = max(
            max_discovery_iterations_before_write + 1,
            int(os.environ.get("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_STRICT_WRITE", "10") or "10"),
        )
        tree_only_tool_names = {"workspace_tree", "list_tree", "list_directory"}
        navigation_tools_disabled = False
        write_tools_only = False
        strict_write_only = False
        non_write_discovery_iterations = 0
        proactive_write_escalations = 0
        strict_write_escalations = 0
        strict_mode_rejected_tool_calls = 0
        no_op_write_tool_requests = 0
        hit_max_iterations = False

        def _tool_name(tool_def: dict) -> str:
            try:
                fn = tool_def.get("function") if isinstance(tool_def, dict) else None
                return str((fn or {}).get("name") or "")
            except Exception:
                return ""

        def _without_navigation_tools(tool_defs: list[dict]) -> list[dict]:
            filtered = [tool for tool in (tool_defs or []) if _tool_name(tool) not in tree_only_tool_names]
            return filtered or list(tool_defs or [])

        def _write_priority_tools(tool_defs: list[dict]) -> list[dict]:
            # Keep write tools plus targeted code discovery tools. A strict write-only
            # set causes many models to request unavailable read_file/search_files and
            # stall without edits.
            write_names = {"write_file", "edit_file"}
            discovery_names = {"read_file", "search_files"}
            allowed = write_names | discovery_names
            # Preserve read/search when available for surgical edits.
            filtered = [tool for tool in (tool_defs or []) if _tool_name(tool) in allowed]
            return filtered or list(tool_defs or [])

        def _strict_write_only_tools(tool_defs: list[dict]) -> list[dict]:
            write_names = {"write_file", "edit_file"}
            filtered = [tool for tool in (tool_defs or []) if _tool_name(tool) in write_names]
            if filtered:
                return filtered
            return _write_priority_tools(tool_defs)

        for iteration in range(max_iterations):
            raw = await self._call_backend_raw(backend, messages, tools=active_tools, task=task)
            # Merge usage
            for k, v in (raw.get("usage") or {}).items():
                accumulated_usage[k] = accumulated_usage.get(k, 0) + (v or 0)

            tool_calls = raw.get("tool_calls") or []
            active_tool_name_set = {name for name in (_tool_name(tool) for tool in active_tools) if name}
            if not tool_calls:
                if allow_writes and not observed_tool_call and forced_tool_followups < max_forced_tool_followups:
                    forced_tool_followups += 1
                    output_text = str(raw.get("output") or "").strip()
                    if output_text:
                        messages.append({"role": "assistant", "content": output_text})
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                            "Tool-use requirement (mandatory for this writable coding run):\n"
                                "- You must call at least one workspace tool now (for example list_directory, read_file, search_files, write_file, edit_file).\n"
                                "- Your next response must contain at least one tool call; do not return plain-text-only output.\n"
                                "- Do not ask the user to restate the task.\n"
                                "- Start implementing a minimal first slice immediately, then continue with additional edits as needed."
                            ),
                        }
                    )
                    logger.warning(
                        "[AGENT] task=%s forcing tool-call followup attempt=%d (no tool calls observed yet)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                    continue
                if (
                    allow_writes
                    and observed_tool_call
                    and not observed_non_tree_tool_call
                    and not observed_write_tool_call
                    and forced_tool_followups < max_forced_tool_followups
                ):
                    forced_tool_followups += 1
                    if not navigation_tools_disabled:
                        active_tools = _without_navigation_tools(active_tools)
                        navigation_tools_disabled = True
                    output_text = str(raw.get("output") or "").strip()
                    if output_text:
                        messages.append({"role": "assistant", "content": output_text})
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Discovery requirement (mandatory for this writable coding run):\n"
                                "- workspace_tree/list_directory alone are not sufficient to implement code changes.\n"
                                "- Your next response must call a concrete discovery tool such as search_files or read_file.\n"
                                "- After discovery, continue directly to write_file/edit_file changes.\n"
                                "- Do not ask the user to restate the task.\n"
                                "- Plain-text output without tool calls will be ignored."
                            ),
                        }
                    )
                    logger.warning(
                        "[AGENT] task=%s forcing discovery followup attempt=%d (only tree-style tool calls observed)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                    continue
                if allow_writes and observed_tool_call and not observed_write_tool_call and forced_tool_followups < max_forced_tool_followups:
                    forced_tool_followups += 1
                    if not write_tools_only:
                        active_tools = _write_priority_tools(active_tools)
                        write_tools_only = True
                    output_text = str(raw.get("output") or "").strip()
                    if output_text:
                        messages.append({"role": "assistant", "content": output_text})
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Write requirement (mandatory for this writable coding run):\n"
                                "- You have used tools, but have not made any file edits yet.\n"
                                "- Use read_file/search_files only as needed, then perform write_file/edit_file in this turn.\n"
                                "- Your next response must include at least one write operation via write_file or edit_file.\n"
                                "- Modify existing files when integrating into an existing codebase.\n"
                                "- Do not ask the user for files; read and edit files directly via tools.\n"
                                "- Plain-text output without tool calls will be ignored."
                            ),
                        }
                    )
                    logger.warning(
                        "[AGENT] task=%s forcing write followup attempt=%d (no write/edit tool calls observed yet)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                    continue
                if allow_writes and not observed_tool_call:
                    logger.warning(
                        "[AGENT] task=%s writable run ended without any tool calls (forced_followups=%d)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                if allow_writes and observed_tool_call and not observed_write_tool_call:
                    logger.warning(
                        "[AGENT] task=%s writable run ended without any write/edit tool calls (forced_followups=%d)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                if allow_writes and observed_tool_call and not observed_non_tree_tool_call:
                    logger.warning(
                        "[AGENT] task=%s writable run ended with only tree-style tool calls (forced_followups=%d)",
                        task.id if task else "?",
                        forced_tool_followups,
                    )
                # No more tool calls — model returned final content
                last_result = raw
                break

            observed_tool_call = True
            saw_write_tool_in_iteration = False
            saw_non_tree_tool_in_iteration = False
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tool_name = str(tc.get("name") or "")
                tc_args = parse_tool_call_arguments(tc.get("arguments", {}))
                no_op_edit_request = bool(
                    allow_writes
                    and tool_name == "edit_file"
                    and "old_text" in tc_args
                    and "new_text" in tc_args
                    and str(tc_args.get("old_text")) == str(tc_args.get("new_text"))
                )
                strict_rejected = bool(
                    allow_writes
                    and strict_write_only
                    and tool_name
                    and tool_name not in {"write_file", "edit_file"}
                )
                if strict_rejected:
                    strict_mode_rejected_tool_calls += 1
                    observed_non_tree_tool_call = True
                    saw_non_tree_tool_in_iteration = True
                elif no_op_edit_request:
                    no_op_write_tool_requests += 1
                    observed_non_tree_tool_call = True
                    saw_non_tree_tool_in_iteration = True
                elif allow_writes and tool_name in {"write_file", "edit_file"}:
                    observed_write_tool_call = True
                    saw_write_tool_in_iteration = True
                elif allow_writes and tool_name not in tree_only_tool_names:
                    observed_non_tree_tool_call = True
                    saw_non_tree_tool_in_iteration = True
                record = {
                    "id": str(tc.get("id") or ""),
                    "name": tool_name,
                    "arguments": tc.get("arguments") if isinstance(tc.get("arguments"), dict) else tc_args,
                }
                if strict_rejected:
                    record["rejected_in_strict_write_mode"] = True
                if no_op_edit_request:
                    record["rejected_no_op_edit"] = True
                executed_tool_calls.append(record)
            # Append the assistant message that contains tool_calls
            assistant_msg: dict = {
                "role": "assistant",
                "content": raw.get("output") or "",
            }
            # Some backends need tool_calls on the assistant message itself
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # Execute each tool and append a tool-result message
            for tc in tool_calls:
                tc_id = str(tc.get("id") or "")
                tc_name = str(tc.get("name") or "")
                tc_args = parse_tool_call_arguments(tc.get("arguments", {}))
                logger.debug(
                    "[AGENT] task=%s iteration=%d tool=%s args=%s",
                    task.id if task else "?",
                    iteration,
                    tc_name,
                    list(tc_args.keys()),
                )
                if tc_name and active_tool_name_set and tc_name not in active_tool_name_set:
                    allowed_label = ", ".join(sorted(active_tool_name_set))
                    tool_output = (
                        f"ERROR: Tool '{tc_name}' is not enabled in this step. "
                        f"Use one of: {allowed_label}."
                    )
                elif allow_writes and strict_write_only and tc_name not in {"write_file", "edit_file"}:
                    tool_output = (
                        "ERROR: strict write mode is active. "
                        "Call write_file or edit_file now to implement code changes."
                    )
                elif (
                    allow_writes
                    and tc_name == "edit_file"
                    and "old_text" in tc_args
                    and "new_text" in tc_args
                    and str(tc_args.get("old_text")) == str(tc_args.get("new_text"))
                ):
                    tool_output = (
                        "ERROR: no-op edit detected (old_text equals new_text). "
                        "Provide a real replacement that changes file content."
                    )
                else:
                    tool_output = await asyncio.to_thread(
                        execute_tool, tc_name, tc_args, ws_root, allow_writes=allow_writes
                    )
                tool_msg: dict = {
                    "role": "tool",
                    "name": tc_name,
                    "content": tool_output,
                }
                if tc_id:
                    tool_msg["tool_call_id"] = tc_id
                messages.append(tool_msg)

            if allow_writes:
                if saw_write_tool_in_iteration:
                    non_write_discovery_iterations = 0
                elif saw_non_tree_tool_in_iteration:
                    non_write_discovery_iterations += 1
                    should_escalate = (
                        not write_tools_only
                        and non_write_discovery_iterations >= max_discovery_iterations_before_write
                    )
                    if should_escalate:
                        active_tools = _write_priority_tools(active_tools)
                        write_tools_only = True
                        proactive_write_escalations += 1
                        if forced_tool_followups < max_forced_tool_followups:
                            forced_tool_followups += 1
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Write requirement escalation (mandatory for this writable coding run):\n"
                                    "- Discovery has already been performed in prior tool calls.\n"
                                    "- Stop broad exploration and produce concrete file edits now.\n"
                                    "- You may use read_file/search_files surgically, then write_file/edit_file immediately.\n"
                                    "- Your next response must call write_file or edit_file.\n"
                                    "- Do not return plain-text-only output."
                                ),
                            }
                        )
                        logger.warning(
                            "[AGENT] task=%s escalating to write-priority tools after %d discovery iterations without writes",
                            task.id if task else "?",
                            non_write_discovery_iterations,
                        )
                    strict_should_escalate = (
                        write_tools_only
                        and not strict_write_only
                        and non_write_discovery_iterations >= max_discovery_iterations_before_strict_write
                    )
                    if strict_should_escalate:
                        active_tools = _strict_write_only_tools(active_tools)
                        strict_write_only = True
                        strict_write_escalations += 1
                        if forced_tool_followups < max_forced_tool_followups:
                            forced_tool_followups += 1
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Strict write requirement (mandatory for this writable coding run):\n"
                                    "- Discovery budget is exhausted for this run.\n"
                                    "- Call write_file or edit_file now as your next tool call.\n"
                                    "- Do not call read_file, search_files, workspace_tree, or list_directory.\n"
                                    "- Do not return plain-text-only output."
                                ),
                            }
                        )
                        logger.warning(
                            "[AGENT] task=%s escalating to strict write-only tools after %d discovery iterations without writes",
                            task.id if task else "?",
                            non_write_discovery_iterations,
                        )

            last_result = raw
        else:
            # Circuit breaker hit — still return whatever the last response was
            hit_max_iterations = True
            logger.warning(
                "[AGENT] task=%s hit max_iterations=%d — returning last result",
                task.id if task else "?",
                max_iterations,
            )

        # Build the merged result with accumulated usage
        result = dict(last_result)
        result["usage"] = accumulated_usage
        if executed_tool_calls:
            result["tool_calls_executed"] = executed_tool_calls
        if allow_writes:
            result["agent_loop_diagnostics"] = {
                "allow_writes": True,
                "observed_tool_call": bool(observed_tool_call),
                "observed_non_tree_tool_call": bool(observed_non_tree_tool_call),
                "observed_write_tool_call": bool(observed_write_tool_call),
                "forced_followups_used": int(forced_tool_followups),
                "max_forced_followups": int(max_forced_tool_followups),
                "tree_only_tools": sorted(tree_only_tool_names),
                "navigation_tools_disabled": bool(navigation_tools_disabled),
                "write_tools_only": bool(write_tools_only),
                "strict_write_only": bool(strict_write_only),
                "non_write_discovery_iterations": int(non_write_discovery_iterations),
                "max_discovery_iterations_before_write": int(max_discovery_iterations_before_write),
                "max_discovery_iterations_before_strict_write": int(max_discovery_iterations_before_strict_write),
                "proactive_write_escalations": int(proactive_write_escalations),
                "strict_write_escalations": int(strict_write_escalations),
                "strict_mode_rejected_tool_calls": int(strict_mode_rejected_tool_calls),
                "no_op_write_tool_requests": int(no_op_write_tool_requests),
                "hit_max_iterations": bool(hit_max_iterations),
                "active_tool_names": [name for name in (_tool_name(tool) for tool in active_tools) if name],
            }
        return result

    async def _call_backend_raw(
        self,
        backend: "BackendConfig",
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        task: "Task | None" = None,
    ) -> dict:
        """Single-shot call to a cloud backend with optional tool definitions.

        Returns a dict with keys:
          - ``output``: str — the text content from the model (may be empty if tool_calls present)
          - ``tool_calls``: list[dict] — each item has ``id``, ``name``, ``arguments`` (dict)
          - ``usage``: dict
          - ``finish_reason``: str
        """
        if backend.provider == "ollama_cloud":
            return await self._call_ollama_cloud_raw(backend, messages, tools=tools)
        elif backend.provider == "openai":
            return await self._call_openai_raw(backend, messages, tools=tools)
        elif backend.provider == "claude":
            return await self._call_claude_raw(backend, messages, tools=tools)
        else:
            # Unsupported provider for raw call — fall back to dispatch (no tools)
            result = await self._dispatch_backend(backend, messages, task=task)
            return {**result, "tool_calls": []}

    async def _call_ollama_cloud_raw(
        self,
        backend: "BackendConfig",
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        api_key = await self._resolve_api_key(
            backend.api_key_ref or "OLLAMA_API_KEY", "OLLAMA_API_KEY"
        )
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable "
                f"'{backend.api_key_ref or 'OLLAMA_API_KEY'}' with your Ollama API key."
            )
        messages = _messages_for_ollama(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        response_format = params_dict.pop("response_format", None)
        base_body: dict = {
            "messages": messages,
            "stream": False,
            # Worker and direct-cloud paths share the same bounded-response policy.
            "think": False,
            "options": _ollama_options(params_dict),
        }
        if response_format == "json":
            base_body["format"] = "json"
        if tools:
            base_body["tools"] = tools

        base_url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api").rstrip("/")

        async def _do_chat(client: httpx.AsyncClient, model_name: str) -> dict:
            body = dict(base_body)
            body["model"] = model_name
            response = await client.post(
                f"{base_url}/chat",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            detail = ""
            if not response.is_success:
                try:
                    pd = response.json()
                    if isinstance(pd, dict):
                        detail = str(pd.get("error") or pd.get("detail") or pd.get("message") or "").strip()
                except Exception:
                    detail = (response.text or "").strip()
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = response.status_code
                msg = f"Ollama Cloud request failed ({status})"
                if self._is_ollama_model_not_found(status, detail):
                    raise _OllamaModelNotFound(backend.model) from e
                raise BackendError(f"{msg}: {detail}" if detail else msg) from e

            data = response.json()
            msg_obj = data.get("message") or {}
            output = str(msg_obj.get("content") or "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            }
            finish_reason = str(data.get("done_reason") or data.get("finish_reason") or "").strip()

            raw_tool_calls = (
                msg_obj.get("tool_calls")
                or msg_obj.get("toolCalls")
                or data.get("tool_calls")
                or data.get("toolCalls")
                or []
            )
            tool_calls = _parse_ollama_tool_calls(raw_tool_calls)

            result: dict = {"output": output, "tool_calls": tool_calls, "usage": usage}
            if finish_reason:
                result["finish_reason"] = finish_reason
            if model_name != str(backend.model or "").strip():
                result["resolved_model"] = model_name
            for key in ("thinking", "reasoning", "reasoning_content", "analysis"):
                value = msg_obj.get(key)
                if value in (None, "", [], {}):
                    value = data.get(key)
                if value not in (None, "", [], {}):
                    result[key] = value
            return result

        model_variants = self._ollama_cloud_model_variants(backend.model)
        if not model_variants:
            model_variants = [str(backend.model or "").strip()]

        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            for model_name in model_variants:
                try:
                    return await _do_chat(client, model_name)
                except _OllamaModelNotFound:
                    continue
            await self._pull_ollama_cloud_model(base_url, api_key, str(backend.model or "").strip())
            for model_name in model_variants:
                try:
                    return await _do_chat(client, model_name)
                except _OllamaModelNotFound:
                    continue
        raise BackendError(
            f"Ollama Cloud model not found for configured name '{backend.model}' and aliases: {', '.join(model_variants)}"
        )

    async def _call_openai_raw(
        self,
        backend: "BackendConfig",
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        api_key = await self._resolve_api_key(
            backend.api_key_ref or "OPENAI_API_KEY", "OPENAI_API_KEY"
        )
        if not api_key:
            raise BackendError(
                f"API key not found. Set '{backend.api_key_ref or 'OPENAI_API_KEY'}' env var."
            )
        messages = _messages_for_openai(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        body: dict = {"model": backend.model, "messages": messages}
        body.update(params_dict)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            msg_obj = choice.get("message") or {}
            output = str(msg_obj.get("content") or "")
            finish_reason = str(choice.get("finish_reason") or "").strip()

            raw_tool_calls = msg_obj.get("tool_calls") or []
            tool_calls = _parse_openai_tool_calls(raw_tool_calls)

            result: dict = {"output": output, "tool_calls": tool_calls, "usage": data.get("usage") or {}}
            if finish_reason:
                result["finish_reason"] = finish_reason
            return result

    async def _call_claude_raw(
        self,
        backend: "BackendConfig",
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        api_key = await self._resolve_api_key(
            backend.api_key_ref or "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        )
        if not api_key:
            raise BackendError(
                f"API key not found. Set '{backend.api_key_ref or 'ANTHROPIC_API_KEY'}' env var."
            )
        system_prompt, chat_messages = _claude_payload_messages(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        max_tokens = params_dict.pop("max_tokens", 4096)
        body: dict = {
            "model": backend.model,
            "max_tokens": max_tokens,
            "messages": _convert_tool_messages_for_claude(chat_messages),
        }
        if system_prompt:
            body["system"] = system_prompt
        body.update(params_dict)
        if tools:
            # Claude uses slightly different tool format
            body["tools"] = _convert_tools_for_claude(tools)

        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            stop_reason = str(data.get("stop_reason") or "").strip()

            # Parse content blocks — may include text and tool_use
            content_blocks = data.get("content") or []
            output_parts = []
            tool_calls = []
            for block in content_blocks:
                block_type = str(block.get("type") or "")
                if block_type == "text":
                    output_parts.append(str(block.get("text") or ""))
                elif block_type == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": block.get("input") or {},
                    })

            output = "".join(output_parts)
            result: dict = {
                "output": output,
                "tool_calls": tool_calls,
                "usage": data.get("usage") or {},
            }
            if stop_reason:
                result["finish_reason"] = stop_reason
            return result

    async def stream(self, task: Task) -> AsyncGenerator[dict[str, Any], None]:
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except BotNotFoundError:
            raise

        if not bot.enabled:
            raise NoViableBackendError(f"Bot {task.bot_id} is disabled")

        last_error: Exception = NoViableBackendError("No backends configured")
        attempts: list[str] = []
        transformed_payload = self._apply_input_transform(bot, task.payload)
        workspace_root, allow_writes = _agent_workspace_context(bot, task)
        for backend in bot.backends:
            try:
                preferred_backend = await _backend_with_preferred_model(backend, task, self.model_registry)
                effective_backend = _backend_with_retry_params(preferred_backend, task)
                prepared_payload = _prepare_payload_for_backend(bot, effective_backend, transformed_payload, task=task)
                yield {
                    "event": "backend_selected",
                    "provider": effective_backend.provider,
                    "model": effective_backend.model,
                    "worker_id": effective_backend.worker_id,
                }
                if workspace_root is not None and _backend_supports_tools(effective_backend):
                    result = await self._run_agent_loop(
                        effective_backend,
                        prepared_payload,
                        workspace_root,
                        allow_writes=allow_writes,
                        task=task,
                    )
                    yield {"event": "final", **(result if isinstance(result, dict) else {"output": str(result)})}
                else:
                    async for event in self._dispatch_backend_stream(effective_backend, prepared_payload, task=task):
                        yield event
                return
            except Exception as e:
                attempts.append(f"{backend.provider}/{backend.model}: {str(e or '').strip() or repr(e)}")
                logger.warning(
                    "Backend %s/%s failed for stream task %s: %s",
                    backend.provider,
                    backend.model,
                    task.id,
                    e,
                )
                last_error = e
                continue

        raise NoViableBackendError(_backend_failure_message(task.id, last_error, attempts)) from last_error

    async def _dispatch_backend(self, backend: BackendConfig, payload: Any, task: Task | None = None) -> Any:
        if _is_non_mutating_test_task(task) and backend.type in {"cli", "browser", "documentation", "custom"}:
            raise BackendError(
                "Test-mode tasks do not execute CLI, browser, or custom backends; configure an LLM backend for analysis."
            )
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm"):
            worker = await self._resolve_worker_for_llm_backend(backend, task=task)
            await self._require_fresh_autonomous_worker_probe(worker, task)
            if worker.status != "online":
                raise BackendError(
                    f"Worker {worker.id} is not online (status={worker.status})"
                )
            self._record_execution_provenance(task, backend, worker=worker)
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        elif backend.type == "cloud_api":
            self._record_execution_provenance(task, backend)
            if backend.provider == "openai":
                return await self._call_openai(backend, safe_payload)
            elif backend.provider == "ollama_cloud":
                return await self._call_ollama_cloud(backend, safe_payload)
            elif backend.provider == "claude":
                return await self._call_claude(backend, safe_payload)
            elif backend.provider == "gemini":
                return await self._call_gemini(backend, safe_payload)
            elif backend.provider == "vertex":
                return await self._call_vertex(backend, safe_payload)
            else:
                raise BackendError(f"Unknown cloud_api provider: {backend.provider}")
        elif backend.type == "cli":
            if not backend.worker_id:
                raise BackendError("worker_id is required for cli backends")
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            await self._require_fresh_autonomous_worker_probe(worker, task)
            await self._require_task_worker_tools(worker, task)
            self._record_execution_provenance(task, backend, worker=worker)
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        elif backend.type == "browser":
            worker = await self._resolve_browser_worker(backend, task=task)
            await self._require_fresh_autonomous_worker_probe(worker, task)
            self._record_execution_provenance(task, backend, worker=worker)
            return await self._dispatch_browser_inspection(worker, backend, safe_payload, task=task)
        elif backend.type == "documentation":
            worker = await self._resolve_documentation_worker(backend, task=task)
            await self._require_fresh_autonomous_worker_probe(worker, task)
            self._record_execution_provenance(task, backend, worker=worker)
            return await self._dispatch_documentation_write(worker, backend, safe_payload, task=task)
        elif backend.type == "custom":
            self._record_execution_provenance(task, backend)
            return await self._dispatch_custom_backend(backend, safe_payload, task=task)
        else:
            raise BackendError(f"Unsupported backend type: {backend.type}")

    async def _dispatch_custom_backend(
        self,
        backend: BackendConfig,
        payload: Any,
        task: Task | None = None,
    ) -> Any:
        provider = str(backend.provider or "").strip().lower()
        if provider == "http_connection":
            return await self._dispatch_http_connection_backend(payload, task=task)
        raise BackendError(f"Unsupported custom backend provider: {backend.provider}")

    async def _dispatch_http_connection_backend(self, payload: Any, task: Task | None = None) -> Any:
        if task is None:
            raise BackendError("http_connection backend requires a task context")
        if not isinstance(payload, dict):
            raise BackendError("http_connection backend requires a JSON object payload")
        await self._authorize_http_connection_actions(payload, task)
        return await asyncio.to_thread(self._run_http_connection_backend_sync, payload, task.bot_id)

    @staticmethod
    def _connection_actions_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_actions = payload.get("connection_actions")
        if isinstance(raw_actions, dict):
            return [raw_actions]
        if isinstance(raw_actions, list):
            return [item for item in raw_actions if isinstance(item, dict)]
        if isinstance(payload.get("connection_action"), dict):
            return [payload["connection_action"]]
        return []

    async def _authorize_http_connection_actions(self, payload: dict[str, Any], task: Task) -> None:
        """Fail closed for all state-changing OpenAPI actions before dispatch."""

        actions = self._connection_actions_from_payload(payload)
        if not actions:
            raise BackendError("http_connection backend requires at least one connection action")
        connection_ref = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
        connection = self._connection_resolver.find_bot_connection(
            str(task.bot_id),
            requested_name=str(connection_ref.get("name") or payload.get("connection_name") or "").strip() or None,
            requested_id=str(connection_ref.get("id") or payload.get("connection_id") or "").strip() or None,
        )
        if connection is None:
            raise BackendError(
                "Requested bot connection was not found or multiple connections are attached "
                "without an explicit connection.id/name selector"
            )
        if str(connection.get("kind") or "").strip().lower() != "http":
            raise BackendError("http_connection backend only supports HTTP connections")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(f"Bot {task.bot_id} was not found for connection-action authorization") from exc

        policy = bot_execution_policy(bot)
        required_approval_keys: list[str] = []
        for action in actions:
            declared = connection_runtime.resolve_declared_http_action(
                str(connection.get("schema_text") or ""),
                action,
            )
            if declared is None:
                raise BackendError("The requested HTTP connection action is not declared in the attached schema")
            method = str(declared.get("method") or "").upper()
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            action_key = connection_action_key(
                connection.get("name"),
                {"operation_id": declared.get("operation_id")},
            )
            if action_key not in policy.connection_action_allowlist:
                raise BackendError(f"Bot {bot.id} is not authorized for connection mutation {action_key}")
            if action.get("agent_approval") is not None and (
                action_key not in policy.connection_action_owner_approval_required
            ):
                raise BackendError(
                    f"{action_key} may only request a remote approval when owner approval is required"
                )
            if action_key in policy.connection_action_owner_approval_required:
                required_approval_keys.append(action_key)

        if not required_approval_keys:
            return
        if len(required_approval_keys) != 1:
            raise BackendError("Only one owner-approved connection mutation is allowed per task")
        approval_id = str(payload.get("owner_approval_id") or "").strip()
        if not approval_id:
            raise BackendError(f"{required_approval_keys[0]} requires a valid, unused owner approval")
        if self._connection_action_approval_store is None:
            raise BackendError("Connection action approval service is unavailable")
        try:
            connection_action_payload_digest(payload)
            consumed = await self._connection_action_approval_store.consume(
                approval_id=approval_id,
                bot_id=bot.id,
                action_key=required_approval_keys[0],
                payload=payload,
            )
        except ValueError as exc:
            raise BackendError("Connection action approval payload is invalid") from exc
        if not consumed:
            raise BackendError(f"{required_approval_keys[0]} requires a valid, unused owner approval")

    def _run_http_connection_backend_sync(self, payload: dict[str, Any], bot_id: str) -> dict[str, Any]:
        connection_ref = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
        requested_name = str(connection_ref.get("name") or payload.get("connection_name") or "").strip()
        requested_id = str(connection_ref.get("id") or payload.get("connection_id") or "").strip()
        continue_on_error = bool(payload.get("continue_on_error", False))

        actions = self._connection_actions_from_payload(payload)
        if not actions:
            raise BackendError("http_connection backend requires at least one connection action")
        connection = self._connection_resolver.find_bot_connection(
            str(bot_id),
            requested_name=requested_name or None,
            requested_id=requested_id or None,
        )
        if connection is None:
            raise BackendError(
                "Requested bot connection was not found or multiple connections are attached "
                "without an explicit connection.id/name selector"
            )
        if str(connection.get("kind") or "").strip().lower() != "http":
            raise BackendError("http_connection backend only supports HTTP connections")

        config = connection.get("config") if isinstance(connection.get("config"), dict) else {}
        config = connection_secrets.resolve_connection_config(config)
        auth = connection_secrets.resolve_auth_payload(connection.get("auth") if isinstance(connection.get("auth"), dict) else {})
        schema_text = str(connection.get("schema_text") or "")

        action_results: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        completed_actions: list[str] = []
        failed_actions: list[str] = []

        for index, action in enumerate(actions):
            op_id = str(action.get("operation_id") or action.get("path") or f"action_{index + 1}").strip()
            result = connection_runtime.test_http_connection(
                config=config if isinstance(config, dict) else {},
                auth=auth if isinstance(auth, dict) else {},
                schema_text=schema_text,
                payload=action,
            )
            action_result = {"operation_id": op_id, **result}
            action_results.append(action_result)
            if bool(result.get("ok")):
                completed_actions.append(op_id)
            else:
                failed_actions.append(op_id)
                detail = str(result.get("body_preview") or result.get("error") or "").strip()
                hint = _http_action_error_hint(op_id, action, result)
                errors.append(f"{op_id} failed with status {result.get('status')}: {detail}{hint}".strip())
                if not continue_on_error:
                    break

        if failed_actions and continue_on_error:
            warnings.append("One or more connection actions failed while continue_on_error was enabled.")

        return {
            "import_status": "success" if not failed_actions else "failed",
            "connection_name": str(connection.get("name") or ""),
            "connection_id": int(connection.get("id") or 0),
            "completed_actions": completed_actions,
            "failed_actions": failed_actions,
            "action_results": action_results,
            "warnings": warnings,
            "errors": errors,
        }

    def _apply_input_transform(self, bot: Any, payload: Any) -> Any:
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return payload
        config = routing_rules.get("input_transform")
        if not isinstance(config, dict) or not bool(config.get("enabled", False)):
            return payload
        template = config.get("template")
        if template is None:
            return payload
        return _transform_template_value(template, payload)

    async def _dispatch_backend_stream(
        self, backend: BackendConfig, payload: Any, task: Task | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        if _is_non_mutating_test_task(task) and backend.type in {"cli", "browser", "custom"}:
            raise BackendError(
                "Test-mode tasks do not execute CLI, browser, or custom backends; configure an LLM backend for analysis."
            )
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm", "cli"):
            worker = await self._resolve_worker_for_llm_backend(backend, task=task) if backend.type != "cli" else await self.worker_registry.get(backend.worker_id)  # type: ignore[arg-type]
            await self._require_fresh_autonomous_worker_probe(worker, task)
            if backend.type == "cli":
                await self._require_task_worker_tools(worker, task)
            if worker.status != "online":
                raise BackendError(
                    f"Worker {worker.id} is not online (status={worker.status})"
                )
            yield {
                "event": "dispatch_started",
                "worker_id": worker.id,
                "host": worker.host,
                "port": worker.port,
                "provider": backend.provider,
                "model": backend.model,
            }
            async for event in self._dispatch_to_worker_stream(worker, backend, safe_payload):
                yield event
            return
        if backend.type == "cloud_api":
            result = await self._dispatch_backend(backend, payload, task=task)
            yield {"event": "final", **result}
            return
        if backend.type == "browser":
            raise BackendError("Browser backends do not support streaming")
        raise BackendError(f"Unsupported backend type: {backend.type}")

    async def _apply_cloud_context_policy(
        self,
        backend: BackendConfig,
        payload: Any,
        task: Task | None = None,
    ) -> Any:
        # Applies only to cloud backends; local/remote worker execution keeps full payload.
        if backend.type != "cloud_api":
            return payload
        if not isinstance(payload, list):
            return payload

        policy = await self._resolve_cloud_context_policy(backend=backend, task=task)

        has_context = any(
            isinstance(m, dict)
            and str(m.get("role", "")).lower() == "system"
            and str(m.get("content", "")).startswith("Context:\n")
            for m in payload
        )
        if not has_context:
            return payload

        if policy == "allow":
            return payload
        if policy == "block":
            raise BackendError(
                "Cloud context policy blocks sending context payloads to cloud providers"
            )

        # redact policy
        redacted = []
        for m in payload:
            if (
                isinstance(m, dict)
                and str(m.get("role", "")).lower() == "system"
                and str(m.get("content", "")).startswith("Context:\n")
            ):
                redacted.append(
                    {
                        **m,
                        "content": "Context:\n[REDACTED_BY_POLICY]",
                    }
                )
            else:
                redacted.append(m)
        return redacted

    async def _resolve_cloud_context_policy(self, backend: BackendConfig, task: Task | None = None) -> str:
        default_policy = os.environ.get("NEXUSAI_CLOUD_CONTEXT_POLICY", "allow").strip().lower()
        if default_policy not in {"allow", "redact", "block"}:
            default_policy = "allow"
        if backend.type != "cloud_api":
            return default_policy

        provider = str(backend.provider or "").strip().lower()
        if not provider:
            return default_policy
        if not task or not task.metadata or not getattr(task.metadata, "project_id", None):
            return default_policy
        if self.project_registry is None:
            return default_policy

        project_id = str(task.metadata.project_id or "").strip()
        if not project_id:
            return default_policy

        try:
            project = await self.project_registry.get(project_id)
        except Exception:
            return default_policy

        settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
        cfg = settings.get("cloud_context_policy") if isinstance(settings.get("cloud_context_policy"), dict) else {}
        provider_policies = cfg.get("provider_policies") if isinstance(cfg.get("provider_policies"), dict) else {}
        bot_overrides = cfg.get("bot_overrides") if isinstance(cfg.get("bot_overrides"), dict) else {}

        baseline = str(provider_policies.get(provider, default_policy)).strip().lower()
        if baseline not in {"allow", "redact", "block"}:
            baseline = default_policy
        if baseline == "block":
            return "block"

        bot_id = str(task.bot_id or "").strip()
        bot_cfg = bot_overrides.get(bot_id) if isinstance(bot_overrides.get(bot_id), dict) else {}
        override = str(bot_cfg.get(provider, "")).strip().lower()
        if override not in {"allow", "redact", "block"}:
            override = ""

        if baseline == "redact":
            if override == "block":
                return "block"
            return "redact"

        # baseline allow
        if override:
            return override
        return "allow"

    async def _worker_request_headers(self, worker: Worker) -> dict[str, str]:
        """Resolve an optional node request token without exposing it in task data."""
        token_ref = str(getattr(worker, "request_token_env", "") or "").strip()
        if not token_ref:
            return {}
        token = os.environ.get(token_ref, "").strip()
        if not token:
            raise BackendError(
                f"Worker {worker.id} declares request token '{token_ref}', but it is not configured."
            )
        return {"X-Nexus-Worker-Token": token}

    async def _dispatch_to_worker(
        self, worker: Worker, backend: BackendConfig, payload: Any
    ) -> Any:
        url = f"http://{worker.host}:{worker.port}/infer"
        headers = await self._worker_request_headers(worker)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        # Apply provider-specific param normalization (e.g., Ollama num_predict default)
        if str(backend.provider or "").strip().lower() == "ollama":
            params_dict = _ollama_options(params_dict)
        body = {
            "model": backend.model,
            "provider": backend.provider,
            "messages": payload if isinstance(payload, list) else [{"role": "user", "content": str(payload)}],
            "params": params_dict,
        }
        if backend.gpu_id:
            body["gpu_id"] = backend.gpu_id
        if backend.command:
            body["command"] = backend.command
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(url, json=body, headers=headers)
                response.raise_for_status()
                return response.json()
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                prev = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * prev)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_to_worker_stream(
        self, worker: Worker, backend: BackendConfig, payload: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        url = f"http://{worker.host}:{worker.port}/infer/stream"
        headers = await self._worker_request_headers(worker)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        # Apply provider-specific param normalization (e.g., Ollama num_predict default)
        if str(backend.provider or "").strip().lower() == "ollama":
            params_dict = _ollama_options(params_dict)
        body = {
            "model": backend.model,
            "provider": backend.provider,
            "messages": payload if isinstance(payload, list) else [{"role": "user", "content": str(payload)}],
            "params": params_dict,
        }
        if backend.gpu_id:
            body["gpu_id"] = backend.gpu_id
        if backend.command:
            body["command"] = backend.command
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        saw_token = False
        logger.info(
            "Dispatching stream task to worker=%s provider=%s model=%s url=%s",
            worker.id,
            backend.provider,
            backend.model,
            url,
        )
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                async with client.stream("POST", url, json=body, headers=headers) as response:
                    response.raise_for_status()
                    buffer = ""
                    event_type = "message"
                    async for chunk in response.aiter_text():
                        if not chunk:
                            continue
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            if not block.strip():
                                continue
                            event_type = "message"
                            data_text = ""
                            for line in block.splitlines():
                                if line.startswith("event:"):
                                    event_type = line[6:].strip()
                                elif line.startswith("data:"):
                                    data_text += line[5:].strip()
                            if not data_text:
                                continue
                            payload_obj = json.loads(data_text)
                            if isinstance(payload_obj, dict):
                                payload_obj.setdefault("event", event_type)
                                if event_type == "token" and not saw_token:
                                    saw_token = True
                                    logger.info(
                                        "First stream token received worker=%s provider=%s model=%s",
                                        worker.id,
                                        backend.provider,
                                        backend.model,
                                    )
                                yield payload_obj
            finally:
                logger.info(
                    "Stream task finished worker=%s provider=%s model=%s elapsed_ms=%.1f saw_token=%s",
                    worker.id,
                    backend.provider,
                    backend.model,
                    (time.perf_counter() - started) * 1000.0,
                    saw_token,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                prev = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * prev)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _call_openai(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "OPENAI_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "OPENAI_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your OpenAI API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        messages = _messages_for_openai(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        body: dict = {
            "model": backend.model,
            "messages": messages,
        }
        body.update(params_dict)
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            output = data["choices"][0]["message"]["content"]
            finish_reason = ""
            try:
                finish_reason = str((data.get("choices") or [{}])[0].get("finish_reason") or "").strip()
            except Exception:
                finish_reason = ""
            result = {"output": output, "usage": data.get("usage", {})}
            if finish_reason:
                result["finish_reason"] = finish_reason
            return result

    @staticmethod
    def _is_ollama_model_not_found(status: int, detail: str) -> bool:
        """Return True when the Ollama API returned a 404 indicating a missing model."""
        if status != 404:
            return False
        lower = detail.lower()
        return "not found" in lower or "model" in lower or not detail

    @staticmethod
    def _ollama_cloud_model_variants(model: str) -> list[str]:
        raw = str(model or "").strip()
        if not raw:
            return []
        candidates: list[str] = [raw]
        lowered = raw.lower()
        prefixes = ("ollama_cloud/", "ollama/")
        for prefix in prefixes:
            if lowered.startswith(prefix):
                candidates.append(raw[len(prefix):].strip())
                break
        if raw.endswith("-cloud"):
            candidates.append(raw[: -len("-cloud")].strip())
        if raw.endswith(":cloud"):
            candidates.append(raw[: -len(":cloud")].strip())
        if raw.endswith("/cloud"):
            candidates.append(raw[: -len("/cloud")].strip())
        seen: set[str] = set()
        ordered: list[str] = []
        for item in candidates:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    @staticmethod
    async def _pull_ollama_cloud_model(
        base_url: str,
        api_key: str,
        model: str,
        *,
        pull_timeout: float = 1800.0,
    ) -> None:
        """
        Ask the Ollama Cloud endpoint to pull/download *model*.

        Ollama exposes POST /api/pull which streams progress lines; we wait
        for completion (stream=False) or until the pull_timeout elapses.
        If the endpoint does not support /api/pull this raises BackendError so
        the caller can surface a clear message.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.info("Ollama Cloud: model '%s' not found — attempting auto-pull", model)
        pull_url = f"{base_url}/pull"
        async with httpx.AsyncClient(timeout=httpx.Timeout(pull_timeout, connect=10.0)) as client:
            try:
                pull_resp = await client.post(
                    pull_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "stream": False},
                )
                content_type = pull_resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    raise BackendError(
                        f"Ollama Cloud pull endpoint returned an HTML page instead of JSON. "
                        f"The /api/pull endpoint is not supported on this server. "
                        f"Pull the model manually on the server: `ollama pull {model}`"
                    )
                if pull_resp.status_code == 404:
                    raise BackendError(
                        f"Ollama Cloud model '{model}' not found and /api/pull is not supported "
                        f"by this endpoint. Please pull the model manually on the server: "
                        f"`ollama pull {model}`"
                    )
                pull_resp.raise_for_status()
                _log.info("Ollama Cloud: model '%s' pulled successfully", model)
            except BackendError:
                raise
            except httpx.HTTPStatusError as e:
                detail = ""
                try:
                    pd = pull_resp.json()
                    if isinstance(pd, dict):
                        detail = str(pd.get("error") or pd.get("detail") or "").strip()
                except Exception:
                    detail = (pull_resp.text or "").strip()
                raise BackendError(
                    f"Ollama Cloud auto-pull of '{model}' failed: {detail or str(e)}"
                ) from e

    async def _call_ollama_cloud(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "OLLAMA_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "OLLAMA_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Ollama API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        messages = _messages_for_ollama(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        response_format = params_dict.pop("response_format", None)
        base_body: dict = {
            "messages": messages,
            "stream": False,
            # Keep ordinary direct-cloud dispatch aligned with worker runtimes.
            "think": False,
            "options": _ollama_options(params_dict),
        }
        if response_format == "json":
            base_body["format"] = "json"
        base_url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api").rstrip("/")

        async def _do_chat(client: httpx.AsyncClient, model_name: str) -> Any:
            body = dict(base_body)
            body["model"] = model_name
            response = await client.post(
                f"{base_url}/chat",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            detail = ""
            if not response.is_success:
                try:
                    payload_data = response.json()
                    if isinstance(payload_data, dict):
                        detail = str(
                            payload_data.get("error")
                            or payload_data.get("detail")
                            or payload_data.get("message")
                            or ""
                        ).strip()
                except Exception:
                    detail = (response.text or "").strip()
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = response.status_code
                if self._is_ollama_model_not_found(status, detail):
                    raise _OllamaModelNotFound(backend.model) from e
                msg = f"Ollama Cloud request failed ({status})"
                raise BackendError(f"{msg}: {detail}" if detail else msg) from e
            data = response.json()
            output = data.get("message", {}).get("content", "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            }
            finish_reason = str(data.get("done_reason") or data.get("finish_reason") or "").strip()
            result: dict = {"output": output, "usage": usage}
            if finish_reason:
                result["finish_reason"] = finish_reason
            if model_name != str(backend.model or "").strip():
                result["resolved_model"] = model_name
            for key in ("thinking", "reasoning", "reasoning_content", "analysis"):
                value = data.get("message", {}).get(key) if isinstance(data.get("message"), dict) else None
                if value in (None, "", [], {}):
                    value = data.get(key)
                if value not in (None, "", [], {}):
                    result[key] = value
            return result

        model_variants = self._ollama_cloud_model_variants(backend.model)
        if not model_variants:
            model_variants = [str(backend.model or "").strip()]

        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            for model_name in model_variants:
                try:
                    return await _do_chat(client, model_name)
                except _OllamaModelNotFound:
                    continue
            await self._pull_ollama_cloud_model(base_url, api_key, str(backend.model or "").strip())
            for model_name in model_variants:
                try:
                    return await _do_chat(client, model_name)
                except _OllamaModelNotFound:
                    continue
        raise BackendError(
            f"Ollama Cloud model not found for configured name '{backend.model}' and aliases: {', '.join(model_variants)}"
        )

    async def _call_claude(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "ANTHROPIC_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Anthropic API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        system_prompt, messages = _claude_payload_messages(messages)
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        max_tokens = params_dict.pop("max_tokens", 1024)
        body: dict = {
            "model": backend.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            body["system"] = system_prompt
        body.update(params_dict)
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            output = data["content"][0]["text"]
            finish_reason = str(data.get("stop_reason") or "").strip()
            result = {"output": output, "usage": data.get("usage", {})}
            if finish_reason:
                result["finish_reason"] = finish_reason
            return result

    async def _call_gemini(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "GEMINI_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "GEMINI_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Gemini API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        body = {
            "contents": _gemini_contents(messages),
        }
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        generation_config = _google_generation_config(params_dict)
        if generation_config:
            body["generationConfig"] = generation_config
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{backend.model}:generateContent"
        )
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            output = data["candidates"][0]["content"]["parts"][0]["text"]
            finish_reason = ""
            try:
                finish_reason = str((data.get("candidates") or [{}])[0].get("finishReason") or "").strip()
            except Exception:
                finish_reason = ""
            result = {"output": output, "usage": data.get("usageMetadata", {})}
            if finish_reason:
                result["finish_reason"] = finish_reason
            return result

    def _service_account_jwt(self, service_account: Dict[str, Any], *, scope: str) -> str:
        def _b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        private_key_pem = str(service_account.get("private_key") or "").strip()
        client_email = str(service_account.get("client_email") or "").strip()
        token_uri = str(service_account.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
        if not private_key_pem or not client_email:
            raise BackendError("Vertex service account JSON must include private_key and client_email")
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": client_email,
            "scope": scope,
            "aud": token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        header_raw = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        claims_raw = _b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        signing_input = f"{header_raw}.{claims_raw}".encode("utf-8")
        try:
            private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        except Exception as exc:
            raise BackendError(f"Failed to parse Vertex service account private key: {exc}") from exc
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header_raw}.{claims_raw}.{_b64url(signature)}"

    async def _vertex_access_token(self, service_account: Dict[str, Any]) -> str:
        client_email = str(service_account.get("client_email") or "").strip().lower()
        cached = self._vertex_token_cache.get(client_email)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

        token_uri = str(service_account.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
        assertion = self._service_account_jwt(
            service_account,
            scope="https://www.googleapis.com/auth/cloud-platform",
        )
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            data = response.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise BackendError("Vertex token exchange returned no access_token")
        expires_in = int(data.get("expires_in") or 3600)
        if client_email:
            self._vertex_token_cache[client_email] = (token, time.time() + max(120, expires_in - 30))
        return token

    async def _call_vertex(self, backend: BackendConfig, payload: Any) -> Any:
        credential_ref = backend.api_key_ref or "VERTEX_SERVICE_ACCOUNT_JSON"
        credential_blob = await self._resolve_api_key(credential_ref, "VERTEX_SERVICE_ACCOUNT_JSON")
        if not credential_blob:
            raise BackendError(
                f"Vertex credentials not found. Set '{credential_ref}' in key vault or environment to a service-account JSON."
            )
        service_account: Dict[str, Any] = {}
        parsed_ok = False
        try:
            candidate = json.loads(credential_blob)
            if isinstance(candidate, dict):
                service_account = candidate
                parsed_ok = True
        except Exception:
            parsed_ok = False
        if not parsed_ok:
            try:
                with open(credential_blob, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
                if isinstance(candidate, dict):
                    service_account = candidate
                    parsed_ok = True
            except Exception:
                parsed_ok = False
        if not parsed_ok:
            raise BackendError("Vertex credential must be a service-account JSON string or path to a JSON file")

        access_token = await self._vertex_access_token(service_account)
        payload_messages: Any = payload
        payload_project_id: Optional[str] = None
        payload_location: Optional[str] = None
        if isinstance(payload, dict):
            payload_messages = payload.get("messages")
            payload_project_id = str(payload.get("vertex_project_id") or "").strip() or None
            payload_location = str(payload.get("vertex_location") or "").strip() or None

        project_id = (
            str(service_account.get("project_id") or "").strip()
            or str(os.environ.get("VERTEX_PROJECT_ID", "") or "").strip()
        )
        if payload_project_id:
            project_id = payload_project_id
        if not project_id:
            raise BackendError("Vertex project_id is required (service-account project_id or VERTEX_PROJECT_ID)")

        location = str(os.environ.get("VERTEX_LOCATION", "us-central1") or "us-central1").strip()
        if payload_location:
            location = payload_location
        location = str(location or "us-central1").strip() or "us-central1"

        model_ref = str(backend.model or "").strip()
        if not model_ref:
            raise BackendError("Vertex backend model is required")

        if isinstance(payload_messages, list):
            messages = payload_messages
        else:
            messages = [{"role": "user", "content": str(payload_messages if payload_messages is not None else payload)}]
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        anthropic_model = _vertex_anthropic_model_ref(model_ref)
        body: Dict[str, Any]
        if anthropic_model:
            path = f"projects/{project_id}/locations/{location}/publishers/anthropic/models/{anthropic_model}:rawPredict"
            system_prompt, chat_messages = _claude_payload_messages(messages)
            max_tokens = int(params_dict.pop("max_tokens", 1024) or 1024)
            body = {
                "anthropic_version": "vertex-2023-10-16",
                "messages": _convert_tool_messages_for_claude(chat_messages),
                "max_tokens": max_tokens,
            }
            if system_prompt:
                body["system"] = system_prompt
            for key in ("temperature", "top_p", "top_k", "stop_sequences", "thinking", "stream"):
                if key in params_dict:
                    body[key] = params_dict[key]
        else:
            if model_ref.startswith("projects/"):
                path = f"{model_ref}:generateContent"
            elif model_ref.startswith("publishers/"):
                path = f"projects/{project_id}/locations/{location}/{model_ref}:generateContent"
            else:
                path = f"projects/{project_id}/locations/{location}/publishers/google/models/{model_ref}:generateContent"
            body = {"contents": _gemini_contents(messages)}
            generation_config = _google_generation_config(params_dict)
            if generation_config:
                body["generationConfig"] = generation_config

        url = f"https://{location}-aiplatform.googleapis.com/v1/{path}"
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        output = ""
        finish_reason = ""
        usage: Dict[str, Any] = {}
        if anthropic_model:
            try:
                blocks = data.get("content") or []
                texts = [str(block.get("text") or "") for block in blocks if isinstance(block, dict) and str(block.get("type") or "") == "text"]
                output = "\n".join([t for t in texts if t]).strip()
            except Exception:
                output = ""
            finish_reason = str(data.get("stop_reason") or data.get("finish_reason") or "").strip()
            usage_raw = data.get("usage")
            if isinstance(usage_raw, dict):
                usage = usage_raw
        else:
            try:
                output = str((data.get("candidates") or [{}])[0]["content"]["parts"][0].get("text") or "")
            except Exception:
                output = ""
            try:
                finish_reason = str((data.get("candidates") or [{}])[0].get("finishReason") or "").strip()
            except Exception:
                finish_reason = ""
            usage_raw = data.get("usageMetadata")
            if isinstance(usage_raw, dict):
                usage = usage_raw
        result = {"output": output, "usage": usage}
        if finish_reason:
            result["finish_reason"] = finish_reason
        return result

    async def _resolve_api_key(self, api_key_ref: str, default_env_var: str) -> str:
        if self.key_vault and api_key_ref:
            try:
                return (await self.key_vault.get_secret(api_key_ref)).strip()
            except Exception:
                # Fall through to environment-variable lookup for backward compatibility.
                pass

        if api_key_ref:
            return os.environ.get(api_key_ref, "").strip()
        return os.environ.get(default_env_var, "").strip()

    async def _validate_model_if_catalog_present(self, backend: BackendConfig) -> None:
        if backend.type in {"browser", "documentation"} or (
            backend.type == "custom"
            and str(backend.provider or "").strip().lower() == "http_connection"
        ):
            return
        if not self.model_registry:
            return
        try:
            has_models = await self.model_registry.has_any()
            if not has_models:
                return
            exists = await self.model_registry.exists(backend.provider, backend.model)
            if not exists:
                raise BackendError(
                    f"Model '{backend.model}' (provider '{backend.provider}') "
                    "is not present/enabled in the model catalog."
                )
        except BackendError:
            raise
        except Exception:
            # If model registry lookup fails unexpectedly, avoid blocking execution.
            return

    async def _required_tools_for_task(self, task: Task | None) -> list[str]:
        if task is None or not str(task.bot_id or "").strip():
            return []
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception:
            return []
        return required_worker_tools(bot)

    async def _require_task_worker_tools(self, worker: Worker, task: Task | None) -> None:
        missing_tools = worker_missing_tools(worker, await self._required_tools_for_task(task))
        if missing_tools:
            raise BackendError(
                f"Worker {worker.id} is missing required tool capabilities: {', '.join(missing_tools)}"
            )

    async def _require_fresh_autonomous_worker_probe(
        self,
        worker: Worker,
        task: Task | None,
    ) -> None:
        """Require recent attested evidence before a scheduled task reaches a worker."""
        if not _is_autonomous_schedule_task(task):
            return
        if self._worker_probe_store is None:
            raise BackendError(
                "Autonomous schedule dispatch requires a configured worker probe store."
            )
        probe = await self._worker_probe_store.get(worker.id)
        if not isinstance(probe, dict):
            raise BackendError(
                f"Autonomous schedule dispatch requires a recent ready probe for worker {worker.id}."
            )
        if str(probe.get("probe_status") or "").strip().lower() != "ready":
            raise BackendError(
                f"Autonomous schedule dispatch blocked: worker {worker.id} probe is not ready."
            )
        checked_at = str(probe.get("checked_at") or "").strip()
        try:
            checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackendError(
                f"Autonomous schedule dispatch blocked: worker {worker.id} probe has no valid timestamp."
            ) from exc
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -30 or age_seconds > autonomous_worker_probe_max_age_seconds():
            raise BackendError(
                f"Autonomous schedule dispatch blocked: worker {worker.id} probe is stale."
            )

    async def _resolve_worker_for_llm_backend(self, backend: BackendConfig, *, task: Task | None = None) -> Worker:
        required_tools = await self._required_tools_for_task(task)
        if backend.worker_id:
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            if not self._worker_has_capacity(worker, backend):
                raise BackendError(
                    f"Worker {worker.id} has no remaining task capacity for backend type {backend.type}"
                )
            missing_tools = worker_missing_tools(worker, required_tools)
            if missing_tools:
                raise BackendError(
                    f"Worker {worker.id} is missing required tool capabilities: {', '.join(missing_tools)}"
                )
            return worker

        workers = await self.worker_registry.list()
        candidates = [
            w
            for w in workers
            if w.enabled
            and w.status == "online"
            and self._worker_supports_backend(w, backend)
            and self._worker_has_capacity(w, backend)
            and not worker_missing_tools(w, required_tools)
        ]
        if not candidates:
            if required_tools:
                raise BackendError(
                    f"No online worker supports provider={backend.provider} model={backend.model} "
                    f"with required tools: {', '.join(required_tools)}"
                )
            raise BackendError(
                f"No online worker supports provider={backend.provider} model={backend.model}"
            )
        return min(candidates, key=self._score_worker)

    def _worker_supports_backend(self, worker: Worker, backend: BackendConfig) -> bool:
        backend_provider = str(backend.provider or "").strip().lower()
        backend_model = str(backend.model or "").strip()
        expected_capability_type = "tool" if backend.type in {"browser", "documentation"} else "llm"
        for cap in worker.capabilities:
            if str(cap.type).lower() != expected_capability_type:
                continue
            if str(cap.provider).lower() != backend_provider:
                continue
            if backend_model in (cap.models or []):
                return True
        return False

    async def _resolve_browser_worker(
        self, backend: BackendConfig, *, task: Task | None = None
    ) -> Worker:
        if str(backend.provider or "").strip().lower() != "browser" or str(
            backend.model or ""
        ).strip().lower() != "browser-ui":
            raise BackendError("Browser backends must use provider=browser and model=browser-ui")
        if not backend.worker_id:
            raise BackendError("worker_id is required for browser backends")
        try:
            worker = await self.worker_registry.get(backend.worker_id)
        except Exception as exc:
            raise BackendError(f"Worker not found: {backend.worker_id}") from exc
        if not worker.enabled or worker.status != "online":
            raise BackendError(f"Worker {worker.id} is not online and enabled")
        if not self._worker_has_capacity(worker, backend):
            raise BackendError(
                f"Worker {worker.id} has no remaining task capacity for backend type {backend.type}"
            )
        if not self._worker_supports_backend(worker, backend):
            raise BackendError(f"Worker {worker.id} does not advertise browser/browser-ui")
        await self._require_task_worker_tools(worker, task)
        return worker

    async def _resolve_documentation_worker(
        self, backend: BackendConfig, *, task: Task | None = None
    ) -> Worker:
        """Resolve one attested worker for a fixed Docs Hub write contract."""

        if str(backend.provider or "").strip().lower() != "documentation" or str(
            backend.model or ""
        ).strip().lower() != "documentation-v1":
            raise BackendError("Documentation backends must use provider=documentation and model=documentation-v1")
        if not backend.worker_id:
            raise BackendError("worker_id is required for documentation backends")
        try:
            worker = await self.worker_registry.get(backend.worker_id)
        except Exception as exc:
            raise BackendError(f"Worker not found: {backend.worker_id}") from exc
        if not worker.enabled or worker.status != "online":
            raise BackendError(f"Worker {worker.id} is not online and enabled")
        if not self._worker_has_capacity(worker, backend):
            raise BackendError(
                f"Worker {worker.id} has no remaining task capacity for backend type {backend.type}"
            )
        if not self._worker_supports_backend(worker, backend):
            raise BackendError(f"Worker {worker.id} does not advertise documentation/documentation-v1")
        await self._require_task_worker_tools(worker, task)
        return worker

    async def _consume_required_browser_action_approval(
        self,
        *,
        bot: Any,
        action_key: str,
        payload: dict[str, Any],
    ) -> None:
        """Consume a one-time owner approval when a bot marks an action as sensitive."""

        policy = bot_execution_policy(bot)
        required_actions = {
            str(item or "").strip()
            for item in policy.browser_action_owner_approval_required
            if str(item or "").strip()
        }
        if action_key not in required_actions:
            return
        approval_id = str(payload.get("owner_approval_id") or "").strip()
        if not approval_id:
            raise BackendError(f"{action_key} requires a valid, unused owner approval")
        if self._browser_action_approval_store is None:
            raise BackendError("Browser action approval service is unavailable")
        try:
            # Calculate the digest here as well so malformed payloads fail before any worker request.
            browser_action_payload_digest(payload)
            consumed = await self._browser_action_approval_store.consume(
                approval_id=approval_id,
                bot_id=bot.id,
                action_key=action_key,
                payload=payload,
            )
        except ValueError as exc:
            raise BackendError("Browser action approval payload is invalid") from exc
        if not consumed:
            raise BackendError(f"{action_key} requires a valid, unused owner approval")

    @staticmethod
    def _browser_action_request_body(
        payload: dict[str, Any], allowed_fields: set[str]
    ) -> dict[str, Any]:
        """Keep control-plane approval material out of worker-bound payloads."""

        return {
            field: payload[field]
            for field in allowed_fields - {"browser_action", "owner_approval_id"}
            if field in payload
        }

    async def _dispatch_documentation_write(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: Any,
        *,
        task: Task | None,
    ) -> dict[str, Any]:
        """Dispatch one allowlisted Markdown create or compare-and-save operation."""

        if task is None:
            raise BackendError("Documentation writes require a persisted task")
        if not isinstance(payload, dict):
            raise BackendError("Documentation writes require a JSON object payload")
        allowed_fields = {"action", "path", "content", "expectedContentHash"}
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Documentation write payload contains unsupported fields: " + ", ".join(unexpected_fields)
            )
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"create", "save"}:
            raise BackendError("Documentation action must be create or save")
        path = payload.get("path")
        content = payload.get("content")
        if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
            raise BackendError("Documentation writes require a non-empty path and text content")
        has_expected_hash = "expectedContentHash" in payload
        if action == "save" and not has_expected_hash:
            raise BackendError("Documentation save requires expectedContentHash")
        if action == "create" and has_expected_hash:
            raise BackendError("Documentation create cannot include expectedContentHash")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(f"Bot {task.bot_id} was not found for documentation authorization") from exc
        action_key = f"documentation.{action}"
        if action_key not in bot_execution_policy(bot).documentation_action_allowlist:
            raise BackendError(f"Bot {bot.id} is not authorized for {action_key}")
        if not backend.api_key_ref:
            raise BackendError("Documentation backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Documentation worker request token is not configured")
        try:
            url = f"{worker_base_url(worker)}/documentation/write"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        body = {field: payload[field] for field in allowed_fields if field in payload}
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(url, json=body, headers={"X-Nexus-Worker-Token": token})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Documentation worker returned an invalid write response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_browser_inspection(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: Any,
        task: Task | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BackendError("Browser inspection backend requires a JSON object payload")
        if "browser_action" in payload:
            browser_action = str(payload.get("browser_action") or "").strip()
            if browser_action == "test_builder":
                return await self._dispatch_browser_test_builder_action(worker, backend, payload, task=task)
            if browser_action == "question_bank":
                question_bank_action = str(payload.get("action") or "").strip().lower()
                if question_bank_action == "create_one":
                    return await self._dispatch_browser_question_bank_create(
                        worker, backend, payload, task=task
                    )
                if question_bank_action == "export_evidence":
                    return await self._dispatch_browser_question_bank_evidence_export(
                        worker, backend, payload, task=task
                    )
                return await self._dispatch_browser_question_bank_patch(worker, backend, payload, task=task)
            raise BackendError("Unsupported browser action")
        allowed_fields = {"path", "text_limit", "element_limit"}
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Browser inspection payload contains unsupported fields: " + ", ".join(unexpected_fields)
            )
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise BackendError("Browser inspection payload requires a non-empty path")
        normalized_path = path.strip()
        if task is not None:
            try:
                bot = await self.bot_registry.get(task.bot_id)
            except Exception as exc:
                raise BackendError(
                    f"Bot {task.bot_id} was not found for browser inspection authorization"
                ) from exc
            allowed_paths = {
                str(candidate).strip()
                for candidate in bot_execution_policy(bot).browser_inspection_path_allowlist
                if str(candidate).strip()
            }
            if allowed_paths and normalized_path not in allowed_paths:
                raise BackendError(
                    f"Bot {bot.id} is not authorized to inspect path {normalized_path}"
                )
        if not backend.api_key_ref:
            raise BackendError("Browser backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Browser worker request token is not configured")
        body = {"path": normalized_path}
        for field in ("text_limit", "element_limit"):
            if field in payload:
                body[field] = payload[field]
        try:
            url = f"{worker_base_url(worker)}/browser/inspect"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Nexus-Worker-Token": token},
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Browser worker returned an invalid inspection response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_browser_test_builder_action(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: dict[str, Any],
        *,
        task: Task | None,
    ) -> dict[str, Any]:
        """Dispatch the fixed, draft-only Test Builder workflow to a pinned worker."""

        allowed_fields = {
            "browser_action",
            "action",
            "mode",
            "confirmation",
            "owner_approval_id",
            "course_id",
            "lesson_id",
            "title",
            "pass_threshold_pct",
            "time_limit_seconds",
            "allow_review",
            "banks",
            "acknowledge_attempt_reset",
        }
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Test Builder payload contains unsupported fields: " + ", ".join(unexpected_fields)
            )
        if str(payload.get("browser_action") or "").strip() != "test_builder":
            raise BackendError("Browser actions must declare browser_action=test_builder")
        action = str(payload.get("action") or "").strip().lower()
        if action == "publish":
            raise BackendError("Browser workers cannot publish")
        if action not in {"save_configuration", "build_from_banks"}:
            raise BackendError("Unsupported Test Builder action")
        if str(payload.get("mode") or "").strip().lower() != "draft":
            raise BackendError("Test Builder actions are limited to draft mode")
        if not str(payload.get("confirmation") or "").strip():
            raise BackendError("Test Builder actions require explicit confirmation")
        if task is None:
            raise BackendError("Test Builder actions require a persisted task")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(f"Bot {task.bot_id} was not found for Test Builder authorization") from exc
        action_key = f"test_builder.{action}"
        if action_key not in bot_execution_policy(bot).browser_action_allowlist:
            raise BackendError(f"Bot {bot.id} is not authorized for {action_key}")
        await self._consume_required_browser_action_approval(
            bot=bot,
            action_key=action_key,
            payload=payload,
        )
        if action == "build_from_banks" and payload.get("acknowledge_attempt_reset") is not True:
            raise BackendError("Building from banks requires explicit attempt-reset acknowledgement")
        if not backend.api_key_ref:
            raise BackendError("Browser backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Browser worker request token is not configured")
        try:
            url = f"{worker_base_url(worker)}/browser/test-builder"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        body = self._browser_action_request_body(payload, allowed_fields)
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Nexus-Worker-Token": token},
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Browser worker returned an invalid Test Builder response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_browser_question_bank_patch(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: dict[str, Any],
        *,
        task: Task | None,
    ) -> dict[str, Any]:
        """Dispatch one explicitly authorized existing-question UI patch."""

        allowed_fields = {
            "browser_action",
            "action",
            "confirmation",
            "owner_approval_id",
            "bank_id",
            "question_id",
            "expected",
            "changes",
            "review_evidence",
        }
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Question Bank payload contains unsupported fields: " + ", ".join(unexpected_fields)
            )
        if str(payload.get("browser_action") or "").strip() != "question_bank":
            raise BackendError("Browser actions must declare browser_action=question_bank")
        action = str(payload.get("action") or "").strip().lower()
        if action == "publish":
            raise BackendError("Browser workers cannot publish")
        if action != "patch_existing":
            raise BackendError("Unsupported Question Bank action")
        if not str(payload.get("confirmation") or "").strip():
            raise BackendError("Question Bank patches require explicit confirmation")
        if (
            not isinstance(payload.get("expected"), dict)
            or not isinstance(payload.get("changes"), dict)
            or not isinstance(payload.get("review_evidence"), dict)
        ):
            raise BackendError("Question Bank patches require expected, changes, and reviewer evidence objects")
        if task is None:
            raise BackendError("Question Bank patches require a persisted task")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(f"Bot {task.bot_id} was not found for Question Bank authorization") from exc
        action_key = f"question_bank.{action}"
        if action_key not in bot_execution_policy(bot).browser_action_allowlist:
            raise BackendError(f"Bot {bot.id} is not authorized for {action_key}")
        await self._consume_required_browser_action_approval(
            bot=bot,
            action_key=action_key,
            payload=payload,
        )
        if not backend.api_key_ref:
            raise BackendError("Browser backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Browser worker request token is not configured")
        try:
            url = f"{worker_base_url(worker)}/browser/question-bank"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        body = self._browser_action_request_body(payload, allowed_fields)
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Nexus-Worker-Token": token},
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Browser worker returned an invalid Question Bank response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_browser_question_bank_create(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: dict[str, Any],
        *,
        task: Task | None,
    ) -> dict[str, Any]:
        """Dispatch one shortage-approved Question Bank creation through the fixed UI workflow."""

        allowed_fields = {
            "browser_action",
            "action",
            "confirmation",
            "owner_approval_id",
            "bank_id",
            "candidate",
            "review_evidence",
        }
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Question Bank creation payload contains unsupported fields: "
                + ", ".join(unexpected_fields)
            )
        if str(payload.get("browser_action") or "").strip() != "question_bank":
            raise BackendError("Browser actions must declare browser_action=question_bank")
        if str(payload.get("action") or "").strip().lower() != "create_one":
            raise BackendError("Unsupported Question Bank creation action")
        if not str(payload.get("confirmation") or "").strip():
            raise BackendError("Question Bank creation requires explicit confirmation")
        if not isinstance(payload.get("candidate"), dict) or not isinstance(
            payload.get("review_evidence"), dict
        ):
            raise BackendError("Question Bank creation requires candidate and reviewer evidence objects")
        if task is None:
            raise BackendError("Question Bank creation requires a persisted task")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(
                f"Bot {task.bot_id} was not found for Question Bank authorization"
            ) from exc
        action_key = "question_bank.create_one"
        if action_key not in bot_execution_policy(bot).browser_action_allowlist:
            raise BackendError(f"Bot {bot.id} is not authorized for {action_key}")
        await self._consume_required_browser_action_approval(
            bot=bot,
            action_key=action_key,
            payload=payload,
        )
        if not backend.api_key_ref:
            raise BackendError("Browser backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Browser worker request token is not configured")
        try:
            url = f"{worker_base_url(worker)}/browser/question-bank-create"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        body = self._browser_action_request_body(payload, allowed_fields)
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Nexus-Worker-Token": token},
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Browser worker returned an invalid Question Bank creation response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_browser_question_bank_evidence_export(
        self,
        worker: Worker,
        backend: BackendConfig,
        payload: dict[str, Any],
        *,
        task: Task | None,
    ) -> dict[str, Any]:
        """Dispatch one read-only complete Question Bank evidence export."""

        allowed_fields = {
            "browser_action",
            "action",
            "bank_id",
            "approvedReadOnlyActions",
        }
        unexpected_fields = sorted(set(payload) - allowed_fields)
        if unexpected_fields:
            raise BackendError(
                "Question Bank evidence export payload contains unsupported fields: "
                + ", ".join(unexpected_fields)
            )
        if str(payload.get("browser_action") or "").strip() != "question_bank":
            raise BackendError("Browser actions must declare browser_action=question_bank")
        if str(payload.get("action") or "").strip().lower() != "export_evidence":
            raise BackendError("Unsupported Question Bank evidence export action")
        if task is None:
            raise BackendError("Question Bank evidence export requires a persisted task")
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except Exception as exc:
            raise BackendError(
                f"Bot {task.bot_id} was not found for Question Bank authorization"
            ) from exc
        action_key = "question_bank.export_evidence"
        if action_key not in bot_execution_policy(bot).browser_action_allowlist:
            raise BackendError(f"Bot {bot.id} is not authorized for {action_key}")
        if not backend.api_key_ref:
            raise BackendError("Browser backends require api_key_ref for the worker request token")
        token = await self._resolve_api_key(backend.api_key_ref, "")
        if not token:
            raise BackendError("Browser worker request token is not configured")
        try:
            url = f"{worker_base_url(worker)}/browser/question-bank-export"
        except WorkerProbeError as exc:
            raise BackendError(f"Worker {worker.id} has an invalid address") from exc

        body = {field: payload[field] for field in allowed_fields - {"browser_action"} if field in payload}
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Nexus-Worker-Token": token},
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise BackendError("Browser worker returned an invalid Question Bank evidence response")
                return result
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                previous = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * previous)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    def _score_worker(self, worker: Worker) -> float:
        metrics = worker.metrics
        queue_depth = int(getattr(metrics, "queue_depth", 0) or 0)
        load = float(getattr(metrics, "load", 0.0) or 0.0)
        gpu_util = getattr(metrics, "gpu_utilization", None) or []
        gpu_avg = (sum(gpu_util) / len(gpu_util)) if gpu_util else 0.0
        inflight = int(self._inflight_by_worker.get(worker.id, 0))
        latency_ms = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
        return (
            (queue_depth * 5.0)
            + (inflight * 4.0)
            + (load / 20.0)
            + (gpu_avg / 25.0)
            + (latency_ms / 500.0)
        )

    def get_worker_runtime_metrics(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for worker_id in set(self._inflight_by_worker.keys()) | set(self._latency_ema_ms.keys()):
            out[worker_id] = {
                "inflight": float(self._inflight_by_worker.get(worker_id, 0)),
                "latency_ema_ms": float(self._latency_ema_ms.get(worker_id, self._default_latency_ms)),
            }
        return out
