import asyncio
import json
import logging
import os
import re
import time
from pathlib import PurePosixPath
from typing import Any, AsyncGenerator

import httpx

from shared.bot_policy import bot_allows_repo_output
from shared.exceptions import BackendError, BotNotFoundError, NoViableBackendError
from shared.models import BackendConfig, BackendParams, Task, Worker
from shared.settings_manager import SettingsManager

logger = logging.getLogger(__name__)


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
    if max_tokens is not None and "num_predict" not in options:
        options["num_predict"] = max_tokens
    # When no num_predict is set, apply a platform default so Ollama's low built-in
    # cap (128 tokens in older versions) does not silently truncate responses.
    # Setting -1 means "unlimited" in Ollama; override via settings key
    # default_ollama_num_predict if you need a hard cap.
    if "num_predict" not in options:
        default_predict = _settings_int("default_ollama_num_predict", -1)
        if default_predict != 0:
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
            updates["max_tokens"] = _retry_incremented_value(
                params_dict["max_tokens"],
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


def _payload_to_messages(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, list):
        normalized: list[dict[str, str]] = []
        for item in payload:
            if isinstance(item, dict):
                role = str(item.get("role") or "user")
                content = item.get("content")
                if isinstance(content, str):
                    normalized.append({"role": role, "content": content})
                else:
                    normalized.append({"role": role, "content": json.dumps(content if content is not None else "", ensure_ascii=False)})
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
        parts.append(_truncate_text(conversation_transcript, 2200))
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
        from dashboard.db import get_db
        from dashboard.models import BotConnection, Connection
    except Exception:
        return []

    db = get_db()
    try:
        links = db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_id)).all()
        connection_ids = [int(link.connection_id) for link in links]
        if not connection_ids:
            return []
        return (
            db.query(Connection)
            .filter(Connection.id.in_(connection_ids), Connection.enabled.is_(True))
            .order_by(Connection.name.asc())
            .all()
        )
    except Exception as exc:
        logger.warning("Failed to load attached bot connections for %s: %s", bot_id, exc)
        return []
    finally:
        db.close()


def _resolve_attached_connection(
    rows: list[Any],
    *,
    requested_name: str | None = None,
    requested_id: str | None = None,
) -> Any | None:
    if requested_id:
        match = next((row for row in rows if str(getattr(row, "id", "")) == str(requested_id)), None)
        if match is not None:
            return match
    if requested_name:
        match = next(
            (row for row in rows if str(getattr(row, "name", "")).strip().lower() == str(requested_name).strip().lower()),
            None,
        )
        if match is not None:
            return match
    if len(rows) == 1:
        return rows[0]
    return None


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

    try:
        from dashboard.connections_service import parse_openapi_actions
    except Exception:
        parse_openapi_actions = None  # type: ignore[assignment]

    parts: list[str] = [
        "Attached connection schemas:",
        "Use these attached connection definitions as authoritative for field names, nesting, and allowed JSON shapes.",
        "Do not invent fields outside the attached schemas and examples.",
    ]
    remaining_chars = max_total_chars

    for row in target_rows:
        section: list[str] = [f"Connection: {str(getattr(row, 'name', '') or '').strip() or getattr(row, 'id', '')} ({str(getattr(row, 'kind', '') or '').strip() or 'unknown'})"]
        description = str(getattr(row, "description", "") or "").strip()
        if description:
            section.append(f"Description: {description}")

        try:
            connection_config = json.loads(getattr(row, "config_json", "{}") or "{}")
        except Exception:
            connection_config = {}
        if isinstance(connection_config, dict):
            if str(getattr(row, "kind", "") or "").strip().lower() == "http":
                base_url = str(connection_config.get("base_url") or "").strip()
                if base_url:
                    section.append(f"Base URL: {base_url}")
            if str(getattr(row, "kind", "") or "").strip().lower() == "database":
                readonly = bool(connection_config.get("readonly", False))
                section.append(f"Readonly: {'true' if readonly else 'false'}")

        schema_text = str(getattr(row, "schema_text", "") or "").strip()
        if include_actions and parse_openapi_actions and str(getattr(row, "kind", "") or "").strip().lower() == "http" and schema_text:
            try:
                actions = parse_openapi_actions(schema_text)
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

    try:
        from dashboard.connections_service import resolve_auth_payload, test_http_connection
    except Exception:
        return ""

    try:
        connection_config = json.loads(getattr(connection, "config_json", "{}") or "{}")
    except Exception:
        connection_config = {}
    try:
        auth_payload = resolve_auth_payload(json.loads(getattr(connection, "auth_json", "{}") or "{}"))
    except Exception:
        auth_payload = {}
    schema_text = str(getattr(connection, "schema_text", "") or "")

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
        result = test_http_connection(
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
    if bot_id:
        connection_suffix = _connection_context_prompt_suffix(bot_id, bot, payload).strip()
        if connection_suffix:
            suffix_parts.append(connection_suffix)
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
    if backend.type == "custom":
        return payload
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
            normalized.append({"role": role, "content": content_parts[0]["text"]})
        else:
            normalized.append({"role": role, "content": content_parts})
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
        normalized.append(entry)
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
    ) -> None:
        self.bot_registry = bot_registry
        self.worker_registry = worker_registry
        self.key_vault = key_vault
        self.model_registry = model_registry
        self.project_registry = project_registry
        self._inflight_by_worker: dict[str, int] = {}
        self._latency_ema_ms: dict[str, float] = {}
        self._latency_alpha = float(os.environ.get("NEXUSAI_WORKER_LATENCY_EMA_ALPHA", "0.30"))
        self._default_latency_ms = float(os.environ.get("NEXUSAI_WORKER_DEFAULT_LATENCY_MS", "800"))

    def _worker_capacity_limit(self, worker: Worker, backend: BackendConfig) -> int:
        if str(getattr(backend, "type", "") or "").strip().lower() == "local_llm":
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
        transformed_payload = self._apply_input_transform(bot, task.payload)
        for backend in bot.backends:
            try:
                effective_backend = _backend_with_retry_params(backend, task)
                prepared_payload = _prepare_payload_for_backend(bot, effective_backend, transformed_payload, task=task)
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
        for backend in bot.backends:
            try:
                effective_backend = _backend_with_retry_params(backend, task)
                prepared_payload = _prepare_payload_for_backend(bot, effective_backend, transformed_payload, task=task)
                yield {
                    "event": "backend_selected",
                    "provider": effective_backend.provider,
                    "model": effective_backend.model,
                    "worker_id": effective_backend.worker_id,
                }
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
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm"):
            worker = await self._resolve_worker_for_llm_backend(backend)
            if worker.status != "online":
                raise BackendError(
                    f"Worker {worker.id} is not online (status={worker.status})"
                )
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        elif backend.type == "cloud_api":
            if backend.provider == "openai":
                return await self._call_openai(backend, safe_payload)
            elif backend.provider == "ollama_cloud":
                return await self._call_ollama_cloud(backend, safe_payload)
            elif backend.provider == "claude":
                return await self._call_claude(backend, safe_payload)
            elif backend.provider == "gemini":
                return await self._call_gemini(backend, safe_payload)
            else:
                raise BackendError(f"Unknown cloud_api provider: {backend.provider}")
        elif backend.type == "cli":
            if not backend.worker_id:
                raise BackendError("worker_id is required for cli backends")
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        elif backend.type == "custom":
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
        return await asyncio.to_thread(self._run_http_connection_backend_sync, payload, task.bot_id)

    def _run_http_connection_backend_sync(self, payload: dict[str, Any], bot_id: str) -> dict[str, Any]:
        from dashboard.connections_service import resolve_auth_payload, test_http_connection
        from dashboard.db import get_db
        from dashboard.models import BotConnection, Connection

        connection_ref = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
        requested_name = str(connection_ref.get("name") or payload.get("connection_name") or "").strip()
        requested_id = str(connection_ref.get("id") or payload.get("connection_id") or "").strip()
        continue_on_error = bool(payload.get("continue_on_error", False))

        raw_actions = payload.get("connection_actions")
        if isinstance(raw_actions, dict):
            actions = [raw_actions]
        elif isinstance(raw_actions, list):
            actions = [item for item in raw_actions if isinstance(item, dict)]
        elif isinstance(payload.get("connection_action"), dict):
            actions = [payload["connection_action"]]
        else:
            actions = []
        if not actions:
            raise BackendError("http_connection backend requires at least one connection action")

        db = get_db()
        try:
            links = db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_id)).all()
            connection_ids = [int(link.connection_id) for link in links]
            if not connection_ids:
                raise BackendError(f"Bot {bot_id} has no attached connections")
            rows = db.query(Connection).filter(Connection.id.in_(connection_ids)).all()
            if requested_id:
                connection = next((row for row in rows if str(row.id) == requested_id), None)
            elif requested_name:
                connection = next((row for row in rows if str(row.name) == requested_name), None)
            elif len(rows) == 1:
                connection = rows[0]
            else:
                raise BackendError("Multiple bot connections are attached; specify connection.name or connection.id")
            if connection is None:
                raise BackendError("Requested bot connection was not found")
            if str(connection.kind or "").strip().lower() != "http":
                raise BackendError("http_connection backend only supports HTTP connections")

            config = json.loads(connection.config_json or "{}")
            auth = resolve_auth_payload(json.loads(connection.auth_json or "{}"))
            schema_text = str(connection.schema_text or "")
        finally:
            db.close()

        action_results: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        completed_actions: list[str] = []
        failed_actions: list[str] = []

        for index, action in enumerate(actions):
            op_id = str(action.get("operation_id") or action.get("path") or f"action_{index + 1}").strip()
            result = test_http_connection(
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
            "connection_name": str(connection.name),
            "connection_id": int(connection.id),
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
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm", "cli"):
            worker = await self._resolve_worker_for_llm_backend(backend) if backend.type != "cli" else await self.worker_registry.get(backend.worker_id)  # type: ignore[arg-type]
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

    async def _dispatch_to_worker(
        self, worker: Worker, backend: BackendConfig, payload: Any
    ) -> Any:
        url = f"http://{worker.host}:{worker.port}/infer"
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
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(url, json=body)
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
                async with client.stream("POST", url, json=body) as response:
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
        body: dict = {
            "model": backend.model,
            "messages": messages,
            "stream": False,
            "options": _ollama_options(params_dict),
        }
        base_url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api").rstrip("/")
        async with httpx.AsyncClient(timeout=_cloud_timeout()) as client:
            response = await client.post(
                f"{base_url}/chat",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                detail = ""
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
                status = response.status_code
                if detail:
                    raise BackendError(f"Ollama Cloud request failed ({status}): {detail}") from e
                raise BackendError(f"Ollama Cloud request failed ({status})") from e
            data = response.json()
            output = data.get("message", {}).get("content", "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            }
            finish_reason = str(data.get("done_reason") or data.get("finish_reason") or "").strip()
            result = {"output": output, "usage": usage}
            if finish_reason:
                result["finish_reason"] = finish_reason
            return result

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
        if params_dict:
            body["generationConfig"] = params_dict
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

    async def _resolve_worker_for_llm_backend(self, backend: BackendConfig) -> Worker:
        if backend.worker_id:
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            if not self._worker_has_capacity(worker, backend):
                raise BackendError(
                    f"Worker {worker.id} has no remaining task capacity for backend type {backend.type}"
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
        ]
        if not candidates:
            raise BackendError(
                f"No online worker supports provider={backend.provider} model={backend.model}"
            )
        return min(candidates, key=self._score_worker)

    def _worker_supports_backend(self, worker: Worker, backend: BackendConfig) -> bool:
        backend_provider = str(backend.provider or "").strip().lower()
        backend_model = str(backend.model or "").strip()
        for cap in worker.capabilities:
            if str(cap.type).lower() != "llm":
                continue
            if str(cap.provider).lower() != backend_provider:
                continue
            if backend_model in (cap.models or []):
                return True
        return False

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
