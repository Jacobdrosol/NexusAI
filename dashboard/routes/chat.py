"""Chat dashboard page and proxy endpoints."""
from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict, Iterable

import requests
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required

from dashboard.bot_chat_profiles import bot_chat_profile, bot_chat_tool_access, with_bot_chat_profiles
from dashboard.cp_client import get_cp_client
from dashboard.routes._sse_proxy import proxy_upstream_sse_lines
from dashboard.work_overview import manager_id_for_task, project_id_for_task
from shared.chat_attachments import CHAT_ATTACHMENT_MAX_FILES, CHAT_ATTACHMENT_MAX_TOTAL_BYTES

bp = Blueprint("chat", __name__)

UNSCOPED_PROJECT_FILTER = "__unscoped__"
_CHAT_SELECTABLE_ROLES = {
    "assistant",
    "coding_assistant",
    "code-reviewer",
    "tutor",
}
_ALLOWED_CONVERSATION_SCOPES = {"global", "project", "bridged"}
_CHAT_CONTEXT_ITEM_MAX_CHARS = 12000
_CHAT_CONTEXT_ITEM_ID_MAX_CHARS = 256
_CHAT_MESSAGE_CONTENT_MAX_CHARS = 120000


def _bot_value(bot: Any, key: str, default: Any = None) -> Any:
    if isinstance(bot, dict):
        return bot.get(key, default)
    return getattr(bot, key, default)


def _routing_rules(bot: Any) -> dict[str, Any]:
    rules = _bot_value(bot, "routing_rules", {}) or {}
    return rules if isinstance(rules, dict) else {}


def _policy_string_list(bot: Any, key: str) -> list[str]:
    policy = _bot_value(bot, "execution_policy", {}) or {}
    if not isinstance(policy, dict):
        return []
    values = policy.get(key)
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        label = str(value or "").strip()
        if label and label not in result:
            result.append(label)
    return result


def _http_connection_backend_count(bot: Any) -> int:
    backends = _bot_value(bot, "backends", []) or []
    if not isinstance(backends, list):
        return 0
    count = 0
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        provider = str(backend.get("provider") or "").strip().lower()
        backend_type = str(backend.get("type") or "").strip().lower()
        if provider == "http_connection" or backend_type == "http_connection":
            count += 1
    return count


def _chat_selectable_bots(bots: Iterable[Any]) -> list[Any]:
    selectable: list[dict[str, Any]] = []
    for bot in bots:
        rules = _routing_rules(bot)
        operator_profile = rules.get("operator_profile") if isinstance(rules.get("operator_profile"), dict) else {}
        chat_tool_access = rules.get("chat_tool_access") if isinstance(rules.get("chat_tool_access"), dict) else {}
        autonomy = str(operator_profile.get("autonomy") or "").strip().lower()
        role = str(_bot_value(bot, "role", "") or "").strip().lower()
        name = str(_bot_value(bot, "name", "") or "").strip().lower()
        bot_id = str(_bot_value(bot, "id", "") or "").strip().lower()
        if (
            autonomy == "manual_chat_only"
            or bool(chat_tool_access.get("enabled"))
            or role in _CHAT_SELECTABLE_ROLES
            or bot_id.startswith("personal-")
            or "chat" in name
        ):
            if isinstance(bot, dict):
                selectable.append(dict(bot))
            elif hasattr(bot, "model_dump"):
                dumped = bot.model_dump()
                if isinstance(dumped, dict):
                    selectable.append(dumped)
            else:
                selectable.append(
                    {
                        "id": _bot_value(bot, "id"),
                        "name": _bot_value(bot, "name"),
                        "role": _bot_value(bot, "role"),
                        "backends": _bot_value(bot, "backends", []),
                        "routing_rules": rules,
                        "memory_profiles_enabled": bool(_bot_value(bot, "memory_profiles_enabled", False)),
                        "execution_policy": _bot_value(bot, "execution_policy", None),
                    }
                )
    return with_bot_chat_profiles(selectable)


def _readiness_by_bot_id(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("readiness")
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            continue
        checks = row.get("checks") if isinstance(row.get("checks"), list) else []
        messages = [
            str(check.get("message") or check.get("component") or "").strip()
            for check in checks
            if isinstance(check, dict)
            and str(check.get("message") or check.get("component") or "").strip()
        ]
        failed = [
            str(check.get("message") or check.get("component") or "").strip()
            for check in checks
            if isinstance(check, dict)
            and str(check.get("status") or "").strip().lower() in {"failed", "blocking"}
            and str(check.get("message") or check.get("component") or "").strip()
        ]
        state = str(row.get("state") or "").strip().lower()
        if not state:
            state = "ready" if bool(row.get("ready")) else "blocked"
        result[bot_id] = {
            "state": state,
            "ready": bool(row.get("ready")) if "ready" in row else state == "ready",
            "detail": failed[0]
            if failed
            else (messages[0] if messages else ("Ready" if state == "ready" else "Readiness unavailable.")),
            "failed": len(failed),
        }
    return result


def _with_bot_readiness(bots: Iterable[Any], readiness_payload: Any) -> list[dict[str, Any]]:
    readiness = _readiness_by_bot_id(readiness_payload)
    enriched: list[dict[str, Any]] = []
    for bot in bots:
        if isinstance(bot, dict):
            row = dict(bot)
        elif hasattr(bot, "model_dump"):
            dumped = bot.model_dump()
            row = dumped if isinstance(dumped, dict) else {}
        else:
            row = {
                "id": _bot_value(bot, "id"),
                "name": _bot_value(bot, "name"),
                "role": _bot_value(bot, "role"),
                "backends": _bot_value(bot, "backends", []),
                "routing_rules": _routing_rules(bot),
                "memory_profiles_enabled": bool(_bot_value(bot, "memory_profiles_enabled", False)),
                "execution_policy": _bot_value(bot, "execution_policy", None),
                "assignment_capabilities": _bot_value(bot, "assignment_capabilities", None),
            }
        bot_id = str(row.get("id") or "").strip()
        row["readiness"] = readiness.get(
            bot_id,
            {"state": "unknown", "ready": False, "detail": "Readiness not reported.", "failed": 0},
        )
        enriched.append(row)
    return enriched


def _with_chat_bot_readiness(chat_bots: list[dict[str, Any]], readiness_payload: Any) -> list[dict[str, Any]]:
    return _with_bot_readiness(chat_bots, readiness_payload)


def _assignment_manager_bots(bots: Iterable[Any]) -> list[dict[str, Any]]:
    managers: list[dict[str, Any]] = []
    for bot in bots:
        row = dict(bot) if isinstance(bot, dict) else {}
        capabilities = row.get("assignment_capabilities")
        if isinstance(capabilities, dict) and bool(capabilities.get("is_project_manager")):
            managers.append(row)
    return managers


def _selected_project_work_summary(cp: Any, project_ids: list[str]) -> dict[str, Any]:
    scoped_project_ids = [str(pid or "").strip() for pid in project_ids if str(pid or "").strip()]
    if not scoped_project_ids:
        return {"available": False, "project_ids": [], "counts": {}, "total": 0, "by_manager": [], "recent": []}
    try:
        tasks = _cp_list_tasks_safe(
            cp,
            statuses=["queued", "blocked", "running", "failed"],
            limit=200,
            include_content=False,
        )
    except Exception:
        return {
            "available": False,
            "project_ids": scoped_project_ids,
            "counts": {},
            "total": 0,
            "by_manager": [],
            "recent": [],
            "error": "Task snapshot unavailable.",
        }
    if not isinstance(tasks, list):
        return {
            "available": False,
            "project_ids": scoped_project_ids,
            "counts": {},
            "total": 0,
            "by_manager": [],
            "recent": [],
            "error": "Task snapshot unavailable.",
        }

    scoped = [task for task in tasks if isinstance(task, dict) and project_id_for_task(task) in scoped_project_ids]
    counts: dict[str, int] = {}
    by_manager: dict[str, dict[str, Any]] = {}
    for task in scoped:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
        manager_id = manager_id_for_task(task) or "unassigned"
        row = by_manager.setdefault(
            manager_id,
            {
                "manager_id": manager_id,
                "queued": 0,
                "blocked": 0,
                "running": 0,
                "failed": 0,
                "total": 0,
            },
        )
        if status in row:
            row[status] += 1
        row["total"] += 1

    recent = sorted(
        scoped,
        key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""),
        reverse=True,
    )[:5]
    return {
        "available": True,
        "project_ids": scoped_project_ids,
        "counts": counts,
        "total": len(scoped),
        "by_manager": sorted(by_manager.values(), key=lambda row: (-int(row.get("total") or 0), str(row.get("manager_id") or "")))[:5],
        "recent": [
            {
                "id": str(task.get("id") or ""),
                "bot_id": str(task.get("bot_id") or ""),
                "status": str(task.get("status") or "unknown"),
                "manager_id": manager_id_for_task(task) or "unassigned",
                "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
            }
            for task in recent
        ],
    }


def _bot_readiness_blocker_from_cp(cp: Any, bot_id: str) -> str:
    safe_bot_id = str(bot_id or "").strip()
    if not safe_bot_id or not hasattr(cp, "list_bot_readiness"):
        return ""
    try:
        readiness_payload = cp.list_bot_readiness()
    except Exception:
        return ""
    readiness = _readiness_by_bot_id(readiness_payload).get(safe_bot_id)
    if not readiness:
        return ""
    state = str(readiness.get("state") or "").strip().lower()
    if state not in {"blocked", "disabled"}:
        return ""
    detail = str(readiness.get("detail") or "").strip()
    return f"{safe_bot_id} is {state}: {detail}" if detail else f"{safe_bot_id} is {state}"


def _model_catalog_blocker_from_cp(cp: Any, model_id: str) -> str:
    _, blocker = _model_catalog_entry_from_cp(cp, model_id)
    return blocker


def _model_catalog_entry_from_cp(cp: Any, model_id: str) -> tuple[dict[str, Any] | None, str]:
    safe_model_id = str(model_id or "").strip()
    if not safe_model_id or not hasattr(cp, "list_models"):
        return None, ""
    try:
        models = cp.list_models()
    except Exception:
        return None, ""
    if not isinstance(models, list) or not models:
        return None, ""
    for model in models:
        if not isinstance(model, dict):
            continue
        candidate_id = str(model.get("id") or "").strip()
        if candidate_id != safe_model_id:
            continue
        if model.get("enabled", True) is False:
            label = str(model.get("name") or candidate_id).strip() or candidate_id
            return None, f"{label} is disabled"
        return model, ""
    return None, f"{safe_model_id} is not in the enabled model catalog"


def _bot_display_labels(bots: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for bot in bots or []:
        if not isinstance(bot, dict):
            continue
        bot_id = str(bot.get("id") or "").strip()
        if not bot_id:
            continue
        name = str(bot.get("name") or "").strip()
        labels[bot_id] = f"{name} ({bot_id})" if name and name != bot_id else bot_id
    return labels


def _model_display_labels(models: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for model in models or []:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            continue
        provider = str(model.get("provider") or "").strip()
        name = str(model.get("name") or model_id).strip() or model_id
        labels[model_id] = f"{provider} / {name}" if provider else name
    return labels


def _model_capabilities(model: dict[str, Any] | None) -> list[str]:
    raw = (model or {}).get("capabilities")
    if not isinstance(raw, list):
        return []
    return [str(item or "").strip() for item in raw if str(item or "").strip()]


def _model_supports_image_attachments(*, provider: str, model_name: str, capabilities: list[str] | None = None) -> bool:
    normalized_capabilities = {str(item or "").strip().lower() for item in (capabilities or [])}
    if normalized_capabilities & {"image", "images", "vision", "multimodal"}:
        return True
    provider_key = str(provider or "").strip().lower()
    lowered = str(model_name or "").strip().lower()
    if provider_key == "gemini":
        return True
    if provider_key == "openai":
        return any(token in lowered for token in ("gpt-4o", "gpt-4.1", "gpt-5"))
    if provider_key == "claude":
        return any(token in lowered for token in ("claude-3", "claude-4"))
    if provider_key in {"ollama_cloud", "ollama"}:
        return any(
            token in lowered
            for token in ("vision", "-vl", "qwen2.5-vl", "qwen-vl", "qwen3-vl", "qwen3.5:", "llava", "gemma3")
        )
    return False


def _effective_model_context_from_cp(
    cp: Any,
    conversation: dict[str, Any],
    bot: dict[str, Any] | None,
    requested_bot_id: str,
) -> dict[str, Any]:
    default_model_id = str(conversation.get("default_model_id") or "").strip()
    default_bot_id = str(conversation.get("default_bot_id") or "").strip()
    safe_requested_bot_id = str(requested_bot_id or "").strip()
    route_uses_default_model = bool(
        default_model_id
        and (
            not safe_requested_bot_id
            or not default_bot_id
            or safe_requested_bot_id == default_bot_id
        )
    )
    if route_uses_default_model:
        catalog_model, blocker = _model_catalog_entry_from_cp(cp, default_model_id)
        if catalog_model:
            provider = str(catalog_model.get("provider") or "").strip()
            model_name = str(catalog_model.get("name") or catalog_model.get("id") or "").strip()
            capabilities = _model_capabilities(catalog_model)
            return {
                "source": "conversation_default_model",
                "provider": provider or None,
                "model": model_name or None,
                "model_id": str(catalog_model.get("id") or default_model_id).strip() or default_model_id,
                "capabilities": capabilities,
                "image_attachments_supported": _model_supports_image_attachments(
                    provider=provider,
                    model_name=model_name,
                    capabilities=capabilities,
                ),
                "blocker": "",
            }
        if blocker:
            return {
                "source": "conversation_default_model",
                "provider": None,
                "model": None,
                "model_id": default_model_id,
                "capabilities": [],
                "image_attachments_supported": False,
                "blocker": blocker,
            }

    backend = None
    for candidate in (bot or {}).get("backends") or []:
        if isinstance(candidate, dict):
            backend = candidate
            break
    provider = str((backend or {}).get("provider") or "").strip()
    model_name = str((backend or {}).get("model") or "").strip()
    return {
        "source": "bot_backend",
        "provider": provider or None,
        "model": model_name or None,
        "model_id": None,
        "capabilities": [],
        "image_attachments_supported": _model_supports_image_attachments(
            provider=provider,
            model_name=model_name,
            capabilities=[],
        ),
        "blocker": "" if backend else "no bot backend",
    }


def _default_route_model_compatibility_blocker_from_cp(cp: Any, default_bot_id: str, default_model_id: str) -> str:
    safe_bot_id = str(default_bot_id or "").strip()
    safe_model_id = str(default_model_id or "").strip()
    if not safe_bot_id or not safe_model_id or not hasattr(cp, "list_bots"):
        return ""
    model, model_blocker = _model_catalog_entry_from_cp(cp, safe_model_id)
    if model_blocker or not model:
        return ""
    expected_provider = str(model.get("provider") or "").strip()
    if not expected_provider:
        return ""
    try:
        bots = cp.list_bots()
    except Exception:
        return ""
    if not isinstance(bots, list):
        return ""
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        if str(bot.get("id") or "").strip() != safe_bot_id:
            continue
        for backend in bot.get("backends") or []:
            if not isinstance(backend, dict):
                continue
            backend_type = str(backend.get("type") or "").strip()
            if backend_type and backend_type not in {"cloud_api", "local_llm", "remote_llm"}:
                continue
            if str(backend.get("provider") or "").strip() == expected_provider:
                return ""
        return f"default_model_id provider '{expected_provider}' is not available on default_bot_id '{safe_bot_id}'"
    return ""


def _attachment_payload_blocker(raw_attachments: Any) -> str:
    if raw_attachments is None:
        return ""
    if not isinstance(raw_attachments, list):
        return "attachments must be a list"
    if len(raw_attachments) > CHAT_ATTACHMENT_MAX_FILES:
        return f"too many attachments; maximum is {CHAT_ATTACHMENT_MAX_FILES} files per message"
    total_bytes = 0
    for item in raw_attachments:
        if not isinstance(item, dict):
            return "attachments must contain objects"
        try:
            size_bytes = int(item.get("size_bytes") or 0)
        except Exception:
            size_bytes = 0
        total_bytes += max(0, size_bytes)
    if total_bytes > CHAT_ATTACHMENT_MAX_TOTAL_BYTES:
        return f"attachments exceed {CHAT_ATTACHMENT_MAX_TOTAL_BYTES} bytes total"
    return ""


def _context_payload_blocker(raw_context_items: Any, raw_context_item_ids: Any) -> str:
    if raw_context_items is not None:
        if not isinstance(raw_context_items, list):
            return "context_items must be a list"
        if len(raw_context_items) > 50:
            return "context_items is limited to 50 items"
        for item in raw_context_items:
            if not isinstance(item, str):
                return "context_items must contain strings"
            if len(item) > _CHAT_CONTEXT_ITEM_MAX_CHARS:
                return f"context_items entries are limited to {_CHAT_CONTEXT_ITEM_MAX_CHARS} characters"
    if raw_context_item_ids is not None:
        if not isinstance(raw_context_item_ids, list):
            return "context_item_ids must be a list"
        if len(raw_context_item_ids) > 200:
            return "context_item_ids is limited to 200 ids"
        for item_id in raw_context_item_ids:
            if not isinstance(item_id, str):
                return "context_item_ids must contain strings"
            if not item_id.strip():
                return "context_item_ids must contain non-empty strings"
            if len(item_id) > _CHAT_CONTEXT_ITEM_ID_MAX_CHARS:
                return f"context_item_ids entries are limited to {_CHAT_CONTEXT_ITEM_ID_MAX_CHARS} characters"
    return ""


def _message_content_blocker(content: str) -> str:
    if len(str(content or "")) > _CHAT_MESSAGE_CONTENT_MAX_CHARS:
        return f"message content is limited to {_CHAT_MESSAGE_CONTENT_MAX_CHARS} characters"
    return ""


def _attachments_include_image(attachments: Any) -> bool:
    if not isinstance(attachments, list):
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        mime_type = str(item.get("mime_type") or "").strip().lower()
        if kind == "image" or mime_type.startswith("image/"):
            return True
    return False


def _image_attachment_model_blocker_from_cp(
    cp: Any,
    conversation_id: str,
    requested_bot_id: str,
    attachments: Any,
) -> str:
    if not _attachments_include_image(attachments):
        return ""
    conversation = _conversation_from_cp(cp, conversation_id)
    if not conversation:
        return "conversation unavailable"
    default_model_id = str(conversation.get("default_model_id") or "").strip()
    default_bot_id = str(conversation.get("default_bot_id") or "").strip()
    safe_requested_bot_id = str(requested_bot_id or "").strip()
    route_uses_default_model = bool(
        default_model_id
        and (
            not safe_requested_bot_id
            or not default_bot_id
            or safe_requested_bot_id == default_bot_id
        )
    )
    if route_uses_default_model:
        catalog_model, catalog_blocker = _model_catalog_entry_from_cp(cp, default_model_id)
        if catalog_blocker:
            return f"effective model unavailable: {catalog_blocker}"
        if not catalog_model:
            return f"effective model unavailable: {default_model_id} is not in the enabled model catalog"
    context = _effective_chat_context_from_cp(
        cp,
        conversation_id,
        requested_bot_id=requested_bot_id,
        use_workspace_tools=False,
        inline_coding_enabled=False,
    )
    if context is None:
        return "conversation unavailable"
    model = context.get("model") if isinstance(context.get("model"), dict) else {}
    if model.get("image_attachments_supported") is True:
        return ""
    blocker = str(model.get("blocker") or "").strip()
    if blocker:
        return f"effective model unavailable: {blocker}"
    return "selected chat bot model does not support image attachments"


def _conversation_from_cp(cp: Any, conversation_id: str) -> dict[str, Any] | None:
    safe_conversation_id = str(conversation_id or "").strip()
    if not safe_conversation_id or not hasattr(cp, "list_conversations"):
        return None
    try:
        conversations = cp.list_conversations(archived="all")
    except TypeError:
        try:
            conversations = cp.list_conversations()
        except Exception:
            return None
    except Exception:
        return None
    for row in _normalize_conversation_rows(conversations):
        if str(row.get("id") or "").strip() == safe_conversation_id:
            return row
    return None


def _conversation_default_bot_id_from_cp(cp: Any, conversation_id: str) -> str:
    conversation = _conversation_from_cp(cp, conversation_id)
    return str((conversation or {}).get("default_bot_id") or "").strip()


def _workspace_tool_request_blocker_from_cp(cp: Any, conversation_id: str, requested_bot_id: str) -> str:
    conversation = _conversation_from_cp(cp, conversation_id)
    if not conversation:
        return "conversation unavailable"

    effective_bot_id = str(requested_bot_id or conversation.get("default_bot_id") or "").strip()
    if not effective_bot_id:
        return "no selected bot with tool access"

    bots: list[Any] = []
    try:
        bots = cp.list_bots() if hasattr(cp, "list_bots") else []
    except Exception:
        bots = []
    bot = next(
        (row for row in bots if isinstance(row, dict) and str(row.get("id") or "").strip() == effective_bot_id),
        None,
    )
    if not bot:
        return f"bot {effective_bot_id} unavailable"

    project_id = str(conversation.get("project_id") or "").strip()
    if not project_id:
        return "no scoped project"

    chat_access = _chat_tool_access_from_conversation(conversation)
    bot_access = bot_chat_tool_access(bot)
    if project_id:
        try:
            project_access = _normalize_project_chat_tool_access(cp.get_project_chat_tool_access(project_id))
        except Exception:
            project_access = _normalize_project_chat_tool_access(None)
    else:
        project_access = _normalize_project_chat_tool_access(None)

    reasons: list[str] = []
    if not bot_access.get("enabled"):
        reasons.append("bot off")
    if not chat_access.get("enabled"):
        reasons.append("chat off")
    if not project_access.get("enabled"):
        reasons.append("project off")

    shared_modes = _tool_modes(bot_access) & _tool_modes(chat_access) & _tool_modes(project_access)
    if not reasons and not shared_modes:
        reasons.append("no shared tool mode")

    return ", ".join(reasons)


def _bot_from_cp(cp: Any, bot_id: str) -> dict[str, Any] | None:
    safe_bot_id = str(bot_id or "").strip()
    if not safe_bot_id:
        return None
    try:
        bots = cp.list_bots() if hasattr(cp, "list_bots") else []
    except Exception:
        bots = []
    return next(
        (row for row in bots if isinstance(row, dict) and str(row.get("id") or "").strip() == safe_bot_id),
        None,
    )


def _inline_coding_request_blocker_from_cp(cp: Any, conversation_id: str, requested_bot_id: str) -> str:
    conversation = _conversation_from_cp(cp, conversation_id)
    if not conversation:
        return "conversation unavailable"
    if not str(conversation.get("project_id") or "").strip():
        return "no scoped project"

    effective_bot_id = str(requested_bot_id or conversation.get("default_bot_id") or "").strip()
    if not effective_bot_id:
        return "no selected coding-capable bot"
    bot = _bot_from_cp(cp, effective_bot_id)
    if not bot:
        return f"bot {effective_bot_id} unavailable"

    policy = bot.get("execution_policy") if isinstance(bot.get("execution_policy"), dict) else {}
    repo_output_mode = str(policy.get("repo_output_mode") or "deny").strip().lower()
    if repo_output_mode != "allow":
        return f"bot {effective_bot_id} repo output is {repo_output_mode or 'deny'}"
    return ""


def _project_memory_profiles_enabled_from_cp(cp: Any, project_id: str) -> bool:
    safe_project_id = str(project_id or "").strip()
    if not safe_project_id:
        return False
    try:
        projects = cp.list_projects() if hasattr(cp, "list_projects") else []
    except Exception:
        projects = []
    return _project_memory_profiles_enabled(projects, safe_project_id)


def _effective_chat_context_from_cp(
    cp: Any,
    conversation_id: str,
    *,
    requested_bot_id: str = "",
    use_workspace_tools: bool = False,
    inline_coding_enabled: bool = False,
) -> dict[str, Any] | None:
    conversation = _conversation_from_cp(cp, conversation_id)
    if not conversation:
        return None

    effective_bot_id = str(requested_bot_id or conversation.get("default_bot_id") or "").strip()
    bot = _bot_from_cp(cp, effective_bot_id)
    normalized_profile = bot_chat_profile(bot or {}) if bot else None
    project_id = str(conversation.get("project_id") or "").strip()
    chat_access = _chat_tool_access_from_conversation(conversation)
    bot_access = bot_chat_tool_access(bot or {})
    if project_id:
        try:
            project_access = _normalize_project_chat_tool_access(cp.get_project_chat_tool_access(project_id))
        except Exception:
            project_access = _normalize_project_chat_tool_access(None)
    else:
        project_access = _normalize_project_chat_tool_access(None)

    project_memory_enabled = True
    if project_id:
        project_memory_enabled = _project_memory_profiles_enabled_from_cp(cp, project_id)
    chat_memory_enabled = bool(conversation.get("memory_profiles_enabled", True))
    bot_memory_enabled = bool((bot or {}).get("memory_profiles_enabled", False))
    memory_reasons: list[str] = []
    if not effective_bot_id or not bot:
        memory_reasons.append("no bot selected")
    if not chat_memory_enabled:
        memory_reasons.append("chat off")
    if bot and not bot_memory_enabled:
        memory_reasons.append("bot off")
    if project_id and not project_memory_enabled:
        memory_reasons.append("project off")
    memory_active = bool(bot and chat_memory_enabled and bot_memory_enabled and project_memory_enabled)

    tool_reasons: list[str] = []
    if not effective_bot_id or not bot:
        tool_reasons.append("no selected bot with tool access")
    if bot and not bot_access.get("enabled"):
        tool_reasons.append("bot off")
    if not chat_access.get("enabled"):
        tool_reasons.append("chat off")
    if not project_id:
        tool_reasons.append("no scoped project")
    if project_id and not project_access.get("enabled"):
        tool_reasons.append("project off")
    shared_tool_modes = sorted(_tool_modes(bot_access) & _tool_modes(chat_access) & _tool_modes(project_access))
    if bot and bot_access.get("enabled") and chat_access.get("enabled") and project_access.get("enabled") and not shared_tool_modes:
        tool_reasons.append("no shared tool mode")
    tools_available = bool(bot and project_id and not tool_reasons and shared_tool_modes)

    coding_blocker = _inline_coding_request_blocker_from_cp(cp, conversation_id, effective_bot_id)
    inline_available = not bool(coding_blocker)
    model_context = _effective_model_context_from_cp(
        cp,
        conversation,
        bot,
        requested_bot_id=effective_bot_id,
    )

    return {
        "conversation_id": conversation.get("id"),
        "project_id": project_id or None,
        "bot": {
            "id": effective_bot_id or None,
            "name": str((bot or {}).get("name") or effective_bot_id or "").strip() or None,
            "available": bool(bot),
            "chat_profile": normalized_profile,
            "connection_actions": _policy_string_list(bot or {}, "connection_action_allowlist"),
            "owner_approval_actions": _policy_string_list(bot or {}, "connection_action_owner_approval_required"),
            "browser_actions": _policy_string_list(bot or {}, "browser_action_allowlist"),
            "browser_owner_approval_actions": _policy_string_list(bot or {}, "browser_action_owner_approval_required"),
            "http_connection_backend_count": _http_connection_backend_count(bot or {}),
        },
        "route": {
            "default_bot_id": conversation.get("default_bot_id"),
            "default_model_id": conversation.get("default_model_id"),
            "requested_bot_id": str(requested_bot_id or "").strip() or None,
        },
        "model": model_context,
        "memory": {
            "active": memory_active,
            "profile_id": conversation.get("memory_profile_id") or "default",
            "chat_enabled": chat_memory_enabled,
            "bot_enabled": bot_memory_enabled,
            "project_enabled": project_memory_enabled if project_id else None,
            "reasons": memory_reasons,
        },
        "workspace_tools": {
            "requested": bool(use_workspace_tools),
            "available": tools_available,
            "request_allowed": (not use_workspace_tools) or tools_available,
            "modes": shared_tool_modes,
            "chat_access": chat_access,
            "bot_access": bot_access,
            "project_access": project_access if project_id else None,
            "reasons": tool_reasons,
        },
        "inline_coding": {
            "requested": bool(inline_coding_enabled),
            "available": inline_available,
            "request_allowed": (not inline_coding_enabled) or inline_available,
            "blocker": coding_blocker,
        },
    }


def _task_sort_key(task: dict[str, Any]) -> tuple[int, int, str, str]:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    try:
        step_number = int(payload.get("step_number") or 0)
    except Exception:
        step_number = 0
    try:
        trigger_depth = int(metadata.get("trigger_depth") or 0)
    except Exception:
        trigger_depth = 0
    return (step_number, trigger_depth, str(task.get("created_at") or ""), str(task.get("updated_at") or ""))


def _task_output_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("output", "content", "text", "result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        try:
            return json.dumps(result, indent=2, sort_keys=True, default=str)
        except Exception:
            return str(result)
    if result is None:
        return ""
    return str(result)


def _task_truncation_note(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    finish_reason = str(result.get("finish_reason") or "").strip().lower()
    if finish_reason in {"length", "max_tokens", "max_output_tokens", "token_limit", "max_new_tokens"}:
        return "Model output likely hit token limit and may be incomplete."
    usage = result.get("usage")
    if isinstance(usage, dict):
        try:
            if int(usage.get("completion_tokens") or 0) >= 4096:
                return "Model output may be truncated (completion_tokens reached 4096)."
        except Exception:
            return ""
    return ""


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _append_indented(lines: list[str], text: str, *, indent: str = "  ") -> None:
    raw = str(text or "")
    if not raw:
        lines.append(f"{indent}(empty)")
        return
    for line in raw.splitlines():
        lines.append(f"{indent}{line}")


def _append_json_section(lines: list[str], label: str, value: Any) -> None:
    lines.append(f"- {label}:")
    _append_indented(lines, _json_text(value))


def _payload_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def _content_part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return _json_text(part)
    part_type = str(part.get("type") or "").strip().lower()
    if part_type == "text":
        return str(part.get("text") or "")
    if part_type == "image_url":
        image_obj = part.get("image_url")
        if isinstance(image_obj, dict):
            return f"[image_url] {str(image_obj.get('url') or '').strip()}"
        return "[image_url]"
    if part_type:
        return f"[{part_type}] {_json_text(part)}"
    return _json_text(part)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            text = _content_part_text(part).strip()
            if text:
                chunks.append(text)
        return "\n".join(chunks)
    if content is None:
        return ""
    return _json_text(content)


def _append_prompt_transcript(lines: list[str], payload: Any) -> None:
    messages = _payload_messages(payload)
    if not messages:
        return
    lines.append(f"- Prompt Messages: {len(messages)}")
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role") or "unknown").strip() or "unknown"
        lines.append(f"  - [{index}] role={role}")
        content_text = _message_content_text(message.get("content"))
        _append_indented(lines, content_text, indent="      ")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            lines.append("      tool_calls:")
            _append_indented(lines, _json_text(tool_calls), indent="        ")
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_call_id:
            lines.append(f"      tool_call_id: {tool_call_id}")


def _append_reasoning_sections(lines: list[str], result_obj: Any) -> None:
    if not isinstance(result_obj, dict):
        lines.append("- Model Thinking / Reasoning: not present in backend result.")
        return
    reasoning_keys = (
        "thinking",
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "analysis",
        "thoughts",
        "thinking_trace",
    )
    found = False
    for key in reasoning_keys:
        value = result_obj.get(key)
        if value in (None, "", [], {}):
            continue
        if not found:
            lines.append("- Model Thinking / Reasoning:")
            found = True
        lines.append(f"  - {key}:")
        _append_indented(lines, _json_text(value), indent="    ")
    if not found:
        lines.append("- Model Thinking / Reasoning: not present in backend result.")


def _assignment_full_recap(orchestration_id: str, tasks: list[dict[str, Any]]) -> str:
    ordered = sorted([task for task in tasks if isinstance(task, dict)], key=_task_sort_key)
    lines: list[str] = [
        f"Assignment Full Recap ({len(ordered)} tasks):",
        f"Orchestration ID: {orchestration_id}",
        "",
    ]
    for task in ordered:
        payload_value = task.get("payload")
        payload = payload_value if isinstance(payload_value, dict) else {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        bot_id = str(task.get("bot_id") or "unknown")
        workstream = str(payload.get("workstream") or payload.get("title") or "").strip()
        title = workstream or bot_id
        step_number = payload.get("step_number")
        step_count = payload.get("step_count")
        status = str(task.get("status") or "unknown")
        source = str(metadata.get("source") or "").strip()
        trigger_depth = metadata.get("trigger_depth")
        if step_number and step_count:
            step_label = f"Step {step_number}/{step_count}"
        elif trigger_depth is not None:
            step_label = f"Trigger depth {trigger_depth}"
        else:
            step_label = "Task"
        lines.extend(
            [
                f"{step_label}: {title}",
                f"- Status: {status}",
                f"- Task ID: {str(task.get('id') or '').strip() or 'unknown'}",
                f"- Bot: {bot_id}",
            ]
        )
        created_at = str(task.get("created_at") or "").strip()
        updated_at = str(task.get("updated_at") or "").strip()
        if created_at:
            lines.append(f"- Created At: {created_at}")
        if updated_at:
            lines.append(f"- Updated At: {updated_at}")
        if source:
            lines.append(f"- Source: {source}")
        deliverables = payload.get("deliverables") if isinstance(payload.get("deliverables"), list) else []
        if deliverables:
            lines.append("- Deliverables:")
            for item in deliverables:
                lines.append(f"  - {item}")
        _append_json_section(lines, "Task JSON", task)
        _append_json_section(lines, "Payload JSON", payload_value)
        _append_prompt_transcript(lines, payload_value)
        _append_json_section(lines, "Metadata JSON", metadata)
        result_obj = task.get("result")
        _append_json_section(lines, "Result JSON", result_obj)
        _append_reasoning_sections(lines, result_obj)
        if isinstance(result_obj, dict):
            if isinstance(result_obj.get("tool_calls"), list):
                lines.append(f"- Tool Calls Requested: {len(result_obj.get('tool_calls') or [])}")
                _append_json_section(lines, "Tool Calls Requested JSON", result_obj.get("tool_calls"))
            if isinstance(result_obj.get("tool_calls_executed"), list):
                lines.append(f"- Tool Calls Executed: {len(result_obj.get('tool_calls_executed') or [])}")
                _append_json_section(lines, "Tool Calls Executed JSON", result_obj.get("tool_calls_executed"))
            if isinstance(result_obj.get("usage"), dict):
                _append_json_section(lines, "Usage JSON", result_obj.get("usage"))
            finish_reason = str(result_obj.get("finish_reason") or "").strip()
            if finish_reason:
                lines.append(f"- Finish Reason: {finish_reason}")
        output = _task_output_text(task.get("result"))
        if output:
            lines.append("- Full Output:")
            lines.append(output)
        error = task.get("error")
        if error is not None:
            if isinstance(error, dict) and error.get("message"):
                lines.append(f"- Error: {error.get('message')}")
            _append_json_section(lines, "Error JSON", error)
        note = _task_truncation_note(task.get("result"))
        if note:
            lines.append(f"- Note: {note}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
logger = logging.getLogger(__name__)


def _humanize_bot_id(bot_id: str) -> str:
    raw = str(bot_id or "").strip()
    if not raw:
        return "Unknown Bot"
    cleaned = raw.replace("_", " ").replace("-", " ").strip()
    parts = [part for part in cleaned.split() if part]
    if not parts:
        return raw
    return " ".join(part.upper() if part.lower() in {"pm", "ui", "qc", "db"} else part.capitalize() for part in parts)


def _is_failed_pm_run_message(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if str(metadata.get("mode") or "").strip() not in {"pm_run_report", "assign_summary", "assign_pending"}:
        return False
    run_status = str(metadata.get("run_status") or "").strip().lower()
    ingest_allowed = metadata.get("ingest_allowed")
    return run_status == "failed" or ingest_allowed is False


def _cp_error_response(cp, fallback: str = "control plane unavailable"):
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = ""
    status_code = None
    if isinstance(err, dict):
        detail = str(err.get("detail") or "").strip()
        raw_code = err.get("status_code")
        if isinstance(raw_code, int) and 400 <= raw_code <= 599:
            status_code = raw_code
    return jsonify({"error": detail or fallback}), (status_code or 502)


def _stream_cp_headers(cp) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = ""
    if hasattr(cp, "api_token"):
        token = str(getattr(cp, "api_token") or "").strip()
    if not token:
        token = (os.environ.get("CONTROL_PLANE_API_TOKEN", "") or "").strip()
    if token:
        headers["X-Nexus-API-Key"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _current_memory_user_id() -> str | None:
    email = str(getattr(current_user, "email", "") or "").strip()
    if email:
        return email
    try:
        value = str(current_user.get_id() or "").strip()
    except Exception:
        value = ""
    return value or None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, raw in value.items():
            safe[str(key)] = _json_safe(raw)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_bridge_project_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip() for item in raw]
        return [value for value in values if value]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        # Legacy rows may store JSON text or a single project id.
        try:
            parsed = json.loads(text)
        except Exception:
            return [text]
        if isinstance(parsed, (list, tuple, set)):
            values = [str(item or "").strip() for item in parsed]
            return [value for value in values if value]
        if isinstance(parsed, str):
            value = parsed.strip()
            return [value] if value else []
        return []
    return []


def _normalize_conversation_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    if not cid:
        return None
    normalized = dict(raw)
    normalized["id"] = cid
    title = str(raw.get("title") or "").strip()
    normalized["title"] = title or cid
    normalized["project_id"] = str(raw.get("project_id") or "").strip() or None
    normalized["scope"] = str(raw.get("scope") or "").strip() or "global"
    normalized["default_bot_id"] = str(raw.get("default_bot_id") or "").strip() or None
    normalized["default_model_id"] = str(raw.get("default_model_id") or "").strip() or None
    normalized["owner_user_id"] = str(raw.get("owner_user_id") or "").strip() or None
    normalized["memory_profiles_enabled"] = bool(raw.get("memory_profiles_enabled", True))
    normalized["memory_profile_id"] = str(raw.get("memory_profile_id") or "default").strip() or "default"
    normalized["created_at"] = str(raw.get("created_at") or "").strip() or None
    normalized["updated_at"] = str(raw.get("updated_at") or "").strip() or None
    normalized["archived_at"] = str(raw.get("archived_at") or "").strip() or None
    normalized["bridge_project_ids"] = _normalize_bridge_project_ids(raw.get("bridge_project_ids"))
    normalized["tool_access_enabled"] = bool(raw.get("tool_access_enabled") or False)
    normalized["tool_access_filesystem"] = bool(raw.get("tool_access_filesystem") or False)
    normalized["tool_access_repo_search"] = bool(raw.get("tool_access_repo_search") or False)
    return normalized


def _normalize_conversation_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_conversation_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _workspace_tool_access_requested(data: dict[str, Any]) -> bool:
    return bool(
        data.get("tool_access_enabled", False)
        or data.get("tool_access_filesystem", False)
        or data.get("tool_access_repo_search", False)
    )


def _workspace_tool_access_allowed_for_create(data: dict[str, Any]) -> bool:
    scope = str(data.get("scope") or "global").strip()
    project_id = str(data.get("project_id") or "").strip()
    return scope in {"project", "bridged"} and bool(project_id)


def _normalize_create_conversation_scope(data: dict[str, Any]) -> str:
    scope = str(data.get("scope") or "global").strip() or "global"
    if scope not in _ALLOWED_CONVERSATION_SCOPES:
        raise ValueError("scope must be one of: global, project, bridged")
    return scope


def _normalize_project_chat_tool_access(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "filesystem": bool(raw.get("filesystem", False)),
        "repo_search": bool(raw.get("repo_search", False)),
    }


def _chat_tool_access_from_conversation(conversation: dict[str, Any] | None) -> dict[str, bool]:
    row = conversation if isinstance(conversation, dict) else {}
    return {
        "enabled": bool(row.get("tool_access_enabled", False)),
        "filesystem": bool(row.get("tool_access_filesystem", False)),
        "repo_search": bool(row.get("tool_access_repo_search", False)),
    }


def _tool_modes(access: dict[str, bool]) -> set[str]:
    if not bool(access.get("enabled", False)):
        return set()
    modes: set[str] = set()
    if bool(access.get("filesystem", False)):
        modes.add("filesystem")
    if bool(access.get("repo_search", False)):
        modes.add("repo_search")
    return modes


def _request_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _request_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    parsed = int(value)
    return max(minimum, min(maximum, parsed))


def _project_memory_profiles_enabled(projects: list[Any], project_id: str) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False
    for project in projects:
        if not isinstance(project, dict):
            continue
        if str(project.get("id") or "").strip() == normalized_project_id:
            return bool(project.get("memory_profiles_enabled", False))
    return False


def _conversation_matches_project(row: dict[str, Any], project_id: str) -> bool:
    pid = str(project_id or "").strip()
    if not pid:
        return True
    primary_id = str(row.get("project_id") or "").strip()
    bridge_ids = {str(item or "").strip() for item in row.get("bridge_project_ids") or []}
    bridge_ids.discard("")
    if pid == UNSCOPED_PROJECT_FILTER:
        return not primary_id and not bridge_ids
    return primary_id == pid or pid in bridge_ids


def _normalize_message_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    mid = str(raw.get("id") or "").strip()
    if not mid:
        return None
    normalized: dict[str, Any] = {
        "id": mid,
        "role": str(raw.get("role") or "").strip() or "assistant",
        "content": str(raw.get("content") or ""),
        "created_at": str(raw.get("created_at") or "").strip() or None,
        "bot_id": str(raw.get("bot_id") or "").strip() or None,
        "provider": str(raw.get("provider") or "").strip() or None,
        "model": str(raw.get("model") or "").strip() or None,
        "metadata": None,
    }
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        normalized["metadata"] = _json_safe(metadata)
    elif isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            normalized["metadata"] = _json_safe(parsed) if isinstance(parsed, dict) else None
        except Exception:
            normalized["metadata"] = None
    return normalized


def _normalize_message_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_message_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_vault_item_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    metadata = raw.get("metadata")
    return {
        "id": item_id,
        "title": str(raw.get("title") or item_id).strip() or item_id,
        "namespace": str(raw.get("namespace") or "").strip() or None,
        "project_id": str(raw.get("project_id") or "").strip() or None,
        "content": str(raw.get("content") or ""),
        "created_at": str(raw.get("created_at") or "").strip() or None,
        "updated_at": str(raw.get("updated_at") or "").strip() or None,
        "metadata": _json_safe(metadata) if isinstance(metadata, dict) else None,
    }


def _normalize_vault_item_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("items", "results", "data"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                raw = candidate
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for row in raw:
        normalized = _normalize_vault_item_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _cp_list_messages_safe(cp: Any, conversation_id: str, *, limit: int | None) -> Any:
    try:
        return cp.list_messages(conversation_id, limit=limit)
    except TypeError:
        return cp.list_messages(conversation_id)


def _cp_list_vault_items_safe(
    cp: Any,
    *,
    namespace: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    include_content: bool = True,
) -> Any:
    try:
        return cp.list_vault_items(
            namespace=namespace,
            project_id=project_id,
            limit=limit,
            include_content=include_content,
        )
    except TypeError:
        return cp.list_vault_items(namespace=namespace, project_id=project_id, limit=limit)


def _cp_list_tasks_safe(cp: Any, **kwargs) -> Any:
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


@bp.get("/chat")
@login_required
def chat_page() -> str:
    cp = get_cp_client()
    page_error: str | None = None
    active_project_filter = str(request.args.get("project_id") or "").strip()
    try:
        try:
            conversations = _normalize_conversation_rows(cp.list_conversations(archived="all") or [])
        except Exception:
            conversations = []
            page_error = "Conversation list is temporarily unavailable."

        try:
            bots = cp.list_bots() or []
        except Exception:
            bots = []
        try:
            bot_readiness_payload = cp.list_bot_readiness() if hasattr(cp, "list_bot_readiness") else {}
        except Exception:
            bot_readiness_payload = {}

        try:
            projects = cp.list_projects() or []
        except Exception:
            projects = []
        try:
            model_catalog = cp.list_models() or []
        except Exception:
            model_catalog = []

        if active_project_filter:
            conversations = [c for c in conversations if _conversation_matches_project(c, active_project_filter)]

        selected_id = str(request.args.get("conversation_id") or "").strip()
        selected = None
        messages: list[dict[str, Any]] = []
        repo_context_items: list[dict[str, Any]] = []
        repo_context_sections: list[dict[str, Any]] = []
        repo_context_item_ids: list[str] = []
        selected_project_ids: list[str] = []
        if selected_id:
            for c in conversations:
                if c.get("id") == selected_id:
                    selected = c
                    break
            try:
                messages = _normalize_message_rows(
                    _cp_list_messages_safe(cp, selected_id, limit=None) or []
                )
            except Exception:
                messages = []
                page_error = page_error or "Selected conversation messages could not be loaded."

        if selected:
            project_id = str(selected.get("project_id") or "").strip()
            if project_id:
                selected_project_ids.append(project_id)
            for bridged in selected.get("bridge_project_ids") or []:
                value = str(bridged or "").strip()
                if value and value not in selected_project_ids:
                    selected_project_ids.append(value)

            for pid in selected_project_ids:
                namespace = f"project:{pid}:repo"
                if hasattr(cp, "get_project_github_context_sync_status"):
                    try:
                        status = cp.get_project_github_context_sync_status(pid) or {}
                        if isinstance(status, dict):
                            context_sync = status.get("context_sync") if isinstance(status.get("context_sync"), dict) else {}
                            ns = str(context_sync.get("namespace") or "").strip()
                            if ns:
                                namespace = ns
                    except Exception:
                        namespace = f"project:{pid}:repo"
                try:
                    items_raw = _cp_list_vault_items_safe(
                        cp,
                        namespace=namespace,
                        project_id=pid,
                        limit=30,
                        include_content=False,
                    ) or []
                except Exception:
                    items_raw = []
                items = _normalize_vault_item_rows(items_raw)
                if items:
                    repo_context_sections.append(
                        {
                            "project_id": pid,
                            "namespace": namespace,
                            "items": items,
                        }
                    )
                    repo_context_items.extend(items)
                    for item in items:
                        item_id = str(item.get("id") or "").strip()
                        if item_id and item_id not in repo_context_item_ids:
                            repo_context_item_ids.append(item_id)

        selected_project_id = str((selected or {}).get("project_id") or "").strip()
        selected_project_memory_profiles_enabled = _project_memory_profiles_enabled(projects, selected_project_id)
        selected_project_chat_tool_access = _normalize_project_chat_tool_access(
            cp.get_project_chat_tool_access(selected_project_id)
            if selected_project_id and hasattr(cp, "get_project_chat_tool_access")
            else None
        )
        selected_project_work = _selected_project_work_summary(cp, selected_project_ids)

        try:
            vault_items_raw = _cp_list_vault_items_safe(cp, limit=30, include_content=False) or []
        except Exception:
            vault_items_raw = []
        vault_items = _normalize_vault_item_rows(vault_items_raw)

        bots = _with_bot_readiness(bots, bot_readiness_payload)
        chat_bots = _chat_selectable_bots(bots)
        assignment_bots = _assignment_manager_bots(bots)

        return render_template(
            "chat.html",
            conversations=[c for c in conversations if not c.get("archived_at")],
            archived_conversations=[c for c in conversations if c.get("archived_at")],
            active_project_filter=active_project_filter,
            unscoped_project_filter=UNSCOPED_PROJECT_FILTER,
            selected_conversation=selected,
            messages=messages,
            bots=bots,
            chat_bots=chat_bots,
            assignment_bots=assignment_bots,
            projects=projects,
            vault_items=vault_items,
            repo_context_items=repo_context_items,
            repo_context_sections=repo_context_sections,
            repo_context_item_ids=repo_context_item_ids,
            selected_project_memory_profiles_enabled=selected_project_memory_profiles_enabled,
            selected_project_chat_tool_access=selected_project_chat_tool_access,
            selected_project_work=selected_project_work,
            model_catalog=model_catalog,
            bot_display_labels=_bot_display_labels(bots),
            model_display_labels=_model_display_labels(model_catalog),
            chat_attachment_limits={
                "max_files": CHAT_ATTACHMENT_MAX_FILES,
                "max_total_bytes": CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
            },
            error=page_error,
        )
    except Exception:
        logger.exception(
            "chat_page failed unexpectedly",
            extra={"conversation_id": str(request.args.get("conversation_id") or "").strip() or None},
        )
        return render_template(
            "chat.html",
            conversations=[],
            archived_conversations=[],
            active_project_filter=active_project_filter,
            unscoped_project_filter=UNSCOPED_PROJECT_FILTER,
            selected_conversation=None,
            messages=[],
            bots=[],
            chat_bots=[],
            assignment_bots=[],
            projects=[],
            vault_items=[],
            repo_context_items=[],
            repo_context_sections=[],
            repo_context_item_ids=[],
            selected_project_memory_profiles_enabled=False,
            selected_project_chat_tool_access=_normalize_project_chat_tool_access(None),
            selected_project_work={"available": False, "project_ids": [], "counts": {}, "total": 0, "by_manager": [], "recent": []},
            model_catalog=[],
            bot_display_labels={},
            model_display_labels={},
            chat_attachment_limits={
                "max_files": CHAT_ATTACHMENT_MAX_FILES,
                "max_total_bytes": CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
            },
            error="Chat view is temporarily unavailable. Start a new chat or refresh.",
        )


@bp.post("/api/chat/conversations")
@login_required
def api_create_conversation():
    data: dict[str, Any] = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    try:
        scope = _normalize_create_conversation_scope(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if _workspace_tool_access_requested(data) and not _workspace_tool_access_allowed_for_create(data):
        return jsonify({"error": "workspace tools require a project-scoped or bridged conversation"}), 400
    cp = get_cp_client()
    default_bot_id = str(data.get("default_bot_id") or "").strip()
    default_model_id = str(data.get("default_model_id") or "").strip()
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, default_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Default bot is unavailable: {readiness_blocker}"}), 409
    model_blocker = _model_catalog_blocker_from_cp(cp, default_model_id)
    if model_blocker:
        return jsonify({"error": f"Default model is unavailable: {model_blocker}"}), 409
    route_blocker = _default_route_model_compatibility_blocker_from_cp(cp, default_bot_id, default_model_id)
    if route_blocker:
        return jsonify({"error": f"Default route is unavailable: {route_blocker}"}), 409
    created = cp.create_conversation(
        {
            "title": title,
            "project_id": data.get("project_id"),
            "bridge_project_ids": data.get("bridge_project_ids") or [],
            "scope": scope,
            "default_bot_id": default_bot_id or None,
            "default_model_id": default_model_id or None,
            "owner_user_id": _current_memory_user_id(),
            "memory_profiles_enabled": bool(data.get("memory_profiles_enabled", True)),
            "memory_profile_id": data.get("memory_profile_id") or "default",
            "tool_access_enabled": bool(data.get("tool_access_enabled", False)),
            "tool_access_filesystem": bool(data.get("tool_access_filesystem", False)),
            "tool_access_repo_search": bool(data.get("tool_access_repo_search", False)),
        }
    )
    if created is None:
        return _cp_error_response(cp)
    return jsonify(created), 201


@bp.delete("/api/chat/conversations/<conversation_id>")
@login_required
def api_delete_conversation(conversation_id: str):
    cp = get_cp_client()
    ok = cp.delete_conversation(conversation_id)
    if not ok:
        return _cp_error_response(cp, "conversation delete failed")
    return "", 204


@bp.post("/api/chat/conversations/<conversation_id>/archive")
@login_required
def api_archive_conversation(conversation_id: str):
    cp = get_cp_client()
    archived = cp.archive_conversation(conversation_id)
    if archived is None:
        return _cp_error_response(cp, "conversation archive failed")
    return jsonify(archived)


@bp.post("/api/chat/conversations/<conversation_id>/restore")
@login_required
def api_restore_conversation(conversation_id: str):
    cp = get_cp_client()
    restored = cp.restore_conversation(conversation_id)
    if restored is None:
        return _cp_error_response(cp, "conversation restore failed")
    return jsonify(restored)


@bp.put("/api/chat/conversations/<conversation_id>/tool-access")
@login_required
def api_update_conversation_tool_access(conversation_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    updated = cp.update_conversation_tool_access(
        conversation_id=conversation_id,
        enabled=bool(data.get("enabled", False)),
        filesystem=bool(data.get("filesystem", False)),
        repo_search=bool(data.get("repo_search", False)),
    )
    if updated is None:
        return _cp_error_response(cp, "conversation tool access update failed")
    return jsonify(updated)


@bp.put("/api/chat/conversations/<conversation_id>/memory-profile")
@login_required
def api_update_conversation_memory_profile(conversation_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    updated = cp.update_conversation_memory_profile(
        conversation_id=conversation_id,
        enabled=bool(data.get("enabled", True)),
        profile_id=str(data.get("profile_id") or "default").strip() or "default",
    )
    if updated is None:
        return _cp_error_response(cp, "conversation memory profile update failed")
    return jsonify(updated)


@bp.get("/api/chat/conversations/<conversation_id>/effective-context")
@login_required
def api_conversation_effective_context(conversation_id: str):
    cp = get_cp_client()
    context = _effective_chat_context_from_cp(
        cp,
        conversation_id,
        requested_bot_id=str(request.args.get("bot_id") or "").strip(),
        use_workspace_tools=_request_bool(request.args.get("use_workspace_tools", False)),
        inline_coding_enabled=_request_bool(request.args.get("inline_coding_enabled", False)),
    )
    if context is None:
        return jsonify({"error": "conversation unavailable"}), 404
    return jsonify(context)


@bp.put("/api/chat/conversations/<conversation_id>/route-defaults")
@login_required
def api_update_conversation_route_defaults(conversation_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    default_bot_id = str(data.get("default_bot_id") or "").strip()
    default_model_id = str(data.get("default_model_id") or "").strip()
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, default_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Default bot is unavailable: {readiness_blocker}"}), 409
    model_blocker = _model_catalog_blocker_from_cp(cp, default_model_id)
    if model_blocker:
        return jsonify({"error": f"Default model is unavailable: {model_blocker}"}), 409
    route_blocker = _default_route_model_compatibility_blocker_from_cp(cp, default_bot_id, default_model_id)
    if route_blocker:
        return jsonify({"error": f"Default route is unavailable: {route_blocker}"}), 409
    updated = cp.update_conversation_route_defaults(
        conversation_id=conversation_id,
        default_bot_id=default_bot_id or None,
        default_model_id=default_model_id or None,
    )
    if updated is None:
        return _cp_error_response(cp, "conversation route defaults update failed")
    return jsonify(updated)


@bp.post("/api/chat/messages")
@login_required
def api_send_message():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    if not conversation_id or (not content and not attachments):
        return jsonify({"error": "conversation_id and either content or attachments are required"}), 400
    content_blocker = _message_content_blocker(content)
    if content_blocker:
        return jsonify({"error": f"Invalid message content: {content_blocker}"}), 400
    attachment_blocker = _attachment_payload_blocker(data.get("attachments"))
    if attachment_blocker:
        return jsonify({"error": f"Invalid attachments: {attachment_blocker}"}), 400
    context_blocker = _context_payload_blocker(data.get("context_items"), data.get("context_item_ids"))
    if context_blocker:
        return jsonify({"error": f"Invalid context payload: {context_blocker}"}), 400
    cp = get_cp_client()
    bot_id = str(data.get("bot_id") or "").strip()
    readiness_bot_id = bot_id or _conversation_default_bot_id_from_cp(cp, conversation_id)
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, readiness_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Selected bot is unavailable: {readiness_blocker}"}), 409
    image_model_blocker = _image_attachment_model_blocker_from_cp(cp, conversation_id, bot_id, attachments)
    if image_model_blocker:
        return jsonify({"error": f"Image attachments are not available: {image_model_blocker}"}), 409
    use_workspace_tools = _request_bool(data.get("use_workspace_tools", False))
    if use_workspace_tools:
        tool_blocker = _workspace_tool_request_blocker_from_cp(cp, conversation_id, bot_id)
        if tool_blocker:
            return jsonify({"error": f"Workspace tools are not available: {tool_blocker}"}), 409
    inline_coding_enabled = _request_bool(data.get("inline_coding_enabled", False))
    if inline_coding_enabled:
        coding_blocker = _inline_coding_request_blocker_from_cp(cp, conversation_id, bot_id)
        if coding_blocker:
            return jsonify({"error": f"Inline coding is not available: {coding_blocker}"}), 409

    resp = cp.post_message(
        conversation_id,
        {
            "content": content,
            "bot_id": bot_id or data.get("bot_id"),
            "user_id": _current_memory_user_id(),
            "attachments": attachments,
            "context_items": data.get("context_items"),
            "context_item_ids": data.get("context_item_ids"),
            "include_project_context": data.get("include_project_context", False),
            "use_workspace_tools": use_workspace_tools,
            "inline_coding_enabled": inline_coding_enabled,
        },
    )
    if resp is None:
        return _cp_error_response(cp, "chat message failed")
    return jsonify(resp)


@bp.post("/api/chat/assignments/preview")
@login_required
def api_assignment_preview():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    instruction = str(data.get("instruction") or "").strip()
    pm_bot_id = str(data.get("pm_bot_id") or data.get("bot_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    if not pm_bot_id:
        return jsonify({"error": "pm_bot_id is required"}), 400
    context_blocker = _context_payload_blocker(data.get("context_items"), data.get("context_item_ids"))
    if context_blocker:
        return jsonify({"error": f"Invalid context payload: {context_blocker}"}), 400
    cp = get_cp_client()
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, pm_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Project manager bot is unavailable: {readiness_blocker}"}), 409
    preview = cp.preview_assignment(
        {
            "conversation_id": conversation_id,
            "instruction": instruction,
            "pm_bot_id": pm_bot_id,
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
            "context_items": data.get("context_items") if isinstance(data.get("context_items"), list) else [],
            "context_item_ids": data.get("context_item_ids") if isinstance(data.get("context_item_ids"), list) else [],
        }
    )
    if preview is None:
        return _cp_error_response(cp, "assignment preview failed")
    return jsonify(preview)


@bp.post("/api/chat/assignments")
@login_required
def api_create_assignment():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    instruction = str(data.get("instruction") or "").strip()
    pm_bot_id = str(data.get("pm_bot_id") or data.get("bot_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    if not pm_bot_id:
        return jsonify({"error": "pm_bot_id is required"}), 400
    context_blocker = _context_payload_blocker(data.get("context_items"), data.get("context_item_ids"))
    if context_blocker:
        return jsonify({"error": f"Invalid context payload: {context_blocker}"}), 400
    cp = get_cp_client()
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, pm_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Project manager bot is unavailable: {readiness_blocker}"}), 409
    created = cp.create_assignment(
        {
            "conversation_id": conversation_id,
            "instruction": instruction,
            "pm_bot_id": pm_bot_id,
            "run_id": data.get("run_id"),
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
            "context_items": data.get("context_items") if isinstance(data.get("context_items"), list) else [],
            "context_item_ids": data.get("context_item_ids") if isinstance(data.get("context_item_ids"), list) else [],
        }
    )
    if created is None:
        return _cp_error_response(cp, "assignment create failed")
    return jsonify(created)


@bp.get("/api/chat/assignments/<assignment_id>/graph")
@login_required
def api_assignment_graph(assignment_id: str):
    cp = get_cp_client()
    graph = cp.get_assignment_graph(assignment_id)
    if graph is None:
        return _cp_error_response(cp, "assignment graph failed")
    return jsonify(graph)


@bp.post("/api/chat/assignments/<assignment_id>/splice")
@login_required
def api_assignment_splice(assignment_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    from_node_id = str(data.get("from_node_id") or "").strip()
    if not from_node_id:
        return jsonify({"error": "from_node_id is required"}), 400
    context_blocker = _context_payload_blocker(data.get("context_items"), data.get("context_item_ids"))
    if context_blocker:
        return jsonify({"error": f"Invalid context payload: {context_blocker}"}), 400
    cp = get_cp_client()
    result = cp.splice_assignment(
        assignment_id,
        {
            "from_node_id": from_node_id,
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
            "context_items": data.get("context_items") if isinstance(data.get("context_items"), list) else [],
            "context_item_ids": data.get("context_item_ids") if isinstance(data.get("context_item_ids"), list) else [],
        },
    )
    if result is None:
        return _cp_error_response(cp, "assignment splice failed")
    return jsonify(result)


@bp.post("/api/chat/assignments/<assignment_id>/nodes/<node_id>/rerun")
@login_required
def api_assignment_rerun_node(assignment_id: str, node_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.rerun_assignment_node(assignment_id, node_id, payload=data.get("payload"))
    if result is None:
        return _cp_error_response(cp, "assignment node rerun failed")
    return jsonify(result)


@bp.get("/api/chat/assignments/<assignment_id>/lineage")
@login_required
def api_assignment_lineage(assignment_id: str):
    cp = get_cp_client()
    result = cp.list_assignment_lineage(assignment_id)
    if result is None:
        return _cp_error_response(cp, "assignment lineage unavailable")
    return jsonify(result)


@bp.post("/api/chat/assignments/apply")
@login_required
def api_apply_assignment_files():
    data: dict[str, Any] = request.get_json(force=True) or {}
    orchestration_id = (data.get("orchestration_id") or "").strip()
    project_id = (data.get("project_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required"}), 400
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400

    cp = get_cp_client()
    result = cp.apply_project_assignment_to_repo_workspace(
        project_id=project_id,
        orchestration_id=orchestration_id,
        overwrite=_request_bool(data.get("overwrite", True)),
    )
    if result is None:
        return _cp_error_response(cp, "assignment apply failed")
    return jsonify(result)


@bp.post("/api/chat/assignments/review")
@login_required
def api_review_assignment_files():
    data: dict[str, Any] = request.get_json(force=True) or {}
    orchestration_id = (data.get("orchestration_id") or "").strip()
    project_id = (data.get("project_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required"}), 400
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400

    cp = get_cp_client()
    try:
        max_content_chars = _request_int(data.get("max_content_chars"), 20000, minimum=1000, maximum=200000)
        diff_context_lines = _request_int(data.get("diff_context_lines"), 3, minimum=0, maximum=20)
    except (TypeError, ValueError):
        return jsonify({"error": "max_content_chars and diff_context_lines must be integers"}), 400
    result = cp.review_project_assignment_files(
        project_id=project_id,
        orchestration_id=orchestration_id,
        include_content=_request_bool(data.get("include_content", True)),
        max_content_chars=max_content_chars,
        diff_context_lines=diff_context_lines,
    )
    if result is None:
        return _cp_error_response(cp, "assignment review failed")
    return jsonify(result)


@bp.get("/api/chat/conversations/<conversation_id>/messages")
@login_required
def api_list_messages(conversation_id: str):
    cp = get_cp_client()
    raw_limit = request.args.get("limit")
    try:
        limit = max(1, int(raw_limit)) if raw_limit is not None else None
    except Exception:
        limit = None
    messages = _cp_list_messages_safe(cp, conversation_id, limit=limit)
    if messages is None:
        return _cp_error_response(cp, "chat messages unavailable")
    return jsonify(messages)


@bp.post("/api/chat/stream")
@login_required
def api_send_message_stream():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    if not conversation_id or (not content and not attachments):
        return jsonify({"error": "conversation_id and either content or attachments are required"}), 400
    content_blocker = _message_content_blocker(content)
    if content_blocker:
        return jsonify({"error": f"Invalid message content: {content_blocker}"}), 400
    attachment_blocker = _attachment_payload_blocker(data.get("attachments"))
    if attachment_blocker:
        return jsonify({"error": f"Invalid attachments: {attachment_blocker}"}), 400
    context_blocker = _context_payload_blocker(data.get("context_items"), data.get("context_item_ids"))
    if context_blocker:
        return jsonify({"error": f"Invalid context payload: {context_blocker}"}), 400

    cp = get_cp_client()
    bot_id = str(data.get("bot_id") or "").strip()
    readiness_bot_id = bot_id or _conversation_default_bot_id_from_cp(cp, conversation_id)
    readiness_blocker = _bot_readiness_blocker_from_cp(cp, readiness_bot_id)
    if readiness_blocker:
        return jsonify({"error": f"Selected bot is unavailable: {readiness_blocker}"}), 409
    image_model_blocker = _image_attachment_model_blocker_from_cp(cp, conversation_id, bot_id, attachments)
    if image_model_blocker:
        return jsonify({"error": f"Image attachments are not available: {image_model_blocker}"}), 409
    use_workspace_tools = _request_bool(data.get("use_workspace_tools", False))
    if use_workspace_tools:
        tool_blocker = _workspace_tool_request_blocker_from_cp(cp, conversation_id, bot_id)
        if tool_blocker:
            return jsonify({"error": f"Workspace tools are not available: {tool_blocker}"}), 409
    inline_coding_enabled = _request_bool(data.get("inline_coding_enabled", False))
    if inline_coding_enabled:
        coding_blocker = _inline_coding_request_blocker_from_cp(cp, conversation_id, bot_id)
        if coding_blocker:
            return jsonify({"error": f"Inline coding is not available: {coding_blocker}"}), 409
    cp_base = (
        cp.base_url
        if hasattr(cp, "base_url")
        else os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
    )
    stream_url = f"{cp_base.rstrip('/')}/v1/chat/conversations/{conversation_id}/stream"
    payload = {
        "content": content,
        "bot_id": bot_id or data.get("bot_id"),
        "user_id": _current_memory_user_id(),
        "attachments": attachments,
        "context_items": data.get("context_items"),
        "context_item_ids": data.get("context_item_ids"),
        "include_project_context": data.get("include_project_context", False),
        "use_workspace_tools": use_workspace_tools,
        "inline_coding_enabled": inline_coding_enabled,
    }

    heartbeat_seconds = os.environ.get("CHAT_STREAM_HEARTBEAT_SECONDS", "15")

    def _open_upstream():
        return requests.post(
            stream_url,
            json=payload,
            headers=_stream_cp_headers(cp),
            stream=True,
            timeout=(10, None),
        )

    def generate() -> Iterable[str]:
        yield from proxy_upstream_sse_lines(_open_upstream, heartbeat_seconds=heartbeat_seconds)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.post("/api/chat/ingest")
@login_required
def api_ingest_chat():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    namespace = (data.get("namespace") or "global").strip() or "global"
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    cp = get_cp_client()
    conversation = None
    for c in (cp.list_conversations() or []):
        if c.get("id") == conversation_id:
            conversation = c
            break
    messages = cp.list_messages(conversation_id)
    if not conversation or messages is None:
        return jsonify({"error": "conversation or messages unavailable"}), 502

    lines = []
    for m in messages:
        if isinstance(m, dict) and _is_failed_pm_run_message(m):
            continue
        lines.append(f"[{m.get('role', 'unknown')}] {m.get('content', '')}")
    content = "\n\n".join(lines)
    title = f"Chat: {conversation.get('title', conversation_id)}"
    ingested = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": namespace,
            "source_type": "chat",
            "source_ref": conversation_id,
            "metadata": {"conversation_id": conversation_id},
        }
    )
    if ingested is None:
        return jsonify({"error": "vault ingestion failed"}), 502
    return jsonify(ingested), 201


@bp.post("/api/chat/message-to-vault")
@login_required
def api_ingest_message_to_vault():
    data: Dict[str, Any] = request.get_json(force=True) or {}
    message = data.get("message") or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    namespace = (data.get("namespace") or "global").strip() or "global"
    if not isinstance(message, dict):
        return jsonify({"error": "message object is required"}), 400
    if _is_failed_pm_run_message(message):
        return jsonify({"error": "failed PM run reports cannot be ingested into the vault"}), 400
    content = str(message.get("content") or "").strip()
    if not content:
        return jsonify({"error": "message content is required"}), 400

    title = f"Chat Message: {message.get('role', 'unknown')}"
    cp = get_cp_client()
    ingested = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": namespace,
            "source_type": "chat",
            "source_ref": message.get("id"),
            "metadata": {
                "conversation_id": conversation_id or None,
                "role": message.get("role"),
                "bot_id": message.get("bot_id"),
            },
        }
    )
    if ingested is None:
        return jsonify({"error": "vault ingestion failed"}), 502
    return jsonify(ingested), 201


@bp.post("/api/chat/orchestrations/<orchestration_id>/mark-failed")
@login_required
def api_mark_pm_run_failed(orchestration_id: str):
    data: Dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    cp = get_cp_client()
    updated = cp.mark_pm_run_failed(conversation_id, orchestration_id)
    if updated is None:
        return _cp_error_response(cp, fallback="failed to mark PM run as failed")
    return jsonify(updated)


@bp.get("/api/chat/orchestrations/<orchestration_id>/graph")
@login_required
def api_orchestration_graph(orchestration_id: str):
    cp = get_cp_client()
    assignment_graph: Dict[str, Any] = {}
    if hasattr(cp, "get_assignment_graph_by_orchestration"):
        try:
            fetched = cp.get_assignment_graph_by_orchestration(orchestration_id)
        except Exception:
            fetched = None
        if isinstance(fetched, dict):
            assignment_graph = fetched

    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=False)
    if tasks is None:
        fallback_tasks = assignment_graph.get("tasks") if isinstance(assignment_graph.get("tasks"), list) else None
        if fallback_tasks is None:
            return jsonify({"error": "control plane unavailable"}), 502
        tasks = fallback_tasks

    scoped_tasks = [task for task in tasks if isinstance(task, dict)]
    assignment_id = str(assignment_graph.get("assignment_id") or "").strip()
    run_id = str(assignment_graph.get("run_id") or "").strip()
    run_state = str(assignment_graph.get("state") or "").strip()
    node_overrides = (
        assignment_graph.get("node_overrides")
        if isinstance(assignment_graph.get("node_overrides"), dict)
        else {}
    )
    task_by_id = {
        str(task.get("id") or "").strip(): task
        for task in scoped_tasks
        if str(task.get("id") or "").strip()
    }
    bot_cache: Dict[str, Dict[str, Any]] = {}
    bot_name_map: Dict[str, str] = {}
    reference_graph: Dict[str, Any] | None = None
    pipeline_entry_bot_id = ""

    def _bot_doc(bot_id: str) -> Dict[str, Any] | None:
        normalized = str(bot_id or "").strip()
        if not normalized:
            return None
        if normalized in bot_cache:
            return bot_cache[normalized]
        try:
            bot_doc = cp.get_bot(normalized)
        except Exception:
            bot_doc = None
        bot_cache[normalized] = bot_doc if isinstance(bot_doc, dict) else {}
        return bot_cache[normalized] or None

    def _metadata_value(task: Dict[str, Any], key: str) -> str:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        return str(metadata.get(key) or "").strip()

    root_candidates = []
    for task in scoped_tasks:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        source = str(metadata.get("source") or "").strip().lower()
        parent_task_id = str(metadata.get("parent_task_id") or "").strip()
        if source in {"chat_assign", "auto_retry"} or not parent_task_id:
            root_candidates.append(task)
    for task in root_candidates + scoped_tasks:
        for key in ("pipeline_entry_bot_id", "pm_bot_id", "root_pm_bot_id"):
            candidate = _metadata_value(task, key)
            if candidate:
                pipeline_entry_bot_id = candidate
                break
        if pipeline_entry_bot_id:
            break
        candidate_bot_id = str(task.get("bot_id") or "").strip()
        if candidate_bot_id:
            pipeline_entry_bot_id = candidate_bot_id
            break

    ordered_bot_ids: list[str] = []

    def _add_bot_id(bot_id: str) -> None:
        normalized = str(bot_id or "").strip()
        if normalized and normalized not in ordered_bot_ids:
            ordered_bot_ids.append(normalized)

    _add_bot_id(pipeline_entry_bot_id)
    for task in scoped_tasks:
        _add_bot_id(str(task.get("bot_id") or "").strip())

    for bot_id in ordered_bot_ids:
        bot_doc = _bot_doc(bot_id)
        if bot_doc is None:
            bot_name_map[bot_id] = _humanize_bot_id(bot_id)
            continue
        bot_name_map[bot_id] = str(bot_doc.get("name") or _humanize_bot_id(bot_id))
        workflow = bot_doc.get("workflow") if isinstance(bot_doc.get("workflow"), dict) else {}
        candidate_graph = workflow.get("reference_graph") if isinstance(workflow, dict) else None
        if (
            reference_graph is None
            and isinstance(candidate_graph, dict)
            and candidate_graph.get("nodes")
            and (
                not pipeline_entry_bot_id
                or str(candidate_graph.get("entry_bot_id") or "").strip() == pipeline_entry_bot_id
                or bot_id == pipeline_entry_bot_id
            )
        ):
            reference_graph = candidate_graph
    if reference_graph is None:
        for bot_id in ordered_bot_ids:
            bot_doc = _bot_doc(bot_id)
            workflow = bot_doc.get("workflow") if isinstance(bot_doc.get("workflow"), dict) else {}
            candidate_graph = workflow.get("reference_graph") if isinstance(workflow, dict) else None
            if isinstance(candidate_graph, dict) and candidate_graph.get("nodes"):
                reference_graph = candidate_graph
                break

    reference_nodes = (
        reference_graph.get("nodes")
        if isinstance(reference_graph, dict) and isinstance(reference_graph.get("nodes"), list)
        else []
    )
    reference_node_by_bot = {
        str(node.get("bot_id") or "").strip(): node
        for node in reference_nodes
        if isinstance(node, dict) and str(node.get("bot_id") or "").strip()
    }
    stage_order = [
        str(node.get("bot_id") or "").strip()
        for node in reference_nodes
        if isinstance(node, dict) and str(node.get("bot_id") or "").strip()
    ]
    if not stage_order:
        stage_order = [
            "pm-orchestrator",
            "pm-research-analyst",
            "pm-engineer",
            "pm-coder",
            "pm-tester",
            "pm-security-reviewer",
            "pm-database-engineer",
            "pm-ui-tester",
            "pm-final-qc",
        ]
    for bot_id in ordered_bot_ids:
        if bot_id and bot_id not in stage_order:
            stage_order.append(bot_id)

    root_node_id = f"orchestrator::{orchestration_id}"
    synthetic_root_bot_id = pipeline_entry_bot_id or "pm-orchestrator"
    synthetic_root_name = bot_name_map.get(synthetic_root_bot_id, _humanize_bot_id(synthetic_root_bot_id))
    synthetic_root_stage_kind = str((reference_node_by_bot.get(synthetic_root_bot_id) or {}).get("stage_kind") or "entry")
    has_explicit_entry_task = any(
        str((task.get("metadata") or {}).get("source") or "").strip().lower() in {"chat_assign", "auto_retry"}
        for task in scoped_tasks
    )
    is_chat_assignment = any(
        str((task.get("metadata") or {}).get("source") or "").strip().lower() in {"chat_assign", "auto_retry", "bot_trigger"}
        for task in scoped_tasks
    )

    nodes = []
    edges = []
    if scoped_tasks and is_chat_assignment and not has_explicit_entry_task:
        nodes.append(
            {
                "id": root_node_id,
                "title": synthetic_root_name,
                "step_id": synthetic_root_bot_id,
                "status": "completed",
                "bot_id": synthetic_root_bot_id,
                "display_name": synthetic_root_name,
                "stage_key": synthetic_root_bot_id,
                "stage_kind": synthetic_root_stage_kind,
                "depends_on": [],
                "synthetic": True,
                "details": {
                    "task_id": root_node_id,
                    "run_id": root_node_id,
                    "title": synthetic_root_name,
                    "bot_id": synthetic_root_bot_id,
                    "bot_name": synthetic_root_name,
                    "status": "completed",
                    "source": "synthetic_root",
                    "step_id": synthetic_root_bot_id,
                    "trigger_rule_id": "",
                    "trigger_depth": 0,
                    "parent_task_id": "",
                    "join_task_ids": [],
                    "fanout_id": "",
                    "fanout_branch_key": "",
                },
            }
        )

    for t in scoped_tasks:
        task_id = str(t.get("id"))
        metadata = t.get("metadata") or {}
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        step_id = str(metadata.get("step_id") or "").strip()
        bot_id_label = str(t.get("bot_id") or "").strip()
        bot_name = bot_name_map.get(bot_id_label, _humanize_bot_id(bot_id_label))
        title = str(
            payload.get("title")
            or payload.get("workstream")
            or metadata.get("pipeline_name")
            or step_id
            or bot_id_label
            or task_id
        )
        depends_on = [str(dep) for dep in (t.get("depends_on") or []) if str(dep).strip()]
        source = str(metadata.get("source") or "").strip().lower()
        parent_task_id = str(metadata.get("parent_task_id") or "").strip()
        trigger_rule_id = str(metadata.get("trigger_rule_id") or "").strip()
        fanout_id = str(metadata.get("fanout_id") or payload.get("fanout_id") or "").strip()
        fanout_branch_key = str(metadata.get("fanout_branch_key") or payload.get("fanout_branch_key") or "").strip()
        join_task_ids = [str(jid) for jid in (payload.get("join_task_ids") or []) if str(jid).strip()]
        retry_of_task_id = str(metadata.get("retry_of_task_id") or "").strip()
        retried_by_task_id = str(metadata.get("retried_by_task_id") or "").strip()
        branch_index = None
        for raw_value in (
            payload.get("workstream_index"),
            payload.get("research_step_index"),
            metadata.get("workstream_index"),
            metadata.get("research_step_index"),
        ):
            try:
                if raw_value is None or str(raw_value).strip() == "":
                    continue
                branch_index = int(raw_value)
                break
            except Exception:
                continue
        lane_key = fanout_branch_key or (str(branch_index) if branch_index is not None else "")

        if not depends_on:
            if join_task_ids:
                # Show all joined sibling tasks as dependencies (fan-in)
                depends_on = join_task_ids
            elif source == "bot_trigger" and parent_task_id:
                depends_on = [parent_task_id]
            elif source in {"chat_assign", "auto_retry"} and is_chat_assignment and not has_explicit_entry_task:
                depends_on = [root_node_id]

        nodes.append(
            {
                "id": task_id,
                "title": title,
                "step_id": step_id,
                "status": t.get("status"),
                "bot_id": bot_id_label,
                "display_name": bot_name,
                "stage_key": bot_id_label,
                "stage_kind": str((reference_node_by_bot.get(bot_id_label) or {}).get("stage_kind") or ""),
                "branch_index": branch_index,
                "lane_key": lane_key,
                "depends_on": depends_on,
                "status_variant": str(t.get("status") or "queued").strip().lower() or "queued",
                "is_rerouted": False,
                "is_retried": bool(
                    str(source) in {"auto_retry", "manual_retry"}
                    or retry_of_task_id
                    or retried_by_task_id
                    or str(t.get("status") or "").strip().lower() == "retried"
                ),
                "details": {
                    "task_id": task_id,
                    "run_id": task_id,
                    "title": title,
                    "bot_id": bot_id_label,
                    "bot_name": bot_name,
                    "status": t.get("status"),
                    "source": source,
                    "step_id": step_id,
                    "trigger_rule_id": trigger_rule_id,
                    "trigger_depth": metadata.get("trigger_depth"),
                    "parent_task_id": parent_task_id,
                    "join_task_ids": join_task_ids,
                    "fanout_id": fanout_id,
                    "fanout_branch_key": fanout_branch_key,
                    "workstream_index": payload.get("workstream_index"),
                    "research_step_index": payload.get("research_step_index"),
                    "lane_key": lane_key,
                    "retry_of_task_id": retry_of_task_id,
                    "retried_by_task_id": retried_by_task_id,
                    "created_at": t.get("created_at"),
                    "synthetic": False,
                },
            }
        )
        for dep in depends_on:
            edges.append({"from": str(dep), "to": task_id})

    stage_index = {stage_id: index for index, stage_id in enumerate(stage_order)}
    for node in nodes:
        if bool(node.get("synthetic")):
            continue
        details = node.get("details") if isinstance(node.get("details"), dict) else {}
        parent_task_id = str(details.get("parent_task_id") or "").strip()
        source = str(details.get("source") or "").strip().lower()
        parent_task = task_by_id.get(parent_task_id)
        parent_bot_id = str((parent_task or {}).get("bot_id") or "").strip()
        current_stage = stage_index.get(str(node.get("stage_key") or node.get("bot_id") or "").strip(), -1)
        parent_stage = stage_index.get(parent_bot_id, -1)
        is_rerouted = source == "bot_trigger" and parent_stage >= 0 and current_stage >= 0 and current_stage < parent_stage
        node["is_rerouted"] = is_rerouted
        if is_rerouted:
            node["status_variant"] = "rerouted"
        elif bool(node.get("is_retried")):
            node["status_variant"] = "retried"
        else:
            node["status_variant"] = str(node.get("status") or "queued").strip().lower() or "queued"
        details["route_state"] = "sent_back_and_reran" if is_rerouted else ("retried" if bool(node.get("is_retried")) else "")
        details["parent_bot_id"] = parent_bot_id
        node["details"] = details

    return jsonify(
        {
            "orchestration_id": orchestration_id,
            "assignment_id": assignment_id or None,
            "run_id": run_id or None,
            "state": run_state or None,
            "node_overrides": node_overrides,
            "nodes": nodes,
            "edges": edges,
            "stage_order": stage_order,
            "reference_graph": reference_graph or {},
        }
    )


@bp.get("/api/chat/orchestrations/<orchestration_id>/recap")
@login_required
def api_orchestration_recap(orchestration_id: str):
    cp = get_cp_client()
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=True)
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    recap = _assignment_full_recap(orchestration_id, tasks)
    return jsonify({"orchestration_id": orchestration_id, "task_count": len(tasks), "recap": recap})
