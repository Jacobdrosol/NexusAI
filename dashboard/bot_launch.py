from __future__ import annotations

import json
from typing import Any


def _lookup_payload_path(payload: Any, path: str) -> Any:
    current = payload
    for part in [segment for segment in str(path or "").split(".") if segment]:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None
    return current


def _resolve_transform_value(expr: str, payload: Any) -> Any:
    raw_expr = str(expr or "").strip()
    mode = "value"
    if raw_expr.startswith("json:"):
        mode = "json"
        raw_expr = raw_expr[5:].strip()
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
            return None
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
        return _resolve_transform_value(expr, payload)
    return template


def normalize_launch_payload(bot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    routing_rules = bot.get("routing_rules") if isinstance(bot, dict) else None
    if not isinstance(routing_rules, dict):
        return payload

    input_transform = routing_rules.get("input_transform")
    if isinstance(input_transform, dict) and bool(input_transform.get("enabled", False)):
        template = input_transform.get("template")
        if template is not None:
            transformed = _transform_template_value(template, payload)
            if isinstance(transformed, dict):
                return transformed

    output_contract = routing_rules.get("output_contract")
    if isinstance(output_contract, dict) and str(output_contract.get("mode") or "").strip().lower() == "payload_transform":
        template = output_contract.get("template")
        if template is not None:
            transformed = _transform_template_value(template, payload)
            if isinstance(transformed, dict):
                return transformed

    return payload


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
