"""Normalized checks for declared worker tooling."""
from __future__ import annotations

import re
from typing import Any, Iterable, List


def normalize_tool_name(value: Any) -> str:
    return re.sub(r"[-_\s]+", "-", str(value or "").strip().lower()).strip("-")


def required_worker_tools(bot: Any) -> List[str]:
    policy = getattr(bot, "execution_policy", None) or {}
    raw = policy.get("required_worker_tools", []) if isinstance(policy, dict) else getattr(policy, "required_worker_tools", [])
    if not isinstance(raw, list):
        return []
    tools: List[str] = []
    for item in raw:
        normalized = normalize_tool_name(item)
        if normalized and normalized not in tools:
            tools.append(normalized)
    return tools


def worker_missing_tools(worker: Any, required_tools: Iterable[str]) -> List[str]:
    required = [normalize_tool_name(item) for item in required_tools]
    required = [item for item in required if item]
    if not required:
        return []

    advertised: set[str] = set()
    for capability in getattr(worker, "capabilities", None) or []:
        capability_type = normalize_tool_name(getattr(capability, "type", ""))
        if capability_type not in {"tool", "custom"}:
            continue
        advertised.add(normalize_tool_name(getattr(capability, "provider", "")))
        for model in getattr(capability, "models", None) or []:
            normalized = normalize_tool_name(model)
            if normalized:
                advertised.add(normalized)
    return [tool for tool in required if tool not in advertised]
