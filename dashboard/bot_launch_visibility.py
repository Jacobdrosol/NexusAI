from __future__ import annotations

from typing import Any


def blocked_launch_bot_ids(tooling_status: dict[str, Any] | None) -> set[str]:
    """Return bot IDs with concrete tooling states that should hide quick launch buttons."""
    blocked: set[str] = set()
    for row in (tooling_status or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "").strip().lower()
        if state not in {"blocked", "disabled"}:
            continue
        bot_id = str(row.get("bot_id") or "").strip()
        if bot_id:
            blocked.add(bot_id)
    return blocked
