import asyncio
import copy
import heapq
import json
import logging
import os
import posixpath
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from control_plane.task_result_files import extract_file_candidates
from shared.bot_policy import bot_allows_repo_output
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


class _TaskPolicyViolation(Exception):
    """Policy violation with stable machine-readable reason codes.

    The code field is a stable identifier for the violation type.
    The details dict always includes 'reason_code' for machine-readable processing.
    """
    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Optional[Dict[str, Any]] = None,
        result: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "").strip() or "workflow_policy_violation"
        self.details = dict(details) if details else {}
        # Ensure reason_code is always present for machine-readable processing
        if "reason_code" not in self.details:
            self.details["reason_code"] = self.code
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


def _settings_bool(name: str, default: bool) -> bool:
    try:
        value = SettingsManager.instance().get(name, default)
    except Exception:
        return default
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        "documentation output contains broken internal markdown links",
        "documentation workstream emitted markdown files outside its assigned deliverables",
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


_PM_WORKSTREAM_DATABASE_KEYWORDS = (
    "database",
    "db ",
    " db",
    "schema",
    "migration",
    "sql",
    "table",
    "query",
    "index",
    "postgres",
    "postgresql",
    "sqlite",
    "mysql",
    "entity framework",
    "ef core",
)

_PM_WORKSTREAM_UI_KEYWORDS = (
    "frontend",
    "front-end",
    "react",
    "razor",
    ".razor",
    ".tsx",
    ".jsx",
    "blazor",
    "component",
    "page",
    "screen",
    "layout",
    "view",
    "ui ",
    " ui",
    "user interface",
    "user-facing",
)
_PM_WORKSTREAM_API_KEYWORDS = (
    "api",
    "endpoint",
    "controller",
    "route",
    "swagger",
    "openapi",
    "presigned url",
)
_PM_WORKSTREAM_SECURITY_KEYWORDS = (
    "virus",
    "scan",
    "scanner",
    "security",
    "validation",
    "mime",
    "file type",
    "file-size",
    "file size",
    "quarantine",
)
_PM_WORKSTREAM_WORKER_KEYWORDS = (
    "worker",
    "queue",
    "grading",
    "poll",
    "polling",
    "consumer",
    "background service",
    "processor",
)
_PM_WORKSTREAM_TRIGGER_KEYWORDS = (
    "webhook",
    "scheduler",
    "schedule",
    "cron",
    "callback",
    "trigger",
)
_PM_WORKSTREAM_OPERATIONS_KEYWORDS = (
    "monitor",
    "monitoring",
    "metrics",
    "telemetry",
    "instrumentation",
    "observability",
    "alert",
    "alerts",
    "logging",
    "log",
)

_PM_ASSIGNMENT_RESEARCH_STEP_CAP = 3
_PM_ASSIGNMENT_RESEARCH_STEP_SPLIT_CAP = 6
_PM_ASSIGNMENT_WORKSTREAM_STEP_CAP = 5
_PM_ASSIGNMENT_WORKSTREAM_STEP_SPLIT_CAP = 6
_PM_ASSIGNMENT_RESEARCH_REPO_LANE_KEYWORDS = (
    "repo",
    "repository",
    "code",
    "file",
    "files",
    "implementation",
    "stack",
    "runtime",
)
_PM_ASSIGNMENT_RESEARCH_DATA_LANE_KEYWORDS = (
    "data",
    "requirement",
    "requirements",
    "schema",
    "database",
    "state",
    "context",
    "acceptance",
)
_PM_ASSIGNMENT_RESEARCH_ONLINE_LANE_KEYWORDS = (
    "online",
    "external",
    "docs",
    "documentation",
    "reference",
    "references",
    "standard",
    "standards",
    "current",
    "latest",
)
_PM_ASSIGNMENT_RESEARCH_SPLIT_MARKERS = (
    "part ",
    "chunk ",
    "batch ",
    "shard ",
    "segment ",
    "slice ",
    "continuation",
    "follow-up",
    "overflow",
    "1/",
    "2/",
    "3/",
)
_PM_ASSIGNMENT_RESEARCH_INCLUDED_STEP_KINDS = (
    "specification",
    "research",
    "analysis",
    "discovery",
    "investigation",
)
_PM_ASSIGNMENT_RESEARCH_EXCLUDED_STEP_KINDS = (
    "implementation",
    "repo_change",
    "coding",
    "execution",
    "validation",
    "review",
    "qa",
    "test_execution",
    "finalization",
    "orchestration_finalization",
    "orchestration_finalisation",
)
_PM_ASSIGNMENT_DEFAULT_RESEARCH_STEP_SPECS = (
    {
        "lane": "repo",
        "id": "step_1_code",
        "title": "Repository implementation patterns",
        "instruction": (
            "Inspect the repository directly for stack, runtime constraints, nearby implementations, existing "
            "components, file-structure expectations, and coding/test patterns relevant to the request."
        ),
        "acceptance_criteria": [
            "Repo implementation patterns and runtime constraints are identified from concrete files",
            "Code and test conventions are grounded in the actual repository",
        ],
        "deliverables": [
            "Repo/runtime constraints summary",
            "Existing implementation inventory",
        ],
        "evidence_requirements": [
            "Concrete repo-profile or existing-file evidence",
            "Relevant file/path inventory tied to the requested work",
        ],
        "quality_gates": ["No unsupported stack or runtime assumptions are introduced"],
    },
    {
        "lane": "data",
        "id": "step_1_data",
        "title": "Requirements and data context",
        "instruction": (
            "Use the assignment request, project context, vault knowledge, and available data context to extract "
            "requirements, acceptance criteria, dependencies, prior decisions, and schema/state considerations."
        ),
        "acceptance_criteria": [
            "Requirements and prior project constraints are captured clearly",
            "Relevant data, schema, or state-management concerns are identified when applicable",
        ],
        "deliverables": [
            "Requirements summary artifact",
            "Project and data-context summary",
        ],
        "evidence_requirements": [
            "Requirements artifact with acceptance criteria",
            "Concrete project, vault, or data-context evidence",
        ],
        "quality_gates": ["No prior project or data constraints are ignored or contradicted"],
    },
    {
        "lane": "online",
        "id": "step_1_online",
        "title": "External docs and standards",
        "instruction": (
            "Research external documentation, standards, or online references only when the assignment requires it. "
            "If no external research is needed, state that explicitly instead of inventing it."
        ),
        "acceptance_criteria": [
            "External references are used only when necessary",
            "Any online research is relevant, current, and tied back to the requested work",
        ],
        "deliverables": [
            "External research summary or explicit no-external-research note",
        ],
        "evidence_requirements": [
            "Current external reference evidence when used",
            "Explicit statement when external research is not required",
        ],
        "quality_gates": ["No unnecessary or unsupported external assumptions are introduced"],
    },
)
_PM_ASSIGNMENT_WORKSTREAM_SPLIT_MARKERS = (
    "part ",
    "chunk ",
    "batch ",
    "shard ",
    "segment ",
    "slice ",
    "overflow",
    "1/",
    "2/",
    "3/",
)
_PM_WORKSTREAM_UI_PATH_HINTS = (
    ".tsx",
    ".jsx",
    ".razor",
    ".cshtml",
    "/components/",
    "/component/",
    "/pages/",
    "/page/",
    "/views/",
    "/view/",
    "/frontend/",
    "/ui/",
)


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
    """Extract deliverable file paths from a task payload, walking the full source_payload chain.

    Checks both top-level ``deliverables`` fields and nested ``workstream.deliverables`` at
    every depth so trigger-spawned tasks with multi-level payloads are handled correctly.
    PM plan step tasks store deliverables at the top level; trigger-spawned workstream tasks
    nest them inside a ``workstream`` sub-dict.  Both structures are handled here.
    """
    deliverables: List[str] = []
    seen_ids: Set[int] = set()
    for current in _iter_payload_chain(payload):
        current_id = id(current)
        if current_id in seen_ids:
            continue
        seen_ids.add(current_id)
        # Top-level deliverables (PM plan step tasks store deliverables directly here)
        deliverables.extend(_normalize_string_list(current.get("deliverables")))
        # Workstream sub-dict (trigger-spawned workstream tasks use this structure)
        workstream = current.get("workstream")
        if isinstance(workstream, dict):
            ws_id = id(workstream)
            if ws_id not in seen_ids:
                seen_ids.add(ws_id)
                deliverables.extend(_normalize_string_list(workstream.get("deliverables")))
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
    # Execution artifacts (test reports, coverage files, .xml/.json results) are NOT
    # documentation files even though they may have .txt or other doc-like extensions.
    # Exclude them before checking whether all remaining files are documentation.
    repo_files = [
        item for item in workstream_items
        if _looks_like_repo_file(item) and not _is_assignment_execution_artifact_file(item)
    ]
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


def _payload_assignment_scope(payload: Dict[str, Any]) -> Dict[str, Any]:
    for current in _iter_payload_chain(payload):
        scope = current.get("assignment_scope")
        if isinstance(scope, dict):
            return scope
    return {}


def _payload_is_docs_only_request(payload: Dict[str, Any]) -> bool:
    scope = _payload_assignment_scope(payload)
    if bool(scope.get("docs_only", False)):
        return True
    return _payload_requests_docs_only_outputs(payload)


def _payload_requests_docs_only_outputs(payload: Dict[str, Any]) -> bool:
    scope = _payload_assignment_scope(payload)
    if bool(scope.get("docs_only", False)):
        return True
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


def _result_repo_output_candidate_paths(result: Any) -> List[str]:
    """Extract repo file paths from result artifacts that represent ownership claims.

    Excludes execution artifacts (test reports, coverage, logs) and probable test files
    so that validation-only evidence is not misclassified as repo ownership.
    """
    paths: List[str] = []
    seen: Set[str] = set()
    for candidate in extract_file_candidates(result):
        path = str(candidate.get("path") or "").strip().replace("\\", "/").strip("`")
        if not path or path in seen:
            continue
        seen.add(path)
        if not _looks_like_repo_file(path):
            continue
        # Exclude execution artifacts (reports/, coverage/, test_logs/, .xml/.html/.txt/.json/.log)
        if _is_assignment_execution_artifact_file(path):
            continue
        # Exclude probable test files - validation roles may reference test files as evidence
        # but these are not repo ownership claims
        if _is_probable_test_file(path):
            continue
        paths.append(path)
    return paths


def _artifact_repo_paths(value: Any) -> List[str]:
    paths: List[str] = []
    seen: Set[str] = set()
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    for item in items:
        raw_path = ""
        if isinstance(item, dict):
            raw_path = str(item.get("path") or "").strip()
        elif isinstance(item, str):
            raw_path = item.strip()
        path = raw_path.replace("\\", "/").strip("`")
        if not path or path in seen:
            continue
        seen.add(path)
        if not _looks_like_repo_file(path):
            continue
        if _is_assignment_execution_artifact_file(path):
            continue
        paths.append(path)
    return paths


def _docs_only_unexpected_document_repo_paths(payload: Dict[str, Any], result: Any) -> List[str]:
    if _assignment_step_kind(payload) != "repo_change" or not _payload_is_docs_only_request(payload):
        return []
    expected_docs = {
        path
        for path in _assignment_expected_repo_files(payload)
        if _is_documentation_like_repo_file(path)
    }
    if not expected_docs:
        return []
    unexpected: List[str] = []
    seen: Set[str] = set()
    for candidate in extract_file_candidates(result):
        path = str(candidate.get("path") or "").strip().replace("\\", "/").strip("`")
        if not path or path in seen:
            continue
        seen.add(path)
        if not _looks_like_repo_file(path):
            continue
        if _is_assignment_execution_artifact_file(path):
            continue
        if not _is_documentation_like_repo_file(path):
            continue
        if path in expected_docs:
            continue
        unexpected.append(path)
    return unexpected


def _docs_only_non_writer_branch_may_reference_upstream_docs(
    payload: Dict[str, Any],
    result: Any,
) -> bool:
    if not _payload_is_docs_only_request(payload):
        return False
    step_kind = _assignment_step_kind(payload)
    if step_kind not in {"specification", "planning", "test_execution", "review"}:
        return False
    repo_output_paths = _result_repo_output_candidate_paths(result)
    if not repo_output_paths:
        return False
    allowed_paths = {
        path
        for path in _assignment_expected_repo_files(payload)
        if _is_documentation_like_repo_file(path)
    }
    if step_kind == "planning" and isinstance(result, dict):
        workstreams = result.get("implementation_workstreams")
        if isinstance(workstreams, list):
            for workstream in workstreams:
                if not isinstance(workstream, dict):
                    continue
                allowed_paths.update(
                    path
                    for path in _artifact_repo_paths(workstream.get("deliverables"))
                    if _is_documentation_like_repo_file(path)
                )
        allowed_paths.update(
            path
            for path in _artifact_repo_paths(_result_explicit_artifacts(result))
            if _is_documentation_like_repo_file(path)
        )
    else:
        allowed_paths.update(
            path
            for path in _artifact_repo_paths(payload.get("upstream_artifacts"))
            if _is_documentation_like_repo_file(path)
        )
    return bool(allowed_paths) and all(path in allowed_paths for path in repo_output_paths)


def _docs_only_workstream_violations(result: Any) -> List[str]:
    if not isinstance(result, dict):
        return []
    workstreams = result.get("implementation_workstreams")
    if not isinstance(workstreams, list):
        return []
    violations: List[str] = []
    for workstream in workstreams:
        if not isinstance(workstream, dict):
            continue
        title = str(workstream.get("title") or "").strip() or "unnamed workstream"
        for item in _normalize_string_list(workstream.get("deliverables")):
            normalized = str(item or "").strip().replace("\\", "/").strip("`")
            if not normalized:
                continue
            if _looks_like_repo_file(normalized) and not _is_documentation_like_repo_file(normalized):
                violations.append(f"{title}: {normalized}")
    return violations


def _docs_only_canonical_workstream_mismatch(
    payload: Dict[str, Any],
    result: Any,
) -> Optional[Dict[str, Any]]:
    if _assignment_step_kind(payload) != "planning" or not isinstance(result, dict):
        return None
    canonical_paths = [
        str(item or "").strip().replace("\\", "/").strip("`")
        for item in _normalize_string_list(payload.get("canonical_doc_paths"))
        if str(item or "").strip()
    ]
    if not canonical_paths:
        return None
    workstreams = result.get("implementation_workstreams")
    if not isinstance(workstreams, list) or not workstreams:
        return {
            "expected_paths": canonical_paths,
            "actual_paths": [],
            "exact_path_errors": ["implementation_workstreams missing or empty"],
        }

    actual_paths: List[str] = []
    exact_path_errors: List[str] = []
    for index, workstream in enumerate(workstreams):
        if not isinstance(workstream, dict):
            exact_path_errors.append(f"workstream {index + 1}: not an object")
            continue
        title = str(workstream.get("title") or "").strip() or f"workstream {index + 1}"
        path = str(workstream.get("path") or "").strip().replace("\\", "/").strip("`")
        deliverables = [
            str(item or "").strip().replace("\\", "/").strip("`")
            for item in _normalize_string_list(workstream.get("deliverables"))
            if str(item or "").strip()
        ]
        if path:
            actual_paths.append(path)
        else:
            exact_path_errors.append(f"{title}: missing path")
        if len(deliverables) != 1:
            exact_path_errors.append(f"{title}: deliverables must contain exactly one markdown path")
        elif path and deliverables[0] != path:
            exact_path_errors.append(f"{title}: deliverables[0] must match path")

    if actual_paths != canonical_paths or exact_path_errors:
        return {
            "expected_paths": canonical_paths,
            "actual_paths": actual_paths,
            "exact_path_errors": exact_path_errors,
        }
    return None


def _assignment_scope_alignment_error(payload: Dict[str, Any], result: Any) -> str:
    scope = _payload_assignment_scope(payload)
    if not scope or not isinstance(result, dict):
        return ""

    fragments: List[str] = []
    for key in ("summary", "handoff_notes", "architecture", "implementation_plan", "findings", "evidence", "requirements", "risks"):
        value = result.get(key)
        if isinstance(value, list):
            fragments.extend(str(item or "").strip() for item in value[:16] if str(item or "").strip())
        elif value not in (None, ""):
            fragments.append(str(value).strip())

    workstreams = result.get("implementation_workstreams")
    if isinstance(workstreams, list):
        for workstream in workstreams[:12]:
            if not isinstance(workstream, dict):
                continue
            for key in ("title", "instruction", "test_strategy"):
                value = str(workstream.get(key) or "").strip()
                if value:
                    fragments.append(value)
            fragments.extend(_normalize_string_list(workstream.get("deliverables"))[:8])

    for artifact in _result_explicit_artifacts(result)[:12]:
        label = str(artifact.get("label") or "").strip()
        path = str(artifact.get("path") or "").strip()
        content = str(artifact.get("content") or "").strip()
        if label:
            fragments.append(label)
        if path:
            fragments.append(path)
        if content:
            fragments.append(content[:800])

    combined = "\n".join(fragment for fragment in fragments if fragment).lower()
    if not combined:
        return ""

    if bool(scope.get("avoid_external_apis", False)):
        dependency_patterns = (
            r"\buse\s+the\s+desmos\s+api\b",
            r"\brely\s+on\s+the\s+desmos\s+api\b",
            r"\bintegrat(?:e|ing)\s+the\s+desmos\s+api\b",
            r"\buse\s+the\s+geogebra\s+api\b",
            r"\brely\s+on\s+the\s+geogebra\s+api\b",
            r"\brely\s+on\s+an?\s+external\s+api\b",
            r"\buse\s+an?\s+external\s+api\b",
            r"\bintegrat(?:e|ing)\s+an?\s+external\s+api\b",
            r"\brely\s+on\s+an?\s+third-?party\s+api\b",
            r"\buse\s+an?\s+third-?party\s+api\b",
            r"\bintegrat(?:e|ing)\s+an?\s+third-?party\s+api\b",
            r"\brely\s+on\s+an?\s+external\s+product\b",
            r"\buse\s+an?\s+external\s+product\b",
            r"\bwire\s+the\s+desmos\s+api\b",
            r"\bwire\s+an?\s+external\s+api\b",
        )
        allowed_context_patterns = (
            r"\bwithout\s+external\s+api(?:\s+calls?)?\b",
            r"\bwithout\s+the\s+desmos\s+api\b",
            r"\bavoid\s+external\s+api(?:\s+calls?)?\b",
            r"\bavoid\s+the\s+desmos\s+api\b",
            r"\bdo\s+not\s+rely\s+on\s+external\s+api(?:\s+calls?)?\b",
            r"\bdo\s+not\s+use\s+external\s+api(?:\s+calls?)?\b",
            r"\bno\s+external\s+api(?:\s+calls?)?\b",
            r"\bhost(?:ed)?\s+locally\b",
            r"\boffline\s+hosting\b",
            r"\blocal\s+hosting\b",
            r"\bopen-source\b",
        )
        positive_match = any(re.search(pattern, combined, re.IGNORECASE) for pattern in dependency_patterns)
        allowed_context = any(re.search(pattern, combined, re.IGNORECASE) for pattern in allowed_context_patterns)
        if positive_match and not allowed_context:
            return (
                "Assignment scope requires an in-house / no-external-API approach, "
                "but the output proposes an external product or third-party API dependency."
            )

    return ""


def _looks_like_assignment_test_execution_payload(payload: Dict[str, Any]) -> bool:
    # Role hint takes precedence - if explicitly tester/qa, treat as test execution
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    if role_hint in {"tester", "qa"}:
        # Docs-only workstreams don't require test execution even for tester roles.
        if _is_docs_only_workstream_validation(payload):
            return False
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
    seen: Set[str] = set()

    def _add_candidate(value: Any) -> None:
        normalized = str(value or "").strip().replace("\\", "/").strip("`")
        if not normalized or normalized in seen:
            return
        if _looks_like_repo_file(normalized):
            seen.add(normalized)
            files.append(normalized)

    _add_candidate(payload.get("path"))
    workstream = payload.get("workstream") if isinstance(payload.get("workstream"), dict) else {}
    _add_candidate(workstream.get("path"))
    for item in _normalize_string_list(payload.get("deliverables")):
        _add_candidate(item)
    for item in _normalize_string_list(workstream.get("deliverables")):
        _add_candidate(item)
    return files


def _docs_only_declared_markdown_paths(payload: Dict[str, Any]) -> Set[str]:
    declared: Set[str] = set()
    canonical_paths: Set[str] = set()

    def _add_candidate(value: Any) -> None:
        normalized = str(value or "").strip().replace("\\", "/").strip("`")
        if not normalized or not _looks_like_repo_file(normalized):
            return
        if not _is_documentation_like_repo_file(normalized):
            return
        declared.add(normalized)

    for item in _normalize_string_list(payload.get("canonical_doc_paths")):
        normalized = str(item or "").strip().replace("\\", "/").strip("`")
        if not normalized or not _looks_like_repo_file(normalized):
            continue
        if not _is_documentation_like_repo_file(normalized):
            continue
        canonical_paths.add(normalized)

    for item in _assignment_expected_repo_files(payload):
        _add_candidate(item)
    for item in _artifact_repo_paths(payload.get("upstream_artifacts")):
        _add_candidate(item)

    workstream = payload.get("workstream") if isinstance(payload.get("workstream"), dict) else {}
    for item in _normalize_string_list(workstream.get("deliverables")):
        _add_candidate(item)
    _add_candidate(workstream.get("path"))

    declared.update(canonical_paths)

    for item in _normalize_string_list(payload.get("planned_doc_paths")):
        normalized = str(item or "").strip().replace("\\", "/").strip("`")
        if canonical_paths and normalized not in canonical_paths:
            continue
        _add_candidate(normalized)

    planned_workstreams = payload.get("planned_workstreams")
    if isinstance(planned_workstreams, list):
        for item in planned_workstreams:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip().replace("\\", "/").strip("`")
            if not canonical_paths or not path or path in canonical_paths:
                _add_candidate(path)
            for deliverable in _normalize_string_list(item.get("deliverables")):
                normalized = str(deliverable or "").strip().replace("\\", "/").strip("`")
                if canonical_paths and normalized not in canonical_paths:
                    continue
                _add_candidate(normalized)

    return declared


def _non_writer_step_repo_deliverables(payload: Dict[str, Any]) -> List[str]:
    """Return repo-file paths that a non-writer step is not allowed to own.

    Non-writer roles (tester, qa, reviewer, security, security-reviewer, researcher, analyst)
    are strictly validation-only and cannot own ANY repo-file outputs. All deliverables from
    these roles must be validation-only evidence (test reports, coverage files, security findings).
    This applies regardless of docs-only status - even .md documentation files are prohibited
    as repo ownership claims for non-writer roles.

    Writer roles in docs-only workstreams may only produce documentation files (.md).
    """
    step_kind = _assignment_step_kind(payload)
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    is_docs_only = _payload_is_docs_only_request(payload)

    # Non-writer roles cannot own repo deliverables in any step
    non_writer_roles = {"tester", "qa", "reviewer", "security", "security-reviewer", "researcher", "analyst"}
    if role_hint in non_writer_roles:
        # These roles are strictly validation-only; ANY repo-file deliverable is prohibited
        # Only execution artifacts (test reports, coverage, logs) are allowed as evidence
        repo_paths: List[str] = []
        seen: Set[str] = set()
        for item in _normalize_string_list(payload.get("deliverables")):
            normalized = str(item or "").strip().replace("\\", "/").strip("`")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if not _looks_like_repo_file(normalized):
                continue
            # Only allow validation-only artifacts (execution artifacts, test files)
            if _is_assignment_execution_artifact_file(normalized):
                continue
            if _is_probable_test_file(normalized):
                continue
            # Non-writer roles cannot own ANY repo files - all outputs must be validation-only evidence
            # This applies even to .md documentation files in docs-only workstreams
            repo_paths.append(normalized)
        return repo_paths

    # For specification/planning steps, only documentation files are allowed
    if step_kind not in {"specification", "planning"}:
        return []

    repo_paths = []
    seen: Set[str] = set()
    for item in _normalize_string_list(payload.get("deliverables")):
        normalized = str(item or "").strip().replace("\\", "/").strip("`")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if not _looks_like_repo_file(normalized):
            continue
        if _is_assignment_execution_artifact_file(normalized):
            continue
        # For docs-only workstreams, even writer roles can only produce documentation
        if is_docs_only and not _is_documentation_like_repo_file(normalized):
            repo_paths.append(normalized)
        elif not is_docs_only:
            # Non-docs-only specification/planning: only non-documentation files are prohibited
            if not _is_documentation_like_repo_file(normalized):
                repo_paths.append(normalized)
    return repo_paths


def _result_explicit_artifacts(result: Any) -> List[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict)]


def _contains_destructive_sql(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if re.search(r"(?im)\b(delete|drop|truncate)\b", normalized):
        return True
    return bool(
        re.search(
            r"(?ims)\balter\s+table\b.*?\b(drop\s+(?:column|constraint)|alter\s+column\b.*?\btype\b)",
            normalized,
        )
    )


def _database_result_repo_candidates(result: Any) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for item in _result_explicit_artifacts(result):
        path = str(item.get("path") or "").strip().replace("\\", "/").strip("`")
        content = str(item.get("content") or "")
        if not path or not _looks_like_repo_file(path):
            continue
        key = (path, content)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"path": path, "content": content})
    for item in extract_file_candidates(result):
        path = str(item.get("path") or "").strip().replace("\\", "/").strip("`")
        content = str(item.get("content") or "")
        if not path or not _looks_like_repo_file(path):
            continue
        key = (path, content)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"path": path, "content": content})
    return candidates


def _database_result_contract_failure(result: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    repo_candidates = _database_result_repo_candidates(result)
    raw_text = str(result.get("raw_text") or result.get("content") or result.get("output") or "").strip()
    status = str(result.get("status") or result.get("outcome") or "").strip().lower()
    sql_candidates = [
        item
        for item in repo_candidates
        if str(item.get("path") or "").strip().lower().endswith(".sql")
    ]
    unexpected_repo_paths = [
        str(item.get("path") or "").strip()
        for item in repo_candidates
        if not str(item.get("path") or "").strip().lower().endswith(".sql")
    ]
    if unexpected_repo_paths:
        preview = ", ".join(unexpected_repo_paths[:5])
        return {
            "code": "database_stage_unexpected_repo_outputs",
            "message": (
                "pm-database-engineer must emit only one canonical SQL migration script; "
                f"unexpected repo outputs were returned: {preview}."
            ),
            "details": {
                "reason_code": "database_stage_unexpected_repo_outputs",
                "paths": unexpected_repo_paths,
            },
        }
    if len(sql_candidates) > 1:
        preview = ", ".join(str(item.get("path") or "").strip() for item in sql_candidates[:5])
        return {
            "code": "database_stage_duplicate_sql_artifacts",
            "message": (
                "pm-database-engineer returned multiple SQL migration artifacts. "
                f"Return exactly one canonical SQL script: {preview}."
            ),
            "details": {
                "reason_code": "database_stage_duplicate_sql_artifacts",
                "paths": [str(item.get("path") or "").strip() for item in sql_candidates],
            },
        }
    if not sql_candidates and re.search(r"(?im)\b(create|alter|insert|update|delete|drop|truncate)\b", raw_text):
        return {
            "code": "database_stage_missing_canonical_sql_artifact",
            "message": (
                "pm-database-engineer emitted SQL content without a canonical `.sql` migration artifact. "
                "Return exactly one SQL script artifact."
            ),
            "details": {
                "reason_code": "database_stage_missing_canonical_sql_artifact",
            },
        }
    if not sql_candidates and status in {"pass", "completed", "complete"}:
        return {
            "code": "database_stage_missing_canonical_sql_artifact",
            "message": (
                "pm-database-engineer completed without returning the required canonical `.sql` migration artifact. "
                "Return exactly one SQL script artifact, or return skip/not_applicable when no database change is needed."
            ),
            "details": {
                "reason_code": "database_stage_missing_canonical_sql_artifact",
            },
        }
    return None


def _database_result_contains_destructive_sql(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    for item in _database_result_repo_candidates(result):
        content = str(item.get("content") or "")
        path = str(item.get("path") or "").strip().lower()
        if _contains_destructive_sql(content):
            return True
        if path.endswith(".sql") and _contains_destructive_sql(content):
            return True
    for candidate in extract_file_candidates(result):
        path = str(candidate.get("path") or "").strip().lower()
        content = str(candidate.get("content") or "")
        if path.endswith(".sql") and _contains_destructive_sql(content):
            return True
    raw_text = str(result.get("raw_text") or result.get("content") or "").strip()
    if _contains_destructive_sql(raw_text):
        return True
    return False


def _strip_repo_output_claims_for_deny_policy(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    normalized = dict(result)
    stripped_paths: List[str] = []
    explicit_artifacts = normalized.get("artifacts")
    if isinstance(explicit_artifacts, list):
        kept_artifacts: List[Any] = []
        for item in explicit_artifacts:
            if not isinstance(item, dict):
                kept_artifacts.append(item)
                continue
            path = str(item.get("path") or "").strip().replace("\\", "/").strip("`")
            if (
                path
                and _looks_like_repo_file(path)
                and not _is_assignment_execution_artifact_file(path)
                and not _is_probable_test_file(path)
            ):
                stripped_paths.append(path)
                continue
            kept_artifacts.append(item)
        normalized["artifacts"] = kept_artifacts
    for field_name in ("files_touched", "changed_files", "created_files", "modified_files"):
        value = normalized.get(field_name)
        if not isinstance(value, list):
            continue
        kept_items: List[Any] = []
        for item in value:
            path = str(item or "").strip().replace("\\", "/").strip("`")
            if (
                path
                and _looks_like_repo_file(path)
                and not _is_assignment_execution_artifact_file(path)
                and not _is_probable_test_file(path)
            ):
                stripped_paths.append(path)
                continue
            kept_items.append(item)
        normalized[field_name] = kept_items
    if stripped_paths:
        existing_notes = normalized.get("normalization_notes")
        if not isinstance(existing_notes, list):
            existing_notes = []
        unique_paths = list(dict.fromkeys(stripped_paths))
        existing_notes.append(
            "Stripped repo-output claims from a deny-policy bot result: " + ", ".join(unique_paths[:10])
        )
        normalized["normalization_notes"] = existing_notes
    return normalized


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


def _extract_markdown_link_targets(content: str) -> List[str]:
    if not content:
        return []
    targets: List[str] = []
    for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", str(content)):
        target = str(match.group(1) or "").strip()
        if not target:
            continue
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if not target:
            continue
        targets.append(target)
    return targets


def _normalize_markdown_link_target(target: str) -> Optional[str]:
    normalized = str(target or "").strip()
    if not normalized:
        return None
    normalized = normalized.split(maxsplit=1)[0].strip()
    normalized = normalized.split("#", 1)[0].strip()
    normalized = normalized.split("?", 1)[0].strip()
    if not normalized or normalized.startswith("#"):
        return None
    lowered = normalized.lower()
    if "://" in normalized or lowered.startswith(("mailto:", "data:", "javascript:")):
        return None
    if lowered.startswith("/"):
        return normalized.strip("/").replace("\\", "/")
    return normalized.replace("\\", "/")


def _resolve_markdown_relative_path(base_path: str, target: str) -> Optional[str]:
    normalized = _normalize_markdown_link_target(target)
    if not normalized:
        return None
    if not normalized.lower().endswith(".md"):
        return None
    base = PurePosixPath(str(base_path or "").replace("\\", "/"))
    if not str(base):
        return None
    base_dir = str(base.parent).replace("\\", "/").strip("/")
    if normalized.startswith("/"):
        resolved = normalized.strip("/")
    else:
        combined = normalized if not base_dir else f"{base_dir}/{normalized}"
        resolved = posixpath.normpath(combined).strip("/")
    if not resolved or resolved.startswith("../") or "/../" in f"/{resolved}/":
        return None
    return resolved


def _docs_markdown_artifacts(items: Any) -> List[Dict[str, str]]:
    artifacts: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return artifacts
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            label_path = str(item.get("label") or item.get("name") or "").strip().replace("\\", "/").strip("`")
            if _looks_like_repo_file(label_path):
                path = label_path
        content = str(item.get("content") or "")
        if not path or not content or not path.lower().endswith(".md"):
            continue
        artifacts.append({"path": path, "content": content})
    return artifacts


def _docs_only_broken_markdown_links_from_artifacts(
    items: Any,
    *,
    allowed_paths: Set[str] | None = None,
) -> List[str]:
    artifacts = _docs_markdown_artifacts(items)
    if not artifacts:
        return []
    artifact_paths = {item["path"] for item in artifacts}
    allowed = {str(item).strip().replace("\\", "/") for item in (allowed_paths or set()) if str(item).strip()}
    broken: List[str] = []
    seen: Set[str] = set()
    for item in artifacts:
        base_path = item["path"]
        for target in _extract_markdown_link_targets(item["content"]):
            resolved = _resolve_markdown_relative_path(base_path, target)
            if not resolved:
                continue
            if resolved in artifact_paths:
                continue
            if resolved in allowed:
                continue
            if Path(resolved).exists():
                continue
            marker = f"{base_path} -> {target}"
            if marker in seen:
                continue
            seen.add(marker)
            broken.append(marker)
    return broken


_DOC_PLACEHOLDER_MARKERS = (
    "... (full content omitted for brevity) ...",
    "full content omitted for brevity",
    "content omitted for brevity",
    "omitted for brevity",
    "... omitted for brevity ...",
)


def _docs_only_placeholder_markdown_artifacts(items: Any) -> List[str]:
    artifacts = _docs_markdown_artifacts(items)
    if not artifacts:
        return []
    placeholders: List[str] = []
    seen: Set[str] = set()
    for item in artifacts:
        path = str(item.get("path") or "").strip()
        content = str(item.get("content") or "")
        lowered = content.lower()
        if not path or path in seen:
            continue
        if any(marker in lowered for marker in _DOC_PLACEHOLDER_MARKERS):
            seen.add(path)
            placeholders.append(path)
    return placeholders


def _synthesize_docs_only_repo_change_contract_result(
    task: Task,
    result: Any,
    *,
    raw_text: str = "",
) -> Optional[Dict[str, Any]]:
    payload = task.payload if isinstance(task.payload, dict) else {}
    if _assignment_step_kind(payload) != "repo_change" or not _payload_is_docs_only_request(payload):
        return None

    expected_files = [
        path
        for path in _assignment_expected_repo_files(payload)
        if _is_documentation_like_repo_file(path)
    ]
    explicit_artifacts = _docs_markdown_artifacts(_result_explicit_artifacts(result))
    extracted_candidates = [
        {
            "path": str(item.get("path") or "").strip().replace("\\", "/"),
            "content": str(item.get("content") or ""),
        }
        for item in extract_file_candidates(result)
        if _is_documentation_like_repo_file(str(item.get("path") or "").strip().replace("\\", "/"))
        and str(item.get("content") or "").strip()
    ]

    artifacts: List[Dict[str, str]] = []
    seen_paths: Set[str] = set()
    for item in explicit_artifacts + extracted_candidates:
        path = item["path"]
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        artifacts.append({"path": path, "content": item["content"]})

    if not artifacts and len(expected_files) == 1 and str(raw_text or "").strip():
        artifacts = [{"path": expected_files[0], "content": str(raw_text).strip()}]
        seen_paths.add(expected_files[0])

    if not artifacts:
        return None

    files_touched = [item["path"] for item in artifacts]
    titles = _normalize_string_list(payload.get("deliverables")) or _normalize_string_list(payload.get("title"))
    summary_prefix = "Created requested documentation deliverable"
    if len(files_touched) > 1:
        summary_prefix = "Created requested documentation deliverables"
    return {
        "status": "complete",
        "change_summary": [
            f"{summary_prefix}: {', '.join(files_touched[:5])}"
        ],
        "files_touched": files_touched,
        "artifacts": artifacts,
        "risks": [
            "Result was normalized from a partial docs-only coder response; downstream validation should verify markdown structure and internal references carefully."
        ],
        "handoff_notes": (
            "Documentation artifacts were recovered into the repo-change contract. "
            "Tester should verify front-matter, internal links, path consistency, and scope alignment against the original assignment."
        ),
    }


def _assignment_result_is_skip(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    outcome = str(result.get("outcome") or "").strip().lower()
    failure_type = str(result.get("failure_type") or "").strip().lower()
    return outcome == "skip" or failure_type in {"skip", "not_applicable", "not-applicable", "n/a"}


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


def _filter_assignment_languages_to_repo_runtime(languages: List[str], root: Path, *, allowed_extra: List[str] | None = None) -> List[str]:
    repo_languages = _assignment_repo_runtime_languages(root, allowed_extra=allowed_extra)
    if not repo_languages:
        return languages
    return [language for language in languages if language in repo_languages]


def _assignment_execution_language(*, applied_paths: List[str], test_files: List[str], root: Path, allowed_extra: List[str] | None = None) -> str:
    languages = _assignment_execution_languages(applied_paths=applied_paths, test_files=test_files, root=root, allowed_extra=allowed_extra)
    if languages:
        return languages[0]
    repo_languages = _assignment_repo_runtime_languages(root, allowed_extra=allowed_extra)
    if repo_languages:
        return repo_languages[0]
    return "python"


def _assignment_execution_languages(*, applied_paths: List[str], test_files: List[str], root: Path, allowed_extra: List[str] | None = None) -> List[str]:
    # Language detection prioritises test files so that a single-language test suite is not
    # overridden by source files written in a different language from the same workspace
    # (e.g. .cs source files should not push Python test files onto dotnet test).
    # Applied source files are used as a secondary signal when there are no test files.
    # Repo-runtime filtering is always applied so that introducing a completely foreign
    # runtime (e.g. Python tests in a pure .NET repo) is caught and raised as an error.
    test_lower = [str(p or "").strip().lower() for p in test_files if str(p or "").strip()]
    source_lower = [str(p or "").strip().lower() for p in applied_paths if str(p or "").strip()]
    # Build candidate languages from test files alone first; fall back to source files only
    # when no test-file signal exists.  This prevents cross-language source pollution.
    primary = test_lower if test_lower else source_lower
    languages: List[str] = []
    if any(p.endswith(".py") for p in primary):
        languages.append("python")
    if any(p.endswith((".ts", ".tsx", ".js", ".jsx")) for p in primary):
        languages.append("node")
    if any(p.endswith(".cs") for p in primary):
        languages.append("dotnet")
    if any(p.endswith(".go") for p in primary):
        languages.append("go")
    if any(p.endswith(".rs") for p in primary):
        languages.append("rust")
    if any(p.endswith((".cpp", ".cc", ".cxx", ".hpp", ".h")) for p in primary):
        languages.append("cpp")
    if languages:
        return _filter_assignment_languages_to_repo_runtime(languages, root=root, allowed_extra=allowed_extra)
    repo_languages = _assignment_repo_runtime_languages(root, allowed_extra=allowed_extra)
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


def _assignment_validation_failure(task: Task, result: Any) -> Optional[Dict[str, Any]]:
    metadata = task.metadata
    if metadata is None:
        return None
    source = str(metadata.source or "").strip().lower()
    if source not in {"chat_assign", "auto_retry"} and not metadata.orchestration_id:
        return None
    payload = task.payload if isinstance(task.payload, dict) else {}
    role_hint = str(payload.get("role_hint") or "").strip().lower()
    step_kind = _assignment_step_kind(payload)
    evidence_requirements = (
        _normalize_string_list(payload.get("evidence_requirements"))
        or _normalize_string_list(payload.get("quality_gates"))
    )
    text = _extract_result_output_text(result).strip()
    lowered = text.lower()

    if _payload_is_docs_only_request(payload):
        docs_only_workstream_violations = _docs_only_workstream_violations(result)
        if docs_only_workstream_violations:
            preview = ", ".join(docs_only_workstream_violations[:5])
            return {
                "code": "docs_only_unexpected_deliverables",
                "message": (
                    "Assignment explicitly requested documentation-only markdown outputs, "
                    "but the planning result created non-document implementation workstreams: "
                    f"{preview}."
                ),
                "details": {
                    "step_kind": step_kind,
                    "violations": docs_only_workstream_violations,
                    "reason_code": "docs_only_non_doc_workstream",
                },
            }
        canonical_workstream_mismatch = _docs_only_canonical_workstream_mismatch(payload, result)
        if canonical_workstream_mismatch:
            preview = ", ".join(canonical_workstream_mismatch["actual_paths"][:5]) or "no valid workstream paths"
            return {
                "code": "docs_only_canonical_suite_mismatch",
                "message": (
                    "Documentation planning result did not honor the required canonical markdown suite. "
                    f"Actual workstream paths: {preview}."
                ),
                "details": {
                    "step_kind": step_kind,
                    "reason_code": "docs_only_canonical_suite_mismatch",
                    "expected_paths": canonical_workstream_mismatch["expected_paths"],
                    "actual_paths": canonical_workstream_mismatch["actual_paths"],
                    "exact_path_errors": canonical_workstream_mismatch["exact_path_errors"],
                },
            }
        unexpected_paths = _result_non_document_repo_paths(result)
        if unexpected_paths:
            preview = ", ".join(unexpected_paths[:5])
            return {
                "code": "docs_only_unexpected_deliverables",
                "message": (
                    "Assignment explicitly requested documentation-only markdown outputs, "
                    f"but generated non-document repo files: {preview}."
                ),
                "details": {
                    "step_kind": step_kind,
                    "paths": unexpected_paths,
                    "reason_code": "docs_only_non_doc_file",
                },
            }
        unexpected_doc_paths = _docs_only_unexpected_document_repo_paths(payload, result)
        if unexpected_doc_paths:
            preview = ", ".join(unexpected_doc_paths[:5])
            return {
                "code": "docs_only_unexpected_deliverables",
                "message": (
                    "Documentation workstream emitted markdown files outside its assigned deliverables: "
                    f"{preview}."
                ),
                "details": {
                    "step_kind": step_kind,
                    "paths": unexpected_doc_paths,
                    "reason_code": "docs_only_wrong_doc_path",
                },
            }
        if step_kind == "repo_change":
            validation_artifacts = _result_explicit_artifacts(result)
            upstream_artifacts = payload.get("upstream_artifacts")
            if isinstance(upstream_artifacts, list):
                validation_artifacts = validation_artifacts + [item for item in upstream_artifacts if isinstance(item, dict)]
            placeholder_docs = _docs_only_placeholder_markdown_artifacts(validation_artifacts)
            if placeholder_docs:
                preview = ", ".join(placeholder_docs[:5])
                return {
                    "code": "docs_only_placeholder_content",
                    "message": (
                        "Documentation output contains placeholder or omitted markdown content in generated artifacts: "
                        f"{preview}."
                    ),
                    "details": {
                        "step_kind": step_kind,
                        "paths": placeholder_docs,
                        "reason_code": "docs_only_placeholder_content",
                    },
                }
            allowed_doc_paths = _docs_only_declared_markdown_paths(payload)
            broken_links = _docs_only_broken_markdown_links_from_artifacts(
                validation_artifacts,
                allowed_paths=allowed_doc_paths,
            )
            if broken_links:
                preview = ", ".join(broken_links[:3])
                return {
                    "code": "docs_only_broken_markdown_links",
                    "message": (
                        "Documentation output contains broken internal markdown links in generated artifacts: "
                        f"{preview}."
                    ),
                    "details": {
                        "step_kind": step_kind,
                        "broken_links": broken_links,
                        "reason_code": "docs_only_broken_markdown_links",
                    },
                }
        if (
            step_kind in {"test_execution", "review"}
            and isinstance(result, dict)
            and str(result.get("failure_type") or result.get("outcome") or "").strip().lower() in {"pass", "completed", "complete"}
        ):
            placeholder_docs = _docs_only_placeholder_markdown_artifacts(payload.get("upstream_artifacts"))
            if placeholder_docs:
                preview = ", ".join(placeholder_docs[:5])
                return {
                    "code": "docs_only_false_pass",
                    "message": (
                        "Documentation validation marked the branch as passed even though upstream artifacts contain placeholder or omitted markdown content: "
                        f"{preview}."
                    ),
                    "details": {
                        "step_kind": step_kind,
                        "reason_code": "docs_only_placeholder_content",
                        "paths": placeholder_docs,
                    },
                }
            allowed_doc_paths = _docs_only_declared_markdown_paths(payload)
            broken_links = _docs_only_broken_markdown_links_from_artifacts(
                payload.get("upstream_artifacts"),
                allowed_paths=allowed_doc_paths,
            )
            if broken_links:
                preview = ", ".join(broken_links[:3])
                return {
                    "code": "docs_only_false_pass",
                    "message": (
                        "Documentation validation marked the branch as passed even though upstream artifacts contain broken internal markdown links: "
                        f"{preview}."
                    ),
                    "details": {
                        "step_kind": step_kind,
                        "reason_code": "docs_only_broken_markdown_links",
                        "broken_links": broken_links,
                    },
                }

    scope_alignment_error = _assignment_scope_alignment_error(payload, result)
    if scope_alignment_error:
        return {
            "code": "assignment_scope_mismatch",
            "message": scope_alignment_error,
            "details": {
                "step_kind": step_kind,
                "reason_code": "assignment_scope_mismatch",
            },
        }

    if task.bot_id == "pm-database-engineer" and not _assignment_result_is_skip(result):
        database_contract_failure = _database_result_contract_failure(result)
        if database_contract_failure:
            return {
                "code": str(database_contract_failure.get("code") or "database_stage_contract_violation"),
                "message": str(database_contract_failure.get("message") or "Database stage contract violated."),
                "details": {
                    "step_kind": step_kind,
                    **(
                        dict(database_contract_failure.get("details"))
                        if isinstance(database_contract_failure.get("details"), dict)
                        else {}
                    ),
                },
            }

    if not text:
        return None

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
            return {
                "code": "assignment_unverified_output",
                "message": (
                    "Assignment task output is unverified and cannot be marked completed: "
                    f"detected '{marker}'."
                ),
                "details": {
                    "marker": marker,
                    "step_kind": step_kind,
                    "reason_code": "assignment_unverified_output",
                },
            }

    if step_kind in {"test_execution", "review"} and _assignment_result_is_skip(result):
        return None

    if step_kind == "repo_change" and not _has_repo_change_evidence(payload, result):
        required = ", ".join(evidence_requirements[:2]) or "repo file artifacts"
        return {
            "code": "assignment_missing_repo_evidence",
            "message": (
                "Assignment repo-change step is missing concrete changed-file evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_repo_change_evidence",
            },
        }

    if step_kind in {"specification", "planning"} and _requires_repo_artifact_evidence(payload):
        if not _has_repo_change_evidence(payload, result):
            required = ", ".join(evidence_requirements[:2]) or "committed file or diff evidence"
            return {
                "code": "assignment_missing_repo_evidence",
                "message": (
                    "Assignment planning/specification step is missing repo-backed evidence; "
                    f"required evidence: {required}."
                ),
                "details": {
                    "required_evidence": evidence_requirements[:2],
                    "step_kind": step_kind,
                    "reason_code": "missing_planning_repo_evidence",
                },
            }

    if _requires_link_evidence(payload) and not _has_non_placeholder_url_evidence(text):
        required = ", ".join(evidence_requirements[:2]) or "link-backed evidence"
        return {
            "code": "assignment_missing_link_evidence",
            "message": (
                "Assignment step is missing non-placeholder link evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_link_evidence",
            },
        }

    if step_kind == "test_execution" and _requires_repo_artifact_evidence(payload):
        if not _has_repo_change_evidence(payload, result):
            required = ", ".join(evidence_requirements[:2]) or "test artifacts"
            return {
                "code": "assignment_missing_test_artifacts",
                "message": (
                    "Assignment test step is missing concrete test artifact evidence; "
                    f"required evidence: {required}."
                ),
                "details": {
                    "required_evidence": evidence_requirements[:2],
                    "step_kind": step_kind,
                    "reason_code": "missing_test_artifact_evidence",
                },
            }

    if step_kind == "test_execution" and not _has_test_execution_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "executed test evidence"
        return {
            "code": "assignment_missing_test_execution_evidence",
            "message": (
                "Assignment test step is missing execution-backed evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_test_execution_evidence",
            },
        }

    if step_kind == "review" and not _has_review_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "review findings tied to changed files"
        return {
            "code": "assignment_missing_review_evidence",
            "message": (
                "Assignment review step is missing concrete review evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_review_evidence",
            },
        }

    if step_kind == "release" and not _has_release_evidence(result, text):
        required = ", ".join(evidence_requirements[:2]) or "release artifacts"
        return {
            "code": "assignment_missing_release_evidence",
            "message": (
                "Assignment release step is missing release-backed evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_release_evidence",
            },
        }

    if step_kind in {"repo_change", "release"} and _requires_commit_sha_evidence(payload) and not _has_non_placeholder_commit_sha(text):
        required = ", ".join(evidence_requirements[:2]) or "commit SHA evidence"
        return {
            "code": "assignment_missing_commit_sha_evidence",
            "message": (
                "Assignment step is missing non-placeholder commit SHA evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_commit_sha_evidence",
            },
        }

    if step_kind in {"repo_change", "release"} and _requires_pull_request_evidence(payload) and not _has_non_placeholder_pull_request_url(text):
        required = ", ".join(evidence_requirements[:2]) or "pull request evidence"
        return {
            "code": "assignment_missing_pull_request_evidence",
            "message": (
                "Assignment step is missing non-placeholder pull request evidence; "
                f"required evidence: {required}."
            ),
            "details": {
                "required_evidence": evidence_requirements[:2],
                "step_kind": step_kind,
                "reason_code": "missing_pull_request_evidence",
            },
        }

    if step_kind == "release" and _requires_release_tag_evidence(payload):
        if not re.search(r"\bv\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", text):
            required = ", ".join(evidence_requirements[:2]) or "release tag evidence"
            return {
                "code": "assignment_missing_release_tag_evidence",
                "message": (
                    "Assignment release step is missing release-tag evidence; "
                    f"required evidence: {required}."
                ),
                "details": {
                    "required_evidence": evidence_requirements[:2],
                    "step_kind": step_kind,
                    "reason_code": "missing_release_tag_evidence",
                },
            }

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
            return {
                "code": "assignment_guidance_instead_of_evidence",
                "message": (
                    "Assignment task output reads like guidance or a checklist rather than executed review/test evidence: "
                    + ", ".join(matched[:3])
                    + "."
                ),
                "details": {
                    "markers": matched[:3],
                    "step_kind": step_kind,
                    "reason_code": "guidance_not_evidence",
                },
            }

        # Non-writer roles (tester/security) must produce validation-only evidence,
        # not repo-file ownership. Check for repo-file artifacts in result.
        repo_outputs = _result_repo_output_candidate_paths(result)
        if repo_outputs:
            # Filter out allowed validation artifacts
            non_validation_outputs = [
                p for p in repo_outputs
                if not _is_assignment_execution_artifact_file(p)
                and not _is_probable_test_file(p)
            ]
            if non_validation_outputs:
                preview = ", ".join(non_validation_outputs[:5])
                return {
                    "code": "non_writer_validation_emitted_repo_ownership",
                    "message": (
                        "Assignment validation step (tester/security/reviewer) emitted repo-file outputs "
                        "that are not validation-only artifacts; validation roles must produce evidence "
                        "only, not own repo deliverables: "
                        f"{preview}."
                    ),
                    "details": {
                        "step_kind": step_kind,
                        "role_hint": role_hint,
                        "paths": non_validation_outputs,
                        "reason_code": "validation_role_cannot_own_repo_files",
                    },
                }
    return None


def _assignment_validation_error(task: Task, result: Any) -> str:
    failure = _assignment_validation_failure(task, result)
    return str(failure.get("message") or "") if isinstance(failure, dict) else ""


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
    _TERMINAL_TASK_STATUSES = {"completed", "failed", "retried", "cancelled"}

    def __init__(
        self,
        scheduler: Any,
        db_path: Optional[str] = None,
        bot_registry: Optional[Any] = None,
        orchestration_workspace_store: Optional[Any] = None,
    ) -> None:
        if orchestration_workspace_store is None:
            from control_plane.orchestration_workspace_store import OrchestrationWorkspaceStore

            orchestration_workspace_store = OrchestrationWorkspaceStore()
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._scheduler = scheduler
        self._bot_registry = bot_registry
        self._project_registry = getattr(scheduler, "project_registry", None)
        self._orchestration_workspace_store = orchestration_workspace_store
        self._db_ready = False
        self._running_task_ids: set[str] = set()
        self._runner_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._retry_tasks: Set[asyncio.Task[Any]] = set()
        self._watchdog_task: Optional[asyncio.Task[Any]] = None
        self._watchdog_state: Dict[str, Dict[str, Any]] = {}
        self._trigger_dispatch_pending: Set[str] = set()
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
            watchdog_task = self._watchdog_task if self._watchdog_task is not None and not self._watchdog_task.done() else None
        pending = runner_tasks + retry_tasks + ([watchdog_task] if watchdog_task is not None else [])
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
            self._watchdog_state.clear()
            self._trigger_dispatch_pending.clear()
            self._watchdog_task = None

    def __del__(self) -> None:
        # Best-effort cancellation for unmanaged instances (e.g. tests without explicit teardown).
        try:
            for task in list(self._runner_tasks.values()):
                if not task.done():
                    task.cancel()
            for task in list(self._retry_tasks):
                if not task.done():
                    task.cancel()
            if self._watchdog_task is not None and not self._watchdog_task.done():
                self._watchdog_task.cancel()
        except Exception:
            pass

    def _task_max_concurrency(self) -> int:
        env_raw = os.environ.get("NEXUSAI_TASK_MAX_CONCURRENCY", "").strip()
        if env_raw:
            try:
                return max(1, int(env_raw))
            except Exception:
                return max(1, self._max_concurrency)
        try:
            configured = SettingsManager.instance().get("task_max_concurrency", self._max_concurrency)
            return max(1, int(configured))
        except Exception:
            return max(1, self._max_concurrency)

    def _provider_concurrency_limits(self) -> dict[str, int]:
        try:
            raw = SettingsManager.instance().get("task_provider_concurrency_limits", {})
        except Exception:
            raw = {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            return {}
        limits: dict[str, int] = {}
        for provider, limit in raw.items():
            key = str(provider or "").strip().lower()
            if not key:
                continue
            try:
                parsed = int(limit)
            except Exception:
                continue
            if parsed <= 0:
                continue
            limits[key] = parsed
        return limits

    async def _bot_provider_keys_for_tasks(self, tasks: list[Task]) -> dict[str, str]:
        if not tasks or self._bot_registry is None:
            return {}
        bot_ids = {str(task.bot_id or "").strip() for task in tasks if str(task.bot_id or "").strip()}
        providers_by_bot: dict[str, str] = {}
        for bot_id in bot_ids:
            try:
                bot = await self._bot_registry.get(bot_id)
            except Exception:
                providers_by_bot[bot_id] = ""
                continue
            provider = ""
            backends = list(getattr(bot, "backends", []) or [])
            if backends:
                provider = str(getattr(backends[0], "provider", "") or "").strip().lower()
            providers_by_bot[bot_id] = provider
        return {task.id: providers_by_bot.get(str(task.bot_id or "").strip(), "") for task in tasks}

    async def _ensure_db(self) -> None:
        """Lazily initialise the SQLite tasks table and load existing rows."""
        if self._db_ready:
            await self._ensure_watchdog_started()
            return
        async with self._init_lock:
            if self._db_ready:
                await self._ensure_watchdog_started()
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

                # Recover orphaned tasks that were left in "running" state from a previous session.
                # Their asyncio runner no longer exists so they would block the queue forever.
                # Re-queue them so they are retried, or mark them failed if retries are exhausted.
                orphaned = [t for t in self._tasks.values() if t.status == "running"]
                if orphaned:
                    max_retries = _settings_int("task_max_retries", 10)
                    now = datetime.now(timezone.utc).isoformat()
                    requeued, failed = [], []
                    for orphan in orphaned:
                        attempt_count = int(
                            (orphan.metadata and getattr(orphan.metadata, "attempt_count", 0)) or 0
                        )
                        if attempt_count >= max_retries:
                            orphan.status = "failed"
                            orphan.error = TaskError(
                                code="ORPHANED",
                                message=(
                                    f"Task was in running state on startup (attempt {attempt_count}); "
                                    f"exceeded max_retries {max_retries}, marked failed."
                                ),
                            )
                            failed.append(orphan.id)
                        else:
                            orphan.status = "queued"
                            orphan.error = None
                            requeued.append(orphan.id)
                        orphan.updated_at = now
                        await db.execute(
                            f"UPDATE {_TASKS_TABLE} SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                            (
                                orphan.status,
                                orphan.updated_at,
                                json.dumps(orphan.error.model_dump()) if orphan.error else None,
                                orphan.id,
                            ),
                        )
                    await db.commit()
                    logger.warning(
                        "Recovered %d orphaned running task(s) on startup — re-queued: %s, failed: %s",
                        len(orphaned),
                        requeued,
                        failed,
                    )
            self._db_ready = True
        await self._ensure_watchdog_started()

    async def _ensure_watchdog_started(self) -> None:
        if self._is_closing or not _settings_bool("running_task_watchdog_enabled", True):
            return
        async with self._lock:
            if self._watchdog_task is not None and not self._watchdog_task.done():
                return
            self._watchdog_task = asyncio.create_task(self._running_task_watchdog())

    async def _running_task_watchdog(self) -> None:
        try:
            while not self._is_closing:
                poll_seconds = max(0.1, _settings_float("running_task_watchdog_poll_seconds", 30.0))
                await asyncio.sleep(poll_seconds)
                if self._is_closing or not _settings_bool("running_task_watchdog_enabled", True):
                    continue
                try:
                    await self._check_running_tasks_for_stall()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Running task watchdog check failed: %s", exc)
        except asyncio.CancelledError:
            raise

    async def _check_running_tasks_for_stall(self) -> None:
        initial_stall_seconds = max(
            0.1,
            _settings_float("running_task_watchdog_initial_stall_seconds", 600.0),
        )
        progress_grace_seconds = max(
            0.1,
            _settings_float("running_task_watchdog_progress_grace_seconds", 300.0),
        )
        now_monotonic = time.monotonic()
        to_retry: List[Tuple[str, str, float, float]] = []

        async with self._lock:
            running_ids = set()
            for task_id, task in self._tasks.items():
                if task.status != "running":
                    self._watchdog_state.pop(task_id, None)
                    continue
                running_ids.add(task_id)
                runner = self._runner_tasks.get(task_id)
                state = self._watchdog_state.get(task_id)
                if state is None:
                    self._watchdog_state[task_id] = {
                        "last_updated_at": str(task.updated_at or ""),
                        "last_progress_monotonic": now_monotonic,
                        "next_deadline_monotonic": now_monotonic + initial_stall_seconds,
                        "grace_issued": False,
                    }
                    continue
                current_updated_at = str(task.updated_at or "")
                if current_updated_at and current_updated_at != str(state.get("last_updated_at") or ""):
                    state["last_updated_at"] = current_updated_at
                    state["last_progress_monotonic"] = now_monotonic
                    state["next_deadline_monotonic"] = now_monotonic + progress_grace_seconds
                    state["grace_issued"] = False
                    continue
                next_deadline = float(state.get("next_deadline_monotonic") or 0.0)
                if now_monotonic < next_deadline:
                    continue
                elapsed_since_progress = max(
                    0.0,
                    now_monotonic - float(state.get("last_progress_monotonic") or now_monotonic),
                )
                if runner is None or runner.done():
                    to_retry.append((task_id, "runner_inactive", next_deadline, elapsed_since_progress))
                    continue
                if not bool(state.get("grace_issued")):
                    state["grace_issued"] = True
                    state["next_deadline_monotonic"] = now_monotonic + progress_grace_seconds
                    logger.warning(
                        "Task %s exceeded running watchdog window; runner still alive, granting %.1fs progress grace",
                        task_id,
                        progress_grace_seconds,
                    )
                    continue
                to_retry.append((task_id, "no_progress_after_live_check", next_deadline, elapsed_since_progress))

            for task_id in list(self._watchdog_state.keys()):
                if task_id not in running_ids:
                    self._watchdog_state.pop(task_id, None)

        for task_id, reason, _, elapsed_since_progress in to_retry:
            await self._retry_stuck_task(task_id, reason=reason, stagnant_seconds=elapsed_since_progress)

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
        # SCOPE ENFORCEMENT
        payload = task.payload if isinstance(task.payload, dict) else {}
        assignment_scope = _payload_assignment_scope(payload)
        if assignment_scope:
            scope_lock = assignment_scope.get("scope_lock")
            if isinstance(scope_lock, dict):
                allowed_artifacts = scope_lock.get("allowed_artifacts") or []
                forbidden_keywords = scope_lock.get("forbidden_keywords") or []
                
                if allowed_artifacts or forbidden_keywords:
                    for candidate in extract_file_candidates(task.result):
                        path = str(candidate.get("path") or "").strip().lower()
                        if not path:
                            continue
                        
                        # Check against forbidden keywords
                        if forbidden_keywords and any(keyword in path for keyword in forbidden_keywords):
                            raise _TaskPolicyViolation(
                                f"Artifact '{path}' violates scope lock: contains forbidden keyword.",
                                code="scope_violation_forbidden",
                                details={"path": path, "scope_lock": scope_lock}
                            )

                        # Check against allowed artifact patterns (glob matching)
                        if allowed_artifacts:
                            import fnmatch
                            if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed_artifacts):
                                raise _TaskPolicyViolation(
                                    f"Artifact '{path}' violates scope lock: not in allowed artifact patterns.",
                                    code="scope_violation_not_allowed",
                                    details={"path": path, "scope_lock": scope_lock}
                                )

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
        if original.status not in {"completed", "failed", "cancelled"}:
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
            pending_dispatch = set(self._trigger_dispatch_pending)
        if pending_dispatch:
            visible_tasks: List[Task] = []
            for task in tasks:
                if task.id in pending_dispatch and task.status in {"completed", "failed"}:
                    visible_tasks.append(task.model_copy(update={"status": "running"}))
                    continue
                visible_tasks.append(task)
            tasks = visible_tasks
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

    def _bot_allows_repo_output_for_task(self, task: Task, bot: Any) -> bool:
        if bot_allows_repo_output(bot):
            return True
        if str(task.bot_id or "").strip().lower() == "pm-database-engineer":
            return True
        return False

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
                    synthesized = _synthesize_docs_only_repo_change_contract_result(task, result, raw_text=raw_text)
                    if synthesized is not None:
                        parsed = synthesized
                    elif allow_parse_failure_fallback:
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
                synthesized = _synthesize_docs_only_repo_change_contract_result(task, normalized, raw_text=raw_text)
                if synthesized is not None:
                    normalized = _merge_with_contract_defaults(normalized, synthesized)
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
        should_track_trigger_dispatch = status in {"completed", "failed"}
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
            if status == "running":
                self._watchdog_state[task_id] = {
                    "last_updated_at": now,
                    "last_progress_monotonic": time.monotonic(),
                    "next_deadline_monotonic": time.monotonic() + max(
                        0.1,
                        _settings_float("running_task_watchdog_initial_stall_seconds", 600.0),
                    ),
                    "grace_issued": False,
                }
            else:
                self._watchdog_state.pop(task_id, None)
            if should_track_trigger_dispatch:
                self._trigger_dispatch_pending.add(task_id)
            else:
                self._trigger_dispatch_pending.discard(task_id)
        await self._persist_task(updated_task)
        await self._upsert_bot_run(updated_task)
        try:
            if status in {"completed", "failed", "cancelled", "retried"}:
                await self._record_artifacts_for_task(updated_task)
                if status in {"completed", "failed"}:
                    await self._dispatch_triggers(updated_task)
                await self._try_unblock_tasks()
        finally:
            if should_track_trigger_dispatch:
                async with self._lock:
                    self._trigger_dispatch_pending.discard(task_id)
        if status == "failed":
            await self._cleanup_failed_orchestration_workspace(updated_task)

    async def _retry_stuck_task(self, task_id: str, *, reason: str, stagnant_seconds: float) -> None:
        await self._ensure_db()
        task: Optional[Task] = None
        runner: Optional[asyncio.Task[Any]] = None
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running":
                self._watchdog_state.pop(task_id, None)
                return
            runner = self._runner_tasks.get(task_id)
            self._watchdog_state.pop(task_id, None)
        if runner is not None and not runner.done():
            runner.cancel()
        task_error = TaskError(
            message=(
                f"Task exceeded running watchdog timeout ({reason}); "
                f"no progress detected for {int(max(0.0, stagnant_seconds))}s."
            ),
            code="task_stuck_timeout",
            details={
                "reason_code": "task_stuck_timeout",
                "watchdog_reason": reason,
                "stagnant_seconds": int(max(0.0, stagnant_seconds)),
                "bot_id": task.bot_id,
                "task_id": task.id,
            },
        )
        if await self._requeue_for_retry(task, task_error):
            logger.warning(
                "Task %s marked for automatic retry by running-task watchdog: reason=%s stagnant=%ss",
                task_id,
                reason,
                int(max(0.0, stagnant_seconds)),
            )
            return
        await self.update_status(task_id, "failed", error=task_error)

    async def _cleanup_failed_orchestration_workspace(self, task: Task) -> None:
        if self._orchestration_workspace_store is None:
            return
        metadata = task.metadata or TaskMetadata()
        project_id = str(metadata.project_id or "").strip()
        orchestration_id = str(metadata.orchestration_id or "").strip()
        source = str(metadata.source or "").strip().lower()
        if not project_id or not orchestration_id or source not in {"chat_assign", "auto_retry", "bot_trigger"}:
            return
        async with self._lock:
            related = [
                candidate
                for candidate in self._tasks.values()
                if candidate.metadata
                and str(candidate.metadata.project_id or "").strip() == project_id
                and str(candidate.metadata.orchestration_id or "").strip() == orchestration_id
                and str(candidate.metadata.source or "").strip().lower() in {"chat_assign", "auto_retry", "bot_trigger"}
            ]
        if any(str(candidate.status or "").strip().lower() not in self._TERMINAL_TASK_STATUSES for candidate in related):
            return
        from control_plane.api.projects import _cleanup_orchestration_temp_workspace

        await _cleanup_orchestration_temp_workspace(
            project_id=project_id,
            orchestration_id=orchestration_id,
            workspace_store=self._orchestration_workspace_store,
            reason="pipeline_failed",
        )

    async def _run_task(self, task_id: str) -> None:
        raw_result: Any = None
        try:
            if self._is_closing:
                return
            await self.update_status(task_id, "running")
            task = await self.get_task(task_id)
            bot = None
            if self._bot_registry is not None:
                try:
                    bot = await self._bot_registry.get(task.bot_id)
                except Exception:
                    bot = None
            payload = task.payload if isinstance(task.payload, dict) else {}
            bot_allows_repo_output_for_task = bool(bot is not None and self._bot_allows_repo_output_for_task(task, bot))
            if bot is not None and not bot_allows_repo_output_for_task:
                # Inject bot role as role_hint if not already present in payload
                if "role_hint" not in payload and bot.role:
                    payload = dict(payload)
                    payload["role_hint"] = str(bot.role).strip().lower()
                assigned_repo_deliverables = _non_writer_step_repo_deliverables(payload)
                if assigned_repo_deliverables:
                    preview = ", ".join(assigned_repo_deliverables[:5])
                    step_kind = _assignment_step_kind(payload) or "non_writer"
                    raise _TaskPolicyViolation(
                        (
                            f"Bot '{task.bot_id}' is not allowed to own repo deliverables for "
                            f"{step_kind} steps, but was assigned: {preview}."
                        ),
                        code="non_writer_assigned_repo_deliverables",
                        details={
                            "bot_id": task.bot_id,
                            "step_kind": step_kind,
                            "paths": assigned_repo_deliverables,
                            "reason_code": "non_writer_assigned_repo_deliverables",
                        },
                    )
            mode = await self._bot_output_contract_mode(task.bot_id)
            internal_result = await self._maybe_run_internal_assignment_step(task)
            if internal_result is not None:
                raw_result = internal_result
            elif mode == "payload_transform":
                raw_result = {"deterministic_transform": True}
            else:
                raw_result = await self._scheduler.schedule(task)
            result = await self._normalize_task_result(task, copy.deepcopy(raw_result))
            if bot is not None and not bot_allows_repo_output_for_task:
                result = _strip_repo_output_claims_for_deny_policy(result)
            result = self._sanitize_pm_assignment_result(task, result)
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
            validation_failure = _assignment_validation_failure(task, result)
            if validation_failure:
                raise _TaskPolicyViolation(
                    str(validation_failure.get("message") or "Workflow policy validation failed."),
                    code=str(validation_failure.get("code") or "workflow_policy_violation"),
                    details={
                        "bot_id": task.bot_id,
                        **(
                            dict(validation_failure.get("details"))
                            if isinstance(validation_failure.get("details"), dict)
                            else {}
                        ),
                    },
                    result=result,
                )
            if task.bot_id == "pm-database-engineer" and _database_result_contains_destructive_sql(result):
                raise _TaskPolicyViolation(
                    "pm-database-engineer returned destructive SQL content; DELETE, DROP, TRUNCATE, and destructive ALTER TABLE statements are forbidden.",
                    code="database_destructive_sql_forbidden",
                    details={
                        "bot_id": task.bot_id,
                        "reason_code": "database_destructive_sql_forbidden",
                    },
                    result=result,
                )
            if bot is not None and not bot_allows_repo_output_for_task:
                repo_output_paths = _result_repo_output_candidate_paths(result)
                if repo_output_paths:
                    preview = ", ".join(repo_output_paths[:5])
                    raise _TaskPolicyViolation(
                        f"Bot '{task.bot_id}' is not allowed to emit repo file outputs, but returned: {preview}.",
                        code="non_writer_emitted_repo_outputs",
                        details={
                            "bot_id": task.bot_id,
                            "step_kind": _assignment_step_kind(payload) or "",
                            "paths": repo_output_paths,
                            "reason_code": "non_writer_emitted_repo_outputs",
                        },
                        result=result,
                    )
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
        except _TaskPolicyViolation as e:
            logger.warning("Task %s workflow policy violation: code=%s details=%s", task_id, e.code, e.details)
            task_error = TaskError(message=str(e), code=e.code, details=e.details)
            task = await self.get_task(task_id)
            await self.update_status(task_id, "failed", result=e.result, error=task_error)
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
            available_slots = max(0, self._task_max_concurrency() - len(self._running_task_ids))
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
            running_snapshot = [
                self._tasks[task_id]
                for task_id in self._running_task_ids
                if task_id in self._tasks
            ]
        provider_limits = self._provider_concurrency_limits()
        if provider_limits:
            provider_keys = await self._bot_provider_keys_for_tasks([*queued, *running_snapshot])
            provider_counts: dict[str, int] = {}
            for task in running_snapshot:
                provider_key = provider_keys.get(task.id, "")
                if not provider_key:
                    continue
                provider_counts[provider_key] = provider_counts.get(provider_key, 0) + 1
            selected: list[Task] = []
            for task in queued:
                if len(selected) >= available_slots:
                    break
                provider_key = provider_keys.get(task.id, "")
                limit = provider_limits.get(provider_key)
                if limit is not None and provider_counts.get(provider_key, 0) >= limit:
                    continue
                selected.append(task)
                if provider_key:
                    provider_counts[provider_key] = provider_counts.get(provider_key, 0) + 1
        else:
            selected = queued[:available_slots]

        async with self._lock:
            still_selected: list[Task] = []
            for task in selected:
                current = self._tasks.get(task.id)
                if current is None or current.status != "queued" or task.id in self._running_task_ids:
                    continue
                self._running_task_ids.add(task.id)
                still_selected.append(current)

        for task in still_selected:
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
            _ensure_orchestration_temp_workspace,
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

        source_root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
        snapshot = await _repo_status_snapshot(root=source_root, cfg=cfg)
        if not bool(snapshot.get("is_repo")):
            raise _TaskExecutionFailure("repo workspace is not a git repository; clone it before running assignment tests")
        workspace_entry = await _ensure_orchestration_temp_workspace(
            project_id=project_id,
            orchestration_id=orchestration_id,
            project_registry=self._project_registry,
            workspace_store=self._orchestration_workspace_store,
            strict=True,
        )
        if workspace_entry is None:
            raise _TaskExecutionFailure("orchestration temp workspace is unavailable for assignment execution")
        root = Path(str(workspace_entry.get("temp_root") or "").strip())
        if not root.exists():
            raise _TaskExecutionFailure("orchestration temp workspace path does not exist")

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
        if self._bot_registry is not None:
            allowed_repo_output_bot_ids: Set[str] = set()
            for bot_id in {str(candidate.bot_id or "").strip() for candidate in scoped_tasks if str(candidate.bot_id or "").strip()}:
                try:
                    bot = await self._bot_registry.get(bot_id)
                except Exception:
                    continue
                candidate_stub = Task(
                    id="",
                    bot_id=bot_id,
                    payload={},
                    status="queued",
                    created_at="",
                    updated_at="",
                )
                if self._bot_allows_repo_output_for_task(candidate_stub, bot):
                    allowed_repo_output_bot_ids.add(bot_id)
            scoped_tasks = [
                candidate for candidate in scoped_tasks if str(candidate.bot_id or "").strip() in allowed_repo_output_bot_ids
            ]
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
            # No test files means this is a non-testable workstream (e.g. documentation-only).
            # Return a structured skip result so downstream triggers can route correctly
            # (result_field="outcome", result_equals="skip" or "pass") rather than failing and
            # sending the coder into an infinite fabrication loop.
            workspace_snap = await _repo_status_snapshot(root=root, cfg=cfg)
            return {
                "outcome": "skip",
                "failure_type": "not_applicable",
                "findings": [
                    "No automated test files were found for this workstream.",
                    "The workstream appears to be documentation-only or does not require automated test execution.",
                ],
                "evidence": [
                    "Applied files: " + (", ".join(applied_paths) if applied_paths else "none"),
                ],
                "handoff_notes": (
                    "This workstream does not require automated test execution. "
                    "Continue to the next validation stage."
                ),
                "artifacts": [],
                "applied_files": applied_files,
                "workspace": workspace_snap,
                "assignment_workspace": workspace_entry,
                "usage": {},
                "executed_commands": [],
                "command_results": [],
                "missing_tools": [],
                "exit_code": None,
                "report": "No test files detected. Workstream skipped for automated test execution.",
            }

        usage_parts: List[Dict[str, Any]] = []
        command_results: List[Dict[str, Any]] = []
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
        languages = _assignment_execution_languages(
            applied_paths=applied_paths,
            test_files=test_files,
            root=root,
            allowed_extra=allowed_extra or None,
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
        result["assignment_workspace"] = workspace_entry
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

    def _trigger_depth_limit(self, metadata: TaskMetadata) -> int:
        default_limit = max(1, _settings_int("bot_trigger_max_depth", 60))
        if str(metadata.run_class or "").strip().lower() == "pm_assignment":
            return max(default_limit, _settings_int("pm_assignment_trigger_max_depth", 120))
        return default_limit

    def _workflow_route_failure_type(self, task: Task) -> str:
        if isinstance(task.result, dict):
            for key in ("failure_type", "outcome", "status"):
                value = str(task.result.get(key) or "").strip().lower()
                if value:
                    return value
        if task.error is not None:
            code = str(task.error.code or "").strip().lower()
            if code:
                return code
            message = str(task.error.message or "").strip().lower()
            if message:
                return message[:80]
        return str(task.status or "unknown").strip().lower() or "unknown"

    def _workflow_route_branch_identity(self, task: Task, payload: Any) -> str:
        metadata = task.metadata or TaskMetadata()
        if isinstance(payload, dict):
            context = self._payload_pm_routing_context(payload)
            context_fanout_id = str(context.get("fanout_id") or "").strip()
            context_branch_key = str(context.get("branch_key") or "").strip()
            context_global_stage = str(context.get("global_stage") or "").strip()
            if context_fanout_id or context_branch_key or context_global_stage:
                context_parts = [
                    part
                    for part in (
                        context_fanout_id,
                        context_global_stage,
                        context_branch_key,
                    )
                    if part
                ]
                if context_parts:
                    return "|".join(context_parts)
            fanout_id = str(self._resolve_fanout_id(payload) or "").strip()
            branch_key = str(self._resolve_join_branch_key(payload) or "").strip()
            if fanout_id or branch_key:
                return "|".join(part for part in (fanout_id, branch_key) if part)
            workstream_index = str(payload.get("workstream_index") or "").strip()
            if workstream_index:
                return workstream_index
            workstream = payload.get("workstream")
            if isinstance(workstream, dict):
                workstream_identity = {
                    "title": str(workstream.get("title") or "").strip().lower(),
                    "instruction": str(workstream.get("instruction") or "").strip().lower(),
                    "path": str(workstream.get("path") or "").strip().replace("\\", "/").lower(),
                    "deliverables": sorted(
                        str(item or "").strip().replace("\\", "/").lower()
                        for item in _normalize_string_list(workstream.get("deliverables"))
                        if str(item or "").strip()
                    ),
                }
                if any(
                    value
                    for key, value in workstream_identity.items()
                    if key != "deliverables"
                ) or workstream_identity["deliverables"]:
                    return self._normalize_branch_token(workstream_identity, fallback="workstream")
        return str(metadata.step_id or metadata.original_task_id or task.id).strip() or task.id

    def _pm_assignment_stable_branch_identity(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        workstream = payload.get("workstream") if isinstance(payload.get("workstream"), dict) else payload
        if not isinstance(workstream, dict):
            return ""
        identity = {
            "title": str(workstream.get("title") or payload.get("title") or "").strip().lower(),
            "instruction": str(workstream.get("instruction") or payload.get("instruction") or "").strip().lower(),
            "path": str(workstream.get("path") or payload.get("path") or "").strip().replace("\\", "/").lower(),
            "deliverables": sorted(
                str(item or "").strip().replace("\\", "/").lower()
                for item in (
                    _normalize_string_list(workstream.get("deliverables"))
                    or _normalize_string_list(payload.get("deliverables"))
                )
                if str(item or "").strip()
            ),
        }
        if not any(value for key, value in identity.items() if key != "deliverables") and not identity["deliverables"]:
            return ""
        return self._normalize_branch_token(identity, fallback="workstream")

    def _workflow_route_repeat_identity(
        self,
        source_task: Task,
        target_bot_id: str,
        payload: Any,
    ) -> str:
        metadata = source_task.metadata or TaskMetadata()
        if (
            str(metadata.run_class or "").strip().lower() == "pm_assignment"
            and str(target_bot_id or "").strip() == "pm-coder"
            and str(source_task.bot_id or "").strip() in {"pm-tester", "pm-security-reviewer"}
        ):
            stable_identity = self._pm_assignment_stable_branch_identity(payload)
            if stable_identity:
                return stable_identity
        return self._workflow_route_branch_identity(source_task, payload)

    async def _workflow_route_repeat_count(
        self,
        source_task: Task,
        target_bot_id: str,
        payload: Any,
    ) -> int:
        metadata = source_task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or source_task.id
        orchestration_id = str(metadata.orchestration_id or "").strip()
        branch_identity = self._workflow_route_repeat_identity(source_task, target_bot_id, payload)
        failure_type = self._workflow_route_failure_type(source_task)

        async with self._lock:
            tasks = list(self._tasks.values())

        repeat_count = 0
        pair_repeat_count = 0
        for candidate in tasks:
            if candidate.bot_id != target_bot_id:
                continue
            if str(candidate.status or "").strip().lower() == "retried":
                continue
            candidate_meta = candidate.metadata or TaskMetadata()
            if str(candidate_meta.source or "").strip().lower() != "bot_trigger":
                continue
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if str(candidate_meta.orchestration_id or "").strip() != orchestration_id:
                continue
            parent_task_id = str(candidate_meta.parent_task_id or "").strip()
            if not parent_task_id:
                continue
            parent_task = next((item for item in tasks if item.id == parent_task_id), None)
            if parent_task is None or parent_task.bot_id != source_task.bot_id:
                continue
            if self._workflow_route_repeat_identity(parent_task, target_bot_id, candidate.payload) != branch_identity:
                continue
            pair_repeat_count += 1
            if self._workflow_route_failure_type(parent_task) == failure_type:
                repeat_count += 1
        return max(repeat_count, pair_repeat_count)

    async def _workflow_route_target_bot_repeat_count(
        self,
        source_task: Task,
        target_bot_id: str,
        payload: Any,
    ) -> int:
        metadata = source_task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or source_task.id
        orchestration_id = str(metadata.orchestration_id or "").strip()
        branch_identity = self._workflow_route_branch_identity(source_task, payload)

        async with self._lock:
            tasks = list(self._tasks.values())

        repeat_count = 0
        for candidate in tasks:
            if candidate.bot_id != target_bot_id:
                continue
            if str(candidate.status or "").strip().lower() == "retried":
                continue
            candidate_meta = candidate.metadata or TaskMetadata()
            if str(candidate_meta.source or "").strip().lower() != "bot_trigger":
                continue
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if str(candidate_meta.orchestration_id or "").strip() != orchestration_id:
                continue
            if self._workflow_route_branch_identity(candidate, candidate.payload) != branch_identity:
                continue
            repeat_count += 1
        return repeat_count

    async def _record_workflow_loop_guard_stop(
        self,
        source_task: Task,
        trigger_id: str,
        target_bot_id: str,
        branch_identity: str,
        failure_type: str,
        repeat_count: int,
        repeat_limit: int,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        details = {
            "trigger_id": trigger_id,
            "target_bot_id": target_bot_id,
            "branch_identity": branch_identity,
            "failure_type": failure_type,
            "repeat_count": repeat_count,
            "repeat_limit": repeat_limit,
            "reason": reason,
        }
        await self._upsert_artifact(
            BotRunArtifact(
                id=f"{source_task.id}:loop-guard:{trigger_id or 'unknown'}:{target_bot_id or 'unknown'}",
                run_id=source_task.id,
                task_id=source_task.id,
                bot_id=source_task.bot_id,
                kind="error",
                label="Workflow Loop Guard Stop",
                content=json.dumps(details, indent=2, sort_keys=True),
                metadata=details,
                created_at=now,
            )
        )

    def _workflow_required_output_fields(self, workflow: Any) -> List[str]:
        return [
            str(field).strip()
            for field in (getattr(workflow, "required_output_fields", None) or [])
            if str(field).strip()
        ]

    async def _halt_task_for_missing_required_output_fields(
        self,
        task: Task,
        required_fields: List[str],
        missing_fields: List[str],
        *,
        blocked_triggers: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        metadata = task.metadata or TaskMetadata()
        result_keys = (
            sorted(str(key) for key in task.result.keys())
            if isinstance(task.result, dict)
            else []
        )
        blocked_triggers = [
            {
                "trigger_id": str(item.get("trigger_id") or "").strip(),
                "target_bot_id": str(item.get("target_bot_id") or "").strip(),
            }
            for item in (blocked_triggers or [])
            if str(item.get("trigger_id") or "").strip() or str(item.get("target_bot_id") or "").strip()
        ]
        message = (
            "Bot output validation failed before trigger dispatch: missing or empty required output fields: "
            + ", ".join(missing_fields)
        )
        validation_error = {
            "annotation": "validation_error",
            "task_id": task.id,
            "bot_id": task.bot_id,
            "orchestration_id": metadata.orchestration_id,
            "reason": "required_output_fields_missing",
            "reason_code": "trigger_output_field_missing",
            "issue_tag": "trigger_output_field_missing",
            "required_fields": list(required_fields),
            "missing_fields": list(missing_fields),
            "blocked_triggers": blocked_triggers,
            "trigger_ids": [
                trigger["trigger_id"]
                for trigger in blocked_triggers
                if trigger.get("trigger_id")
            ],
            "result_keys": result_keys,
            "message": message,
        }
        safe_validation_error = json.loads(json.dumps(validation_error, default=str))
        if blocked_triggers:
            for blocked_trigger in blocked_triggers:
                structured_error = {
                    "bot_id": task.bot_id,
                    "trigger_id": blocked_trigger.get("trigger_id") or "required-output-check",
                    "target_bot_id": blocked_trigger.get("target_bot_id") or "",
                    "missing_fields": list(missing_fields),
                    "task_id": task.id,
                    "orchestration_id": metadata.orchestration_id,
                }
                logger.error(
                    "[TRIGGER] REQUIRED_OUTPUT_VALIDATION_FAILED %s",
                    json.dumps(structured_error, sort_keys=True),
                )
                await self._record_trigger_dispatch_error(
                    source_task=task,
                    trigger_id=structured_error["trigger_id"],
                    target_bot_id=structured_error["target_bot_id"],
                    message=message,
                )
        else:
            logger.error(
                "[TRIGGER] REQUIRED_OUTPUT_VALIDATION_FAILED %s",
                json.dumps(safe_validation_error, sort_keys=True),
            )
            await self._record_trigger_dispatch_error(
                source_task=task,
                trigger_id="required-output-check",
                target_bot_id="",
                message=message,
            )
        await self.update_status(
            task.id,
            "failed",
            result=task.result,
            error=TaskError(
                message=message,
                code="validation_error",
                details={
                    "reason": "required_output_fields_missing",
                    "reason_code": "trigger_output_field_missing",
                    "issue_tag": "trigger_output_field_missing",
                    "validation_error": safe_validation_error,
                },
            ),
        )

    async def _dispatch_triggers(self, task: Task) -> None:
        if self._bot_registry is None:
            return
        try:
            bot = await self._bot_registry.get(task.bot_id)
        except Exception:
            return
        workflow = self._bot_workflow(bot)
        triggers = getattr(workflow, "triggers", None) or []

        metadata = task.metadata or TaskMetadata()

        # For plan-managed orchestrated tasks, skip forward triggers because the PM plan already
        # defines the main forward path. Backward-triggered remediation tasks are created with
        # source="bot_trigger"; those are allowed to flow forward again so repair loops can close.
        source = str(metadata.source or "").strip().lower()
        # Only treat root/top-level assignment tasks as plan-managed. Downstream
        # workflow branches may complete under source="auto_retry" after model
        # truncation or transient failure recovery, but they still need to keep
        # moving forward through their configured explicit workflow.
        is_top_level_assignment_task = not str(metadata.parent_task_id or "").strip() and not str(metadata.trigger_rule_id or "").strip()
        is_plan_managed_orchestrated = source == "chat_assign" or (
            source == "auto_retry" and is_top_level_assignment_task
        )

        trigger_depth = int(metadata.trigger_depth or 0)
        max_depth = self._trigger_depth_limit(metadata)
        if trigger_depth >= max_depth:
            logger.warning("Skipping bot triggers for task %s due to depth cap %s", task.id, max_depth)
            await self._record_workflow_loop_guard_stop(
                source_task=task,
                trigger_id="depth-limit",
                target_bot_id="",
                branch_identity=self._workflow_route_branch_identity(task, task.payload),
                failure_type=self._workflow_route_failure_type(task),
                repeat_count=trigger_depth,
                repeat_limit=max_depth,
                reason="trigger_depth_limit",
            )
            return

        event = "task_completed" if task.status == "completed" else "task_failed"
        logger.info(
            "[TRIGGER] Evaluating triggers for task=%s bot=%s event=%s depth=%d",
            task.id,
            task.bot_id,
            event,
            trigger_depth,
        )

        # Pre-validate required_output_fields declared on the bot's workflow.
        # Missing fields here should halt the orchestration visibly instead of silently.
        _required_output_fields = self._workflow_required_output_fields(workflow)
        if _required_output_fields and task.status == "completed":
            _missing_required = sorted(
                set(_missing_payload_fields(task.result, _required_output_fields))
                | set(_empty_payload_fields(task.result, _required_output_fields))
            )
            if _missing_required:
                _blocked_triggers = [
                    {
                        "trigger_id": str(getattr(trigger, "id", "") or "").strip(),
                        "target_bot_id": str(getattr(trigger, "target_bot_id", "") or "").strip(),
                    }
                    for trigger in triggers
                    if bool(getattr(trigger, "enabled", False)) and getattr(trigger, "event", None) == event
                ]
                await self._halt_task_for_missing_required_output_fields(
                    task,
                    required_fields=_required_output_fields,
                    missing_fields=_missing_required,
                    blocked_triggers=_blocked_triggers,
                )
                return

        _result_mismatch_skipped: list = []
        for trigger in triggers:
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
                            "[TRIGGER] SKIP trigger=%s task=%s reason=plan_managed_orchestrated_forward",
                            trigger.id,
                            task.id,
                        )
                        continue
                if trigger.condition == "has_error" and task.error is None:
                    continue
                result_field_value = None
                if trigger.result_field and isinstance(task.result, dict):
                    result_field_value = self._lookup_result_field(task.result, trigger.result_field)
                if not self._trigger_matches_result(task, trigger):
                    logger.warning(
                        "[TRIGGER] SKIP trigger=%s task=%s reason=result_mismatch "
                        "result_field=%s expected=%s actual=%s result_keys=%s",
                        trigger.id,
                        task.id,
                        trigger.result_field,
                        trigger.result_equals,
                        result_field_value,
                        list(task.result.keys()) if isinstance(task.result, dict) else None,
                    )
                    await self._record_trigger_dispatch_skip(
                        source_task=task,
                        trigger_id=str(getattr(trigger, "id", "") or ""),
                        target_bot_id=str(getattr(trigger, "target_bot_id", "") or ""),
                        details={
                            "reason": "result_field_missing_or_unmatched",
                            "result_field": getattr(trigger, "result_field", None),
                            "result_equals": getattr(trigger, "result_equals", None),
                            "actual_value": str(result_field_value) if result_field_value is not None else None,
                            "result_keys": list(task.result.keys()) if isinstance(task.result, dict) else None,
                        },
                    )
                    _result_mismatch_skipped.append(str(getattr(trigger, "id", "") or ""))
                    continue
                target_bot_id = self._resolve_trigger_target_bot_id(task, trigger.target_bot_id)
                if not target_bot_id:
                    logger.warning("[TRIGGER] SKIP trigger=%s task=%s reason=target_bot_unresolved configured_target=%s", trigger.id, task.id, trigger.target_bot_id)
                    continue
                if self._should_skip_dynamic_pm_trigger(task, target_bot_id=target_bot_id):
                    logger.debug(
                        "[TRIGGER] SKIP trigger=%s task=%s reason=dynamic_pm_stage_managed target=%s",
                        trigger.id,
                        task.id,
                        target_bot_id,
                    )
                    continue
                allowed_bot_ids = [
                    str(item).strip()
                    for item in (metadata.allowed_bot_ids or [])
                    if str(item).strip()
                ]
                if allowed_bot_ids and target_bot_id not in allowed_bot_ids:
                    message = (
                        f"trigger target '{target_bot_id}' is outside the orchestration allowlist for "
                        f"workflow '{metadata.workflow_graph_id or metadata.orchestration_id or 'unknown'}'"
                    )
                    logger.warning("[TRIGGER] SKIP trigger=%s task=%s reason=target_not_allowed target=%s", trigger.id, task.id, target_bot_id)
                    await self._record_trigger_dispatch_error(
                        source_task=task,
                        trigger_id=str(getattr(trigger, "id", "") or ""),
                        target_bot_id=str(target_bot_id or ""),
                        message=message,
                    )
                    continue
                if self._bot_registry is not None:
                    try:
                        await self._bot_registry.get(target_bot_id)
                    except Exception:
                        logger.warning("[TRIGGER] SKIP trigger=%s task=%s reason=target_bot_missing target=%s", trigger.id, task.id, target_bot_id)
                        await self._record_trigger_dispatch_error(
                            source_task=task,
                            trigger_id=str(getattr(trigger, "id", "") or ""),
                            target_bot_id=str(target_bot_id or ""),
                            message=f"trigger target bot '{target_bot_id}' does not exist or is unavailable",
                        )
                        continue
                payloads = self._build_trigger_payloads(task, trigger)
                payloads = await self._apply_dynamic_pm_workstream_routing(
                    task,
                    trigger,
                    payloads,
                    default_target_bot_id=target_bot_id,
                )
                if not payloads:
                    logger.warning("[TRIGGER] SKIP trigger=%s task=%s reason=no_payloads target=%s", trigger.id, task.id, target_bot_id)
                    await self._record_trigger_dispatch_skip(
                        source_task=task,
                        trigger_id=str(getattr(trigger, "id", "") or ""),
                        target_bot_id=str(target_bot_id or ""),
                        details=self._describe_trigger_payload_skip(task, trigger),
                    )
                    continue
                base_child_metadata = self._trigger_child_metadata(
                    metadata,
                    parent_task_id=task.id,
                    trigger_rule_id=trigger.id,
                    inherit_metadata=bool(trigger.inherit_metadata),
                )
                is_fanout = bool(str(getattr(trigger, "fan_out_field", "") or "").strip())
                is_join = self._trigger_uses_join(trigger)
                dispatch_type = "join" if is_join else ("fanout" if is_fanout else "direct")
                logger.info(
                    "[TRIGGER] FIRE trigger=%s task=%s bot=%s dispatch=%s target=%s payloads=%d",
                    trigger.id,
                    task.id,
                    task.bot_id,
                    dispatch_type,
                    target_bot_id,
                    len(payloads),
                )
                compatible_join_triggers = (
                    self._compatible_join_triggers(workflow, task, trigger, target_bot_id)
                    if is_join
                    else None
                )
                if is_join:
                    await self._dispatch_join_trigger(
                        task,
                        trigger,
                        target_bot_id,
                        payloads,
                        base_child_metadata,
                        compatible_join_triggers=compatible_join_triggers,
                    )
                    continue
                for payload in payloads:
                    payload_target_bot_id = (
                        self._dynamic_pm_target_bot_id(payload, target_bot_id)
                        if task.bot_id == "pm-engineer"
                        else target_bot_id
                    )
                    if allowed_bot_ids and payload_target_bot_id not in allowed_bot_ids:
                        message = (
                            f"trigger target '{payload_target_bot_id}' is outside the orchestration allowlist for "
                            f"workflow '{metadata.workflow_graph_id or metadata.orchestration_id or 'unknown'}'"
                        )
                        logger.warning(
                            "[TRIGGER] SKIP trigger=%s task=%s reason=target_not_allowed target=%s",
                            trigger.id,
                            task.id,
                            payload_target_bot_id,
                        )
                        await self._record_trigger_dispatch_error(
                            source_task=task,
                            trigger_id=str(getattr(trigger, "id", "") or ""),
                            target_bot_id=str(payload_target_bot_id or ""),
                            message=message,
                        )
                        continue
                    if self._bot_registry is not None:
                        try:
                            await self._bot_registry.get(payload_target_bot_id)
                        except Exception:
                            logger.warning(
                                "[TRIGGER] SKIP trigger=%s task=%s reason=target_bot_missing target=%s",
                                trigger.id,
                                task.id,
                                payload_target_bot_id,
                            )
                            await self._record_trigger_dispatch_error(
                                source_task=task,
                                trigger_id=str(getattr(trigger, "id", "") or ""),
                                target_bot_id=str(payload_target_bot_id or ""),
                                message=f"trigger target bot '{payload_target_bot_id}' does not exist or is unavailable",
                            )
                            continue
                    branch_metadata = base_child_metadata
                    if isinstance(payload, dict):
                        if str(metadata.run_class or "").strip().lower() == "pm_assignment":
                            repeat_limit = max(1, _settings_int("workflow_route_repeat_limit", 3))
                            repeat_count = await self._workflow_route_repeat_count(task, payload_target_bot_id, payload)
                            if repeat_count >= repeat_limit:
                                logger.warning(
                                    "[TRIGGER] SKIP trigger=%s task=%s reason=workflow_loop_guard target=%s repeat_count=%s",
                                    trigger.id,
                                    task.id,
                                    payload_target_bot_id,
                                    repeat_count,
                                )
                                await self._create_pm_assignment_loop_escalation_task(
                                    source_task=task,
                                    target_bot_id=str(payload_target_bot_id or ""),
                                    trigger_id=str(getattr(trigger, "id", "") or ""),
                                    payload=payload,
                                    repeat_count=repeat_count,
                                    repeat_limit=repeat_limit,
                                    reason="route_repeat_limit",
                                )
                                await self._record_workflow_loop_guard_stop(
                                    source_task=task,
                                    trigger_id=str(getattr(trigger, "id", "") or ""),
                                    target_bot_id=str(payload_target_bot_id or ""),
                                    branch_identity=self._workflow_route_branch_identity(task, payload),
                                    failure_type=self._workflow_route_failure_type(task),
                                    repeat_count=repeat_count,
                                    repeat_limit=repeat_limit,
                                    reason="route_repeat_limit",
                                )
                                continue
                            target_repeat_count = await self._workflow_route_target_bot_repeat_count(
                                task,
                                payload_target_bot_id,
                                payload,
                            )
                            if target_repeat_count >= repeat_limit:
                                logger.warning(
                                    "[TRIGGER] SKIP trigger=%s task=%s reason=workflow_target_repeat_limit target=%s repeat_count=%s",
                                    trigger.id,
                                    task.id,
                                    payload_target_bot_id,
                                    target_repeat_count,
                                )
                                await self._create_pm_assignment_loop_escalation_task(
                                    source_task=task,
                                    target_bot_id=str(payload_target_bot_id or ""),
                                    trigger_id=str(getattr(trigger, "id", "") or ""),
                                    payload=payload,
                                    repeat_count=target_repeat_count,
                                    repeat_limit=repeat_limit,
                                    reason="target_bot_repeat_limit",
                                )
                                await self._record_workflow_loop_guard_stop(
                                    source_task=task,
                                    trigger_id=str(getattr(trigger, "id", "") or ""),
                                    target_bot_id=str(payload_target_bot_id or ""),
                                    branch_identity=self._workflow_route_branch_identity(task, payload),
                                    failure_type=self._workflow_route_failure_type(task),
                                    repeat_count=target_repeat_count,
                                    repeat_limit=repeat_limit,
                                    reason="target_bot_repeat_limit",
                                )
                                continue
                        branch_step_id = self._fanout_step_id(task, trigger, payload)
                        if branch_step_id:
                            existing = await self._find_task_by_step_id(branch_step_id, payload_target_bot_id)
                            if existing is not None and existing.status in {"queued", "running", "blocked", "completed"}:
                                logger.debug("[TRIGGER] SKIP trigger=%s branch_step_id=%s reason=already_exists status=%s", trigger.id, branch_step_id, existing.status)
                                continue
                            branch_metadata = base_child_metadata.model_copy(update={"step_id": branch_step_id})
                        fanout_id = payload.get("fanout_id") if isinstance(payload, dict) else None
                        fanout_idx = payload.get("fanout_index") if isinstance(payload, dict) else None
                        if fanout_id is not None:
                            logger.info(
                                "[FANOUT] trigger=%s source_task=%s fanout_id=%s branch=%s target=%s",
                                trigger.id,
                                task.id,
                                fanout_id,
                                fanout_idx,
                                payload_target_bot_id,
                            )
                    await self.create_task(
                        bot_id=payload_target_bot_id,
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
        # Detect orchestration step gap: all result-conditional triggers silently skipped → possible halt.
        if _result_mismatch_skipped:
            _forward_ids = [
                str(getattr(t, "id", "") or "")
                for t in triggers
                if getattr(t, "event", "") == event
                and getattr(t, "result_field", None)
            ]
            if set(_result_mismatch_skipped) >= set(_forward_ids) and _forward_ids:
                logger.warning(
                    "[TRIGGER] STEP_GAP task=%s bot=%s event=%s skipped=%s — all result-conditional triggers unmatched, orchestration may have halted",
                    task.id,
                    task.bot_id,
                    event,
                    _result_mismatch_skipped,
                )
                await self._record_trigger_dispatch_skip(
                    source_task=task,
                    trigger_id="step-gap",
                    target_bot_id="",
                    details={
                        "reason": "all_result_conditional_triggers_skipped",
                        "skipped_trigger_ids": _result_mismatch_skipped,
                        "event": event,
                        "result_keys": list(task.result.keys()) if isinstance(task.result, dict) else None,
                    },
                )

        try:
            await self._dispatch_dynamic_pm_supplemental_tasks(task)
        except Exception as exc:
            logger.exception("Dynamic PM supplemental dispatch failed for task %s", task.id)
            await self._record_trigger_dispatch_error(
                source_task=task,
                trigger_id="pm-dynamic-supplemental",
                target_bot_id="",
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

    def _trigger_child_metadata(
        self,
        metadata: TaskMetadata,
        *,
        parent_task_id: str,
        trigger_rule_id: str,
        step_id: str = "",
        inherit_metadata: bool = True,
    ) -> TaskMetadata:
        allowed_bot_ids = [
            str(item).strip()
            for item in (metadata.allowed_bot_ids or [])
            if str(item).strip()
        ]
        return TaskMetadata(
            user_id=metadata.user_id if inherit_metadata else None,
            project_id=metadata.project_id if inherit_metadata else None,
            source="bot_trigger",
            priority=metadata.priority if inherit_metadata else None,
            conversation_id=metadata.conversation_id if inherit_metadata else None,
            orchestration_id=metadata.orchestration_id if inherit_metadata else None,
            pipeline_name=metadata.pipeline_name if inherit_metadata else None,
            pipeline_entry_bot_id=metadata.pipeline_entry_bot_id if inherit_metadata else None,
            parent_task_id=parent_task_id,
            trigger_rule_id=trigger_rule_id,
            trigger_depth=int(metadata.trigger_depth or 0) + 1,
            workflow_root_task_id=metadata.workflow_root_task_id or parent_task_id,
            root_pm_bot_id=metadata.root_pm_bot_id,
            allowed_bot_ids=allowed_bot_ids,
            workflow_graph_id=metadata.workflow_graph_id,
            run_class=metadata.run_class,
            step_id=step_id or None,
        )

    def _build_default_trigger_payload(self, task: Task, target_bot_id: str) -> Dict[str, Any]:
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
        if isinstance(task.payload, dict):
            upstream_payload = task.payload.get("source_payload")
            if isinstance(upstream_payload, dict):
                self._promote_trigger_context_fields(base_payload, upstream_payload)
            self._promote_trigger_context_fields(base_payload, task.payload)
            current_context_fields = (
                "workstream",
                "workstream_index",
                "fanout_count",
                "fanout_id",
                "fanout_branch_key",
                "fanout_expected_branch_keys",
                "pm_routing_context",
                "root_pm_bot_id",
                "deterministic_signals",
                "depends_on_steps",
                "context_items",
                "assignment_request",
                "assignment_scope",
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

    def _payload_pm_routing_context(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        for node in self._payload_source_chain(payload):
            context = node.get("pm_routing_context")
            if isinstance(context, dict):
                return context
        return {}

    def _payload_chain_field(self, payload: Any, field_name: str) -> Any:
        if not isinstance(payload, dict):
            return None
        for node in self._payload_source_chain(payload):
            if field_name not in node:
                continue
            value = node.get(field_name)
            if _is_empty_contract_value(value):
                continue
            return value
        return None

    def _pm_assignment_requested_output_paths(self, payload: Dict[str, Any]) -> Set[str]:
        requested: Set[str] = set()
        for index, node in enumerate(self._payload_source_chain(payload)):
            if not isinstance(node, dict):
                continue
            scope = node.get("assignment_scope")
            if isinstance(scope, dict):
                for item in _normalize_string_list(scope.get("requested_output_paths")):
                    normalized = str(item or "").strip().replace("\\", "/").strip("`")
                    if normalized:
                        requested.add(normalized)
            # Only upstream assignment context should authorize repo outputs. The
            # current PM-generated branch payload may itself contain stray paths
            # (top-level files, temp SQL variants, test_logs outputs), and using
            # those generated paths as "requested" would defeat sanitization.
            if index == 0:
                continue
            for item in _normalize_string_list(node.get("deliverables")):
                normalized = str(item or "").strip().replace("\\", "/").strip("`")
                if _looks_like_repo_file(normalized):
                    requested.add(normalized)
            explicit_path = str(node.get("path") or "").strip().replace("\\", "/").strip("`")
            if _looks_like_repo_file(explicit_path):
                requested.add(explicit_path)
        return requested

    def _pm_assignment_path_is_requested(self, path: str, requested_paths: Set[str]) -> bool:
        normalized = str(path or "").strip().replace("\\", "/").strip("`")
        if not normalized:
            return False
        for requested in requested_paths:
            candidate = str(requested or "").strip().replace("\\", "/").strip("`")
            if not candidate:
                continue
            if normalized == candidate:
                return True
            if "/" not in candidate:
                continue
            if "." not in candidate.rsplit("/", 1)[-1] and normalized.startswith(candidate.rstrip("/") + "/"):
                return True
        return False

    def _pm_assignment_sql_variant_key(self, path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/").lower()
        if not normalized.endswith(".sql"):
            return ""
        leaf = normalized.rsplit("/", 1)[-1]
        if not any(token in normalized for token in ("migration", "schema", "ddl", "seed")):
            return ""
        return leaf

    def _pm_assignment_path_rank(self, path: str, requested_paths: Set[str]) -> Tuple[int, int, int, str]:
        normalized = str(path or "").strip().replace("\\", "/").lower()
        explicitly_requested = 0 if self._pm_assignment_path_is_requested(path, requested_paths) else 1
        temp_like = 1 if any(
            segment in normalized
            for segment in ("temp/", "/temp/", "tmp/", "/tmp/", "scratch/", "/scratch/", "/sql/tmp", "/sql/temp")
        ) else 0
        specialist_hint = 0 if any(
            marker in normalized
            for marker in ("/migrations/", "/migration/", "/database/", "/db/", "/schema/", "/sql/")
        ) else 1
        return (explicitly_requested, temp_like, specialist_hint, normalized)

    def _sanitize_pm_assignment_repo_paths(self, entries: List[str], payload: Dict[str, Any]) -> List[str]:
        requested_paths = self._pm_assignment_requested_output_paths(payload)
        kept: List[str] = []
        seen: Set[str] = set()
        canonical_sql_path = ""
        for raw_entry in entries:
            entry = str(raw_entry or "").strip().replace("\\", "/").strip("`")
            if not entry:
                continue
            if not _looks_like_repo_file(entry):
                if entry not in seen:
                    seen.add(entry)
                    kept.append(entry)
                continue
            if (
                not self._pm_assignment_path_is_requested(entry, requested_paths)
                and (
                    _is_assignment_execution_artifact_file(entry)
                    or "/" not in entry
                )
            ):
                continue
            if entry.lower().endswith(".sql"):
                if not canonical_sql_path or self._pm_assignment_path_rank(entry, requested_paths) < self._pm_assignment_path_rank(canonical_sql_path, requested_paths):
                    canonical_sql_path = entry
                continue
            if entry in seen:
                continue
            seen.add(entry)
            kept.append(entry)
        if canonical_sql_path and canonical_sql_path not in seen:
            seen.add(canonical_sql_path)
            kept.append(canonical_sql_path)
        return kept

    def _sanitize_pm_assignment_branch_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload)
        contract_bot_changed = False
        for field_name in ("bot_id", "target_bot_id", "assigned_bot_id"):
            raw_value = str(normalized.get(field_name) or "").strip()
            if raw_value != "pm-coder":
                normalized[field_name] = "pm-coder"
                contract_bot_changed = True
        role_hint = str(normalized.get("role_hint") or "").strip().lower()
        if role_hint not in {"coder", "developer", "engineer", "implementation"}:
            normalized["role_hint"] = "coder"
            contract_bot_changed = True
        step_kind = str(normalized.get("step_kind") or "").strip().lower()
        if step_kind != "repo_change":
            normalized["step_kind"] = "repo_change"
            contract_bot_changed = True
        deliverables = normalized.get("deliverables")
        if isinstance(deliverables, list):
            normalized["deliverables"] = self._sanitize_pm_assignment_repo_paths(
                _normalize_string_list(deliverables),
                normalized,
            )
        explicit_path = str(normalized.get("path") or "").strip().replace("\\", "/").strip("`")
        if explicit_path and _looks_like_repo_file(explicit_path):
            sanitized_paths = self._sanitize_pm_assignment_repo_paths([explicit_path], normalized)
            normalized["path"] = sanitized_paths[0] if sanitized_paths else ""
        workstream = normalized.get("workstream")
        if isinstance(workstream, dict):
            workstream_copy = dict(workstream)
            for field_name in ("bot_id", "target_bot_id", "assigned_bot_id"):
                raw_value = str(workstream_copy.get(field_name) or "").strip()
                if raw_value != "pm-coder":
                    workstream_copy[field_name] = "pm-coder"
                    contract_bot_changed = True
            workstream_role_hint = str(workstream_copy.get("role_hint") or "").strip().lower()
            if workstream_role_hint not in {"coder", "developer", "engineer", "implementation"}:
                workstream_copy["role_hint"] = "coder"
                contract_bot_changed = True
            workstream_step_kind = str(workstream_copy.get("step_kind") or "").strip().lower()
            if workstream_step_kind != "repo_change":
                workstream_copy["step_kind"] = "repo_change"
                contract_bot_changed = True
            workstream_deliverables = workstream_copy.get("deliverables")
            if isinstance(workstream_deliverables, list):
                workstream_copy["deliverables"] = self._sanitize_pm_assignment_repo_paths(
                    _normalize_string_list(workstream_deliverables),
                    normalized,
                )
            workstream_path = str(workstream_copy.get("path") or "").strip().replace("\\", "/").strip("`")
            if workstream_path and _looks_like_repo_file(workstream_path):
                sanitized_paths = self._sanitize_pm_assignment_repo_paths([workstream_path], normalized)
                workstream_copy["path"] = sanitized_paths[0] if sanitized_paths else ""
            normalized["workstream"] = workstream_copy
        if contract_bot_changed:
            existing_notes = normalized.get("normalization_notes")
            if not isinstance(existing_notes, list):
                existing_notes = []
            existing_notes.append("Coerced PM assignment workstream metadata to the contract-first pm-coder branch shape.")
            normalized["normalization_notes"] = existing_notes
        return normalized

    def _pm_assignment_payload_repo_paths(self, payload: Dict[str, Any]) -> List[str]:
        repo_paths: List[str] = []
        seen: Set[str] = set()
        for source in (
            payload,
            payload.get("workstream") if isinstance(payload.get("workstream"), dict) else None,
        ):
            if not isinstance(source, dict):
                continue
            explicit_path = str(source.get("path") or "").strip().replace("\\", "/").strip("`")
            if explicit_path and _looks_like_repo_file(explicit_path) and explicit_path not in seen:
                seen.add(explicit_path)
                repo_paths.append(explicit_path)
            for item in _normalize_string_list(source.get("deliverables")):
                normalized = str(item or "").strip().replace("\\", "/").strip("`")
                if not normalized or normalized in seen or not _looks_like_repo_file(normalized):
                    continue
                seen.add(normalized)
                repo_paths.append(normalized)
        return repo_paths

    def _pm_assignment_payload_repo_tokens(self, payload: Dict[str, Any]) -> List[str]:
        tokens: List[str] = []
        seen: Set[str] = set()
        for path in self._pm_assignment_payload_repo_paths(payload):
            normalized = str(path or "").strip().replace("\\", "/").lower()
            if not normalized:
                continue
            sql_variant_key = self._pm_assignment_sql_variant_key(normalized)
            token = f"sql_variant:{sql_variant_key}" if sql_variant_key else normalized
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _prune_pm_assignment_workstream_payloads(
        self,
        payloads: List[Any],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        if not payloads or not all(isinstance(payload, dict) for payload in payloads):
            return payloads, None
        kept: List[Any] = []
        dropped = 0
        for payload in payloads:
            original_paths = self._pm_assignment_payload_repo_paths(payload)
            if not original_paths:
                kept.append(payload)
                continue
            sanitized_payload = self._sanitize_pm_assignment_branch_payload(payload)
            sanitized_paths = self._pm_assignment_payload_repo_paths(sanitized_payload)
            if sanitized_paths:
                kept.append(sanitized_payload)
                continue
            if all(
                _is_assignment_execution_artifact_file(path)
                or "/" not in path
                or bool(self._pm_assignment_sql_variant_key(path))
                for path in original_paths
            ):
                dropped += 1
                continue
            kept.append(sanitized_payload)
        if dropped == 0:
            return payloads, None
        return kept, {
            "applied": True,
            "reason": "pm_assignment_workstream_path_filter",
            "original_count": len(payloads),
            "kept_count": len(kept),
            "dropped_count": dropped,
        }

    def _sanitize_pm_assignment_result(self, task: Task, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        if task.bot_id != "pm-engineer":
            return result
        if not self._pm_assignment_dynamic_management_enabled(task):
            return result
        workstreams = result.get("implementation_workstreams")
        if not isinstance(workstreams, list) or not all(isinstance(item, dict) for item in workstreams):
            return result

        normalized = dict(result)
        sanitized_workstreams, path_filter_budget = self._prune_pm_assignment_workstream_payloads(workstreams)
        sanitized_workstreams = [
            self._sanitize_pm_assignment_branch_payload(item)
            for item in sanitized_workstreams
            if isinstance(item, dict)
        ]
        sanitized_workstreams, dedupe_budget = self._dedupe_pm_assignment_workstream_payloads(sanitized_workstreams)
        sanitized_workstreams, workstream_budget = self._sanitize_pm_assignment_result_workstream_budget(
            task,
            sanitized_workstreams,
        )
        normalized["implementation_workstreams"] = sanitized_workstreams

        normalization_notes = normalized.get("normalization_notes")
        if not isinstance(normalization_notes, list):
            normalization_notes = []
        if path_filter_budget:
            normalization_notes.append(
                "Filtered PM assignment implementation_workstreams to drop artifact-only or stray-path branches."
            )
        if dedupe_budget:
            normalization_notes.append(
                "Deduplicated PM assignment implementation_workstreams before downstream routing."
            )
        if workstream_budget:
            normalization_notes.append(
                "Capped PM assignment implementation_workstreams to the bounded downstream branch budget."
            )
        if any(
            "contract-first pm-coder branch shape" in str(item)
            for payload in sanitized_workstreams
            for item in (
                payload.get("normalization_notes")
                if isinstance(payload.get("normalization_notes"), list)
                else []
            )
        ):
            normalization_notes.append(
                "Coerced PM assignment implementation_workstreams to contract-first pm-coder branches."
            )
        if normalization_notes:
            normalized["normalization_notes"] = normalization_notes
        return normalized

    def _sanitize_pm_assignment_result_workstream_budget(
        self,
        task: Task,
        payloads: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if not payloads:
            return payloads, None
        policy: Dict[str, Any] = {
            "consulted_root_system_prompt": False,
        }
        payload_root_pm_bot_id = str(self._payload_chain_field(task.payload, "root_pm_bot_id") or "").strip()
        root_pm_bot_id = str((task.metadata or TaskMetadata()).root_pm_bot_id or payload_root_pm_bot_id or "").strip()
        if root_pm_bot_id:
            policy["root_pm_bot_id"] = root_pm_bot_id
        deterministic_signals = self._payload_chain_field(task.payload, "deterministic_signals")
        if isinstance(deterministic_signals, dict):
            policy["deterministic_signals"] = deterministic_signals
        routes = [
            self._classify_pm_workstream_route(
                payload,
                default_target_bot_id="pm-coder",
                policy=policy,
            )
            for payload in payloads
        ]
        capped_payloads, _, budget = self._pm_assignment_workstream_budget(payloads, routes)
        return capped_payloads, budget

    def _pm_assignment_workstream_trigger_items(
        self,
        task: Task,
        trigger: Any,
        items: List[Any],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        metadata = task.metadata or TaskMetadata()
        if task.bot_id != "pm-engineer":
            return items, None
        if str(getattr(trigger, "target_bot_id", "") or "").strip() != "pm-coder":
            return items, None
        if str(metadata.run_class or "").strip().lower() != "pm_assignment":
            return items, None
        if not items or not all(isinstance(item, dict) for item in items):
            return items, None

        payloads, path_filter_budget = self._prune_pm_assignment_workstream_payloads(items)
        payloads = [self._sanitize_pm_assignment_branch_payload(payload) for payload in payloads if isinstance(payload, dict)]
        payloads, dedupe_budget = self._dedupe_pm_assignment_workstream_payloads(payloads)
        policy: Dict[str, Any] = {
            "consulted_root_system_prompt": False,
        }
        payload_root_pm_bot_id = str(self._payload_chain_field(task.payload, "root_pm_bot_id") or "").strip()
        root_pm_bot_id = str((metadata.root_pm_bot_id or payload_root_pm_bot_id or "")).strip()
        if root_pm_bot_id:
            policy["root_pm_bot_id"] = root_pm_bot_id
        deterministic_signals = self._payload_chain_field(task.payload, "deterministic_signals")
        if isinstance(deterministic_signals, dict):
            policy["deterministic_signals"] = deterministic_signals
        routes = [
            self._classify_pm_workstream_route(
                payload,
                default_target_bot_id="pm-coder",
                policy=policy,
            )
            for payload in payloads
        ]
        payloads, _, workstream_budget = self._pm_assignment_workstream_budget(payloads, routes)

        combined_budget = None
        if path_filter_budget or dedupe_budget or workstream_budget:
            combined_budget = {}
            if path_filter_budget:
                combined_budget.update(path_filter_budget)
            if dedupe_budget:
                combined_budget.update(dedupe_budget)
            if workstream_budget:
                combined_budget.update(workstream_budget)
        return payloads, combined_budget

    def _dedupe_pm_assignment_workstream_payloads(
        self,
        payloads: List[Any],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        if not payloads or not all(isinstance(payload, dict) for payload in payloads):
            return payloads, None
        deduped: List[Any] = []
        seen: Set[str] = set()
        seen_repo_fingerprints: Set[str] = set()
        for payload in payloads:
            fingerprint_payload = {
                "title": str(payload.get("title") or "").strip().lower(),
                "instruction": str(payload.get("instruction") or "").strip().lower(),
                "path": str(payload.get("path") or "").strip().replace("\\", "/").lower(),
                "deliverables": sorted(
                    str(item or "").strip().replace("\\", "/").lower()
                    for item in _normalize_string_list(payload.get("deliverables"))
                    if str(item or "").strip()
                ),
                "target_bot_id": str(payload.get("target_bot_id") or "").strip().lower(),
                "role_hint": str(payload.get("role_hint") or "").strip().lower(),
            }
            fingerprint = json.dumps(fingerprint_payload, sort_keys=True)
            if fingerprint in seen:
                continue
            repo_paths = self._pm_assignment_payload_repo_tokens(payload)
            repo_fingerprint = ""
            if repo_paths:
                repo_fingerprint = json.dumps(
                    {
                        "repo_paths": repo_paths,
                        "target_bot_id": str(payload.get("target_bot_id") or "").strip().lower(),
                        "role_hint": str(payload.get("role_hint") or "").strip().lower(),
                    },
                    sort_keys=True,
                )
                if repo_fingerprint in seen_repo_fingerprints:
                    continue
            seen.add(fingerprint)
            if repo_fingerprint:
                seen_repo_fingerprints.add(repo_fingerprint)
            deduped.append(payload)
        if len(deduped) == len(payloads):
            return payloads, None
        return deduped, {
            "applied": True,
            "reason": "pm_assignment_workstream_dedupe",
            "original_count": len(payloads),
            "kept_count": len(deduped),
        }

    def _pm_workstream_explicit_route(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidates = [
            str(payload.get("bot_id") or "").strip().lower(),
            str(payload.get("target_bot_id") or "").strip().lower(),
            str(payload.get("assigned_bot_id") or "").strip().lower(),
            str(payload.get("role_hint") or "").strip().lower(),
        ]
        workstream = payload.get("workstream")
        if isinstance(workstream, dict):
            candidates.extend(
                [
                    str(workstream.get("bot_id") or "").strip().lower(),
                    str(workstream.get("target_bot_id") or "").strip().lower(),
                    str(workstream.get("assigned_bot_id") or "").strip().lower(),
                    str(workstream.get("role_hint") or "").strip().lower(),
                ]
            )
        if any(candidate in {"pm-database-engineer", "database_engineer", "dba", "dba-sql"} for candidate in candidates):
            return {
                "route_kind": "database_coder_branch",
                "target_bot_id": "pm-coder",
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "explicit_workstream_route_coerced_to_coder",
            }
        if any(candidate in {"pm-coder", "frontend_developer", "ui", "ui_tester"} for candidate in candidates):
            haystack = self._pm_workstream_routing_text(payload)
            if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_UI_KEYWORDS):
                return {
                    "route_kind": "ui_coder_validation",
                    "target_bot_id": "pm-coder",
                    "branch_completion_bot_ids": ["pm-security-reviewer"],
                    "route_reason": "explicit_workstream_route",
                }
        return None

    def _pm_dynamic_completion_token(self, branch_key: str, bot_id: str) -> str:
        return f"{self._normalize_branch_token(branch_key, fallback='branch')}:{str(bot_id or '').strip()}"

    def _pm_dynamic_progress_result(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        outcome = str(result.get("outcome") or result.get("status") or "").strip().lower()
        return outcome in {"pass", "skip", "completed", "complete"}

    async def _load_pm_workstream_routing_policy(self, task: Task) -> Dict[str, Any]:
        metadata = task.metadata or TaskMetadata()
        payload_root_pm_bot_id = str(self._payload_chain_field(task.payload, "root_pm_bot_id") or "").strip()
        deterministic_signals = self._payload_chain_field(task.payload, "deterministic_signals")
        policy: Dict[str, Any] = {
            "root_pm_bot_id": str(metadata.root_pm_bot_id or payload_root_pm_bot_id or "").strip(),
            "consulted_root_system_prompt": False,
        }
        if isinstance(deterministic_signals, dict):
            policy["deterministic_signals"] = deterministic_signals
        root_pm_bot_id = policy["root_pm_bot_id"]
        if not root_pm_bot_id or self._bot_registry is None:
            return policy
        try:
            root_pm_bot = await self._bot_registry.get(root_pm_bot_id)
        except Exception:
            return policy
        prompt = str(getattr(root_pm_bot, "system_prompt", None) or "").strip().lower()
        if prompt:
            policy["consulted_root_system_prompt"] = True
        return policy

    def _pm_workstream_routing_text(self, payload: Dict[str, Any]) -> str:
        parts: List[str] = []
        workstream = payload.get("workstream")
        for source in (payload, workstream if isinstance(workstream, dict) else None):
            if not isinstance(source, dict):
                continue
            for field in ("title", "instruction", "test_strategy", "path"):
                value = str(source.get(field) or "").strip()
                if value:
                    parts.append(value)
            for field in ("scope", "deliverables", "acceptance_criteria"):
                for item in _normalize_string_list(source.get(field)):
                    parts.append(item)
        return "\n".join(parts).strip().lower()

    def _pm_workstream_matches_any_keyword(self, haystack: str, keywords: Tuple[str, ...]) -> bool:
        text = str(haystack or "").strip().lower()
        if not text:
            return False
        for keyword in keywords:
            token = str(keyword or "").strip().lower()
            if not token:
                continue
            if token.startswith("."):
                if token in text:
                    return True
                continue
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
                return True
        return False

    def _pm_workstream_has_ui_repo_hint(self, payload: Dict[str, Any]) -> bool:
        for source in (
            payload,
            payload.get("workstream") if isinstance(payload.get("workstream"), dict) else None,
        ):
            if not isinstance(source, dict):
                continue
            candidates = [str(source.get("path") or "").strip()]
            candidates.extend(_normalize_string_list(source.get("deliverables")))
            for candidate in candidates:
                normalized = str(candidate or "").strip().replace("\\", "/").lower()
                if not normalized:
                    continue
                if any(hint in normalized for hint in _PM_WORKSTREAM_UI_PATH_HINTS):
                    return True
        return False

    def _pm_assignment_workstream_fanout_cap(self) -> Tuple[int, int]:
        base_cap = max(1, _settings_int("pm_assignment_workstream_fanout_limit", _PM_ASSIGNMENT_WORKSTREAM_STEP_CAP))
        split_cap = max(base_cap, _settings_int("pm_assignment_workstream_fanout_split_limit", _PM_ASSIGNMENT_WORKSTREAM_STEP_SPLIT_CAP))
        return base_cap, split_cap

    def _pm_assignment_workstream_is_split(self, payload: Dict[str, Any]) -> bool:
        haystack = self._pm_workstream_routing_text(payload)
        if any(marker in haystack for marker in _PM_ASSIGNMENT_WORKSTREAM_SPLIT_MARKERS):
            return True
        return re.search(r"\b(?:part|chunk|batch|shard|segment|slice)\s+\d+\b", haystack) is not None

    def _pm_assignment_workstream_lane(self, payload: Dict[str, Any], route: Dict[str, Any]) -> str:
        route_kind = str(route.get("route_kind") or "").strip()
        if route_kind == "database_coder_branch":
            return "database"
        if route_kind == "ui_coder_validation":
            return "ui"
        haystack = self._pm_workstream_routing_text(payload)
        if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_API_KEYWORDS):
            return "api"
        if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_SECURITY_KEYWORDS):
            return "security"
        if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_WORKER_KEYWORDS):
            return "worker"
        if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_TRIGGER_KEYWORDS):
            return "trigger"
        if self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_OPERATIONS_KEYWORDS):
            return "operations"
        return "generic"

    def _pm_assignment_workstream_budget(
        self,
        payloads: List[Dict[str, Any]],
        routes: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        original_count = len(payloads)
        base_cap, split_cap = self._pm_assignment_workstream_fanout_cap()
        if original_count <= base_cap:
            return payloads, routes, None

        split_required = any(self._pm_assignment_workstream_is_split(payload) for payload in payloads[base_cap:])
        budget = split_cap if split_required else base_cap
        if original_count <= budget:
            return payloads, routes, None

        selected_indexes: List[int] = []
        seen_lanes: Set[str] = set()
        preserved_lanes: List[str] = []
        for index, (payload, route) in enumerate(zip(payloads, routes)):
            lane = self._pm_assignment_workstream_lane(payload, route)
            if lane == "generic" or lane in seen_lanes:
                continue
            selected_indexes.append(index)
            seen_lanes.add(lane)
            preserved_lanes.append(lane)
            if len(selected_indexes) >= budget:
                break
        if len(selected_indexes) < budget:
            for index in range(original_count):
                if index in selected_indexes:
                    continue
                selected_indexes.append(index)
                if len(selected_indexes) >= budget:
                    break

        selected_indexes.sort()
        trimmed_payloads = [payloads[index] for index in selected_indexes]
        trimmed_routes = [routes[index] for index in selected_indexes]
        return trimmed_payloads, trimmed_routes, {
            "applied": True,
            "reason": "pm_assignment_workstream_cap",
            "original_count": original_count,
            "kept_count": len(trimmed_payloads),
            "split_required": split_required,
            "preserved_lanes": preserved_lanes,
        }

    def _refresh_fanout_payload_metadata(self, payloads: List[Any]) -> List[Any]:
        if not payloads or not all(isinstance(payload, dict) for payload in payloads):
            return payloads
        normalized_payloads = [dict(payload) for payload in payloads]
        branch_keys: List[str] = []
        seen_branch_keys: Set[str] = set()
        for payload in normalized_payloads:
            branch_key = self._resolve_join_branch_key(payload)
            if not branch_key or branch_key in seen_branch_keys:
                continue
            seen_branch_keys.add(branch_key)
            branch_keys.append(branch_key)
        for payload in normalized_payloads:
            payload["fanout_count"] = len(normalized_payloads)
            if branch_keys:
                payload["fanout_expected_branch_keys"] = list(branch_keys)
        return normalized_payloads

    def _classify_pm_workstream_route(
        self,
        payload: Dict[str, Any],
        *,
        default_target_bot_id: str,
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        if _payload_is_docs_only_request(payload):
            return {
                "route_kind": "generic_coder",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "docs_only_passthrough",
            }

        explicit_route = self._pm_workstream_explicit_route(payload)
        if explicit_route is not None:
            return explicit_route

        haystack = self._pm_workstream_routing_text(payload)
        consulted_prompt = bool(policy.get("consulted_root_system_prompt"))
        deterministic_signals = policy.get("deterministic_signals") if isinstance(policy.get("deterministic_signals"), dict) else {}
        database_match = self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_DATABASE_KEYWORDS)
        ui_match = self._pm_workstream_matches_any_keyword(haystack, _PM_WORKSTREAM_UI_KEYWORDS)
        if ui_match and (not database_match or self._pm_workstream_has_ui_repo_hint(payload)):
            return {
                "route_kind": "ui_coder_validation",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "system_prompt+keyword" if consulted_prompt else "keyword",
            }
        if database_match:
            return {
                "route_kind": "database_coder_branch",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "system_prompt+keyword_db_via_coder" if consulted_prompt else "keyword_db_via_coder",
            }
        if ui_match:
            return {
                "route_kind": "ui_coder_validation",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "system_prompt+keyword" if consulted_prompt else "keyword",
            }
        if bool(deterministic_signals.get("missing_downstream_stage_db")):
            return {
                "route_kind": "database_coder_branch",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "deterministic_signal_db_via_coder",
            }
        if bool(deterministic_signals.get("missing_downstream_stage_ui")):
            return {
                "route_kind": "ui_coder_validation",
                "target_bot_id": default_target_bot_id,
                "branch_completion_bot_ids": ["pm-security-reviewer"],
                "route_reason": "deterministic_signal_ui",
            }
        return {
            "route_kind": "generic_coder",
            "target_bot_id": default_target_bot_id,
            "branch_completion_bot_ids": ["pm-security-reviewer"],
            "route_reason": "default",
        }

    def _pm_assignment_dynamic_management_enabled(self, task: Task) -> bool:
        metadata = task.metadata or TaskMetadata()
        if str(metadata.run_class or "").strip().lower() == "pm_assignment":
            return True
        payload_root_pm_bot_id = str(self._payload_chain_field(task.payload, "root_pm_bot_id") or "").strip()
        if str(metadata.root_pm_bot_id or payload_root_pm_bot_id or "").strip():
            return True
        deterministic_signals = self._payload_chain_field(task.payload, "deterministic_signals")
        if not isinstance(deterministic_signals, dict):
            return False
        return any(
            bool(deterministic_signals.get(signal_name))
            for signal_name in (
                "missing_downstream_stage_db",
                "missing_downstream_stage_ui",
                "missing_downstream_stage_final_qc",
            )
        )

    async def _apply_dynamic_pm_workstream_routing(
        self,
        task: Task,
        trigger: Any,
        payloads: List[Any],
        *,
        default_target_bot_id: str,
    ) -> List[Any]:
        metadata = task.metadata or TaskMetadata()
        if task.bot_id != "pm-engineer":
            return payloads
        if default_target_bot_id != "pm-coder":
            return payloads
        if not str(getattr(trigger, "fan_out_field", "") or "").strip():
            return payloads
        if not self._pm_assignment_dynamic_management_enabled(task):
            return payloads
        if not payloads or not all(isinstance(payload, dict) for payload in payloads):
            return payloads

        payloads, path_filter_budget = self._prune_pm_assignment_workstream_payloads(payloads)
        if not payloads:
            return []
        payloads = [self._sanitize_pm_assignment_branch_payload(payload) for payload in payloads]
        payloads, dedupe_budget = self._dedupe_pm_assignment_workstream_payloads(payloads)
        policy = await self._load_pm_workstream_routing_policy(task)
        routes = [
            self._classify_pm_workstream_route(
                payload,
                default_target_bot_id=default_target_bot_id,
                policy=policy,
            )
            for payload in payloads
            if isinstance(payload, dict)
        ]
        payloads, routes, workstream_budget = self._pm_assignment_workstream_budget(payloads, routes)
        payloads = self._refresh_fanout_payload_metadata(payloads)
        combined_budget = None
        if path_filter_budget or dedupe_budget or workstream_budget:
            combined_budget = {}
            if path_filter_budget:
                combined_budget.update(path_filter_budget)
            if dedupe_budget:
                combined_budget.update(dedupe_budget)
            if workstream_budget:
                combined_budget.update(workstream_budget)

        global_tokens: List[str] = []
        branch_specs: List[Dict[str, Any]] = []
        for index, (payload, route) in enumerate(zip(payloads, routes)):
            if not isinstance(payload, dict):
                continue
            branch_key = (
                str(payload.get("fanout_branch_key") or "").strip()
                or self._resolve_join_branch_key(payload)
                or self._normalize_branch_token(index, fallback=f"branch-{index}")
            )
            completion_bot_ids = [
                str(bot_id).strip()
                for bot_id in (route.get("branch_completion_bot_ids") or [])
                if str(bot_id).strip()
            ]
            completion_tokens = [
                self._pm_dynamic_completion_token(branch_key, bot_id)
                for bot_id in completion_bot_ids
            ]
            global_tokens.extend(completion_tokens)
            branch_specs.append(
                {
                    "branch_key": branch_key,
                    "completion_bot_ids": completion_bot_ids,
                    "completion_tokens": completion_tokens,
                }
            )

        deduped_global_tokens: List[str] = []
        seen_tokens: Set[str] = set()
        for token in global_tokens:
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            deduped_global_tokens.append(token)

        for payload, route, branch_spec in zip(payloads, routes, branch_specs):
            if not isinstance(payload, dict):
                continue
            if combined_budget:
                existing_budget = payload.get("pm_fanout_budget") if isinstance(payload.get("pm_fanout_budget"), dict) else {}
                merged_budget = dict(existing_budget)
                merged_budget.update(combined_budget)
                payload["pm_fanout_budget"] = merged_budget
            payload["pm_routing_context"] = {
                "dynamic": True,
                "fanout_id": str(payload.get("fanout_id") or "").strip(),
                "branch_key": branch_spec["branch_key"],
                "route_kind": str(route.get("route_kind") or "").strip(),
                "target_bot_id": str(route.get("target_bot_id") or default_target_bot_id).strip(),
                "route_reason": str(route.get("route_reason") or "").strip(),
                "branch_completion_bot_ids": list(branch_spec["completion_bot_ids"]),
                "branch_completion_tokens": list(branch_spec["completion_tokens"]),
                "global_completion_tokens": list(deduped_global_tokens),
                "security_bot_id": "pm-security-reviewer",
                "final_qc_bot_id": "pm-final-qc",
                "consulted_root_system_prompt": bool(policy.get("consulted_root_system_prompt")),
                "root_pm_bot_id": str(policy.get("root_pm_bot_id") or metadata.root_pm_bot_id or "").strip(),
                "deterministic_signals": dict(policy.get("deterministic_signals") or {}),
            }
        return payloads

    def _dynamic_pm_target_bot_id(self, payload: Any, default_target_bot_id: str) -> str:
        context = self._payload_pm_routing_context(payload)
        target_bot_id = str(context.get("target_bot_id") or "").strip()
        return target_bot_id or default_target_bot_id

    def _pm_dynamic_global_stage_context(self, context: Dict[str, Any], *, global_stage: str) -> Dict[str, Any]:
        normalized = dict(context)
        stage_name = str(global_stage or "").strip()
        if not stage_name:
            return normalized
        normalized["global_stage"] = stage_name
        normalized["branch_key"] = f"global:{stage_name}"
        if stage_name == "database":
            normalized["route_kind"] = "global_database_stage"
            normalized["branch_completion_bot_ids"] = []
            normalized["branch_completion_tokens"] = []
        elif stage_name == "security_review":
            normalized["route_kind"] = "global_security_review"
            normalized["branch_completion_bot_ids"] = ["pm-tester"]
            normalized["branch_completion_tokens"] = [
                self._pm_dynamic_completion_token(normalized["branch_key"], "pm-tester")
            ]
        elif stage_name == "ui_validation":
            normalized["route_kind"] = "global_ui_validation"
            normalized["branch_completion_bot_ids"] = []
            normalized["branch_completion_tokens"] = []
        elif stage_name == "final_qc":
            normalized["route_kind"] = "global_final_qc"
            normalized["branch_completion_bot_ids"] = []
            normalized["branch_completion_tokens"] = []
        return normalized

    def _should_skip_dynamic_pm_trigger(self, task: Task, *, target_bot_id: str) -> bool:
        if not self._pm_assignment_dynamic_management_enabled(task):
            return False
        if not isinstance(task.payload, dict):
            return False
        signal_by_stage = {
            "pm-database-engineer": "missing_downstream_stage_db",
            "pm-ui-tester": "missing_downstream_stage_ui",
            "pm-final-qc": "missing_downstream_stage_final_qc",
        }
        signal_name = signal_by_stage.get(str(target_bot_id or "").strip())
        if not signal_name:
            return False
        context = self._payload_pm_routing_context(task.payload)
        deterministic_signals = (
            context.get("deterministic_signals")
            if isinstance(context.get("deterministic_signals"), dict)
            else self._payload_chain_field(task.payload, "deterministic_signals")
        )
        if not isinstance(deterministic_signals, dict):
            return False
        return bool(deterministic_signals.get(signal_name))

    def _pm_assignment_stage_exclusion_reasons(self, payload: Any) -> Dict[str, str]:
        if not isinstance(payload, dict):
            return {}
        scope = _payload_assignment_scope(payload)
        raw_reasons = scope.get("explicit_stage_exclusion_reasons")
        if isinstance(raw_reasons, dict):
            return {
                str(stage_id).strip(): str(reason).strip()
                for stage_id, reason in raw_reasons.items()
                if str(stage_id).strip()
            }
        raw_exclusions = scope.get("explicit_stage_exclusions")
        if not isinstance(raw_exclusions, list):
            return {}
        return {
            str(stage_id).strip(): "assignment_scope_exclusion"
            for stage_id in raw_exclusions
            if str(stage_id).strip()
        }

    def _pm_assignment_stage_is_excluded(self, payload: Any, stage_id: str) -> bool:
        return str(stage_id or "").strip() in self._pm_assignment_stage_exclusion_reasons(payload)

    def _pm_dynamic_context(self, task: Task) -> Dict[str, Any]:
        if not isinstance(task.payload, dict):
            return {}
        context = dict(self._payload_pm_routing_context(task.payload))
        if "dynamic" not in context:
            context["dynamic"] = bool(context)
        if not context.get("fanout_id"):
            context["fanout_id"] = str(self._resolve_fanout_id(task.payload) or "").strip()
        deterministic_signals = context.get("deterministic_signals")
        if not isinstance(deterministic_signals, dict):
            deterministic_signals = self._payload_chain_field(task.payload, "deterministic_signals")
        if isinstance(deterministic_signals, dict):
            context["deterministic_signals"] = dict(deterministic_signals)
        if not context.get("root_pm_bot_id"):
            payload_root_pm_bot_id = str(self._payload_chain_field(task.payload, "root_pm_bot_id") or "").strip()
            metadata = task.metadata or TaskMetadata()
            context["root_pm_bot_id"] = str(metadata.root_pm_bot_id or payload_root_pm_bot_id or "").strip()
        return context

    async def _find_dynamic_pm_stage_task(
        self,
        task: Task,
        *,
        bot_id: str,
        fanout_id: str,
        global_stage: str,
    ) -> Optional[Task]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        orchestration_id = str(metadata.orchestration_id or "").strip()
        project_id = str(metadata.project_id or "").strip()
        best_match: Optional[Task] = None
        for candidate in self._tasks.values():
            candidate_meta = candidate.metadata or TaskMetadata()
            if str(candidate.bot_id or "").strip() != str(bot_id or "").strip():
                continue
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if str(candidate_meta.orchestration_id or "").strip() != orchestration_id:
                continue
            if str(candidate_meta.project_id or "").strip() != project_id:
                continue
            candidate_context = self._payload_pm_routing_context(candidate.payload)
            candidate_global_stage = str(candidate_context.get("global_stage") or "").strip()
            if candidate_global_stage and candidate_global_stage != global_stage:
                continue
            candidate_fanout_id = (
                str(candidate_context.get("fanout_id") or "").strip()
                or (
                    str(self._resolve_fanout_id(candidate.payload) or "").strip()
                    if isinstance(candidate.payload, dict)
                    else ""
                )
            )
            if fanout_id and candidate_fanout_id != fanout_id:
                continue
            if best_match is None or self._task_order_token(candidate) >= self._task_order_token(best_match):
                best_match = candidate
        return best_match

    async def _create_dynamic_pm_database_task(self, task: Task, context: Dict[str, Any]) -> None:
        metadata = task.metadata or TaskMetadata()
        expected_tokens = [
            str(token).strip()
            for token in (context.get("global_completion_tokens") or [])
            if str(token).strip()
        ]
        fanout_id = str(context.get("fanout_id") or "").strip()
        if not expected_tokens or not fanout_id:
            return
        matched_tasks = await self._collect_dynamic_pm_completion_task_map(task, context)
        if any(token not in matched_tasks for token in expected_tokens):
            return
        existing = await self._find_dynamic_pm_stage_task(
            task,
            bot_id="pm-database-engineer",
            fanout_id=fanout_id,
            global_stage="database",
        )
        if self._dynamic_pm_stage_blocks_creation(existing):
            return
        payload = self._build_default_trigger_payload(task, "pm-database-engineer")
        ordered_tasks = [matched_tasks[token] for token in expected_tokens]
        payload["title"] = "Database migration and validation"
        payload["instruction"] = (
            "After all required implementation security branches pass or skip, produce the single canonical "
            "database migration or schema validation result for the assignment. Return skip/not_applicable when no "
            "database change is required."
        )
        payload["join_task_ids"] = [matched.id for matched in ordered_tasks]
        payload["join_results"] = [matched.result for matched in ordered_tasks]
        payload["join_items"] = [
            {
                "source_task_id": matched.id,
                "source_bot_id": matched.bot_id,
                "source_payload": matched.payload,
                "source_result": matched.result,
                "completion_token": token,
            }
            for token, matched in zip(expected_tokens, ordered_tasks)
        ]
        payload["pm_routing_context"] = self._pm_dynamic_global_stage_context(context, global_stage="database")
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=task.id,
            trigger_rule_id="pm-dynamic-database-gate",
            step_id=f"pm-dynamic-db:{fanout_id}",
        )
        await self.create_task(
            bot_id="pm-database-engineer",
            payload=payload,
            metadata=child_metadata,
        )

    async def _create_dynamic_pm_global_ui_validation_task(self, task: Task, context: Dict[str, Any]) -> None:
        metadata = task.metadata or TaskMetadata()
        fanout_id = str(context.get("fanout_id") or "").strip()
        if not fanout_id:
            return
        existing = await self._find_dynamic_pm_stage_task(
            task,
            bot_id="pm-ui-tester",
            fanout_id=fanout_id,
            global_stage="ui_validation",
        )
        if self._dynamic_pm_stage_blocks_creation(existing):
            return
        payload = self._build_default_trigger_payload(task, "pm-ui-tester")
        payload["title"] = "UI validation"
        payload["instruction"] = (
            "Run the single UI validation stage after database work is complete. Preserve build_only mode when UI "
            "browser automation is unavailable or intentionally disabled for this run."
        )
        payload["pm_routing_context"] = self._pm_dynamic_global_stage_context(context, global_stage="ui_validation")
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=task.id,
            trigger_rule_id="pm-dynamic-ui-gate",
            step_id=f"pm-dynamic-ui:{fanout_id}",
        )
        await self.create_task(
            bot_id="pm-ui-tester",
            payload=payload,
            metadata=child_metadata,
        )

    async def _collect_dynamic_pm_completion_task_map(
        self,
        task: Task,
        context: Dict[str, Any],
    ) -> Dict[str, Task]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        fanout_id = str(context.get("fanout_id") or "").strip()
        expected_tokens = {
            str(token).strip()
            for token in (context.get("global_completion_tokens") or [])
            if str(token).strip()
        }
        matched: Dict[str, Task] = {}
        for candidate in self._tasks.values():
            candidate_meta = candidate.metadata or TaskMetadata()
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if metadata.orchestration_id != candidate_meta.orchestration_id:
                continue
            if metadata.project_id != candidate_meta.project_id:
                continue
            if candidate.status != "completed" or not isinstance(candidate.payload, dict):
                continue
            candidate_context = self._payload_pm_routing_context(candidate.payload)
            if not bool(candidate_context.get("dynamic")):
                continue
            if str(candidate_context.get("fanout_id") or "").strip() != fanout_id:
                continue
            if not self._pm_dynamic_progress_result(candidate.result):
                continue
            branch_key = str(candidate_context.get("branch_key") or "").strip()
            if not branch_key:
                continue
            token = self._pm_dynamic_completion_token(branch_key, candidate.bot_id)
            if token not in expected_tokens:
                continue
            existing = matched.get(token)
            if existing is None or self._task_order_token(candidate) >= self._task_order_token(existing):
                matched[token] = candidate
        return matched

    async def _create_dynamic_pm_ui_validation_task(self, task: Task, context: Dict[str, Any]) -> None:
        metadata = task.metadata or TaskMetadata()
        fanout_id = str(context.get("fanout_id") or "").strip()
        branch_key = str(context.get("branch_key") or "").strip()
        if not fanout_id or not branch_key:
            return
        step_id = f"pm-dynamic-ui:{fanout_id}:{branch_key}"
        existing = await self._find_task_by_step_id(step_id, "pm-ui-tester")
        if existing is not None and existing.status in {"queued", "blocked", "running", "completed"}:
            return
        payload = self._build_default_trigger_payload(task, "pm-ui-tester")
        payload["title"] = str(payload.get("title") or f"Validate UI workstream {branch_key}")
        payload["instruction"] = "Validate the completed UI workstream with user-facing UI checks."
        payload["pm_routing_context"] = dict(context)
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=task.id,
            trigger_rule_id="pm-dynamic-ui-validation",
            step_id=step_id,
        )
        await self.create_task(
            bot_id="pm-ui-tester",
            payload=payload,
            metadata=child_metadata,
        )

    async def _maybe_create_dynamic_pm_global_security_task(
        self,
        task: Task,
        context: Dict[str, Any],
    ) -> None:
        metadata = task.metadata or TaskMetadata()
        expected_tokens = [
            str(token).strip()
            for token in (context.get("global_completion_tokens") or [])
            if str(token).strip()
        ]
        fanout_id = str(context.get("fanout_id") or "").strip()
        security_bot_id = str(context.get("security_bot_id") or "pm-security-reviewer").strip()
        if not expected_tokens or not fanout_id or not security_bot_id:
            return
        matched_tasks = await self._collect_dynamic_pm_completion_task_map(task, context)
        if any(token not in matched_tasks for token in expected_tokens):
            return
        step_id = f"pm-dynamic-security:{fanout_id}"
        existing = await self._find_task_by_step_id(step_id, security_bot_id)
        if self._dynamic_pm_stage_blocks_creation(existing):
            return
        payload = self._build_default_trigger_payload(task, security_bot_id)
        ordered_tasks = [matched_tasks[token] for token in expected_tokens]
        payload["title"] = "Global security review"
        payload["instruction"] = "Review all completed implementation and specialist branches before final QC."
        payload["join_task_ids"] = [matched.id for matched in ordered_tasks]
        payload["join_results"] = [matched.result for matched in ordered_tasks]
        payload["join_items"] = [
            {
                "source_task_id": matched.id,
                "source_bot_id": matched.bot_id,
                "source_payload": matched.payload,
                "source_result": matched.result,
                "completion_token": token,
            }
            for token, matched in zip(expected_tokens, ordered_tasks)
        ]
        security_context = self._pm_dynamic_global_stage_context(
            context,
            global_stage="security_review",
        )
        payload["pm_routing_context"] = security_context
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=task.id,
            trigger_rule_id="pm-dynamic-security-gate",
            step_id=step_id,
        )
        await self.create_task(
            bot_id=security_bot_id,
            payload=payload,
            metadata=child_metadata,
        )

    async def _create_dynamic_pm_final_qc_task(self, task: Task, context: Dict[str, Any]) -> None:
        metadata = task.metadata or TaskMetadata()
        fanout_id = str(context.get("fanout_id") or "").strip()
        final_qc_bot_id = str(context.get("final_qc_bot_id") or "pm-final-qc").strip()
        if not fanout_id or not final_qc_bot_id:
            return
        step_id = f"pm-dynamic-final-qc:{fanout_id}"
        existing = await self._find_dynamic_pm_stage_task(
            task,
            bot_id=final_qc_bot_id,
            fanout_id=fanout_id,
            global_stage="final_qc",
        )
        if self._dynamic_pm_stage_blocks_creation(existing):
            return
        payload = self._build_default_trigger_payload(task, final_qc_bot_id)
        payload["title"] = "Final quality control"
        payload["instruction"] = "Perform the terminal final QC pass after implementation and security review are complete."
        final_context = self._pm_dynamic_global_stage_context(
            context,
            global_stage="final_qc",
        )
        payload["pm_routing_context"] = final_context
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=task.id,
            trigger_rule_id="pm-dynamic-final-qc",
            step_id=step_id,
        )
        await self.create_task(
            bot_id=final_qc_bot_id,
            payload=payload,
            metadata=child_metadata,
        )

    async def _dispatch_dynamic_pm_supplemental_tasks(self, task: Task) -> None:
        if str(task.status or "").strip().lower() != "completed":
            return
        if not self._pm_assignment_dynamic_management_enabled(task):
            return
        if not self._pm_dynamic_progress_result(task.result):
            return
        if not isinstance(task.payload, dict):
            return
        context = self._pm_dynamic_context(task)
        if not context:
            return
        deterministic_signals = context.get("deterministic_signals")
        if not isinstance(deterministic_signals, dict):
            return
        if not any(
            bool(deterministic_signals.get(signal_name))
            for signal_name in (
                "missing_downstream_stage_db",
                "missing_downstream_stage_ui",
                "missing_downstream_stage_final_qc",
            )
        ):
            return

        db_excluded = self._pm_assignment_stage_is_excluded(task.payload, "pm-database-engineer")
        ui_excluded = self._pm_assignment_stage_is_excluded(task.payload, "pm-ui-tester")

        if task.bot_id == "pm-security-reviewer":
            if bool(deterministic_signals.get("missing_downstream_stage_db")) and not db_excluded:
                await self._create_dynamic_pm_database_task(task, context)
                return
            if (
                bool(deterministic_signals.get("missing_downstream_stage_ui"))
                and db_excluded
                and not ui_excluded
            ):
                await self._create_dynamic_pm_global_ui_validation_task(task, context)
                return
            if (
                bool(deterministic_signals.get("missing_downstream_stage_final_qc"))
                and db_excluded
                and ui_excluded
            ):
                await self._create_dynamic_pm_final_qc_task(task, context)
                return

        if task.bot_id == "pm-database-engineer":
            if bool(deterministic_signals.get("missing_downstream_stage_ui")) and not ui_excluded:
                await self._create_dynamic_pm_global_ui_validation_task(task, context)
                return
            if bool(deterministic_signals.get("missing_downstream_stage_final_qc")) and ui_excluded:
                await self._create_dynamic_pm_final_qc_task(task, context)
                return

        if task.bot_id == "pm-ui-tester" and bool(deterministic_signals.get("missing_downstream_stage_final_qc")):
            await self._create_dynamic_pm_final_qc_task(task, context)

    async def _create_pm_assignment_loop_escalation_task(
        self,
        *,
        source_task: Task,
        target_bot_id: str,
        trigger_id: str,
        payload: Dict[str, Any],
        repeat_count: int,
        repeat_limit: int,
        reason: str,
    ) -> None:
        metadata = source_task.metadata or TaskMetadata()
        if str(metadata.run_class or "").strip().lower() != "pm_assignment":
            return
        if source_task.bot_id not in {"pm-tester", "pm-security-reviewer"}:
            return
        if target_bot_id != "pm-coder":
            return
        fanout_id = str(payload.get("fanout_id") or self._resolve_fanout_id(payload) or "").strip()
        branch_key = str(payload.get("fanout_branch_key") or self._resolve_join_branch_key(payload) or "").strip()
        if not branch_key:
            branch_key = self._workflow_route_branch_identity(source_task, payload)
        workflow_root_id = str(metadata.workflow_root_task_id or source_task.id).strip() or "root"
        step_id = f"pm-loop-escalation:{workflow_root_id}:{self._normalize_branch_token(branch_key, fallback='branch')}"
        existing = await self._find_task_by_step_id(step_id, "pm-engineer")
        if existing is not None and existing.status in {"queued", "blocked", "running", "completed"}:
            return
        escalation_payload = self._build_default_trigger_payload(source_task, "pm-engineer")
        escalation_payload["title"] = "Escalated PM branch remediation"
        escalation_payload["instruction"] = (
            "A branch exceeded the PM feedback loop retry limit. Reassess the branch architecture, "
            "adjust the plan or workstream contract, and restart remediation forward from engineering."
        )
        escalation_payload["failure_type"] = "escalation_required"
        escalation_payload["fanout_id"] = fanout_id
        escalation_payload["fanout_branch_key"] = branch_key
        for field_name in (
            "workstream",
            "workstream_index",
            "fanout_count",
            "fanout_expected_branch_keys",
            "assignment_request",
            "assignment_scope",
            "root_pm_bot_id",
            "deterministic_signals",
            "pm_routing_context",
        ):
            if field_name in escalation_payload and not _is_empty_contract_value(escalation_payload.get(field_name)):
                continue
            value = payload.get(field_name)
            if _is_empty_contract_value(value):
                continue
            escalation_payload[field_name] = value
        escalation_payload["loop_guard"] = {
            "reason": reason,
            "source_bot_id": source_task.bot_id,
            "target_bot_id": target_bot_id,
            "repeat_count": repeat_count,
            "repeat_limit": repeat_limit,
            "trigger_id": trigger_id,
        }
        escalation_payload["upstream_failure_type"] = self._workflow_route_failure_type(source_task)
        if isinstance(source_task.result, dict):
            escalation_payload["upstream_findings"] = source_task.result.get("findings")
            escalation_payload["upstream_evidence"] = source_task.result.get("evidence")
            escalation_payload["upstream_handoff_notes"] = source_task.result.get("handoff_notes")
        child_metadata = self._trigger_child_metadata(
            metadata,
            parent_task_id=source_task.id,
            trigger_rule_id="pm-loop-escalation",
            step_id=step_id,
        )
        await self.create_task(
            bot_id="pm-engineer",
            payload=escalation_payload,
            metadata=child_metadata,
        )

    def _dynamic_pm_stage_blocks_creation(self, task: Optional[Task]) -> bool:
        if task is None:
            return False
        if task.status in {"queued", "blocked", "running"}:
            return True
        if task.status == "completed" and self._pm_dynamic_progress_result(task.result):
            return True
        return False

    def _build_trigger_payload(self, task: Task, trigger: Any) -> Any:
        payload_template = trigger.payload_template
        target_bot_id = str(getattr(trigger, "target_bot_id", "") or "").strip()
        base_payload = self._build_default_trigger_payload(task, target_bot_id)
        if isinstance(payload_template, dict):
            notes: list[str] = []
            transformed = _transform_template_value(payload_template, base_payload, notes)
            if isinstance(transformed, dict):
                # Return only what the template explicitly asked for.  Do NOT merge in the
                # raw base_payload (which contains source_payload / source_result and their
                # full upstream chains).  Those objects grow exponentially with trigger depth
                # and will overflow context windows by depth 5-6 in deep pipelines.
                # Template variables ({{source_payload.x}}, {{source_result.y}}) are already
                # resolved into concrete values inside `transformed`.
                final: Dict[str, Any] = dict(transformed)
                if isinstance(task.payload, dict):
                    upstream_payload = task.payload.get("source_payload")
                    if isinstance(upstream_payload, dict):
                        self._promote_trigger_context_fields(final, upstream_payload)
                    self._promote_trigger_context_fields(final, task.payload)
                if isinstance(task.result, dict):
                    # Templated payloads still need compact upstream result context so
                    # downstream validation/review stages can inspect the branch output
                    # without relying on files already existing in the live repo.
                    self._promote_trigger_result_fields(final, task.result)
                # Preserve bot-routing hints that the template never sets explicitly.
                for _key in ("role_hint", "step_kind"):
                    if _key in base_payload and _key not in final:
                        final[_key] = base_payload[_key]
                if notes:
                    final["trigger_template_notes"] = notes
                return final
            # Template resolved to a non-dict scalar — fall through to legacy merge.
            merged = dict(base_payload)
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
        return base_payload

    def _pm_assignment_research_fanout_cap(self) -> Tuple[int, int]:
        base_cap = max(1, _settings_int("pm_assignment_research_fanout_limit", _PM_ASSIGNMENT_RESEARCH_STEP_CAP))
        split_cap = max(base_cap, _settings_int("pm_assignment_research_fanout_split_limit", _PM_ASSIGNMENT_RESEARCH_STEP_SPLIT_CAP))
        return base_cap, split_cap

    def _pm_assignment_research_step_text(self, item: Dict[str, Any]) -> str:
        parts = [
            str(item.get("id") or "").strip(),
            str(item.get("title") or "").strip(),
            str(item.get("instruction") or "").strip(),
        ]
        return "\n".join(part for part in parts if part).lower()

    def _pm_assignment_research_lane(self, item: Dict[str, Any]) -> str:
        haystack = self._pm_assignment_research_step_text(item)
        if any(keyword in haystack for keyword in _PM_ASSIGNMENT_RESEARCH_REPO_LANE_KEYWORDS):
            return "repo"
        if any(keyword in haystack for keyword in _PM_ASSIGNMENT_RESEARCH_DATA_LANE_KEYWORDS):
            return "data"
        if any(keyword in haystack for keyword in _PM_ASSIGNMENT_RESEARCH_ONLINE_LANE_KEYWORDS):
            return "online"
        return "generic"

    def _pm_assignment_research_step_is_split(self, item: Dict[str, Any]) -> bool:
        haystack = self._pm_assignment_research_step_text(item)
        if any(marker in haystack for marker in _PM_ASSIGNMENT_RESEARCH_SPLIT_MARKERS):
            return True
        return re.search(r"\b(?:part|chunk|batch|shard|segment|slice)\s+\d+\b", haystack) is not None

    def _pm_assignment_research_step_is_research(self, item: Dict[str, Any]) -> bool:
        step_kind = str(item.get("step_kind") or "").strip().lower()
        if step_kind in _PM_ASSIGNMENT_RESEARCH_EXCLUDED_STEP_KINDS:
            return False
        if step_kind in _PM_ASSIGNMENT_RESEARCH_INCLUDED_STEP_KINDS:
            return True

        haystack = self._pm_assignment_research_step_text(item)
        if any(marker in haystack for marker in ("final qc", "quality check", "sign-off", "sign off")):
            return False

        bot_id = str(item.get("bot_id") or "").strip().lower()
        if bot_id == "pm-research-analyst":
            return True

        role_hint = str(item.get("role_hint") or "").strip().lower()
        if role_hint in {"researcher", "research_analyst", "pm-research-analyst", "analyst"}:
            return True

        if any(
            marker in haystack
            for marker in (
                "implementation",
                "migrate",
                "migration",
                "schema",
                "build ",
                "deploy ",
                "test ",
                "validate ",
            )
        ):
            return False
        return self._pm_assignment_research_lane(item) != "generic"

    def _pm_assignment_research_trigger_items(
        self,
        task: Task,
        trigger: Any,
        items: List[Any],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        metadata = task.metadata or TaskMetadata()
        if task.bot_id != "pm-orchestrator":
            return items, None
        if str(metadata.run_class or "").strip().lower() != "pm_assignment":
            return items, None
        if str(getattr(trigger, "target_bot_id", "") or "").strip() != "pm-research-analyst":
            return items, None
        if not items or not all(isinstance(item, dict) for item in items):
            return items, None

        filtered = [
            item
            for item in items
            if self._pm_assignment_research_step_is_research(item)
        ]
        if not filtered or len(filtered) == len(items):
            return items, None

        return filtered, {
            "applied": True,
            "reason": "pm_assignment_research_trigger_filter",
            "original_count": len(items),
            "kept_count": len(filtered),
        }

    def _pm_assignment_default_research_item_for_lane(self, lane: str) -> Dict[str, Any]:
        lane_key = str(lane or "").strip().lower()
        for spec in _PM_ASSIGNMENT_DEFAULT_RESEARCH_STEP_SPECS:
            if str(spec.get("lane") or "").strip().lower() != lane_key:
                continue
            return {
                "id": spec["id"],
                "title": spec["title"],
                "instruction": spec["instruction"],
                "bot_id": "pm-research-analyst",
                "role_hint": "researcher",
                "step_kind": "specification",
                "depends_on": [],
                "acceptance_criteria": list(spec["acceptance_criteria"]),
                "deliverables": list(spec["deliverables"]),
                "evidence_requirements": list(spec["evidence_requirements"]),
                "quality_gates": list(spec["quality_gates"]),
            }
        return {
            "id": f"step_1_{lane_key or 'research'}",
            "title": "Research task",
            "instruction": "Research the assignment and return concrete evidence.",
            "bot_id": "pm-research-analyst",
            "role_hint": "researcher",
            "step_kind": "specification",
            "depends_on": [],
        }

    def _pm_assignment_enforce_default_research_items(
        self,
        task: Task,
        trigger: Any,
        items: Optional[List[Any]],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        metadata = task.metadata or TaskMetadata()
        if task.bot_id != "pm-orchestrator":
            return list(items or []), None
        if str(metadata.run_class or "").strip().lower() != "pm_assignment":
            return list(items or []), None
        if str(getattr(trigger, "target_bot_id", "") or "").strip() != "pm-research-analyst":
            return list(items or []), None

        normalized_items = [item for item in (items or []) if isinstance(item, dict)]
        split_required = any(
            self._pm_assignment_research_step_is_split(item)
            for item in normalized_items
        )
        if split_required and len(normalized_items) > _PM_ASSIGNMENT_RESEARCH_STEP_CAP:
            return normalized_items, None

        selected_by_lane: Dict[str, Dict[str, Any]] = {}
        for item in normalized_items:
            lane = self._pm_assignment_research_lane(item)
            if lane in {"repo", "data", "online"} and lane not in selected_by_lane:
                selected_by_lane[lane] = dict(item)

        required_lanes = ("repo", "data", "online")
        enforced_items: List[Dict[str, Any]] = []
        defaulted = False
        for lane in required_lanes:
            item = selected_by_lane.get(lane)
            default_item = self._pm_assignment_default_research_item_for_lane(lane)
            if item is None:
                item = default_item
                defaulted = True
            else:
                item = dict(item)
                item["id"] = default_item["id"]
                item["title"] = default_item["title"]
                item.setdefault("bot_id", "pm-research-analyst")
                item.setdefault("role_hint", "researcher")
                item.setdefault("step_kind", "specification")
                item.setdefault("depends_on", [])
            enforced_items.append(item)

        default_specs = [self._pm_assignment_default_research_item_for_lane(lane) for lane in required_lanes]
        canonical_defaults_match = (
            not defaulted
            and len(normalized_items) == len(required_lanes)
            and all(self._pm_assignment_research_lane(item) in {"repo", "data", "online"} for item in normalized_items)
            and all(
                str(item.get("id") or "").strip() == str(default_item.get("id") or "").strip()
                and str(item.get("title") or "").strip() == str(default_item.get("title") or "").strip()
                for item, default_item in zip(normalized_items, default_specs)
            )
        )
        if canonical_defaults_match:
            return normalized_items, None

        return enforced_items, {
            "applied": True,
            "reason": "pm_assignment_research_default_three",
            "original_count": len(normalized_items),
            "kept_count": len(enforced_items),
            "split_required": split_required,
            "defaulted_missing_lanes": defaulted,
        }

    def _pm_assignment_research_fanout_budget(
        self,
        task: Task,
        trigger: Any,
        items: List[Any],
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        metadata = task.metadata or TaskMetadata()
        if task.bot_id != "pm-orchestrator":
            return items, None
        if str(metadata.run_class or "").strip().lower() != "pm_assignment":
            return items, None
        if str(getattr(trigger, "target_bot_id", "") or "").strip() != "pm-research-analyst":
            return items, None
        if not items or not all(isinstance(item, dict) for item in items):
            return items, None

        original_count = len(items)
        base_cap, split_cap = self._pm_assignment_research_fanout_cap()
        if original_count <= base_cap:
            return items, None

        split_required = any(self._pm_assignment_research_step_is_split(item) for item in items[base_cap:])
        budget = split_cap if split_required else base_cap
        if original_count <= budget:
            return items, None

        trimmed: List[Any] = []
        if split_required:
            trimmed = list(items[:budget])
        else:
            seen_lanes: Set[str] = set()
            for item in items:
                lane = self._pm_assignment_research_lane(item)
                if lane == "generic" or lane in seen_lanes:
                    continue
                trimmed.append(item)
                seen_lanes.add(lane)
                if len(trimmed) >= budget:
                    break
            if len(trimmed) < budget:
                for item in items:
                    if item in trimmed:
                        continue
                    trimmed.append(item)
                    if len(trimmed) >= budget:
                        break

        return trimmed[:budget], {
            "applied": True,
            "reason": "pm_assignment_research_fanout_cap",
            "original_count": original_count,
            "kept_count": min(original_count, budget),
            "split_required": split_required,
        }

    def _build_trigger_payloads(self, task: Task, trigger: Any) -> List[Any]:
        payload = self._build_trigger_payload(task, trigger)
        fan_out_field = str(getattr(trigger, "fan_out_field", "") or "").strip()
        if not fan_out_field:
            return [payload]
        if not isinstance(payload, dict):
            return [payload]
        items = self._resolve_fan_out_items(payload, task, fan_out_field)
        research_default_budget = None
        if not isinstance(items, list):
            items, research_default_budget = self._pm_assignment_enforce_default_research_items(
                task,
                trigger,
                [],
            )
            if not items:
                return []
        items, trigger_filter = self._pm_assignment_research_trigger_items(task, trigger, items)
        items, research_default_budget = self._pm_assignment_enforce_default_research_items(
            task,
            trigger,
            items,
        )
        items, fanout_budget = self._pm_assignment_research_fanout_budget(task, trigger, items)
        items, implementation_budget = self._pm_assignment_workstream_trigger_items(task, trigger, items)
        alias = str(getattr(trigger, "fan_out_alias", "") or "").strip() or "item"
        index_alias = str(getattr(trigger, "fan_out_index_alias", "") or "").strip() or "item_index"
        total = len(items)
        fanout_id = self._fanout_id(task, trigger)
        payloads: List[Any] = []
        branch_keys: List[str] = []
        for idx, item in enumerate(items):
            next_payload = dict(payload)
            next_payload[alias] = dict(item) if isinstance(item, dict) else item
            next_payload[index_alias] = idx
            if isinstance(next_payload.get(alias), dict):
                self._promote_fanout_item_fields(next_payload, next_payload[alias])
            next_payload["fanout_count"] = total
            if fanout_id:
                next_payload["fanout_id"] = fanout_id
            pm_fanout_budget: Dict[str, Any] = {}
            if trigger_filter:
                pm_fanout_budget.update(trigger_filter)
            if research_default_budget:
                preserve_trigger_filter_budget = bool(
                    trigger_filter
                    and not bool(research_default_budget.get("defaulted_missing_lanes"))
                    and int(research_default_budget.get("original_count") or 0)
                    == int(research_default_budget.get("kept_count") or 0)
                )
                if preserve_trigger_filter_budget:
                    pm_fanout_budget.update(
                        {
                            key: value
                            for key, value in research_default_budget.items()
                            if key not in {"reason", "original_count", "kept_count"}
                        }
                    )
                else:
                    pm_fanout_budget.update(research_default_budget)
            if fanout_budget:
                pm_fanout_budget.update(fanout_budget)
            if implementation_budget:
                pm_fanout_budget.update(implementation_budget)
            if pm_fanout_budget:
                next_payload["pm_fanout_budget"] = pm_fanout_budget
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
            "pm_routing_context",
            "root_pm_bot_id",
            "deterministic_signals",
            "depends_on_steps",
            "context_items",
            "assignment_request",
            "assignment_scope",
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

    def _compatible_join_triggers(
        self,
        workflow: Any,
        task: Task,
        trigger: Any,
        target_bot_id: str,
    ) -> List[Any]:
        triggers = getattr(workflow, "triggers", None) or []
        compatible: List[Any] = []
        trigger_group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        trigger_expected_field = str(getattr(trigger, "join_expected_field", "") or "").strip()
        trigger_sort_field = str(getattr(trigger, "join_sort_field", "") or "").strip()
        trigger_result_field = str(getattr(trigger, "result_field", "") or "").strip()
        trigger_condition = str(getattr(trigger, "condition", "") or "").strip()
        trigger_event = str(getattr(trigger, "event", "") or "").strip()
        for candidate in triggers:
            if not getattr(candidate, "enabled", True):
                continue
            if not self._trigger_uses_join(candidate):
                continue
            if str(getattr(candidate, "event", "") or "").strip() != trigger_event:
                continue
            if str(getattr(candidate, "condition", "") or "").strip() != trigger_condition:
                continue
            if str(getattr(candidate, "join_group_field", "") or "").strip() != trigger_group_field:
                continue
            if str(getattr(candidate, "join_expected_field", "") or "").strip() != trigger_expected_field:
                continue
            if str(getattr(candidate, "join_sort_field", "") or "").strip() != trigger_sort_field:
                continue
            if str(getattr(candidate, "result_field", "") or "").strip() != trigger_result_field:
                continue
            if self._resolve_trigger_target_bot_id(task, getattr(candidate, "target_bot_id", "")) != target_bot_id:
                continue
            compatible.append(candidate)
        if not compatible:
            return [trigger]
        compatible.sort(key=lambda item: str(getattr(item, "id", "") or ""))
        return compatible

    async def _dispatch_join_trigger(
        self,
        task: Task,
        trigger: Any,
        target_bot_id: str,
        payloads: List[Any],
        next_metadata: TaskMetadata,
        *,
        compatible_join_triggers: Optional[List[Any]] = None,
    ) -> None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            aggregate_payload = await self._build_join_payload(
                task,
                trigger,
                target_bot_id,
                payload,
                compatible_join_triggers=compatible_join_triggers,
            )
            if aggregate_payload is None:
                continue
            join_step_id = self._join_step_id(
                task,
                trigger,
                payload,
                target_bot_id=target_bot_id,
                compatible_join_triggers=compatible_join_triggers,
            )
            if not join_step_id:
                continue
            existing = await self._find_task_by_step_id(join_step_id, target_bot_id)
            if existing is not None:
                logger.debug("[JOIN] SKIP trigger=%s join_step_id=%s reason=already_fired", trigger.id, join_step_id)
                continue
            logger.info(
                "[JOIN] FIRE trigger=%s source_task=%s fanout_id=%s target=%s",
                trigger.id,
                task.id,
                aggregate_payload.get("fanout_id") or payload.get("fanout_id"),
                target_bot_id,
            )
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
        *,
        compatible_join_triggers: Optional[List[Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        expected_field = str(getattr(trigger, "join_expected_field", "") or "").strip()
        items_alias = str(getattr(trigger, "join_items_alias", "") or "").strip() or "items"
        result_field = str(getattr(trigger, "join_result_field", "") or "").strip()
        result_items_alias = str(getattr(trigger, "join_result_items_alias", "") or "").strip() or "join_result_items"
        sort_field = str(getattr(trigger, "join_sort_field", "") or "").strip()

        group_value = self._lookup_result_field(payload, group_field) if group_field else None
        sibling_payloads = self._collect_join_payloads(
            task,
            trigger,
            group_field,
            group_value,
            compatible_join_triggers=compatible_join_triggers,
        )
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
                "[JOIN] WAIT trigger=%s fanout_id=%s completed=%d expected=%d missing=%s",
                trigger.id,
                fanout_id or group_value,
                len(selected_payloads),
                expected_count,
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
                "[JOIN] WAIT trigger=%s fanout_id=%s completed=%d expected=%d",
                trigger.id,
                fanout_id or group_value,
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
        *,
        compatible_join_triggers: Optional[List[Any]] = None,
    ) -> List[Dict[str, Any]]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        event = "task_completed" if task.status == "completed" else "task_failed"
        compatible = list(compatible_join_triggers or [trigger])
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
            matching_trigger = next(
                (join_trigger for join_trigger in compatible if self._trigger_matches_result(candidate, join_trigger)),
                None,
            )
            if matching_trigger is None:
                continue
            candidate_payload = self._build_trigger_payload(candidate, matching_trigger)
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

    def _join_step_id(
        self,
        task: Task,
        trigger: Any,
        payload: Dict[str, Any],
        *,
        target_bot_id: str = "",
        compatible_join_triggers: Optional[List[Any]] = None,
    ) -> Optional[str]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        fanout_id = self._resolve_fanout_id(payload)
        normalized_fanout = self._normalize_branch_token(fanout_id, fallback="fanout") if fanout_id else "nofanout"
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        group_value = self._lookup_result_field(payload, group_field) if group_field else "__all__"
        normalized_group = self._normalize_branch_token(group_value, fallback="group")
        canonical_trigger_id = str(getattr(trigger, "id", "") or "").strip()
        compatible_ids = sorted(
            {
                str(getattr(item, "id", "") or "").strip()
                for item in (compatible_join_triggers or [])
                if str(getattr(item, "id", "") or "").strip()
            }
        )
        if compatible_ids:
            canonical_trigger_id = compatible_ids[0]
        return f"join:{task.bot_id}:{canonical_trigger_id}:{root_id}:{normalized_fanout}:{normalized_group}"

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
        latest_match: Optional[Task] = None
        async with self._lock:
            for task in self._tasks.values():
                if task.bot_id != bot_id:
                    continue
                metadata = task.metadata or TaskMetadata()
                if metadata.step_id == step_id:
                    if latest_match is None or self._task_order_token(task) >= self._task_order_token(latest_match):
                        latest_match = task
        return latest_match

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
