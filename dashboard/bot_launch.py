from __future__ import annotations

from typing import Any


def normalize_launch_profile(bot: dict[str, Any]) -> dict[str, Any] | None:
    routing_rules = bot.get("routing_rules") if isinstance(bot, dict) else None
    profile = None
    if isinstance(routing_rules, dict):
        profile = routing_rules.get("launch_profile")
    if profile is None and isinstance(bot, dict):
        profile = bot.get("launch_profile")
    if not isinstance(profile, dict):
        return None

    payload = profile.get("payload")
    if payload is None and isinstance(routing_rules, dict):
        input_contract = routing_rules.get("input_contract")
        if isinstance(input_contract, dict):
            candidate = input_contract.get("default_payload")
            if isinstance(candidate, dict):
                payload = candidate
    if not isinstance(payload, dict):
        return None

    label = str(profile.get("label") or bot.get("name") or bot.get("id") or "Launch Bot").strip()
    description = str(profile.get("description") or "").strip()
    project_id = str(profile.get("project_id") or "").strip() or None
    priority_raw = profile.get("priority")
    try:
        priority = int(priority_raw) if priority_raw not in (None, "") else None
    except (TypeError, ValueError):
        priority = None

    return {
        "enabled": bool(profile.get("enabled", True)),
        "label": label,
        "description": description,
        "payload": payload,
        "project_id": project_id,
        "priority": priority,
        "show_on_overview": bool(profile.get("show_on_overview", True)),
        "show_on_tasks": bool(profile.get("show_on_tasks", True)),
        "is_pipeline": bool(profile.get("is_pipeline", False)),
        "pipeline_name": str(profile.get("pipeline_name") or label).strip() or label,
    }


def launchable_bots(bots: list[dict[str, Any]], *, surface: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bot in bots:
        profile = normalize_launch_profile(bot)
        if not profile or not profile["enabled"]:
            continue
        if surface == "overview" and not profile["show_on_overview"]:
            continue
        if surface == "tasks" and not profile["show_on_tasks"]:
            continue
        rows.append(
            {
                "id": str(bot.get("id") or ""),
                "name": str(bot.get("name") or bot.get("id") or "Bot"),
                "role": str(bot.get("role") or ""),
                "launch_profile": profile,
            }
        )
    rows.sort(key=lambda item: (item["launch_profile"]["label"].lower(), item["name"].lower()))
    return rows
