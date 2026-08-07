"""Shared bot chat profile normalization for dashboard surfaces."""
from __future__ import annotations

from typing import Any


CHAT_PROFILE_LABELS = {
    "chat": "Chat Only",
    "tutor": "Tutor / Reasoning",
    "vision": "Vision / STEM",
    "coding": "Coding",
    "automation": "Automation",
}


def _dict_from_any(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def bot_chat_tool_access(bot: dict[str, Any]) -> dict[str, Any]:
    routing = bot.get("routing_rules") if isinstance(bot.get("routing_rules"), dict) else {}
    raw = routing.get("chat_tool_access") if isinstance(routing, dict) else None
    if not isinstance(raw, dict):
        raw = routing.get("tool_access") if isinstance(routing, dict) else None
    cfg = raw if isinstance(raw, dict) else {}
    enabled = bool(cfg.get("enabled", False))
    filesystem = bool(cfg.get("filesystem", False)) if enabled else False
    repo_search = bool(cfg.get("repo_search", False)) if enabled else False
    web_search = bool(cfg.get("web_search", False)) if enabled else False
    mode_error = "no enabled tool mode" if enabled and not (filesystem or repo_search or web_search) else ""
    return {
        "enabled": enabled,
        "filesystem": filesystem,
        "repo_search": repo_search,
        "web_search": web_search,
        "mode_error": mode_error,
    }


def _legacy_direct_chat_enabled(bot: dict[str, Any]) -> bool:
    """Retain existing chat availability until a bot is explicitly reconfigured."""
    routing = bot.get("routing_rules") if isinstance(bot.get("routing_rules"), dict) else {}
    operator_profile = routing.get("operator_profile") if isinstance(routing.get("operator_profile"), dict) else {}
    autonomy = str(operator_profile.get("autonomy") or "").strip().lower()
    role = str(bot.get("role") or "").strip().lower()
    name = str(bot.get("name") or "").strip().lower()
    bot_id = str(bot.get("id") or "").strip().lower()
    tools = bot_chat_tool_access(bot)
    return bool(
        autonomy == "manual_chat_only"
        or tools.get("enabled")
        or role in {"assistant", "coding_assistant", "code-reviewer", "tutor"}
        or bot_id.startswith("personal-")
        or "chat" in name
    )


def bot_direct_chat_access(bot: dict[str, Any]) -> dict[str, Any]:
    """Normalize direct-chat eligibility separately from bot execution and tools."""
    routing = bot.get("routing_rules") if isinstance(bot.get("routing_rules"), dict) else {}
    raw = routing.get("direct_chat") if isinstance(routing.get("direct_chat"), dict) else None
    explicit = isinstance(raw, dict) and isinstance(raw.get("enabled"), bool)
    return {
        "enabled": bool(raw.get("enabled")) if explicit else _legacy_direct_chat_enabled(bot),
        "explicit": explicit,
    }


def bot_chat_profile(bot: dict[str, Any]) -> dict[str, Any]:
    routing = bot.get("routing_rules") if isinstance(bot.get("routing_rules"), dict) else {}
    raw_profile = routing.get("chat_profile") if isinstance(routing, dict) else None
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    operator_profile = routing.get("operator_profile") if isinstance(routing, dict) else None
    operator_profile = operator_profile if isinstance(operator_profile, dict) else {}
    launch_profile = routing.get("launch_profile") if isinstance(routing, dict) else None
    launch_profile = launch_profile if isinstance(launch_profile, dict) else {}
    assignment_capabilities = _dict_from_any(bot.get("assignment_capabilities"))
    tool_access = bot_chat_tool_access(bot)
    direct_chat = bot_direct_chat_access(bot)
    policy = _dict_from_any(bot.get("execution_policy"))
    mode = str(profile.get("mode") or "").strip().lower()
    if mode not in CHAT_PROFILE_LABELS:
        if (
            str(policy.get("repo_output_mode") or "").strip().lower() == "allow"
            or bool(policy.get("inline_coding_default", False))
            or bool(tool_access.get("filesystem", False))
        ):
            mode = "coding"
        elif any(
            str(cap or "").strip().lower() in {"vision", "image", "images", "multimodal", "math"}
            for backend in bot.get("backends") or []
            for cap in (
                backend.get("capabilities", [])
                if isinstance(backend, dict) and isinstance(backend.get("capabilities"), list)
                else []
            )
        ):
            mode = "vision"
        else:
            mode = "chat"
    capabilities: list[str] = []
    def add_capability(label: str) -> None:
        safe_label = str(label or "").strip()
        if safe_label and safe_label not in capabilities:
            capabilities.append(safe_label)

    for capability in profile.get("capabilities", []) if isinstance(profile.get("capabilities"), list) else []:
        add_capability(str(capability or ""))
    if bool(profile.get("attachments", True)):
        add_capability("attachments")
    if bool(profile.get("document_generation", False)):
        add_capability("document_generation")
    if bool(profile.get("document_editing", False)):
        add_capability("document_editing")
    if mode in {"vision", "tutor"} or bool(profile.get("image_understanding", False)):
        add_capability("image_understanding")
    if mode in {"vision", "tutor"} or bool(profile.get("diagrams", False)):
        add_capability("diagrams")
    if mode in {"vision", "tutor"} or bool(profile.get("math_reasoning", False)):
        add_capability("math_reasoning")
    if mode in {"vision", "tutor"} or bool(profile.get("physics_reasoning", False)):
        add_capability("physics_reasoning")
    if mode in {"vision", "tutor"} or bool(profile.get("engineering_reasoning", False)):
        add_capability("engineering_reasoning")
    if mode == "tutor" or bool(profile.get("step_by_step_reasoning", False)):
        add_capability("step_by_step_reasoning")
    if bool(tool_access.get("repo_search", False)):
        add_capability("repo_search")
    if bool(tool_access.get("filesystem", False)):
        add_capability("filesystem")
    if bool(tool_access.get("web_search", False)):
        add_capability("web_search")
    if bool(policy.get("inline_coding_default", False)):
        add_capability("inline_coding_default")
    if str(policy.get("repo_output_mode") or "").strip().lower() == "allow":
        add_capability("repo_output")
    tool_labels: list[str] = []
    if bool(tool_access.get("filesystem", False)):
        tool_labels.append("filesystem")
    if bool(tool_access.get("repo_search", False)):
        tool_labels.append("repo_search")
    if bool(tool_access.get("web_search", False)):
        tool_labels.append("web_search")
    autonomy = str(operator_profile.get("autonomy") or "").strip() or "unspecified"
    if bool(assignment_capabilities.get("is_project_manager", False)):
        use_label = "Project manager"
    elif bool(launch_profile.get("is_pipeline", False)) or bool(assignment_capabilities.get("is_pipeline_entry", False)):
        use_label = "Pipeline entry"
    elif routing.get("workflow") or routing.get("external_trigger"):
        use_label = "Workflow worker"
    elif autonomy == "scheduled_worker":
        use_label = "Scheduled worker"
    elif bool(tool_access.get("mode_error")):
        use_label = "Tool policy incomplete"
    elif bool(tool_access.get("enabled", False)) or str(policy.get("repo_output_mode") or "").strip().lower() == "allow":
        use_label = "Tool-enabled chat"
    elif autonomy == "manual_chat_only":
        use_label = "Manual chat"
    else:
        use_label = "General chat"
    return {
        "mode": mode,
        "label": str(profile.get("label") or CHAT_PROFILE_LABELS[mode]).strip(),
        "description": str(profile.get("description") or "").strip(),
        "capabilities": capabilities,
        "document_generation": bool(profile.get("document_generation", False)),
        "document_editing": bool(profile.get("document_editing", False)),
        "tool_access": tool_access,
        "tool_label": (
            ", ".join(tool_labels)
            if bool(tool_access.get("enabled", False)) and tool_labels
            else (str(tool_access.get("mode_error") or "").strip() or "off")
        ),
        "autonomy": autonomy,
        "use_label": use_label,
        "repo_output_mode": str(policy.get("repo_output_mode") or "deny").strip().lower(),
        "inline_coding_default": bool(policy.get("inline_coding_default", False)),
        "direct_chat_enabled": bool(direct_chat["enabled"]),
        "direct_chat_explicit": bool(direct_chat["explicit"]),
    }


def with_bot_chat_profiles(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for bot in bots:
        row = dict(bot)
        row["chat_profile"] = bot_chat_profile(row)
        row["direct_chat_access"] = bot_direct_chat_access(row)
        enriched.append(row)
    return enriched
