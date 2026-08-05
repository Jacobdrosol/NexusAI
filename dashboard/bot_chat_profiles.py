"""Shared bot chat profile normalization for dashboard surfaces."""
from __future__ import annotations

from typing import Any


CHAT_PROFILE_LABELS = {
    "chat": "Chat Only",
    "tutor": "Tutor / Reasoning",
    "vision": "Vision / Math",
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
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "filesystem": bool(cfg.get("filesystem", False)),
        "repo_search": bool(cfg.get("repo_search", False)),
    }


def bot_chat_profile(bot: dict[str, Any]) -> dict[str, Any]:
    routing = bot.get("routing_rules") if isinstance(bot.get("routing_rules"), dict) else {}
    raw_profile = routing.get("chat_profile") if isinstance(routing, dict) else None
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    operator_profile = routing.get("operator_profile") if isinstance(routing, dict) else None
    operator_profile = operator_profile if isinstance(operator_profile, dict) else {}
    tool_access = bot_chat_tool_access(bot)
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
    if mode in {"vision", "tutor"} or bool(profile.get("image_understanding", False)):
        add_capability("image_understanding")
    if mode in {"vision", "tutor"} or bool(profile.get("diagrams", False)):
        add_capability("diagrams")
    if mode in {"vision", "tutor"} or bool(profile.get("math_reasoning", False)):
        add_capability("math_reasoning")
    if mode == "tutor" or bool(profile.get("step_by_step_reasoning", False)):
        add_capability("step_by_step_reasoning")
    if bool(tool_access.get("repo_search", False)):
        add_capability("repo_search")
    if bool(tool_access.get("filesystem", False)):
        add_capability("filesystem")
    if bool(policy.get("inline_coding_default", False)):
        add_capability("inline_coding_default")
    if str(policy.get("repo_output_mode") or "").strip().lower() == "allow":
        add_capability("repo_output")
    tool_labels: list[str] = []
    if bool(tool_access.get("filesystem", False)):
        tool_labels.append("filesystem")
    if bool(tool_access.get("repo_search", False)):
        tool_labels.append("repo_search")
    return {
        "mode": mode,
        "label": str(profile.get("label") or CHAT_PROFILE_LABELS[mode]).strip(),
        "description": str(profile.get("description") or "").strip(),
        "capabilities": capabilities,
        "tool_access": tool_access,
        "tool_label": ", ".join(tool_labels) if bool(tool_access.get("enabled", False)) else "off",
        "autonomy": str(operator_profile.get("autonomy") or "").strip() or "unspecified",
        "repo_output_mode": str(policy.get("repo_output_mode") or "deny").strip().lower(),
        "inline_coding_default": bool(policy.get("inline_coding_default", False)),
    }


def with_bot_chat_profiles(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for bot in bots:
        row = dict(bot)
        row["chat_profile"] = bot_chat_profile(row)
        enriched.append(row)
    return enriched
