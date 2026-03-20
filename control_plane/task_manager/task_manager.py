import asyncio
import heapq
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from control_plane.task_result_files import extract_file_candidates
from shared.exceptions import TaskNotFoundError
from shared.models import BotRun, BotRunArtifact, Task, TaskError, TaskMetadata
from shared.settings_manager import SettingsManager
from control_plane.scheduler.dependency_engine import DependencyEngine

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")
_TASKS_TABLE = "cp_tasks"
_TASK_DEPENDENCIES_TABLE = "cp_task_dependencies"
_BOT_RUNS_TABLE = "cp_bot_runs"
_BOT_RUN_ARTIFACTS_TABLE = "cp_bot_run_artifacts"

_CREATE_TASKS = f"""
CREATE TABLE IF NOT EXISTS {_TASKS_TABLE} (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT,
    payload    TEXT,
    metadata   TEXT,
    depends_on TEXT,
    status     TEXT,
    result     TEXT,
    error      TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

_CREATE_TASK_DEPENDENCIES = f"""
CREATE TABLE IF NOT EXISTS {_TASK_DEPENDENCIES_TABLE} (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
)
"""

_CREATE_BOT_RUNS = f"""
CREATE TABLE IF NOT EXISTS {_BOT_RUNS_TABLE} (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    bot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    metadata TEXT,
    result TEXT,
    error TEXT,
    triggered_by_task_id TEXT,
    trigger_rule_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
)
"""

_CREATE_BOT_RUN_ARTIFACTS = f"""
CREATE TABLE IF NOT EXISTS {_BOT_RUN_ARTIFACTS_TABLE} (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    content TEXT,
    path TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
)
"""


class _TaskExecutionFailure(Exception):
    def __init__(self, message: str, *, result: Any = None) -> None:
        super().__init__(message)
        self.result = result


def _summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())
        return f"Object with keys: {', '.join(keys[:12])}" if keys else "Empty object"
    if isinstance(payload, list):
        return f"List with {len(payload)} item(s)"
    return str(type(payload).__name__)


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_json_payload(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty output")
    candidates = [raw]
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fence_matches if match.strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for start_char in ("{", "["):
            start = candidate.find(start_char)
            if start < 0:
                continue
            try:
                parsed, end = decoder.raw_decode(candidate[start:])
                if candidate[start + end :].strip():
                    continue
                return parsed
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON object or array found")


def _lookup_nested_path(payload: Any, path: str) -> Any:
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


def _lookup_payload_path(payload: Any, path: str) -> Any:
    return _lookup_nested_path(payload, path)


def _split_transform_expr_list(expr: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
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


def _resolve_transform_value(expr: str, payload: Any, notes: list[str]) -> Any:
    mode = "value"
    raw_expr = str(expr or "").strip()
    if raw_expr.startswith("json:"):
        mode = "json"
        raw_expr = raw_expr[5:].strip()
    if raw_expr.startswith("render:"):
        render_path = raw_expr[len("render:") :].strip()
        if render_path.startswith("payload."):
            render_path = render_path[8:].strip()
        rendered = _transform_template_value(_lookup_payload_path(payload, render_path), payload, notes)
        if mode == "json":
            return rendered
        return rendered
    if raw_expr.startswith("coalesce:"):
        candidates = _split_transform_expr_list(raw_expr[len("coalesce:") :])
        for candidate in candidates:
            literal_ok, literal_value = _parse_transform_literal(candidate)
            if literal_ok:
                if literal_value is not None:
                    return literal_value
                continue
            value = _resolve_transform_value(("json:" + candidate) if mode == "json" else candidate, payload, notes)
            if value not in (None, "", [], {}):
                return value
        return None
    literal_ok, literal_value = _parse_transform_literal(raw_expr)
    if literal_ok:
        return literal_value
    path = raw_expr
    if path.startswith("payload."):
        path = path[8:].strip()
    value = _lookup_payload_path(payload, path)
    if mode == "json":
        if value in (None, ""):
            return [] if path.endswith("_json") else None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            notes.append(f"Could not parse JSON field: {path}")
            return None
    return value


def _transform_template_value(template: Any, payload: Any, notes: list[str]) -> Any:
    if isinstance(template, dict):
        return {str(key): _transform_template_value(value, payload, notes) for key, value in template.items()}
    if isinstance(template, list):
        return [_transform_template_value(item, payload, notes) for item in template]
    if not isinstance(template, str):
        return template

    raw = template.strip()
    if raw.startswith("{{") and raw.endswith("}}"):
        expr = raw[2:-2].strip()
        return _resolve_transform_value(expr, payload, notes)
    return template


def _is_empty_contract_value(value: Any) -> bool:
    return value in (None, "", [], {})


def _missing_payload_fields(payload: Any, fields: list[str]) -> list[str]:
    if not isinstance(payload, dict):
        return [str(field) for field in fields]
    missing: list[str] = []
    for field in fields:
        path = str(field).strip()
        if not path:
            continue
        if _lookup_payload_path(payload, path) is None:
            missing.append(path)
    return missing


def _empty_payload_fields(payload: Any, fields: list[str]) -> list[str]:
    if not isinstance(payload, dict):
        return [str(field) for field in fields]
    empty: list[str] = []
    for field in fields:
        path = str(field).strip()
        if not path:
            continue
        if _is_empty_contract_value(_lookup_payload_path(payload, path)):
            empty.append(path)
    return empty


def _looks_like_flat_launch_payload(payload: Any, required_fields: list[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = {str(key).strip() for key in payload.keys() if str(key).strip()}
    if len(keys) < 3:
        return False
    required = [str(field).strip() for field in required_fields if str(field).strip()]
    if not required:
        return False
    if any(field in keys for field in required):
        return False
    has_serialized_fields = any(key.endswith("_json") for key in keys)
    has_brief_like_fields = bool(keys.intersection({"topic", "subject", "scope", "audience", "language", "level"}))
    return has_serialized_fields and has_brief_like_fields


def _looks_like_trigger_wrapper_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = {str(key).strip() for key in payload.keys() if str(key).strip()}
    if not keys:
        return False
    return bool(keys.intersection({"source_payload", "source_result", "source_task_id", "source_bot_id"}))


def _payload_satisfies_output_contract(payload: Any, required_fields: list[str], non_empty_fields: list[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    if required_fields:
        missing = [field for field in required_fields if str(field) not in payload]
        if missing:
            return False
    if non_empty_fields:
        empty_fields = [
            str(field)
            for field in non_empty_fields
            if _is_empty_contract_value(_lookup_payload_path(payload, str(field)))
        ]
        if empty_fields:
            return False
    return bool(required_fields or non_empty_fields)


def _merge_with_contract_defaults(value: Any, defaults: Any) -> Any:
    if defaults is None:
        return value
    if _is_empty_contract_value(value):
        return defaults
    if isinstance(value, dict) and isinstance(defaults, dict):
        merged: dict[str, Any] = dict(value)
        for key, default_value in defaults.items():
            current_value = merged.get(key)
            if key not in merged:
                merged[key] = default_value
            else:
                merged[key] = _merge_with_contract_defaults(current_value, default_value)
        return merged
    if isinstance(value, list) and isinstance(defaults, list):
        return value if len(value) > 0 else defaults
    return value


def _result_usage(task: Task) -> dict[str, Any]:
    if not isinstance(task.result, dict):
        return {}
    usage = task.result.get("usage")
    return usage if isinstance(usage, dict) else {}


def _usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and "input_tokens" in usage:
        prompt = usage.get("input_tokens")
    if completion is None and "output_tokens" in usage:
        completion = usage.get("output_tokens")
    if prompt is None and "promptTokenCount" in usage:
        prompt = usage.get("promptTokenCount")
    if completion is None and "candidatesTokenCount" in usage:
        completion = usage.get("candidatesTokenCount")

    total = usage.get("total_tokens")
    if total is None:
        try:
            total = int(prompt or 0) + int(completion or 0)
        except (TypeError, ValueError):
            total = None

    summary = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    for key, value in usage.items():
        if key not in summary:
            summary[key] = value
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _is_output_contract_error_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return "output contract" in text


def _extract_result_usage(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    usage = result.get("usage")
    return usage if isinstance(usage, dict) else {}


def _extract_result_finish_reason(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    raw = result.get("finish_reason")
    if raw in (None, ""):
        usage = _extract_result_usage(result)
        raw = usage.get("finish_reason")
    return str(raw or "").strip().lower()


def _extract_completion_tokens(result: Any) -> Optional[int]:
    usage = _extract_result_usage(result)
    for key in ("completion_tokens", "output_tokens", "candidatesTokenCount", "eval_count"):
        if key in usage:
            try:
                return int(usage.get(key))
            except (TypeError, ValueError):
                continue
    return None


def _extract_result_output_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    for key in ("output", "content", "text", "result"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _looks_like_trigger_wrapper_instruction(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("triggered by bot ")


def _looks_like_truncated_result(result: Any) -> bool:
    finish_reason = _extract_result_finish_reason(result)
    if finish_reason in {"length", "max_tokens", "max_output_tokens", "token_limit", "max_new_tokens"}:
        return True
    completion_tokens = _extract_completion_tokens(result)
    if completion_tokens is None:
        return False
    output = _extract_result_output_text(result).strip()
    # Only flag as truncated if output ends mid-sentence (incomplete syntax)
    # Don't flag based on token count alone - models can legitimately produce long outputs
    if not output:
        return False
    if output.endswith(("...", "```", "`", ":", ",", "(", "[", "{", "|")):
        return True
    # Check if output ends mid-sentence (no proper ending punctuation)
    # But only flag this if the finish_reason suggests truncation was attempted
    # Otherwise long but complete outputs are valid
    return False


def _execution_summary(task: Task) -> dict[str, Any]:
    created = _parse_iso_dt(task.created_at)
    updated = _parse_iso_dt(task.updated_at)
    duration_ms = None
    if created is not None and updated is not None:
        duration_ms = max(0, int((updated - created).total_seconds() * 1000))

    usage_summary = _usage_summary(_result_usage(task))
    metadata = task.metadata.model_dump() if task.metadata else {}
    return {
        "task_id": task.id,
        "bot_id": task.bot_id,
        "status": task.status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "duration_ms": duration_ms,
        "metadata": metadata,
        "usage": usage_summary,
        "has_result": task.result is not None,
        "has_error": task.error is not None,
    }


def _execution_report_markdown(task: Task) -> str:
    summary = _execution_summary(task)
    usage = summary.get("usage") or {}
    lines = [
        f"# Execution Report: {task.bot_id}",
        "",
        f"- Task ID: {task.id}",
        f"- Status: {task.status}",
        f"- Created: {task.created_at}",
        f"- Updated: {task.updated_at}",
        f"- Duration (ms): {summary.get('duration_ms') if summary.get('duration_ms') is not None else '—'}",
        f"- Project: {summary['metadata'].get('project_id') or '—'}",
        f"- Source: {summary['metadata'].get('source') or '—'}",
        f"- Orchestration: {summary['metadata'].get('orchestration_id') or '—'}",
    ]
    if usage:
        lines.extend(
            [
                "",
                "## Token Usage",
                f"- Prompt Tokens: {usage.get('prompt_tokens', '—')}",
                f"- Completion Tokens: {usage.get('completion_tokens', '—')}",
                f"- Total Tokens: {usage.get('total_tokens', '—')}",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _settings_int(name: str, default: int) -> int:
    try:
        return int(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _settings_float(name: str, default: float) -> float:
    try:
        return float(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _is_retryable_error_message(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    retryable_markers = [
        "timeout",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "no valid json object or array found",
        "output contract requires structured json output",
        "output contract missing required fields",
        "output contract requires a json object",
        "output contract requires a json array",
        "internal server error",
        "server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "too many requests",
        "rate limit",
        "truncated at token limit",
        "model output likely truncated",
        "connection reset",
        "temporarily unavailable",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    ]
    return any(marker in normalized for marker in retryable_markers)


def _prefers_truncation_retry(task: Task) -> bool:
    metadata = task.metadata
    if metadata is None:
        return False
    source = str(metadata.source or "").strip().lower()
    if source in {"chat_assign", "auto_retry"}:
        return True
    return bool(metadata.orchestration_id)


def _normalize_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _normalize_assignment_step_kind(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "spec": "specification",
        "requirements": "specification",
        "design": "planning",
        "architecture": "planning",
        "implementation": "repo_change",
        "implement": "repo_change",
        "code": "repo_change",
        "testing": "test_execution",
        "tests": "test_execution",
        "qa": "test_execution",
        "reviewer": "review",
        "security_review": "review",
        "ship": "release",
        "merge": "release",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"specification", "planning", "repo_change", "test_execution", "review", "release"}:
        return normalized
    return ""


def _looks_like_repo_path(value: str) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if "/" not in text:
        return False
    leaf = text.rsplit("/", 1)[-1]
    return "." in leaf and " " not in leaf


def _looks_like_repo_file(value: str) -> bool:
    text = str(value or "").strip().replace("\\", "/").strip("`")
    if not text or " " in text:
        return False
    if "/" in text:
        return _looks_like_repo_path(text)
    return "." in text and not text.lower().startswith("http")


def _infer_assignment_step_kind(payload: Dict[str, Any]) -> str:
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    deliverables = _normalize_string_list(payload.get("deliverables"))
    text = " ".join(
        [
            str(payload.get("title") or ""),
            str(payload.get("instruction") or ""),
            role_hint,
            " ".join(deliverables),
        ]
    ).lower()
    if role_hint in {"tester", "qa"}:
        return "test_execution"
    if role_hint in {"reviewer", "security", "security-reviewer"}:
        return "review"
    if role_hint in {"researcher", "analyst"}:
        return "specification"
    if role_hint in {"coder", "developer", "engineer"} and any(_looks_like_repo_path(item) for item in deliverables):
        return "repo_change"
    if any(token in text for token in ("release", "merge", "deploy", "tag", "ship")):
        return "release"
    if any(token in text for token in ("test", "coverage", "qa", "pytest", "verification")):
        return "test_execution"
    if any(token in text for token in ("review", "audit", "security", "finding")):
        return "review"
    if any(token in text for token in ("spec", "requirement", "acceptance criteria", "user story")):
        return "specification"
    if any(token in text for token in ("implement", "code", "patch", "refactor", "api route", "component")):
        return "repo_change"
    if any(_looks_like_repo_path(item) for item in deliverables):
        return "repo_change"
    if any(token in text for token in ("plan", "design", "architecture", "migration", "rollback")):
        return "planning"
    return ""


def _assignment_step_kind(payload: Dict[str, Any]) -> str:
    explicit = _normalize_assignment_step_kind(payload.get("step_kind"))
    if explicit:
        return explicit
    return _infer_assignment_step_kind(payload)


def _trigger_target_role_hint(bot_id: str) -> str:
    normalized = str(bot_id or "").strip().lower()
    mapping = {
        "pm-orchestrator": "pm",
        "pm-research-analyst": "researcher",
        "pm-engineer": "engineer",
        "pm-coder": "coder",
        "pm-tester": "tester",
        "pm-security-reviewer": "reviewer",
        "pm-database-engineer": "dba",
        "pm-ui-tester": "ui",
        "pm-final-qc": "final-qc",
    }
    return mapping.get(normalized, "")


def _trigger_target_step_kind(bot_id: str) -> str:
    normalized = str(bot_id or "").strip().lower()
    mapping = {
        "pm-orchestrator": "planning",
        "pm-research-analyst": "specification",
        "pm-engineer": "planning",
        "pm-coder": "repo_change",
        "pm-tester": "test_execution",
        "pm-security-reviewer": "review",
        "pm-database-engineer": "review",
        "pm-ui-tester": "review",
        "pm-final-qc": "review",
    }
    return mapping.get(normalized, "")


def _is_probable_test_file(value: str) -> bool:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return False
    lowered = normalized.lower()
    path_markers = (
        "/test" in lowered
        or lowered.startswith("test")
        or lowered.startswith("tests/")
        or ".tests/" in lowered
        or "/tests." in lowered
    )
    return any(
        lowered.endswith(ext)
        for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".rs", ".cpp", ".cc", ".cxx")
    ) and (
        path_markers
        or lowered.endswith("_test.py")
        or lowered.endswith("_test.go")
        or lowered.endswith(".test.ts")
        or lowered.endswith(".test.tsx")
        or lowered.endswith(".spec.ts")
        or lowered.endswith(".spec.tsx")
        or lowered.endswith(".test.js")
        or lowered.endswith(".spec.js")
        or lowered.endswith("tests.cs")
        or lowered.endswith("tests.rs")
        or lowered.endswith("test.cpp")
        or lowered.endswith("test.cc")
        or lowered.endswith("test.cxx")
    )


def _is_documentation_like_repo_file(value: str) -> bool:
    normalized = str(value or "").strip().replace("\\", "/").strip("`")
    if not _looks_like_repo_file(normalized):
        return False
    lowered = normalized.lower()
    leaf = lowered.rsplit("/", 1)[-1]
    if lowered.startswith("docs/"):
        return True
    if leaf in {
        "readme.md",
        "changelog.md",
        "release_notes.md",
        "release-notes.md",
        "qa_checklist.md",
        "implementation_plan.md",
        "risk_log.md",
        "research_handoff.md",
        "ui_changes.md",
        "db_schema_changes.md",
    }:
        return True
    return lowered.endswith((".md", ".mdx", ".rst", ".adoc", ".txt"))


def _is_assignment_execution_artifact_file(value: str) -> bool:
    normalized = str(value or "").strip().replace("\\", "/").lower()
    if not _looks_like_repo_file(normalized):
        return False
    return (
        normalized.startswith("reports/")
        or normalized.startswith("coverage/")
        or normalized.startswith("test_logs/")
        or normalized.endswith((".xml", ".html", ".txt", ".json", ".log"))
    )


def _workstream_deliverables(payload: Dict[str, Any]) -> List[str]:
    deliverables: List[str] = []
    candidates: List[Any] = [payload.get("workstream"), payload.get("source_payload")]
    source_payload = payload.get("source_payload")
    if isinstance(source_payload, dict):
        candidates.append(source_payload.get("workstream"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        deliverables.extend(_normalize_string_list(candidate.get("deliverables")))
    seen: Dict[str, None] = {}
    normalized_items: List[str] = []
    for item in deliverables:
        normalized = str(item or "").strip().replace("\\", "/").strip("`")
        if not normalized or normalized in seen:
            continue
        seen[normalized] = None
        normalized_items.append(normalized)
    return normalized_items


def _is_docs_only_workstream_validation(payload: Dict[str, Any]) -> bool:
    workstream_items = _workstream_deliverables(payload)
    if not workstream_items:
        return False
    repo_files = [item for item in workstream_items if _looks_like_repo_file(item)]
    if not repo_files:
        return False
    if any(_is_probable_test_file(item) for item in repo_files):
        return False
    return all(_is_documentation_like_repo_file(item) for item in repo_files)


def _iter_payload_chain(payload: Dict[str, Any], *, max_depth: int = 8):
    current: Any = payload
    seen: Set[int] = set()
    for _ in range(max_depth):
        if not isinstance(current, dict):
            return
        current_id = id(current)
        if current_id in seen:
            return
        seen.add(current_id)
        yield current
        current = current.get("source_payload")


def _payload_requests_docs_only_outputs(payload: Dict[str, Any]) -> bool:
    texts: List[str] = []
    for current in _iter_payload_chain(payload):
        for field in ("title", "instruction"):
            value = str(current.get(field) or "").strip().lower()
            if value:
                texts.append(value)
        workstream = current.get("workstream")
        if isinstance(workstream, dict):
            for field in ("title", "instruction"):
                value = str(workstream.get(field) or "").strip().lower()
                if value:
                    texts.append(value)
            deliverables = " ".join(_normalize_string_list(workstream.get("deliverables"))).lower()
            if deliverables:
                texts.append(deliverables)
    combined = " ".join(texts)
    if not combined:
        return False
    has_docs_signal = any(
        marker in combined
        for marker in (
            "documentation",
            "markdown",
            ".md",
            "docs/",
            "docs\\",
        )
    )
    has_docs_only_signal = any(
        marker in combined
        for marker in (
            "only .md",
            "only md",
            "only markdown",
            "markdown only",
            "docs only",
            "documentation only",
            "only .md documents",
            "only markdown documents",
            "no code edited",
            "no other code edited",
            "shouldn't see any other code edited",
            "shouldnt see any other code edited",
        )
    )
    return has_docs_signal and has_docs_only_signal


def _result_non_document_repo_paths(result: Any) -> List[str]:
    paths: List[str] = []
    seen: Set[str] = set()
    for candidate in extract_file_candidates(result):
        path = str(candidate.get("path") or "").strip().replace("\\", "/").strip("`")
        if not path or path in seen:
            continue
        seen.add(path)
        if not _looks_like_repo_file(path):
            continue
        if _is_assignment_execution_artifact_file(path) or _is_probable_test_file(path):
            continue
        if _is_documentation_like_repo_file(path):
            continue
        paths.append(path)
    return paths


def _looks_like_assignment_test_execution_payload(payload: Dict[str, Any]) -> bool:
    if _is_docs_only_workstream_validation(payload):
        return False
    # Role hint takes precedence - if explicitly tester/qa, treat as test execution
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    if role_hint in {"tester", "qa"}:
        return True
    if role_hint in {"researcher", "analyst"}:
        return False
    
    # Then check explicit step_kind
    explicit_step_kind = _normalize_assignment_step_kind(payload.get("step_kind"))
    if explicit_step_kind == "test_execution":
        return True
    if explicit_step_kind in {"specification", "planning", "repo_change", "review", "release"}:
        return False
    
    # Finally, infer from payload content
    step_kind = _assignment_step_kind(payload)
    if step_kind == "repo_change":
        return False
    if step_kind == "test_execution":
        return True
    
    deliverables = _normalize_string_list(payload.get("deliverables"))
    evidence = _normalize_string_list(payload.get("evidence_requirements"))
    combined = " ".join(
        [
            str(payload.get("title") or ""),
            str(payload.get("instruction") or ""),
            " ".join(deliverables),
            " ".join(evidence),
        ]
    ).lower()
    if any(_is_probable_test_file(item) for item in deliverables):
        return True
    if any(item.lower().endswith((".xml", ".json", ".txt", ".html", ".log", ".out", ".cov")) for item in deliverables):
        if any(token in combined for token in ("test", "coverage", "qa", "verification")):
            return True
    return any(token in combined for token in ("executed test command output", "coverage report", "pass/fail"))


def _assignment_expected_repo_files(payload: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for item in _normalize_string_list(payload.get("deliverables")):
        normalized = str(item).strip().replace("\\", "/").strip("`")
        if _looks_like_repo_file(normalized):
            files.append(normalized)
    return files


def _result_explicit_artifacts(result: Any) -> List[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict)]


def _has_repo_change_evidence(_payload: Dict[str, Any], result: Any) -> bool:
    if extract_file_candidates(result):
        return True
    explicit_artifacts = _result_explicit_artifacts(result)
    if any(str(item.get("path") or "").strip() for item in explicit_artifacts):
        return True
    return False


def _has_test_execution_evidence(result: Any, text: str) -> bool:
    if isinstance(result, dict):
        for key in ("executed_commands", "command_results", "coverage", "coverage_report", "test_results", "exit_code"):
            if result.get(key) not in (None, "", [], {}):
                return True
    explicit_artifacts = _result_explicit_artifacts(result)
    if any(
        str(item.get("label") or "").strip().lower().find("coverage") >= 0
        or str(item.get("path") or "").strip().lower().endswith((".xml", ".json", ".txt", ".html"))
        for item in explicit_artifacts
    ):
        return True
    patterns = (
        r"\b\d+\s+passed\b",
        r"\b\d+\s+failed\b",
        r"\bcollected\s+\d+\s+items\b",
        r"\btotal\b[^\n]{0,40}\b\d{1,3}%",
        r"\bcoverage\b[^\n]{0,40}\b\d{1,3}%",
        r"\bexit code\b\s*[:=]?\s*\d+",
        r"\bran\s+\d+\s+tests?\b",
        r"===+\s*test session starts",
        r"===+\s*failures",
        r"\bpassed in\s+\d",
    )
    lowered = text.lower()
    return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _assignment_test_report_paths(payload: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for item in _assignment_expected_repo_files(payload):
        normalized = str(item or "").strip().replace("\\", "/")
        if normalized.lower().endswith((".xml", ".json", ".txt", ".html", ".log", ".out", ".cov")):
            paths.append(normalized)
    return paths


def _assignment_test_source_files(paths: List[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for raw in paths:
        normalized = str(raw or "").strip().replace("\\", "/")
        if not normalized:
            continue
        if _is_probable_test_file(normalized):
            seen.setdefault(normalized, None)
    return list(seen.keys())


def _assignment_python_coverage_target(paths: List[str]) -> Optional[str]:
    candidates: List[str] = []
    for raw in paths:
        normalized = str(raw or "").strip().replace("\\", "/")
        lowered = normalized.lower()
        if not lowered.endswith(".py"):
            continue
        if lowered.startswith("tests/") or "/tests/" in lowered:
            continue
        if lowered.startswith("docs/") or lowered.startswith("issues/") or lowered.startswith("specification/"):
            continue
        parts = [part for part in normalized.split("/") if part]
        if not parts:
            continue
        if parts[0] == "src" and len(parts) >= 2:
            candidates.append("/".join(parts[:2]))
        else:
            candidates.append(parts[0])
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.count("/"), len(item)))
    return candidates[0]


def _assignment_repo_runtime_languages(root: Path, *, allowed_extra: List[str] | None = None) -> List[str]:
    detected: List[str] = []
    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        detected.append("python")
    if (root / "package.json").exists():
        detected.append("node")
    try:
        if any(root.glob("*.sln")) or any(root.rglob("*.csproj")) or any(root.rglob("*.fsproj")) or any(root.rglob("*.vbproj")):
            detected.append("dotnet")
    except Exception:
        pass
    if (root / "go.mod").exists():
        detected.append("go")
    if (root / "Cargo.toml").exists():
        detected.append("rust")
    try:
        if (root / "CMakeLists.txt").exists() or (root / "Makefile").exists() or any(root.rglob("*.vcxproj")):
            detected.append("cpp")
    except Exception:
        if (root / "CMakeLists.txt").exists() or (root / "Makefile").exists():
            detected.append("cpp")
    # Allow additional runtimes from context (e.g., TypeScript for multi-language lesson blocks)
    if allowed_extra:
        for lang in allowed_extra:
            normalized = str(lang).strip().lower()
            if normalized and normalized not in detected:
                detected.append(normalized)
    return detected


def _generated_assignment_languages(paths: List[str]) -> List[str]:
    languages: List[str] = []
    prioritized = [str(path or "").strip().lower() for path in paths if str(path or "").strip()]
    if any(path.endswith(".py") for path in prioritized):
        languages.append("python")
    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in prioritized):
        languages.append("node")
    if any(path.endswith(".cs") for path in prioritized):
        languages.append("dotnet")
    if any(path.endswith(".go") for path in prioritized):
        languages.append("go")
    if any(path.endswith(".rs") for path in prioritized):
        languages.append("rust")
    if any(path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".h")) for path in prioritized):
        languages.append("cpp")
    return languages


def _filter_assignment_languages_to_repo_runtime(languages: List[str], root: Path) -> List[str]:
    repo_languages = _assignment_repo_runtime_languages(root)
    if not repo_languages:
        return languages
    return [language for language in languages if language in repo_languages]


def _assignment_execution_language(*, applied_paths: List[str], test_files: List[str], root: Path) -> str:
    languages = _assignment_execution_languages(applied_paths=applied_paths, test_files=test_files, root=root)
    if languages:
        return languages[0]
    repo_languages = _assignment_repo_runtime_languages(root)
    if repo_languages:
        return repo_languages[0]
    return "python"


def _assignment_execution_languages(*, applied_paths: List[str], test_files: List[str], root: Path) -> List[str]:
    prioritized = [str(path or "").strip().lower() for path in (test_files + applied_paths) if str(path or "").strip()]
    languages: List[str] = []
    if any(path.endswith(".py") for path in prioritized):
        languages.append("python")
    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in prioritized):
        languages.append("node")
    if any(path.endswith(".cs") for path in prioritized):
        languages.append("dotnet")
    if any(path.endswith(".go") for path in prioritized):
        languages.append("go")
    if any(path.endswith(".rs") for path in prioritized):
        languages.append("rust")
    if any(path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".h")) for path in prioritized):
        languages.append("cpp")
    if languages:
        return _filter_assignment_languages_to_repo_runtime(languages, root=root)
    repo_languages = _assignment_repo_runtime_languages(root)
    if repo_languages:
        return repo_languages
    return ["python"]


def _assignment_node_test_command(root: Path) -> List[str]:
    if (root / "pnpm-lock.yaml").exists():
        return ["pnpm", "test", "--", "--runInBand", "--coverage"]
    if (root / "yarn.lock").exists():
        return ["yarn", "test", "--runInBand", "--coverage"]
    return ["npm", "test", "--", "--runInBand", "--coverage"]


def _assignment_cpp_test_command(root: Path) -> List[str]:
    if (root / "CMakeLists.txt").exists():
        return ["ctest", "--test-dir", "build", "--output-on-failure"]
    if (root / "Makefile").exists():
        return ["make", "test"]
    return ["ctest", "--output-on-failure"]


def _missing_assignment_runtime_tools(command_results: List[Dict[str, Any]]) -> List[str]:
    tools: List[str] = []
    for item in command_results:
        error = str(item.get("error") or "").strip().lower()
        if not error.startswith("command not found"):
            continue
        command = [str(part) for part in (item.get("command") or []) if str(part).strip()]
        tool = ""
        if command:
            tool = Path(command[0]).stem.strip().lower()
        if not tool and ":" in error:
            tool = error.split(":", 1)[1].strip().lower()
        if tool and tool not in tools:
            tools.append(tool)
    return tools


def _generated_repo_runtime_mismatch_message(*, root: Path, result: Any, allowed_extra: List[str] | None = None) -> str:
    candidates = extract_file_candidates(result)
    paths = [
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in candidates
        if str(item.get("path") or "").strip()
    ]
    relevant_paths = [
        path for path in paths
        if not path.lower().startswith(("docs/", "issues/", "specification/"))
    ]
    generated_languages = _generated_assignment_languages(relevant_paths)
    if not generated_languages:
        return ""
    repo_languages = _assignment_repo_runtime_languages(root, allowed_extra=allowed_extra)
    if not repo_languages:
        return ""
    unexpected = [language for language in generated_languages if language not in repo_languages]
    if not unexpected:
        return ""
    return (
        "generated repo files introduce unsupported runtime(s) for this repo: "
        + ", ".join(unexpected)
        + ". repo runtime profile declares: "
        + ", ".join(repo_languages)
    )


def _find_repo_paths_in_text(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+", text)


def _has_review_evidence(result: Any, text: str) -> bool:
    if isinstance(result, dict):
        for key in ("findings", "review_findings", "files_reviewed", "diff_summary", "review_summary"):
            if result.get(key) not in (None, "", [], {}):
                return True
    file_paths = _find_repo_paths_in_text(text)
    lowered = text.lower()
    finding_markers = (
        "ordered by severity",
        "finding",
        "findings",
        "severity",
        "no findings",
        "merge ready",
        "files reviewed",
        "diff",
    )
    return bool(file_paths) and any(marker in lowered for marker in finding_markers)


def _has_release_evidence(result: Any, text: str) -> bool:
    if isinstance(result, dict):
        for key in ("pull_request_url", "merged_pull_request_url", "release_tag", "commit_sha", "merge_commit", "branch"):
            if result.get(key) not in (None, "", [], {}):
                return True
    lowered = text.lower()
    checks = 0
    if "pull request" in lowered or re.search(r"https?://[^\s]+/pull/\d+", text):
        checks += 1
    if re.search(r"\bv\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", text):
        checks += 1
    if re.search(r"\b[0-9a-f]{7,40}\b", lowered):
        checks += 1
    if "merged" in lowered or "released" in lowered or "tagged" in lowered:
        checks += 1
    return checks >= 2


def _extract_urls(text: str) -> List[str]:
    return re.findall(r"https?://[^\s)>\]`]+", str(text or ""))


def _has_non_placeholder_url_evidence(text: str) -> bool:
    urls = _extract_urls(text)
    if not urls:
        return False
    for url in urls:
        lowered = url.lower()
        if any(marker in lowered for marker in ("[org]", "[repo]", "placeholder", "example.com")):
            continue
        return True
    return False


def _extract_commit_shas(text: str) -> List[str]:
    return re.findall(r"\b[0-9a-f]{7,40}\b", str(text or "").lower())


def _has_non_placeholder_commit_sha(text: str) -> bool:
    for sha in _extract_commit_shas(text):
        if sha in {"a1b2c3d4", "deadbeef", "abcdef12", "1234567"}:
            continue
        return True
    return False


def _has_non_placeholder_pull_request_url(text: str) -> bool:
    urls = _extract_urls(text)
    for url in urls:
        lowered = url.lower()
        if any(marker in lowered for marker in ("[org]", "[repo]", "<number>", "placeholder")):
            continue
        if re.search(r"/pulls?/\d+\b", lowered):
            return True
    return False


def _requires_repo_artifact_evidence(payload: Dict[str, Any]) -> bool:
    deliverables = _assignment_expected_repo_files(payload)
    evidence = " ".join(_normalize_string_list(payload.get("evidence_requirements"))).lower()
    quality = " ".join(_normalize_string_list(payload.get("quality_gates"))).lower()
    combined = f"{evidence} {quality}"
    if deliverables:
        return True
    repo_markers = (
        "file committed",
        "committed to the repo",
        "diff",
        "commit sha",
        "pull request",
        "pr ",
        "review comments",
        "feature branch",
    )
    return any(marker in combined for marker in repo_markers)


def _requires_link_evidence(payload: Dict[str, Any]) -> bool:
    step_kind = _assignment_step_kind(payload)
    evidence = " ".join(_normalize_string_list(payload.get("evidence_requirements"))).lower()
    deliverables = " ".join(_normalize_string_list(payload.get("deliverables"))).lower()
    if "only include live non-placeholder links if they actually exist" in evidence:
        return False
    if "only include non-placeholder commit or pull request evidence if it actually exists" in evidence:
        return False
    if step_kind in {"specification", "planning"} and any(
        token in deliverables for token in ("issue definitions", "milestone definition", "project board proposal")
    ):
        return False
    combined = f"{evidence} {deliverables}"
    link_markers = (
        "url",
        "urls",
        "github issue",
        "milestone",
        "project board",
        "github actions",
        "workflow run",
        "ci run",
        "pull request url",
        "pr url",
        "tag url",
    )
    return any(marker in combined for marker in link_markers)


def _requires_commit_sha_evidence(payload: Dict[str, Any]) -> bool:
    combined = " ".join(
        _normalize_string_list(payload.get("evidence_requirements"))
        + _normalize_string_list(payload.get("deliverables"))
    ).lower()
    if "only include non-placeholder commit or pull request evidence if it actually exists" in combined:
        return False
    return "commit sha" in combined or "commit hash" in combined or "merge commit sha" in combined


def _requires_pull_request_evidence(payload: Dict[str, Any]) -> bool:
    combined = " ".join(
        _normalize_string_list(payload.get("evidence_requirements"))
        + _normalize_string_list(payload.get("deliverables"))
    ).lower()
    if "only include non-placeholder commit or pull request evidence if it actually exists" in combined:
        return False
    return "pull request" in combined or "pr url" in combined or "merged pr" in combined or "merged pull request" in combined


def _requires_release_tag_evidence(payload: Dict[str, Any]) -> bool:
    combined = " ".join(
        _normalize_string_list(payload.get("evidence_requirements"))
        + _normalize_string_list(payload.get("deliverables"))
    ).lower()
    return "git tag" in combined or "release tag" in combined or "tag url" in combined


def _assignment_validation_error(task: Task, result: Any) -> str:
    metadata = task.metadata
    if metadata is None:
        return ""
    source = str(metadata.source or "").strip().lower()
    if source not in {"chat_assign", "auto_retry"} and not metadata.orchestration_id:
        return ""
    payload = task.payload if isinstance(task.payload, dict) else {}
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    step_kind = _assignment_step_kind(payload)
    evidence_requirements = (
        _normalize_string_list(payload.get("evidence_requirements"))
        or _normalize_string_list(payload.get("quality_gates"))
    )
    text = _extract_result_output_text(result).strip()
    lowered = text.lower()

    if _payload_requests_docs_only_outputs(payload):
        unexpected_paths = _result_non_document_repo_paths(result)
        if unexpected_paths:
            preview = ", ".join(unexpected_paths[:5])
            return (
                "Assignment explicitly requested documentation-only markdown outputs, "
                f"but generated non-document repo files: {preview}."
            )

    if not text:
        return ""

    invalid_markers = [
        "pending - not executed",
        "pending – not executed",
        "not executed",
        "simulated issue urls",
        "pending creation",
        "dry-run checklist",
        "projected compliance",
        "actual repository is not available",
        "repository is not available",
        "actual source files were not supplied",
        "source files were not supplied",
        "validation environment does not have the actual source code",
        "because the actual repository is not available",
        "adapt the items to the concrete code",
        "simulated issue url",
        "simulated pull request",
        "example pull request",
        "placeholder pull request",
        "cannot directly authenticate",
        "copy-paste ready",
        "placeholder)",
        "(placeholder",
        "simulated ids",
        "simulated build log",
        "mocked but representative",
        "mocked but realistic",
        "evidence placeholders",
        "user to fill",
    ]
    for marker in invalid_markers:
        if marker in lowered:
            return (
                "Assignment task output is unverified and cannot be marked completed: "
                f"detected '{marker}'."
            )

    if step_kind == "repo_change" and not _has_repo_change_evidence(payload, result):
        required = ", ".join(evidence_requirements[:2]) or "repo file artifacts"
        return (
            "Assignment repo-change step is missing concrete changed-file evidence; "
            f"required evidence: {required}."
        )

    if step_kind in {"specification", "planning"} and _requires_repo_artifact_evidence(payload):
        if not _has_repo_change_evidence(payload, result):
            required = ", ".join(evidence_requirements[:2]) or "committed file or diff evidence"
            return (
                "Assignment planning/specification step is missing repo-backed evidence; "
                f"required evidence: {required}."
            )

    if _requires_link_evidence(payload) and not _has_non_placeholder_url_evidence(text):
        required = ", ".join(evidence_requirements[:2]) or "link-backed evidence"
        return (
            "Assignment step is missing non-placeholder link evidence; "
            f"required evidence: {required}."
        )

    if step_kind == "test_execution" and _requires_repo_artifact_evidence(payload):
        if not _has_repo_change_evidence(payload, result):
            required = ", ".join(evidence_requirements[:2]) or "test artifacts"
            return (
                "Assignment test step is missing concrete test artifact evidence; "
                f"required evidence: {required}."
            )

    if step_kind == "test_execution" and not _has_test_execution_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "executed test evidence"
        return (
            "Assignment test step is missing execution-backed evidence; "
            f"required evidence: {required}."
        )

    if step_kind == "review" and not _has_review_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "review findings tied to changed files"
        return (
            "Assignment review step is missing concrete review evidence; "
            f"required evidence: {required}."
        )

    if step_kind == "release" and not _has_release_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "release artifacts"
        return (
            "Assignment release step is missing release-backed evidence; "
            f"required evidence: {required}."
        )

    if step_kind in {"repo_change", "release"} and _requires_commit_sha_evidence(payload) and not _has_non_placeholder_commit_sha(text):
        required = ", ".join(evidence_requirements[:2]) or "commit SHA evidence"
        return (
            "Assignment step is missing non-placeholder commit SHA evidence; "
            f"required evidence: {required}."
        )

    if step_kind in {"repo_change", "release"} and _requires_pull_request_evidence(payload) and not _has_non_placeholder_pull_request_url(text):
        required = ", ".join(evidence_requirements[:2]) or "pull request evidence"
        return (
            "Assignment step is missing non-placeholder pull request evidence; "
            f"required evidence: {required}."
        )

    if step_kind == "release" and _requires_release_tag_evidence(payload):
        if not re.search(r"\bv\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", text):
            required = ", ".join(evidence_requirements[:2]) or "release tag evidence"
            return (
                "Assignment release step is missing release-tag evidence; "
                f"required evidence: {required}."
            )

    if role_hint in {"tester", "qa", "reviewer", "security", "security-reviewer"}:
        soft_markers = [
            "execute the following",
            "verification method",
            "where to inspect",
            "suggested review commands",
            "please proceed with the execution steps",
            "feel free to reach out",
            "good luck with the final merge",
            "should be verified before",
            "use as a checklist",
            "evidence to check",
            "proceed with the merge only after",
        ]
        matched = [marker for marker in soft_markers if marker in lowered]
        if matched:
            return (
                "Assignment task output reads like guidance or a checklist rather than executed review/test evidence: "
                + ", ".join(matched[:3])
                + "."
            )
    return ""


def _task_report_markdown(task: Task) -> str:
    execution = _execution_summary(task)
    usage = execution.get("usage") or {}
    lines = [
        f"# Run Report: {task.bot_id}",
        "",
        f"- Status: {task.status}",
        f"- Task ID: {task.id}",
        f"- Created: {task.created_at}",
        f"- Updated: {task.updated_at}",
        f"- Duration (ms): {execution.get('duration_ms') if execution.get('duration_ms') is not None else '—'}",
    ]
    if task.metadata:
        if task.metadata.project_id:
            lines.append(f"- Project: {task.metadata.project_id}")
        if task.metadata.source:
            lines.append(f"- Source: {task.metadata.source}")
        if task.metadata.parent_task_id:
            lines.append(f"- Triggered By Task: {task.metadata.parent_task_id}")
        if task.metadata.trigger_rule_id:
            lines.append(f"- Trigger Rule: {task.metadata.trigger_rule_id}")
        if task.metadata.trigger_depth is not None:
            lines.append(f"- Trigger Depth: {task.metadata.trigger_depth}")
        if task.metadata.orchestration_id:
            lines.append(f"- Orchestration ID: {task.metadata.orchestration_id}")

    lines.extend(["", "## Payload", _summarize_payload(task.payload)])
    if usage:
        lines.extend(
            [
                "",
                "## Usage",
                f"- Prompt Tokens: {usage.get('prompt_tokens', '—')}",
                f"- Completion Tokens: {usage.get('completion_tokens', '—')}",
                f"- Total Tokens: {usage.get('total_tokens', '—')}",
            ]
        )

    if task.error is not None:
        lines.extend(
            [
                "",
                "## Error",
                f"- Message: {task.error.message}",
                f"- Code: {task.error.code or '—'}",
            ]
        )
    elif task.result is not None:
        lines.append("")
        lines.append("## Result")
        if isinstance(task.result, dict):
            report = task.result.get("report")
            if isinstance(report, str) and report.strip():
                lines.append(report.strip())
            else:
                result_keys = sorted(str(key) for key in task.result.keys())
                lines.append(f"Result keys: {', '.join(result_keys[:20])}" if result_keys else "Empty result object")
            explicit_artifacts = task.result.get("artifacts")
            if isinstance(explicit_artifacts, list):
                lines.append("")
                lines.append(f"Artifacts reported by bot: {len(explicit_artifacts)}")
        else:
            lines.append(str(task.result))
    return "\n".join(lines).strip() + "\n"


class TaskManager:
    def __init__(self, scheduler: Any, db_path: Optional[str] = None, bot_registry: Optional[Any] = None) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._scheduler = scheduler
        self._bot_registry = bot_registry
        self._project_registry = getattr(scheduler, "project_registry", None)
        self._db_ready = False
        self._running_task_ids: set[str] = set()
        self._runner_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._retry_tasks: Set[asyncio.Task[Any]] = set()
        self._is_closing = False
        self._max_concurrency = max(1, int(os.environ.get("NEXUSAI_TASK_MAX_CONCURRENCY", "4")))
        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            if db_url.startswith("sqlite:///"):
                self._db_path = db_url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

    async def close(self) -> None:
        self._is_closing = True
        async with self._lock:
            runner_tasks = [task for task in self._runner_tasks.values() if not task.done()]
            retry_tasks = [task for task in self._retry_tasks if not task.done()]
        pending = runner_tasks + retry_tasks
        if pending:
            # Give active tasks a short grace period to finish DB writes before
            # cancellation so aiosqlite worker threads do not outlive the loop.
            done, still_pending = await asyncio.wait(pending, timeout=2.0)
            if still_pending:
                for pending_task in still_pending:
                    pending_task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
            elif done:
                await asyncio.gather(*done, return_exceptions=True)
        async with self._lock:
            self._runner_tasks.clear()
            self._retry_tasks.clear()
            self._running_task_ids.clear()

    def __del__(self) -> None:
        # Best-effort cancellation for unmanaged instances (e.g. tests without explicit teardown).
        try:
            for task in list(self._runner_tasks.values()):
                if not task.done():
                    task.cancel()
            for task in list(self._retry_tasks):
                if not task.done():
                    task.cancel()
        except Exception:
            pass

    async def _ensure_db(self) -> None:
        """Lazily initialise the SQLite tasks table and load existing rows."""
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_TASKS)
                await db.execute(_CREATE_TASK_DEPENDENCIES)
                await db.execute(_CREATE_BOT_RUNS)
                await db.execute(_CREATE_BOT_RUN_ARTIFACTS)
                await self._migrate_tasks_table(db)
                await db.commit()
                dep_map: Dict[str, List[str]] = {}
                async with db.execute(
                    f"SELECT task_id, depends_on_task_id FROM {_TASK_DEPENDENCIES_TABLE}"
                ) as dep_cursor:
                    dep_rows = await dep_cursor.fetchall()
                    for dep_row in dep_rows:
                        dep_map.setdefault(dep_row["task_id"], []).append(dep_row["depends_on_task_id"])

                async with db.execute(f"SELECT * FROM {_TASKS_TABLE}") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        depends_on = []
                        if row["depends_on"]:
                            depends_on = json.loads(row["depends_on"])
                        elif row["id"] in dep_map:
                            depends_on = dep_map[row["id"]]
                        task = Task(
                            id=row["id"],
                            bot_id=row["bot_id"],
                            payload=json.loads(row["payload"]) if row["payload"] else {},
                            metadata=(
                                TaskMetadata(**json.loads(row["metadata"]))
                                if row["metadata"]
                                else None
                            ),
                            depends_on=depends_on,
                            status=row["status"],
                            result=json.loads(row["result"]) if row["result"] else None,
                            error=(
                                TaskError(**json.loads(row["error"]))
                                if row["error"]
                                else None
                            ),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                        )
                        self._tasks[task.id] = task
            self._db_ready = True

    async def _migrate_tasks_table(self, db: aiosqlite.Connection) -> None:
        """Ensure new table columns exist for upgraded installations."""
        table_columns = {
            _TASKS_TABLE: {
                "bot_id": "TEXT",
                "payload": "TEXT",
                "metadata": "TEXT",
                "depends_on": "TEXT",
                "status": "TEXT",
                "result": "TEXT",
                "error": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
            _BOT_RUNS_TABLE: {
                "payload": "TEXT",
                "metadata": "TEXT",
                "result": "TEXT",
                "error": "TEXT",
                "triggered_by_task_id": "TEXT",
                "trigger_rule_id": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
            },
            _BOT_RUN_ARTIFACTS_TABLE: {
                "content": "TEXT",
                "path": "TEXT",
                "metadata": "TEXT",
            },
        }
        for table_name, columns in table_columns.items():
            async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                existing = await cursor.fetchall()
            existing_names = {row[1] for row in existing}
            for column_name, column_type in columns.items():
                if column_name in existing_names:
                    continue
                await db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )

    async def _persist_task(self, task: Task) -> None:
        """Upsert *task* into the SQLite tasks table."""
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_tasks
                    (id, bot_id, payload, metadata, depends_on, status, result, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    depends_on = excluded.depends_on,
                    status     = excluded.status,
                    result     = excluded.result,
                    error      = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.bot_id,
                    json.dumps(task.payload),
                    json.dumps(task.metadata.model_dump()) if task.metadata else None,
                    json.dumps(task.depends_on),
                    task.status,
                    json.dumps(task.result) if task.result is not None else None,
                    json.dumps(task.error.model_dump()) if task.error else None,
                    task.created_at,
                    task.updated_at,
                ),
            )
            await db.commit()

    async def _upsert_bot_run(self, task: Task) -> None:
        metadata = task.metadata.model_dump() if task.metadata else None
        started_at = task.updated_at if task.status == "running" else None
        completed_at = task.updated_at if task.status in {"completed", "failed", "cancelled", "retried"} else None
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_bot_runs
                    (id, task_id, bot_id, status, payload, metadata, result, error,
                     triggered_by_task_id, trigger_rule_id, created_at, updated_at,
                     started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    metadata = excluded.metadata,
                    result = excluded.result,
                    error = excluded.error,
                    triggered_by_task_id = excluded.triggered_by_task_id,
                    trigger_rule_id = excluded.trigger_rule_id,
                    updated_at = excluded.updated_at,
                    started_at = COALESCE(cp_bot_runs.started_at, excluded.started_at),
                    completed_at = excluded.completed_at
                """,
                (
                    task.id,
                    task.id,
                    task.bot_id,
                    task.status,
                    json.dumps(task.payload),
                    json.dumps(metadata) if metadata is not None else None,
                    json.dumps(task.result) if task.result is not None else None,
                    json.dumps(task.error.model_dump()) if task.error else None,
                    getattr(task.metadata, "parent_task_id", None),
                    getattr(task.metadata, "trigger_rule_id", None),
                    task.created_at,
                    task.updated_at,
                    started_at,
                    completed_at,
                ),
            )
            await db.commit()

    async def _upsert_artifact(self, artifact: BotRunArtifact) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_bot_run_artifacts
                    (id, run_id, task_id, bot_id, kind, label, content, path, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = excluded.label,
                    content = excluded.content,
                    path = excluded.path,
                    metadata = excluded.metadata,
                    created_at = excluded.created_at
                """,
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.task_id,
                    artifact.bot_id,
                    artifact.kind,
                    artifact.label,
                    artifact.content,
                    artifact.path,
                    json.dumps(artifact.metadata),
                    artifact.created_at,
                ),
            )
            await db.commit()

    async def _record_artifacts_for_task(self, task: Task) -> None:
        now = datetime.now(timezone.utc).isoformat()
        artifacts: List[BotRunArtifact] = [
            BotRunArtifact(
                id=f"{task.id}:payload",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="payload",
                label="Task Payload",
                content=json.dumps(task.payload, indent=2, sort_keys=True),
                metadata={},
                created_at=now,
            ),
            BotRunArtifact(
                id=f"{task.id}:report",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="note",
                label="Run Report",
                content=_task_report_markdown(task),
                metadata={
                    "status": task.status,
                    "project_id": getattr(task.metadata, "project_id", None),
                    "source": getattr(task.metadata, "source", None),
                },
                created_at=now,
            ),
        ]
        if task.result is not None:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:result",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="result",
                    label="Task Result",
                    content=json.dumps(task.result, indent=2, sort_keys=True),
                    metadata={},
                    created_at=now,
                )
            )
        execution_summary = _execution_summary(task)
        artifacts.append(
            BotRunArtifact(
                id=f"{task.id}:execution-report",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="note",
                label="Execution Report",
                content=_execution_report_markdown(task),
                metadata=execution_summary,
                created_at=now,
            )
        )
        usage = _usage_summary(_result_usage(task))
        if usage:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:usage",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="note",
                    label="Usage Report",
                    content=json.dumps(usage, indent=2, sort_keys=True),
                    metadata=usage,
                    created_at=now,
                )
            )
        if task.error is not None:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:error",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="error",
                    label="Task Error",
                    content=json.dumps(task.error.model_dump(), indent=2, sort_keys=True),
                    metadata={},
                    created_at=now,
                )
            )

        explicit_artifacts = task.result.get("artifacts") if isinstance(task.result, dict) else None
        if isinstance(explicit_artifacts, list):
            for idx, item in enumerate(explicit_artifacts):
                if not isinstance(item, dict):
                    continue
                artifacts.append(
                    BotRunArtifact(
                        id=f"{task.id}:artifact:{idx}",
                        run_id=task.id,
                        task_id=task.id,
                        bot_id=task.bot_id,
                        kind="file" if item.get("path") else "note",
                        label=str(item.get("label") or item.get("name") or f"Artifact {idx + 1}"),
                        content=(
                            item.get("content")
                            if isinstance(item.get("content"), str)
                            else json.dumps(item.get("content"), indent=2, sort_keys=True)
                            if item.get("content") is not None
                            else None
                        ),
                        path=item.get("path"),
                        metadata={k: v for k, v in item.items() if k not in {"label", "name", "content", "path"}},
                        created_at=now,
                    )
                )

        explicit_paths = {
            str(getattr(artifact, "path", "") or "").strip()
            for artifact in artifacts
            if getattr(artifact, "kind", "") == "file" and getattr(artifact, "path", None)
        }
        extracted_candidates = extract_file_candidates(task.result)
        extracted_idx = 0
        for candidate in extracted_candidates:
            path = str(candidate.get("path") or "").strip()
            if not path or path in explicit_paths:
                continue
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:extracted-file:{extracted_idx}",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="file",
                    label=str(candidate.get("label") or path),
                    content=str(candidate.get("content") or ""),
                    path=path,
                    metadata={
                        "source": str(candidate.get("source") or "extracted_markdown"),
                        "language": candidate.get("language"),
                    },
                    created_at=now,
                )
            )
            extracted_idx += 1

        for artifact in artifacts:
            await self._upsert_artifact(artifact)

    async def _persist_dependencies(self, task: Task) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(f"DELETE FROM {_TASK_DEPENDENCIES_TABLE} WHERE task_id = ?", (task.id,))
            for dep_id in task.depends_on:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO cp_task_dependencies (task_id, depends_on_task_id)
                    VALUES (?, ?)
                    """,
                    (task.id, dep_id),
                )
            await db.commit()

    async def create_task(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
        depends_on: Optional[List[str]] = None,
    ) -> Task:
        await self._ensure_db()
        await self._validate_task_payload(bot_id, payload, metadata=metadata)
        dependencies = depends_on or []
        async with self._lock:
            for dep_id in dependencies:
                if dep_id not in self._tasks:
                    raise TaskNotFoundError(f"Dependency task not found: {dep_id}")
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        initial_status = "blocked" if dependencies else "queued"
        if metadata is not None and not metadata.workflow_root_task_id:
            metadata = metadata.model_copy(update={"workflow_root_task_id": task_id})
        elif metadata is None:
            metadata = TaskMetadata(workflow_root_task_id=task_id)
        task = Task(
            id=task_id,
            bot_id=bot_id,
            payload=payload,
            metadata=metadata,
            depends_on=dependencies,
            status=initial_status,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task_id] = task
        await self._persist_task(task)
        await self._persist_dependencies(task)
        await self._upsert_bot_run(task)
        if task.status == "queued":
            await self._schedule_ready_tasks()
        return task

    async def get_task(self, task_id: str) -> Task:
        await self._ensure_db()
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            return self._tasks[task_id]

    async def retry_task(self, task_id: str, payload_override: Any = None) -> Task:
        await self._ensure_db()
        original = await self.get_task(task_id)
        metadata = original.metadata or TaskMetadata()
        retry_attempt = int(metadata.retry_attempt or 0) + 1
        next_metadata = metadata.model_copy(
            update={
                "retry_attempt": retry_attempt,
                "original_task_id": metadata.original_task_id or original.id,
                "retry_of_task_id": original.id,
                "source": "manual_retry",
            }
        )
        retried_task = await self.create_task(
            bot_id=original.bot_id,
            payload=original.payload if payload_override is None else payload_override,
            metadata=next_metadata,
            depends_on=[],
        )
        if original.status != "failed":
            return retried_task

        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing is None:
                return retried_task
            existing_metadata = existing.metadata or TaskMetadata()
            updated_metadata = existing_metadata.model_copy(
                update={"retried_by_task_id": retried_task.id}
            )
            existing_error = existing.error or TaskError(
                message="Task retried by operator",
                code="retried",
                details={},
            )
            details = dict(existing_error.details) if isinstance(existing_error.details, dict) else {}
            details["retried_by_task_id"] = retried_task.id
            updated_error = existing_error.model_copy(update={"code": "retried", "details": details})
            self._tasks[task_id] = existing.model_copy(
                update={
                    "status": "retried",
                    "metadata": updated_metadata,
                    "error": updated_error,
                    "updated_at": now,
                }
            )
            updated_original = self._tasks[task_id]
        await self._persist_task(updated_original)
        await self._upsert_bot_run(updated_original)
        await self._record_artifacts_for_task(updated_original)
        return retried_task

    async def cancel_task(self, task_id: str) -> Task:
        await self._ensure_db()
        task = await self.get_task(task_id)
        if task.status in {"completed", "failed", "cancelled", "retried"}:
            return task

        runner: Optional[asyncio.Task[Any]] = None
        async with self._lock:
            runner = self._runner_tasks.get(task_id)

        if task.status in {"queued", "blocked"} or runner is None:
            cancelled_error = TaskError(message="Task cancelled by operator", code="cancelled")
            await self.update_status(task_id, "cancelled", error=cancelled_error)
            return await self.get_task(task_id)

        cancelled_error = TaskError(message="Task cancelled by operator", code="cancelled")
        await self.update_status(task_id, "cancelled", error=cancelled_error)
        runner.cancel()
        return await self.get_task(task_id)

    async def list_tasks(
        self,
        orchestration_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        bot_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        await self._ensure_db()
        async with self._lock:
            tasks = list(self._tasks.values())
        if orchestration_id:
            tasks = [
                t
                for t in tasks
                if t.metadata and t.metadata.orchestration_id == orchestration_id
            ]
        if statuses:
            wanted = {str(status).strip().lower() for status in statuses if str(status).strip()}
            tasks = [t for t in tasks if t.status.lower() in wanted]
        if bot_id:
            tasks = [t for t in tasks if str(t.bot_id) == str(bot_id)]
        safe_limit: Optional[int] = None
        if limit is not None:
            safe_limit = max(1, int(limit))
        if safe_limit is not None and len(tasks) > safe_limit:
            tasks = heapq.nlargest(
                safe_limit,
                tasks,
                key=lambda task: (task.updated_at or "", task.created_at or ""),
            )
        else:
            tasks.sort(key=lambda task: (task.updated_at or "", task.created_at or ""), reverse=True)
            if safe_limit is not None:
                tasks = tasks[:safe_limit]
        return tasks

    async def list_bot_runs(self, bot_id: str, limit: int = 50) -> List[BotRun]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM cp_bot_runs
                WHERE bot_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (bot_id, max(1, int(limit))),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            BotRun(
                id=row["id"],
                task_id=row["task_id"],
                bot_id=row["bot_id"],
                status=row["status"],
                payload=json.loads(row["payload"]) if row["payload"] else {},
                metadata=TaskMetadata(**json.loads(row["metadata"])) if row["metadata"] else None,
                result=json.loads(row["result"]) if row["result"] else None,
                error=TaskError(**json.loads(row["error"])) if row["error"] else None,
                triggered_by_task_id=row["triggered_by_task_id"],
                trigger_rule_id=row["trigger_rule_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]

    async def list_bot_run_artifacts(
        self,
        bot_id: str,
        limit: int = 100,
        task_id: Optional[str] = None,
        include_content: bool = True,
    ) -> List[BotRunArtifact]:
        await self._ensure_db()
        sql = f"""
                SELECT * FROM {_BOT_RUN_ARTIFACTS_TABLE}
                WHERE bot_id = ?
            """
        params: List[Any] = [bot_id]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        return [
            BotRunArtifact(
                id=row["id"],
                run_id=row["run_id"],
                task_id=row["task_id"],
                bot_id=row["bot_id"],
                kind=row["kind"],
                label=row["label"],
                content=row["content"] if include_content else None,
                path=row["path"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_bot_run_artifact(self, bot_id: str, artifact_id: str) -> BotRunArtifact:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM {_BOT_RUN_ARTIFACTS_TABLE}
                WHERE bot_id = ? AND id = ?
                LIMIT 1
                """,
                (bot_id, artifact_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise TaskNotFoundError(f"Artifact not found: {artifact_id}")
        return BotRunArtifact(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            bot_id=row["bot_id"],
            kind=row["kind"],
            label=row["label"],
            content=row["content"],
            path=row["path"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=row["created_at"],
        )

    async def _bot_output_contract(self, bot_id: str) -> dict[str, Any]:
        if self._bot_registry is None:
            return {}
        try:
            bot = await self._bot_registry.get(bot_id)
        except Exception:
            return {}
        routing_rules = getattr(bot, "routing_rules", None)
        if isinstance(routing_rules, dict):
            contract = routing_rules.get("output_contract")
            if isinstance(contract, dict):
                return contract
        return {}

    async def _bot_input_contract(self, bot_id: str) -> dict[str, Any]:
        if self._bot_registry is None:
            return {}
        try:
            bot = await self._bot_registry.get(bot_id)
        except Exception:
            return {}
        routing_rules = getattr(bot, "routing_rules", None)
        if isinstance(routing_rules, dict):
            contract = routing_rules.get("input_contract")
            if isinstance(contract, dict):
                return contract
        return {}

    async def _validate_task_payload(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
    ) -> None:
        contract = await self._bot_input_contract(bot_id)
        if not contract:
            return
        enabled = bool(contract.get("enabled", True))
        if not enabled:
            return

        payload_format = str(contract.get("format") or "any").strip().lower()
        required_fields = contract.get("required_fields") if isinstance(contract.get("required_fields"), list) else []
        non_empty_fields = contract.get("non_empty_fields") if isinstance(contract.get("non_empty_fields"), list) else []
        form_fields = contract.get("form_fields") if isinstance(contract.get("form_fields"), list) else []
        default_payload = contract.get("default_payload") if isinstance(contract.get("default_payload"), dict) else {}

        if payload_format == "json_object" and not isinstance(payload, dict):
            raise ValueError("input contract requires a JSON object payload")
        if payload_format == "json_array" and not isinstance(payload, list):
            raise ValueError("input contract requires a JSON array payload")

        validate_before_transform = bool(contract.get("validate_before_transform", False))
        output_mode = await self._bot_output_contract_mode(bot_id)
        has_input_transform = await self._bot_has_enabled_input_transform(bot_id)
        is_intake_role = await self._bot_is_intake_role(bot_id)
        has_launch_form_contract = bool(form_fields) or bool(default_payload)
        is_saved_launch_entry = await self._is_saved_launch_entry(bot_id, metadata)
        looks_like_flat_launch_payload = _looks_like_flat_launch_payload(payload, [str(field) for field in required_fields])
        looks_like_trigger_wrapper_payload = _looks_like_trigger_wrapper_payload(payload)
        if not validate_before_transform and (
            output_mode == "payload_transform"
            or has_input_transform
            or is_intake_role
            or has_launch_form_contract
            or is_saved_launch_entry
            or looks_like_flat_launch_payload
            or looks_like_trigger_wrapper_payload
        ):
            return

        if required_fields:
            missing = _missing_payload_fields(payload, [str(field) for field in required_fields])
            if missing:
                raise ValueError(f"input contract missing required fields: {', '.join(missing)}")
        if non_empty_fields:
            empty = _empty_payload_fields(payload, [str(field) for field in non_empty_fields])
            if empty:
                raise ValueError(f"input contract requires non-empty fields: {', '.join(empty)}")

    async def _bot_output_contract_mode(self, bot_id: str) -> str:
        contract = await self._bot_output_contract(bot_id)
        return str(contract.get("mode") or "model_output").strip().lower()

    async def _bot_has_enabled_input_transform(self, bot_id: str) -> bool:
        bot = await self._bot_registry.get(bot_id)
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return False
        config = routing_rules.get("input_transform")
        return isinstance(config, dict) and bool(config.get("enabled", False)) and config.get("template") is not None

    async def _bot_is_intake_role(self, bot_id: str) -> bool:
        bot = await self._bot_registry.get(bot_id)
        bot_id_value = str(getattr(bot, "id", bot_id) or bot_id).strip().lower()
        if bot_id_value.endswith("intake") or bot_id_value.endswith("-intake"):
            return True
        role = str(getattr(bot, "role", "") or "").strip().lower()
        if role.endswith("intake") or role.endswith("-intake"):
            return True
        name = str(getattr(bot, "name", "") or "").strip().lower()
        return name.endswith("intake")

    async def _is_saved_launch_entry(self, bot_id: str, metadata: Optional[TaskMetadata]) -> bool:
        if metadata is None:
            return False
        source = str(metadata.source or "").strip().lower()
        if source not in {"saved_launch", "saved_launch_pipeline", "manual_retry"}:
            return False
        if metadata.parent_task_id or metadata.trigger_rule_id:
            return False
        return True

    async def _normalize_task_result(self, task: Task, result: Any) -> Any:
        contract = await self._bot_output_contract(task.bot_id)
        if not contract:
            return result

        enabled = bool(contract.get("enabled", True))
        mode = str(contract.get("mode") or "model_output").strip().lower()
        output_format = str(contract.get("format") or "any").strip().lower()
        required_fields = contract.get("required_fields") if isinstance(contract.get("required_fields"), list) else []
        non_empty_fields = contract.get("non_empty_fields") if isinstance(contract.get("non_empty_fields"), list) else []
        defaults_template = contract.get("defaults_template") if isinstance(contract.get("defaults_template"), dict) else None
        fallback_mode = str(contract.get("fallback_mode") or "").strip().lower()
        if fallback_mode not in {"disabled", "missing_only", "parse_failure", "parse_failure_or_missing"}:
            fallback_mode = "parse_failure_or_missing" if defaults_template is not None else "disabled"
        allow_parse_failure_fallback = defaults_template is not None and fallback_mode in {"parse_failure", "parse_failure_or_missing"}
        allow_missing_backfill = defaults_template is not None and fallback_mode in {"missing_only", "parse_failure_or_missing"}
        if (
            not enabled
            or (
                output_format == "any"
                and not required_fields
                and not non_empty_fields
                and defaults_template is None
                and mode != "payload_transform"
            )
        ):
            return result

        if mode == "payload_transform":
            if _payload_satisfies_output_contract(task.payload, [str(field) for field in required_fields], [str(field) for field in non_empty_fields]):
                normalized = task.payload
            else:
                template = contract.get("template")
                if template is None:
                    raise ValueError("output contract payload transform requires a template")
                notes: list[str] = []
                transformed = _transform_template_value(template, task.payload, notes)
                if isinstance(transformed, dict) and "normalization_notes" in transformed:
                    existing = transformed.get("normalization_notes")
                    if not isinstance(existing, list):
                        existing = []
                    transformed["normalization_notes"] = existing + notes
                normalized = transformed
        else:
            normalized = result

        raw_text = ""
        if mode != "payload_transform":
            if isinstance(result, dict):
                for key in ("output", "content", "text", "result"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        raw_text = value
                        break
            elif isinstance(result, str):
                raw_text = result

        if mode != "payload_transform" and (output_format in {"json_object", "json_array"} or required_fields):
            parsed = None
            if isinstance(result, (dict, list)) and output_format == "json_array" and isinstance(result, list):
                parsed = result
            elif raw_text:
                try:
                    parsed = _extract_json_payload(raw_text)
                except ValueError:
                    if allow_parse_failure_fallback:
                        default_notes: list[str] = ["Model output was not parseable JSON; fell back to defaults template."]
                        parsed = _transform_template_value(defaults_template, task.payload, default_notes)
                        if isinstance(parsed, dict):
                            existing_notes = parsed.get("normalization_notes")
                            if not isinstance(existing_notes, list):
                                existing_notes = []
                            parsed["normalization_notes"] = existing_notes + default_notes
                    else:
                        raise
            elif isinstance(result, dict) and output_format in {"json_object", "any"} and required_fields:
                parsed = result
            if parsed is None:
                raise ValueError("output contract requires structured JSON output")
            normalized = parsed

        if allow_missing_backfill:
            default_notes: list[str] = []
            normalized_defaults = _transform_template_value(defaults_template, task.payload, default_notes)
            if isinstance(normalized_defaults, dict):
                normalized = _merge_with_contract_defaults(normalized, normalized_defaults)
                if default_notes and isinstance(normalized, dict):
                    existing_notes = normalized.get("normalization_notes")
                    if not isinstance(existing_notes, list):
                        existing_notes = []
                    normalized["normalization_notes"] = existing_notes + default_notes

        if output_format == "json_object" and not isinstance(normalized, dict):
            raise ValueError("output contract requires a JSON object")
        if output_format == "json_array" and not isinstance(normalized, list):
            raise ValueError("output contract requires a JSON array")

        if required_fields:
            if not isinstance(normalized, dict):
                raise ValueError("required fields can only be validated on JSON objects")
            missing = [field for field in required_fields if str(field) not in normalized]
            if missing:
                raise ValueError(f"output contract missing required fields: {', '.join(str(field) for field in missing)}")
        if non_empty_fields:
            if not isinstance(normalized, dict):
                raise ValueError("non-empty fields can only be validated on JSON objects")
            empty_fields = [
                str(field)
                for field in non_empty_fields
                if _is_empty_contract_value(_lookup_payload_path(normalized, str(field)))
            ]
            if empty_fields:
                raise ValueError(
                    "output contract requires non-empty fields: "
                    + ", ".join(empty_fields)
                )

        if isinstance(result, dict) and isinstance(normalized, dict):
            if "usage" in result and "usage" not in normalized:
                normalized["usage"] = result.get("usage")
            if "artifacts" in result and "artifacts" not in normalized:
                normalized["artifacts"] = result.get("artifacts")
        return normalized

    async def update_status(
        self,
        task_id: str,
        status: str,
        result: Optional[Any] = None,
        error: Optional[TaskError] = None,
    ) -> None:
        await self._ensure_db()
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            now = datetime.now(timezone.utc).isoformat()
            self._tasks[task_id] = self._tasks[task_id].model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error": error,
                    "updated_at": now,
                }
            )
            updated_task = self._tasks[task_id]
        await self._persist_task(updated_task)
        await self._upsert_bot_run(updated_task)
        if status in {"completed", "failed", "cancelled", "retried"}:
            await self._record_artifacts_for_task(updated_task)
            if status in {"completed", "failed"}:
                await self._dispatch_triggers(updated_task)
            await self._try_unblock_tasks()

    async def _run_task(self, task_id: str) -> None:
        raw_result: Any = None
        try:
            if self._is_closing:
                return
            await self.update_status(task_id, "running")
            task = await self.get_task(task_id)
            mode = await self._bot_output_contract_mode(task.bot_id)
            internal_result = await self._maybe_run_internal_assignment_step(task)
            if internal_result is not None:
                raw_result = internal_result
            elif mode == "payload_transform":
                raw_result = {"deterministic_transform": True}
            else:
                raw_result = await self._scheduler.schedule(task)
            result = await self._normalize_task_result(task, raw_result)
            if _prefers_truncation_retry(task) and _looks_like_truncated_result(raw_result):
                task_error = TaskError(
                    message=(
                        "Model output likely truncated at token limit; retrying with increased "
                        "max_tokens/num_predict and num_width/num_ctx."
                    )
                )
                if await self._requeue_for_retry(task, task_error):
                    logger.info("Task %s queued for automatic truncation retry", task_id)
                    return
                raise ValueError(
                    "Model output remained truncated after available retries; increase backend "
                    "max_tokens/num_predict or num_width/num_ctx."
                )
            validation_error = _assignment_validation_error(task, result)
            if validation_error:
                raise ValueError(validation_error)
            payload = task.payload if isinstance(task.payload, dict) else {}
            if (
                self._project_registry is not None
                and _assignment_step_kind(payload) == "repo_change"
                and task.metadata is not None
                and str(task.metadata.source or "").strip().lower() in {"chat_assign", "auto_retry"}
                and str(task.metadata.project_id or "").strip()
            ):
                try:
                    from control_plane.api.projects import _extract_project_repo_workspace, _resolve_repo_workspace_root

                    project = await self._project_registry.get(str(task.metadata.project_id or "").strip())
                    cfg = _extract_project_repo_workspace(project)
                    if bool(cfg.get("enabled", False)):
                        root = _resolve_repo_workspace_root(str(task.metadata.project_id or "").strip(), cfg, require_enabled=True)
                        # Allow additional runtimes from context (e.g., TypeScript for multi-language content)
                        allowed_extra: List[str] = []
                        context_items = payload.get("context_items") or []
                        if isinstance(context_items, list):
                            for item in context_items:
                                item_text = str(item or "").lower()
                                # Detect runtime hints from context
                                if "typescript" in item_text or "node" in item_text:
                                    if "node" not in allowed_extra:
                                        allowed_extra.append("node")
                                if "javascript" in item_text:
                                    if "node" not in allowed_extra:
                                        allowed_extra.append("node")
                                if "python" in item_text:
                                    if "python" not in allowed_extra:
                                        allowed_extra.append("python")
                        runtime_mismatch = _generated_repo_runtime_mismatch_message(root=root, result=result, allowed_extra=allowed_extra or None)
                        if runtime_mismatch:
                            raise ValueError(runtime_mismatch)
                except ValueError:
                    raise
                except Exception:
                    pass
            await self.update_status(task_id, "completed", result=result)
        except asyncio.CancelledError:
            logger.info("Task %s cancelled", task_id)
            raise
        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e)
            task = await self.get_task(task_id)
            error_message = str(e)
            if _is_output_contract_error_message(error_message) and _looks_like_truncated_result(raw_result):
                error_message = (
                    f"{error_message} (likely truncated model output at token limit; "
                    "increase max_tokens/num_predict or reduce expected output size)"
                )
            task_error = TaskError(message=error_message)
            if await self._requeue_for_retry(task, task_error):
                logger.info("Task %s queued for automatic retry", task_id)
            else:
                failure_result = getattr(e, "result", None)
                await self.update_status(task_id, "failed", result=failure_result, error=task_error)
        finally:
            async with self._lock:
                self._running_task_ids.discard(task_id)
                self._runner_tasks.pop(task_id, None)
            if not self._is_closing:
                await self._schedule_ready_tasks()

    async def _requeue_for_retry(self, task: Task, task_error: TaskError) -> bool:
        max_retries = max(0, _settings_int("max_task_retries", 3))
        if max_retries <= 0:
            return False
        if not _is_retryable_error_message(task_error.message):
            return False

        metadata = task.metadata or TaskMetadata()
        current_attempt = int(metadata.retry_attempt or 0)
        if current_attempt >= max_retries:
            return False

        delay_seconds = max(0.0, _settings_float("task_retry_delay", 5.0))
        next_attempt = current_attempt + 1

        async def _delayed_requeue() -> None:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            now = datetime.now(timezone.utc).isoformat()
            async with self._lock:
                existing = self._tasks.get(task.id)
                if existing is None:
                    return
                next_metadata = (existing.metadata or TaskMetadata()).model_copy(
                    update={
                        "retry_attempt": next_attempt,
                        "original_task_id": (existing.metadata.original_task_id if existing.metadata and existing.metadata.original_task_id else existing.id),
                        "retry_of_task_id": existing.id,
                        "source": "auto_retry",
                    }
                )
                self._tasks[task.id] = existing.model_copy(
                    update={
                        "status": "queued",
                        "metadata": next_metadata,
                        "error": task_error,
                        "updated_at": now,
                    }
                )
                updated = self._tasks[task.id]
            await self._persist_task(updated)
            await self._upsert_bot_run(updated)
            await self._schedule_ready_tasks()

        retry_task = asyncio.create_task(_delayed_requeue())
        async with self._lock:
            self._retry_tasks.add(retry_task)

        def _cleanup_retry_task(done_task: asyncio.Task[Any]) -> None:
            self._retry_tasks.discard(done_task)
            if done_task.cancelled():
                return
            exception = done_task.exception()
            if exception is not None:
                logger.warning("Delayed retry task for %s ended with error: %s", task.id, exception)

        retry_task.add_done_callback(_cleanup_retry_task)
        return True

    async def _try_unblock_tasks(self) -> None:
        """Move ready blocked tasks into queued state and schedule them."""
        async with self._lock:
            ready_ids: List[str] = []
            for task_id, task in self._tasks.items():
                if task.status != "blocked":
                    continue
                if DependencyEngine.is_ready(task, self._tasks):
                    now = datetime.now(timezone.utc).isoformat()
                    self._tasks[task_id] = task.model_copy(
                        update={"status": "queued", "updated_at": now}
                    )
                    ready_ids.append(task_id)
            tasks_to_persist = [self._tasks[t_id] for t_id in ready_ids]

        for t in tasks_to_persist:
            await self._persist_task(t)
            await self._upsert_bot_run(t)
        if ready_ids:
            await self._schedule_ready_tasks()

    async def _schedule_ready_tasks(self) -> None:
        if self._is_closing:
            return
        async with self._lock:
            available_slots = max(0, self._max_concurrency - len(self._running_task_ids))
            if available_slots <= 0:
                return
            queued = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.status == "queued" and task.id not in self._running_task_ids
                ),
                key=lambda item: (item.created_at or "", item.updated_at or ""),
            )
            selected = queued[:available_slots]
            for task in selected:
                self._running_task_ids.add(task.id)

        for task in selected:
            runner = asyncio.create_task(self._run_task(task.id))
            async with self._lock:
                self._runner_tasks[task.id] = runner

    async def _maybe_run_internal_assignment_step(self, task: Task) -> Optional[Any]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        metadata = task.metadata or TaskMetadata()
        if not _looks_like_assignment_test_execution_payload(payload):
            return None
        if self._project_registry is None:
            return None
        if str(metadata.source or "").strip().lower() not in {"chat_assign", "auto_retry", "bot_trigger"}:
            return None
        if not metadata.project_id or not metadata.orchestration_id:
            return None
        return await self._run_assignment_test_execution(task)

    async def _run_assignment_test_execution(self, task: Task) -> Dict[str, Any]:
        if self._project_registry is None:
            raise _TaskExecutionFailure("project registry is unavailable for assignment test execution")

        from control_plane.api.projects import (
            _aggregate_usage,
            _allowed_workspace_commands,
            _assignment_file_candidates,
            _bootstrap_command_specs,
            _extract_project_repo_workspace,
            _repo_status_snapshot,
            _resolve_repo_workspace_root,
            _run_repo_command,
            _write_assignment_files,
        )

        payload = task.payload if isinstance(task.payload, dict) else {}
        metadata = task.metadata or TaskMetadata()
        project_id = str(metadata.project_id or "").strip()
        orchestration_id = str(metadata.orchestration_id or "").strip()
        project = await self._project_registry.get(project_id)
        cfg = _extract_project_repo_workspace(project)
        if not bool(cfg.get("enabled", False)):
            raise _TaskExecutionFailure("repo workspace is disabled for this project")
        if not bool(cfg.get("allow_command_execution", False)):
            raise _TaskExecutionFailure("repo workspace command execution is disabled for this project")

        root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
        snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
        if not bool(snapshot.get("is_repo")):
            raise _TaskExecutionFailure("repo workspace is not a git repository; clone it before running assignment tests")

        scoped_tasks = [
            candidate
            for candidate in self._tasks.values()
            if candidate.id != task.id
            and candidate.status == "completed"
            and candidate.metadata
            and candidate.metadata.project_id == project_id
            and candidate.metadata.orchestration_id == orchestration_id
            and str(candidate.metadata.source or "").strip().lower() in {"chat_assign", "auto_retry", "bot_trigger"}
        ]
        scoped_tasks = self._filter_assignment_tasks_to_branch_scope(scoped_tasks, payload)
        file_candidates = _assignment_file_candidates(scoped_tasks)
        if not file_candidates:
            raise _TaskExecutionFailure("no assignment file outputs were detected to run tests against")

        applied_files = _write_assignment_files(root=root, candidates=file_candidates, overwrite=True)
        applied_paths = [str(item.get("path") or "").strip().replace("\\", "/") for item in applied_files]
        test_files = _assignment_test_source_files(applied_paths)
        report_paths = _assignment_test_report_paths(payload)
        source_paths = [
            path for path in applied_paths
            if path and path not in test_files and not path.lower().startswith(("docs/", "issues/", "specification/"))
        ]
        if not test_files:
            raise _TaskExecutionFailure("no generated test files were detected for assignment test execution")

        usage_parts: List[Dict[str, Any]] = []
        command_results: List[Dict[str, Any]] = []
        languages = _assignment_execution_languages(
            applied_paths=applied_paths,
            test_files=test_files,
            root=root,
        )
        if not languages:
            raise _TaskExecutionFailure(
                "generated test files do not match the repo runtime profile; do not introduce a new runtime for this repo"
            )
        artifacts: List[Dict[str, Any]] = []
        missing_reports: List[str] = []
        executed_any_tests = False
        executed_bootstrap_labels: Set[str] = set()
        allowed_commands = _allowed_workspace_commands()

        async def _run_bootstrap_languages(spec_languages: List[str]) -> None:
            for spec in _bootstrap_command_specs(root, spec_languages):
                label = str(spec.get("label") or "bootstrap")
                if label in executed_bootstrap_labels:
                    continue
                bootstrap_cmd = [str(part) for part in spec.get("command") or []]
                if not bootstrap_cmd:
                    continue
                executable = Path(str(bootstrap_cmd[0])).name.lower()
                executable_stem = Path(str(bootstrap_cmd[0])).stem.lower()
                if executable not in allowed_commands and executable_stem not in allowed_commands:
                    continue
                step_result = await _run_repo_command(
                    bootstrap_cmd,
                    cwd=root,
                    timeout_seconds=int(spec.get("timeout_seconds") or 1200),
                )
                usage_parts.append(step_result.get("resource_usage") or {})
                command_results.append(
                    {
                        "label": label,
                        "command": step_result.get("command") or bootstrap_cmd,
                        "exit_code": step_result.get("exit_code"),
                        "ok": bool(step_result.get("ok")),
                        "stdout": str(step_result.get("stdout") or ""),
                        "stderr": str(step_result.get("stderr") or ""),
                        "error": str(step_result.get("error") or ""),
                    }
                )
                executed_bootstrap_labels.add(label)
                if not step_result.get("ok"):
                    result = self._format_assignment_test_execution_result(
                        task=task,
                        applied_files=applied_files,
                        command_results=command_results,
                        artifacts=[],
                        workspace=await _repo_status_snapshot(root=root, cfg=cfg),
                        usage=_aggregate_usage(usage_parts),
                    )
                    raise _TaskExecutionFailure("assignment test bootstrap failed", result=result)

        if "python" in languages:
            await _run_bootstrap_languages(["python"])

            from control_plane.api.projects import _python_runtime_venv_dir

            venv_python = _python_runtime_venv_dir(root) / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
            python_cmd = [str(venv_python)] if venv_python.exists() else ["py"]
            test_cmd = [*python_cmd, "-m", "pytest", *test_files, "-q"]
            coverage_path = next((path for path in report_paths if path.lower().endswith(".xml")), None)
            text_report_paths = [
                path for path in report_paths
                if path.lower().endswith((".txt", ".log"))
            ]
            coverage_target = _assignment_python_coverage_target(source_paths)
            if coverage_target:
                test_cmd.extend(["--cov", coverage_target, "--cov-report=term-missing"])
                if coverage_path:
                    test_cmd.append(f"--cov-report=xml:{coverage_path}")
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_python",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            if text_report_paths:
                text_report_content = str(test_result.get("stdout") or "")
                stderr_text = str(test_result.get("stderr") or "")
                if stderr_text:
                    if text_report_content:
                        text_report_content += "\n"
                    text_report_content += stderr_text
                for relative_path in text_report_paths:
                    report_file = (root / relative_path).resolve(strict=False)
                    report_file.parent.mkdir(parents=True, exist_ok=True)
                    report_file.write_text(text_report_content, encoding="utf-8")
            executed_any_tests = True
        if "node" in languages:
            await _run_bootstrap_languages(["node"])
            test_cmd = _assignment_node_test_command(root)
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_node",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            executed_any_tests = True
        if "dotnet" in languages:
            await _run_bootstrap_languages(["dotnet"])
            results_dir = root / ".nexusai_test_results"
            test_cmd = ["dotnet", "test", "--nologo", "--verbosity", "minimal"]
            coverage_path = next((path for path in report_paths if path.lower().endswith(".xml")), None)
            if coverage_path:
                test_cmd.extend(["--collect:XPlat Code Coverage", "--results-directory", str(results_dir)])
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            if test_result.get("ok") and coverage_path:
                candidates = list(results_dir.rglob("coverage.cobertura.xml")) if results_dir.exists() else []
                if not candidates and results_dir.exists():
                    candidates = list(results_dir.rglob("*.xml"))
                if candidates:
                    target = (root / coverage_path).resolve(strict=False)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidates[0], target)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_dotnet",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            executed_any_tests = True
        if "go" in languages:
            await _run_bootstrap_languages(["go"])
            test_cmd = ["go", "test", "./...", "-count=1"]
            coverage_path = next(
                (
                    path for path in report_paths
                    if path.lower().endswith((".out", ".txt", ".cov"))
                ),
                None,
            )
            if coverage_path:
                test_cmd.append(f"-coverprofile={coverage_path}")
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_go",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            executed_any_tests = True
        if "rust" in languages:
            await _run_bootstrap_languages(["rust"])
            test_cmd = ["cargo", "test", "--all-targets"]
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_rust",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            executed_any_tests = True
        if "cpp" in languages:
            await _run_bootstrap_languages(["cpp"])
            test_cmd = _assignment_cpp_test_command(root)
            test_result = await _run_repo_command(test_cmd, cwd=root, timeout_seconds=1800)
            usage_parts.append(test_result.get("resource_usage") or {})
            command_results.append(
                {
                    "label": "test_execution_cpp",
                    "command": test_result.get("command") or test_cmd,
                    "exit_code": test_result.get("exit_code"),
                    "ok": bool(test_result.get("ok")),
                    "stdout": str(test_result.get("stdout") or ""),
                    "stderr": str(test_result.get("stderr") or ""),
                    "error": str(test_result.get("error") or ""),
                }
            )
            executed_any_tests = True

        if not executed_any_tests:
            raise _TaskExecutionFailure("assignment test execution did not select any runnable test command")

        for relative_path in report_paths:
            artifact_path = (root / relative_path).resolve(strict=False)
            if artifact_path.exists() and artifact_path.is_file():
                artifacts.append(
                    {
                        "kind": "file",
                        "label": relative_path,
                        "path": relative_path,
                        "content": artifact_path.read_text(encoding="utf-8", errors="replace"),
                    }
                )
            else:
                missing_reports.append(relative_path)

        workspace = await _repo_status_snapshot(root=root, cfg=cfg)
        result = self._format_assignment_test_execution_result(
            task=task,
            applied_files=applied_files,
            command_results=command_results,
            artifacts=artifacts,
            workspace=workspace,
            usage=_aggregate_usage(usage_parts),
        )
        missing_tools = _missing_assignment_runtime_tools(command_results)
        if missing_tools:
            findings = [
                "The repo workspace runtime is missing required tools: " + ", ".join(missing_tools) + ".",
                "This is an environment/runtime blocker, not a code implementation defect.",
            ]
            evidence = [
                "Missing tools: " + ", ".join(missing_tools),
            ]
            for item in command_results:
                label = str(item.get("label") or "").strip()
                command = " ".join(str(part) for part in (item.get("command") or []) if str(part).strip())
                error = str(item.get("error") or "").strip()
                if command:
                    evidence.append(f"{label or 'command'}: {command}")
                if error:
                    evidence.append(f"{label or 'command'} error: {error}")
            result.update(
                {
                    "outcome": "fail",
                    "failure_type": "environment_blocker",
                    "findings": findings,
                    "evidence": evidence,
                    "handoff_notes": (
                        "Install the missing repo workspace runtime tools on the execution host/container, "
                        "rebuild/redeploy if needed, then rerun this test step."
                    ),
                }
            )
            return result
        if missing_reports:
            result.update(
                {
                    "outcome": "fail",
                    "failure_type": "implementation_issue",
                    "findings": [
                        "Assignment test execution did not produce all required report files.",
                    ],
                    "evidence": ["Missing report files: " + ", ".join(missing_reports)],
                    "handoff_notes": (
                        "Update the implementation or test setup so the required report artifacts are generated, "
                        "then rerun this test step."
                    ),
                }
            )
            return result
        if not all(bool(item.get("ok")) for item in command_results if str(item.get("label") or "").startswith("test_execution")):
            failed_commands = [
                " ".join(str(part) for part in (item.get("command") or []) if str(part).strip())
                for item in command_results
                if str(item.get("label") or "").startswith("test_execution") and not bool(item.get("ok"))
            ]
            result.update(
                {
                    "outcome": "fail",
                    "failure_type": "implementation_issue",
                    "findings": [
                        "One or more generated test commands failed.",
                    ],
                    "evidence": failed_commands,
                    "handoff_notes": (
                        "Fix the implementation or generated tests based on the execution output, then rerun this test step."
                    ),
                }
            )
            return result
        result.update(
            {
                "outcome": "pass",
                "failure_type": "pass",
                "findings": ["All generated automated tests executed successfully."],
                "evidence": [
                    "Executed test commands: "
                    + ", ".join(
                        " ".join(str(part) for part in (item.get("command") or []) if str(part).strip())
                        for item in command_results
                        if str(item.get("label") or "").startswith("test_execution")
                    )
                ],
                "handoff_notes": "Automated test execution passed. Continue to the next validation stage.",
            }
        )
        return result

    def _filter_assignment_tasks_to_branch_scope(self, tasks: List[Task], payload: Dict[str, Any]) -> List[Task]:
        fanout_id = self._resolve_fanout_id(payload)
        branch_key = self._resolve_join_branch_key(payload)
        if not fanout_id and not branch_key:
            return tasks
        filtered: List[Task] = []
        for candidate in tasks:
            candidate_payload = candidate.payload if isinstance(candidate.payload, dict) else {}
            if fanout_id:
                candidate_fanout_id = self._resolve_fanout_id(candidate_payload)
                if candidate_fanout_id != fanout_id:
                    continue
            if branch_key:
                candidate_branch_key = self._resolve_join_branch_key(candidate_payload)
                if candidate_branch_key != branch_key:
                    continue
            filtered.append(candidate)
        return filtered

    def _format_assignment_test_execution_result(
        self,
        *,
        task: Task,
        applied_files: List[Dict[str, Any]],
        command_results: List[Dict[str, Any]],
        artifacts: List[Dict[str, Any]],
        workspace: Dict[str, Any],
        usage: Dict[str, Any],
    ) -> Dict[str, Any]:
        lines = ["## Executed Commands", ""]
        executed_commands: List[Dict[str, Any]] = []
        missing_tools = _missing_assignment_runtime_tools(command_results)
        for item in command_results:
            command = [str(part) for part in (item.get("command") or [])]
            exit_code = item.get("exit_code")
            stdout = str(item.get("stdout") or "")
            stderr = str(item.get("stderr") or "")
            error = str(item.get("error") or "")
            stdout_excerpt = "\n".join(stdout.splitlines()[:12]).strip()
            stderr_excerpt = "\n".join(stderr.splitlines()[:12]).strip()
            executed_commands.append(
                {
                    "label": item.get("label"),
                    "command": command,
                    "exit_code": exit_code,
                    "stdout_excerpt": stdout_excerpt,
                    "stderr_excerpt": stderr_excerpt,
                    "error": error,
                    "ok": bool(item.get("ok")),
                }
            )
            lines.append(f"- Command: `{' '.join(command)}`")
            lines.append(f"  Exit Code: {exit_code}")
            if error:
                lines.append(f"  Error: {error}")
            if stdout_excerpt:
                lines.append("  Stdout:")
                lines.append("")
                lines.append("  ```text")
                lines.extend(f"  {line}" for line in stdout_excerpt.splitlines())
                lines.append("  ```")
            if stderr_excerpt:
                lines.append("  Stderr:")
                lines.append("")
                lines.append("  ```text")
                lines.extend(f"  {line}" for line in stderr_excerpt.splitlines())
                lines.append("  ```")

        report_text = "\n".join(lines).strip()
        artifact_items = list(artifacts)
        if not any(str(item.get("path") or "").strip().replace("\\", "/") == "test_logs/assignment_test_execution.log" for item in artifact_items):
            artifact_items.append(
                {
                    "kind": "file",
                    "label": "test_logs/assignment_test_execution.log",
                    "path": "test_logs/assignment_test_execution.log",
                    "content": report_text,
                }
            )
        return {
            "report": "\n".join(lines).strip(),
            "executed_commands": executed_commands,
            "command_results": command_results,
            "missing_tools": missing_tools,
            "artifacts": artifact_items,
            "applied_files": applied_files,
            "workspace": workspace,
            "usage": usage,
            "exit_code": next(
                (item.get("exit_code") for item in reversed(command_results) if item.get("exit_code") is not None),
                None,
            ),
        }

    async def _dispatch_triggers(self, task: Task) -> None:
        if self._bot_registry is None:
            return
        try:
            bot = await self._bot_registry.get(task.bot_id)
        except Exception:
            return
        workflow = self._bot_workflow(bot)
        if workflow is None or not workflow.triggers:
            return

        metadata = task.metadata or TaskMetadata()
        
        # For plan-managed orchestrated tasks, skip forward triggers because the PM plan already
        # defines the main forward path. Backward-triggered remediation tasks are created with
        # source="bot_trigger"; those are allowed to flow forward again so repair loops can close.
        source = str(metadata.source or "").strip().lower()
        is_plan_managed_orchestrated = source in {"chat_assign", "auto_retry"}
        
        trigger_depth = int(metadata.trigger_depth or 0)
        max_depth = max(1, _settings_int("bot_trigger_max_depth", 20))
        if trigger_depth >= max_depth:
            logger.warning("Skipping bot triggers for task %s due to depth cap %s", task.id, max_depth)
            return

        event = "task_completed" if task.status == "completed" else "task_failed"
        for trigger in workflow.triggers:
            try:
                if not trigger.enabled or trigger.event != event:
                    continue
                if trigger.condition == "has_result" and task.result is None:
                    continue
                if trigger.condition == "has_error" and task.error is None:
                    continue
                
                # Skip forward triggers only on the plan-managed tasks created directly by
                # assignment orchestration. Trigger-spawned remediation tasks are allowed to
                # move forward through their configured workflow.
                if is_plan_managed_orchestrated:
                    is_forward_trigger = (
                        trigger.condition == "has_result" and
                        not trigger.result_field and  # backward triggers have result_field for failure_type
                        trigger.event == "task_completed"
                    )
                    is_dynamic_forward_trigger = bool(
                        str(getattr(trigger, "fan_out_field", "") or "").strip()
                        or self._trigger_uses_join(trigger)
                    )
                    if is_forward_trigger and not is_dynamic_forward_trigger:
                        logger.debug(
                            "Skipping forward trigger %s for orchestrated task %s (orchestrator manages forward progression)",
                            trigger.id,
                            task.id,
                        )
                        continue
                if trigger.condition == "has_error" and task.error is None:
                    continue
                if not self._trigger_matches_result(task, trigger):
                    continue
                target_bot_id = self._resolve_trigger_target_bot_id(task, trigger.target_bot_id)
                if not target_bot_id:
                    logger.warning("Skipping trigger %s for task %s because target bot could not be resolved", trigger.id, task.id)
                    continue
                payloads = self._build_trigger_payloads(task, trigger)
                if not payloads:
                    logger.warning("Skipping trigger %s for task %s because no payloads were produced", trigger.id, task.id)
                    await self._record_trigger_dispatch_skip(
                        source_task=task,
                        trigger_id=str(getattr(trigger, "id", "") or ""),
                        target_bot_id=str(target_bot_id or ""),
                        details=self._describe_trigger_payload_skip(task, trigger),
                    )
                    continue
                next_metadata = TaskMetadata(
                    user_id=metadata.user_id if trigger.inherit_metadata else None,
                    project_id=metadata.project_id if trigger.inherit_metadata else None,
                    source="bot_trigger",
                    priority=metadata.priority if trigger.inherit_metadata else None,
                    conversation_id=metadata.conversation_id if trigger.inherit_metadata else None,
                    orchestration_id=metadata.orchestration_id if trigger.inherit_metadata else None,
                    pipeline_name=metadata.pipeline_name if trigger.inherit_metadata else None,
                    pipeline_entry_bot_id=metadata.pipeline_entry_bot_id if trigger.inherit_metadata else None,
                    parent_task_id=task.id,
                    trigger_rule_id=trigger.id,
                    trigger_depth=trigger_depth + 1,
                    workflow_root_task_id=metadata.workflow_root_task_id or task.id,
                )
                if self._trigger_uses_join(trigger):
                    await self._dispatch_join_trigger(task, trigger, target_bot_id, payloads, next_metadata)
                    continue
                for payload in payloads:
                    branch_metadata = next_metadata
                    if isinstance(payload, dict):
                        branch_step_id = self._fanout_step_id(task, trigger, payload)
                        if branch_step_id:
                            existing = await self._find_task_by_step_id(branch_step_id, target_bot_id)
                            if existing is not None and existing.status in {"queued", "running", "blocked", "completed"}:
                                continue
                            branch_metadata = next_metadata.model_copy(update={"step_id": branch_step_id})
                    await self.create_task(
                        bot_id=target_bot_id,
                        payload=payload,
                        metadata=branch_metadata,
                    )
            except Exception as exc:
                logger.exception(
                    "Trigger %s failed while dispatching task %s to %s",
                    getattr(trigger, "id", ""),
                    task.id,
                    getattr(trigger, "target_bot_id", ""),
                )
                await self._record_trigger_dispatch_error(
                    source_task=task,
                    trigger_id=str(getattr(trigger, "id", "") or ""),
                    target_bot_id=str(getattr(trigger, "target_bot_id", "") or ""),
                    message=str(exc),
                )

    def _bot_workflow(self, bot: Any) -> Any:
        workflow = getattr(bot, "workflow", None)
        if workflow is not None and getattr(workflow, "triggers", None):
            return workflow
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return workflow
        candidate = routing_rules.get("workflow")
        if candidate is None:
            return workflow
        try:
            from shared.models import BotWorkflow

            parsed = BotWorkflow.model_validate(candidate)
            return parsed if parsed.triggers else workflow
        except Exception:
            return workflow

    async def _record_trigger_dispatch_error(
        self,
        source_task: Task,
        trigger_id: str,
        target_bot_id: str,
        message: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._upsert_artifact(
            BotRunArtifact(
                id=f"{source_task.id}:trigger-error:{trigger_id or 'unknown'}",
                run_id=source_task.id,
                task_id=source_task.id,
                bot_id=source_task.bot_id,
                kind="error",
                label="Trigger Dispatch Error",
                content=json.dumps(
                    {
                        "trigger_id": trigger_id,
                        "target_bot_id": target_bot_id,
                        "message": message,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                metadata={
                    "trigger_id": trigger_id,
                    "target_bot_id": target_bot_id,
                    "message": message,
                },
                created_at=now,
            )
        )

    async def _record_trigger_dispatch_skip(
        self,
        source_task: Task,
        trigger_id: str,
        target_bot_id: str,
        details: Dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._upsert_artifact(
            BotRunArtifact(
                id=f"{source_task.id}:trigger-skip:{trigger_id or 'unknown'}",
                run_id=source_task.id,
                task_id=source_task.id,
                bot_id=source_task.bot_id,
                kind="note",
                label="Trigger Dispatch Skipped",
                content=json.dumps(
                    {
                        "trigger_id": trigger_id,
                        "target_bot_id": target_bot_id,
                        "details": details,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                metadata={
                    "trigger_id": trigger_id,
                    "target_bot_id": target_bot_id,
                    "details": details,
                },
                created_at=now,
            )
        )

    async def _record_join_wait_state(
        self,
        source_task: Task,
        trigger_id: str,
        target_bot_id: str,
        group_value: Any,
        expected_count: Optional[int],
        received_count: int,
        expected_branch_keys: List[str],
        seen_branch_keys: List[str],
        missing_branch_keys: List[str],
        fanout_id: Optional[str],
        reason: str,
    ) -> None:
        normalized_group = self._normalize_branch_token(group_value, fallback="group")
        artifact_id = f"{source_task.id}:join-wait:{trigger_id or 'unknown'}:{normalized_group}"
        details = {
            "trigger_id": trigger_id,
            "target_bot_id": target_bot_id,
            "group_value": group_value,
            "expected_count": expected_count,
            "received_count": received_count,
            "expected_branch_keys": expected_branch_keys,
            "seen_branch_keys": seen_branch_keys,
            "missing_branch_keys": missing_branch_keys,
            "fanout_id": fanout_id,
            "reason": reason,
        }
        safe_details = json.loads(json.dumps(details, default=str))
        await self._upsert_artifact(
            BotRunArtifact(
                id=artifact_id,
                run_id=source_task.id,
                task_id=source_task.id,
                bot_id=source_task.bot_id,
                kind="note",
                label="Join Waiting",
                content=json.dumps(safe_details, indent=2, sort_keys=True),
                metadata=safe_details,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _build_trigger_payload(self, task: Task, trigger: Any) -> Any:
        payload_template = trigger.payload_template
        target_bot_id = str(getattr(trigger, "target_bot_id", "") or "").strip()
        target_role_hint = _trigger_target_role_hint(target_bot_id)
        target_step_kind = _trigger_target_step_kind(target_bot_id)
        base_payload: Dict[str, Any] = {
            "source_bot_id": task.bot_id,
            "source_task_id": task.id,
            "source_status": task.status,
            "source_payload": task.payload,
            "source_result": task.result,
            "source_error": task.error.model_dump() if task.error else None,
            "instruction": f"Triggered by bot {task.bot_id} task {task.id}",
        }
        if target_role_hint:
            base_payload["role_hint"] = target_role_hint
        if target_step_kind:
            base_payload["step_kind"] = target_step_kind
        if isinstance(payload_template, dict):
            notes: list[str] = []
            transformed = _transform_template_value(payload_template, base_payload, notes)
            merged = dict(base_payload)
            if isinstance(transformed, dict):
                merged.update(transformed)
            else:
                merged["payload_template_error"] = "Trigger payload template did not resolve to a JSON object."
            if notes:
                merged["trigger_template_notes"] = notes
            return merged
        if payload_template is not None:
            notes: list[str] = []
            transformed = _transform_template_value(payload_template, base_payload, notes)
            if notes and isinstance(transformed, dict):
                transformed["trigger_template_notes"] = notes
            return transformed
        if isinstance(task.payload, dict):
            upstream_payload = task.payload.get("source_payload")
            if isinstance(upstream_payload, dict):
                self._promote_trigger_context_fields(base_payload, upstream_payload)
            self._promote_trigger_context_fields(base_payload, task.payload)
            # The current task's branch metadata must override inherited source_payload
            # context so nested fan-out/join stages stay scoped to the active branch.
            current_context_fields = (
                "workstream",
                "workstream_index",
                "fanout_count",
                "fanout_id",
                "fanout_branch_key",
                "fanout_expected_branch_keys",
                "depends_on_steps",
                "context_items",
                "global_acceptance_criteria",
                "global_quality_gates",
                "global_risks",
                "project_id",
                "conversation_id",
                "orchestration_id",
            )
            for field in current_context_fields:
                value = task.payload.get(field)
                if _is_empty_contract_value(value):
                    continue
                base_payload[field] = value
        if isinstance(task.result, dict):
            self._promote_trigger_result_fields(base_payload, task.result)
        return base_payload

    def _build_trigger_payloads(self, task: Task, trigger: Any) -> List[Any]:
        payload = self._build_trigger_payload(task, trigger)
        fan_out_field = str(getattr(trigger, "fan_out_field", "") or "").strip()
        if not fan_out_field:
            return [payload]
        if not isinstance(payload, dict):
            return [payload]
        items = self._resolve_fan_out_items(payload, task, fan_out_field)
        if not isinstance(items, list):
            return []
        alias = str(getattr(trigger, "fan_out_alias", "") or "").strip() or "item"
        index_alias = str(getattr(trigger, "fan_out_index_alias", "") or "").strip() or "item_index"
        total = len(items)
        fanout_id = self._fanout_id(task, trigger)
        payloads: List[Any] = []
        branch_keys: List[str] = []
        for idx, item in enumerate(items):
            next_payload = dict(payload)
            next_payload[alias] = item
            next_payload[index_alias] = idx
            if isinstance(item, dict):
                self._promote_fanout_item_fields(next_payload, item)
            next_payload["fanout_count"] = total
            if fanout_id:
                next_payload["fanout_id"] = fanout_id
            branch_key = self._fanout_branch_key(trigger, next_payload)
            if branch_key:
                next_payload["fanout_branch_key"] = branch_key
                branch_keys.append(branch_key)
            payloads.append(next_payload)
        if branch_keys:
            # Persist the expected branch set on every child payload so downstream joins
            # can wait for the exact branch keys instead of relying only on counts.
            unique_branch_keys: List[str] = []
            seen: Set[str] = set()
            for key in branch_keys:
                if key in seen:
                    continue
                seen.add(key)
                unique_branch_keys.append(key)
            for next_payload in payloads:
                if isinstance(next_payload, dict):
                    next_payload["fanout_expected_branch_keys"] = list(unique_branch_keys)
        return payloads

    def _promote_fanout_item_fields(self, payload: Dict[str, Any], item: Dict[str, Any]) -> None:
        promotable_fields = (
            "title",
            "instruction",
            "acceptance_criteria",
            "deliverables",
            "quality_gates",
            "evidence_requirements",
        )
        source_payload = payload.get("source_payload") if isinstance(payload.get("source_payload"), dict) else {}
        for field in promotable_fields:
            value = item.get(field)
            if _is_empty_contract_value(value):
                continue
            current = payload.get(field)
            inherited = source_payload.get(field) if isinstance(source_payload, dict) else None
            if field == "instruction":
                if (
                    _is_empty_contract_value(current)
                    or _looks_like_trigger_wrapper_instruction(current)
                    or current == inherited
                ):
                    payload[field] = value
                continue
            if _is_empty_contract_value(current) or current == inherited:
                payload[field] = value

    def _promote_trigger_context_fields(self, payload: Dict[str, Any], source: Dict[str, Any]) -> None:
        self._promote_fanout_item_fields(payload, source)
        passthrough_fields = (
            "workstream",
            "workstream_index",
            "fanout_count",
            "fanout_id",
            "fanout_branch_key",
            "fanout_expected_branch_keys",
            "depends_on_steps",
            "context_items",
            "global_acceptance_criteria",
            "global_quality_gates",
            "global_risks",
            "project_id",
            "conversation_id",
            "orchestration_id",
        )
        for field in passthrough_fields:
            if field in payload and not _is_empty_contract_value(payload.get(field)):
                continue
            value = source.get(field)
            if _is_empty_contract_value(value):
                continue
            payload[field] = value

    def _promote_trigger_result_fields(self, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        field_map = {
            "failure_type": "upstream_failure_type",
            "findings": "upstream_findings",
            "evidence": "upstream_evidence",
            "handoff_notes": "upstream_handoff_notes",
            "artifacts": "upstream_artifacts",
            "outcome": "upstream_outcome",
            "status": "upstream_status",
        }
        for source_field, target_field in field_map.items():
            if target_field in payload and not _is_empty_contract_value(payload.get(target_field)):
                continue
            value = result.get(source_field)
            if _is_empty_contract_value(value):
                continue
            payload[target_field] = value

    def _resolve_fan_out_items(
        self,
        payload: Dict[str, Any],
        task: Task,
        fan_out_field: str,
    ) -> Optional[List[Any]]:
        for lookup_path in self._fan_out_lookup_paths(fan_out_field):
            items = self._lookup_result_field(payload, lookup_path)
            if isinstance(items, list):
                return items
        if isinstance(task.result, dict):
            for lookup_path in self._fan_out_lookup_paths(fan_out_field):
                # Allow fan-out fields to be expressed relative to both wrapped payload
                # context and the raw task result.
                items = self._lookup_result_field(task.result, lookup_path)
                if isinstance(items, list):
                    return items
        return None

    def _fan_out_lookup_paths(self, fan_out_field: str) -> List[str]:
        raw = str(fan_out_field or "").strip()
        if not raw:
            return []
        paths: List[str] = [raw]
        if raw.startswith("source_result."):
            paths.append(raw[len("source_result.") :].strip())
        if raw.startswith("payload."):
            paths.append(raw[len("payload.") :].strip())
        unique: List[str] = []
        seen: Set[str] = set()
        for path in paths:
            normalized = str(path or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    def _describe_trigger_payload_skip(self, task: Task, trigger: Any) -> Dict[str, Any]:
        fan_out_field = str(getattr(trigger, "fan_out_field", "") or "").strip()
        payload = self._build_trigger_payload(task, trigger)
        details: Dict[str, Any] = {
            "reason": "no_payloads_produced",
            "fan_out_field": fan_out_field or None,
            "trigger_payload_type": type(payload).__name__,
        }
        if not fan_out_field:
            return details
        if not isinstance(payload, dict):
            details["reason"] = "fan_out_requires_object_payload"
            details["trigger_payload_summary"] = _summarize_payload(payload)
            return details

        lookup_paths = self._fan_out_lookup_paths(fan_out_field)
        details["lookup_paths"] = lookup_paths

        payload_value = None
        payload_path = None
        for path in lookup_paths:
            value = self._lookup_result_field(payload, path)
            if isinstance(value, list):
                payload_value = value
                payload_path = path
                break
        details["payload_resolved_path"] = payload_path
        details["payload_resolved_type"] = type(payload_value).__name__ if payload_value is not None else None
        details["payload_resolved_count"] = len(payload_value) if isinstance(payload_value, list) else None

        result_value = None
        result_path = None
        if isinstance(task.result, dict):
            for path in lookup_paths:
                value = self._lookup_result_field(task.result, path)
                if isinstance(value, list):
                    result_value = value
                    result_path = path
                    break
        details["result_resolved_path"] = result_path
        details["result_resolved_type"] = type(result_value).__name__ if result_value is not None else None
        details["result_resolved_count"] = len(result_value) if isinstance(result_value, list) else None

        if payload_value is None and result_value is None:
            details["reason"] = "fan_out_field_not_list"
        elif isinstance(payload_value, list) and len(payload_value) == 0:
            details["reason"] = "fan_out_field_resolved_empty_list"
        elif isinstance(result_value, list) and len(result_value) == 0:
            details["reason"] = "fan_out_result_field_resolved_empty_list"
        return details

    def _trigger_uses_join(self, trigger: Any) -> bool:
        join_fields = (
            "join_expected_field",
            "join_group_field",
            "join_items_alias",
            "join_result_field",
            "join_result_items_alias",
            "join_sort_field",
        )
        return any(bool(str(getattr(trigger, field, "") or "").strip()) for field in join_fields)

    async def _dispatch_join_trigger(
        self,
        task: Task,
        trigger: Any,
        target_bot_id: str,
        payloads: List[Any],
        next_metadata: TaskMetadata,
    ) -> None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            aggregate_payload = await self._build_join_payload(task, trigger, target_bot_id, payload)
            if aggregate_payload is None:
                continue
            join_step_id = self._join_step_id(task, trigger, payload)
            if not join_step_id:
                continue
            existing = await self._find_task_by_step_id(join_step_id, target_bot_id)
            if existing is not None:
                continue
            aggregate_metadata = next_metadata.model_copy(update={"step_id": join_step_id})
            await self.create_task(
                bot_id=target_bot_id,
                payload=aggregate_payload,
                metadata=aggregate_metadata,
            )

    async def _build_join_payload(
        self,
        task: Task,
        trigger: Any,
        target_bot_id: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        expected_field = str(getattr(trigger, "join_expected_field", "") or "").strip()
        items_alias = str(getattr(trigger, "join_items_alias", "") or "").strip() or "items"
        result_field = str(getattr(trigger, "join_result_field", "") or "").strip()
        result_items_alias = str(getattr(trigger, "join_result_items_alias", "") or "").strip() or "join_result_items"
        sort_field = str(getattr(trigger, "join_sort_field", "") or "").strip()

        group_value = self._lookup_result_field(payload, group_field) if group_field else None
        sibling_payloads = self._collect_join_payloads(task, trigger, group_field, group_value)
        fanout_id = self._resolve_fanout_id(payload)
        if fanout_id:
            # Restrict sibling collection to the originating fan-out set so unrelated
            # branches from the same bot/workflow root cannot satisfy this join.
            sibling_payloads = [
                item for item in sibling_payloads if self._resolve_fanout_id(item) == fanout_id
            ]

        expected_branch_keys = self._resolve_fanout_expected_branch_keys(payload)
        expected_count = self._resolve_join_expected_count(payload, expected_field, sibling_payloads, expected_branch_keys)
        if expected_count is None:
            await self._record_join_wait_state(
                source_task=task,
                trigger_id=str(getattr(trigger, "id", "") or ""),
                target_bot_id=target_bot_id,
                group_value=group_value,
                expected_count=None,
                received_count=0,
                expected_branch_keys=expected_branch_keys,
                seen_branch_keys=[],
                missing_branch_keys=[],
                fanout_id=fanout_id,
                reason="invalid_expected_count",
            )
            logger.warning(
                "Skipping join trigger %s for task %s because expected count is invalid: %r",
                trigger.id,
                task.id,
                self._lookup_result_field(payload, expected_field),
            )
            return None

        (
            selected_payloads,
            selected_branch_keys,
            missing_branch_keys,
        ) = self._select_join_payloads(
            sibling_payloads=sibling_payloads,
            expected_count=expected_count,
            expected_branch_keys=expected_branch_keys,
            sort_field=sort_field,
        )

        if missing_branch_keys:
            await self._record_join_wait_state(
                source_task=task,
                trigger_id=str(getattr(trigger, "id", "") or ""),
                target_bot_id=target_bot_id,
                group_value=group_value,
                expected_count=expected_count,
                received_count=len(selected_payloads),
                expected_branch_keys=expected_branch_keys,
                seen_branch_keys=selected_branch_keys,
                missing_branch_keys=missing_branch_keys,
                fanout_id=fanout_id,
                reason="missing_branch_keys",
            )
            logger.info(
                "Join trigger %s waiting for missing branch keys for group %r: %s",
                trigger.id,
                group_value,
                ",".join(missing_branch_keys),
            )
            return None
        if len(selected_payloads) < expected_count:
            await self._record_join_wait_state(
                source_task=task,
                trigger_id=str(getattr(trigger, "id", "") or ""),
                target_bot_id=target_bot_id,
                group_value=group_value,
                expected_count=expected_count,
                received_count=len(selected_payloads),
                expected_branch_keys=expected_branch_keys,
                seen_branch_keys=selected_branch_keys,
                missing_branch_keys=missing_branch_keys,
                fanout_id=fanout_id,
                reason="waiting_for_siblings",
            )
            logger.info(
                "Join trigger %s waiting for sibling outputs for group %r (%s/%s)",
                trigger.id,
                group_value,
                len(selected_payloads),
                expected_count,
            )
            return None

        aggregate_payload = dict(payload)
        aggregate_payload[items_alias] = selected_payloads[:expected_count]
        aggregate_payload["join_results"] = [
            item.get("source_result")
            for item in aggregate_payload[items_alias]
            if isinstance(item, dict) and item.get("source_result") is not None
        ]
        if result_field:
            aggregate_payload[result_items_alias] = [
                self._lookup_result_field(item, result_field)
                for item in aggregate_payload[items_alias]
                if isinstance(item, dict) and self._lookup_result_field(item, result_field) is not None
            ]
        aggregate_payload["join_task_ids"] = [
            str(item.get("source_task_id"))
            for item in aggregate_payload[items_alias]
            if isinstance(item, dict) and item.get("source_task_id")
        ]
        aggregate_payload["join_group"] = group_value
        aggregate_payload["join_count"] = len(aggregate_payload[items_alias])
        aggregate_payload["join_expected_count"] = expected_count
        aggregate_payload["join_fanout_id"] = fanout_id
        aggregate_payload["join_branch_keys"] = selected_branch_keys[:expected_count]
        aggregate_payload["join_expected_branch_keys"] = expected_branch_keys
        aggregate_payload["join_missing_branch_keys"] = missing_branch_keys
        aggregate_payload["join_target_bot_id"] = target_bot_id
        # Reset fan-out scope at join boundaries. Downstream stages can opt in
        # by explicitly setting new fanout_* fields in their payload templates.
        aggregate_payload["fanout_id"] = ""
        aggregate_payload["fanout_count"] = None
        aggregate_payload["fanout_branch_key"] = ""
        aggregate_payload["fanout_expected_branch_keys"] = []
        return aggregate_payload

    def _resolve_join_expected_count(
        self,
        payload: Dict[str, Any],
        expected_field: str,
        sibling_payloads: List[Dict[str, Any]],
        expected_branch_keys: List[str],
    ) -> Optional[int]:
        if expected_branch_keys:
            return len(expected_branch_keys)

        candidates: List[int] = []
        if expected_field:
            parsed = self._coerce_positive_int(self._lookup_result_field(payload, expected_field))
            if parsed is not None:
                candidates.append(parsed)
        payload_fanout_count = self._resolve_fanout_count(payload)
        if payload_fanout_count is not None:
            candidates.append(payload_fanout_count)

        for item in sibling_payloads:
            if expected_field:
                parsed = self._coerce_positive_int(self._lookup_result_field(item, expected_field))
                if parsed is not None:
                    candidates.append(parsed)
            sibling_fanout_count = self._resolve_fanout_count(item)
            if sibling_fanout_count is not None:
                candidates.append(sibling_fanout_count)

        if not candidates:
            return None
        return max(candidates)

    def _select_join_payloads(
        self,
        sibling_payloads: List[Dict[str, Any]],
        expected_count: int,
        expected_branch_keys: List[str],
        sort_field: str,
    ) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
        missing_branch_keys: List[str] = []
        if expected_branch_keys:
            branches: Dict[str, Dict[str, Any]] = {}
            for item in sibling_payloads:
                branch_key = self._resolve_join_branch_key(item)
                if branch_key:
                    branches[branch_key] = item
            missing_branch_keys = [key for key in expected_branch_keys if key not in branches]
            selected_payloads = [branches[key] for key in expected_branch_keys if key in branches]
        else:
            selected_payloads = list(sibling_payloads)

        if sort_field:
            selected_payloads.sort(
                key=lambda item: self._sortable_join_value(self._lookup_result_field(item, sort_field))
            )

        selected_branch_keys = [self._resolve_join_branch_key(item) or "" for item in selected_payloads]
        selected_branch_keys = [key for key in selected_branch_keys if key]
        return selected_payloads[:expected_count], selected_branch_keys[:expected_count], missing_branch_keys

    def _collect_join_payloads(
        self,
        task: Task,
        trigger: Any,
        group_field: str,
        group_value: Any,
    ) -> List[Dict[str, Any]]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        event = "task_completed" if task.status == "completed" else "task_failed"
        matches: Dict[str, Tuple[Task, Dict[str, Any]]] = {}
        for candidate in self._tasks.values():
            candidate_meta = candidate.metadata or TaskMetadata()
            if candidate.bot_id != task.bot_id:
                continue
            if candidate.status != task.status:
                continue
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if metadata.orchestration_id != candidate_meta.orchestration_id:
                continue
            if metadata.project_id != candidate_meta.project_id:
                continue
            if trigger.event != event:
                continue
            if trigger.condition == "has_result" and candidate.result is None:
                continue
            if trigger.condition == "has_error" and candidate.error is None:
                continue
            if not self._trigger_matches_result(candidate, trigger):
                continue
            candidate_payload = self._build_trigger_payload(candidate, trigger)
            if not isinstance(candidate_payload, dict):
                continue
            candidate_group_value = self._lookup_result_field(candidate_payload, group_field) if group_field else None
            if group_field and candidate_group_value != group_value:
                continue
            match_key = str(candidate_meta.step_id or candidate_meta.original_task_id or candidate.id)
            existing = matches.get(match_key)
            if existing is None or self._task_order_token(candidate) >= self._task_order_token(existing[0]):
                matches[match_key] = (candidate, candidate_payload)
        return [payload for _, payload in matches.values()]

    def _fanout_step_id(self, task: Task, trigger: Any, payload: Dict[str, Any]) -> Optional[str]:
        fanout_id = self._fanout_id(task, trigger)
        if not fanout_id:
            return None
        branch_key = self._fanout_branch_key(trigger, payload)
        if not branch_key:
            return None
        return f"{fanout_id}:{branch_key}"

    def _join_step_id(self, task: Task, trigger: Any, payload: Dict[str, Any]) -> Optional[str]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        fanout_id = self._resolve_fanout_id(payload)
        normalized_fanout = self._normalize_branch_token(fanout_id, fallback="fanout") if fanout_id else "nofanout"
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        group_value = self._lookup_result_field(payload, group_field) if group_field else "__all__"
        normalized_group = self._normalize_branch_token(group_value, fallback="group")
        return f"join:{task.bot_id}:{trigger.id}:{root_id}:{normalized_fanout}:{normalized_group}"

    def _fanout_id(self, task: Task, trigger: Any) -> Optional[str]:
        fan_out_field = str(getattr(trigger, "fan_out_field", "") or "").strip()
        if not fan_out_field:
            return None
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        # Include stable parent-branch identity so nested fan-outs from sibling
        # parents do not collide on branch indexes (for example, lesson_index 0
        # for every unit builder branch).
        origin_token = self._fanout_origin_token(task)
        return f"fanout:{task.bot_id}:{trigger.id}:{root_id}:{origin_token}"

    def _fanout_origin_token(self, task: Task) -> str:
        metadata = task.metadata or TaskMetadata()
        raw_origin = (
            metadata.step_id
            or metadata.original_task_id
            or task.id
        )
        return self._normalize_branch_token(raw_origin, fallback="origin")

    def _fanout_branch_key(self, trigger: Any, payload: Dict[str, Any]) -> Optional[str]:
        index_alias = str(getattr(trigger, "fan_out_index_alias", "") or "").strip() or "item_index"
        alias = str(getattr(trigger, "fan_out_alias", "") or "").strip() or "item"
        branch_value = payload.get(index_alias)
        if branch_value is None:
            branch_value = payload.get(alias)
        if branch_value is None:
            return None
        return self._normalize_branch_token(branch_value, fallback="branch")

    def _normalize_branch_token(self, value: Any, fallback: str) -> str:
        try:
            token = json.dumps(value, sort_keys=True, default=str)
        except TypeError:
            token = str(value)
        normalized = re.sub(r"[^a-zA-Z0-9:_-]+", "-", token).strip("-")
        return normalized or fallback

    def _payload_source_chain(self, payload: Any) -> List[Dict[str, Any]]:
        # Traverse nested source_payload wrappers (up to a bounded depth) to locate
        # fan-out metadata regardless of how many trigger hops a branch has passed through.
        chain: List[Dict[str, Any]] = []
        current: Any = payload
        seen: Set[int] = set()
        for _ in range(20):
            if not isinstance(current, dict):
                break
            marker = id(current)
            if marker in seen:
                break
            seen.add(marker)
            chain.append(current)
            current = current.get("source_payload")
        return chain

    def _resolve_fanout_id(self, payload: Dict[str, Any]) -> Optional[str]:
        for node in self._payload_source_chain(payload):
            if "fanout_id" not in node:
                continue
            raw = str(node.get("fanout_id") or "").strip()
            return raw or None
        return None

    def _resolve_fanout_count(self, payload: Dict[str, Any]) -> Optional[int]:
        for node in self._payload_source_chain(payload):
            if "fanout_count" not in node:
                continue
            return self._coerce_positive_int(node.get("fanout_count"))
        return None

    def _resolve_fanout_expected_branch_keys(self, payload: Dict[str, Any]) -> List[str]:
        for node in self._payload_source_chain(payload):
            if "fanout_expected_branch_keys" not in node:
                continue
            raw = node.get("fanout_expected_branch_keys")
            if not isinstance(raw, list):
                return []
            normalized: List[str] = []
            seen: Set[str] = set()
            for item in raw:
                key = self._coerce_branch_key(item)
                if not key or key in seen:
                    continue
                seen.add(key)
                normalized.append(key)
            return normalized
        return []

    def _resolve_join_branch_key(self, payload: Dict[str, Any]) -> Optional[str]:
        for node in self._payload_source_chain(payload):
            if "fanout_branch_key" not in node:
                continue
            return self._coerce_branch_key(node.get("fanout_branch_key"))
        source_task_id = str(payload.get("source_task_id") or "").strip()
        if source_task_id:
            return self._normalize_branch_token(source_task_id, fallback="task")
        return None

    def _coerce_branch_key(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return self._normalize_branch_token(value, fallback="branch")

    def _coerce_positive_int(self, value: Any) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    async def _find_task_by_step_id(self, step_id: str, bot_id: str) -> Optional[Task]:
        await self._ensure_db()
        async with self._lock:
            for task in self._tasks.values():
                if task.bot_id != bot_id:
                    continue
                metadata = task.metadata or TaskMetadata()
                if metadata.step_id == step_id:
                    return task
        return None

    def _sortable_join_value(self, value: Any) -> Any:
        if isinstance(value, (int, float, str)):
            return value
        if value is None:
            return ""
        return json.dumps(value, sort_keys=True, default=str)

    def _task_order_token(self, task: Task) -> Tuple[str, str]:
        return (str(task.updated_at or task.created_at or ""), str(task.id))

    def _resolve_trigger_target_bot_id(self, task: Task, target_bot_id: str) -> Optional[str]:
        raw = str(target_bot_id or "").strip()
        if not raw:
            return None
        if raw == "{{source_bot_id}}":
            if isinstance(task.payload, dict):
                source_bot_id = str(task.payload.get("source_bot_id") or "").strip()
                if source_bot_id:
                    return source_bot_id
            return None
        if raw.startswith("{{") and raw.endswith("}}"):
            expr = raw[2:-2].strip()
            if expr.startswith("result.") and isinstance(task.result, dict):
                resolved = self._lookup_result_field(task.result, expr[7:].strip())
                return str(resolved).strip() or None if resolved is not None else None
            if expr.startswith("payload.") and isinstance(task.payload, dict):
                resolved = self._lookup_result_field(task.payload, expr[8:].strip())
                return str(resolved).strip() or None if resolved is not None else None
        return raw

    def _trigger_matches_result(self, task: Task, trigger: Any) -> bool:
        field = str(getattr(trigger, "result_field", "") or "").strip()
        if not field:
            return True
        if not isinstance(task.result, dict):
            return False
        actual = self._lookup_result_field(task.result, field)
        expected = getattr(trigger, "result_equals", None)
        if expected is None:
            return actual is not None
        return str(actual) == str(expected)

    def _lookup_result_field(self, data: Dict[str, Any], field: str) -> Any:
        return _lookup_nested_path(data, field)
