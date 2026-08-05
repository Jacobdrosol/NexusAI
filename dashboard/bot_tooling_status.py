"""Operator summaries for bot readiness and required worker tooling."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _readiness_by_bot(readiness_payload: Any) -> dict[str, dict[str, Any]]:
    payload = _as_dict(readiness_payload)
    rows = _as_list(payload.get("readiness"))
    return {
        str(row.get("bot_id") or "").strip(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("bot_id") or "").strip()
    }


def _worker_by_id(workers: Any) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("id") or "").strip(): row
        for row in _as_list(workers)
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }


def _probe_by_worker_id(worker_probes_payload: Any) -> dict[str, dict[str, Any]]:
    payload = _as_dict(worker_probes_payload)
    return {
        str(row.get("worker_id") or "").strip(): row
        for row in _as_list(payload.get("probes"))
        if isinstance(row, dict) and str(row.get("worker_id") or "").strip()
    }


def _required_tools(bot: dict[str, Any]) -> list[str]:
    policy = _as_dict(bot.get("execution_policy"))
    tools = []
    for tool in _as_list(policy.get("required_worker_tools")):
        label = str(tool or "").strip()
        if label and label not in tools:
            tools.append(label)
    return tools


def _worker_ids(bot: dict[str, Any]) -> list[str]:
    ids = []
    for backend in _as_list(bot.get("backends")):
        if not isinstance(backend, dict):
            continue
        worker_id = str(backend.get("worker_id") or "").strip()
        if worker_id and worker_id not in ids:
            ids.append(worker_id)
    return ids


def _policy_actions(bot: dict[str, Any], key: str) -> list[str]:
    policy = _as_dict(bot.get("execution_policy"))
    actions = []
    for action in _as_list(policy.get(key)):
        label = str(action or "").strip()
        if label and label not in actions:
            actions.append(label)
    return actions


def _connection_backend_count(bot: dict[str, Any]) -> int:
    count = 0
    for backend in _as_list(bot.get("backends")):
        if not isinstance(backend, dict):
            continue
        provider = str(backend.get("provider") or "").strip().lower()
        backend_type = str(backend.get("type") or "").strip().lower()
        if provider == "http_connection" or backend_type == "http_connection":
            count += 1
    return count


def _connection_context_label(bot: dict[str, Any]) -> str:
    routing = _as_dict(bot.get("routing_rules"))
    config = _as_dict(routing.get("connection_context"))
    if not config:
        return ""
    for key in ("connection_name", "fetch_connection_name", "connection_id", "fetch_connection_id"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    return "attached connection context"


def _failed_messages(readiness: dict[str, Any]) -> list[str]:
    messages = []
    for check in _as_list(readiness.get("checks")):
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").strip().lower()
        if status not in {"failed", "blocking"}:
            continue
        message = str(check.get("message") or check.get("component") or "blocking readiness check").strip()
        if message:
            messages.append(message)
    return messages


def _blocking_category(messages: list[str], required_tools: list[str], worker_ids: list[str]) -> str:
    joined = " ".join(messages).lower()
    if "browser" in joined or "browser-ui" in required_tools:
        return "browser_session"
    if "cli authentication" in joined or "unauthenticated" in joined:
        return "cli_auth"
    if "vault credential" in joined or "credential" in joined:
        return "credential"
    if "worker" in joined or worker_ids:
        return "worker_runtime"
    if "model" in joined:
        return "model"
    if messages:
        return "readiness"
    return "unknown"


def _blocking_category_view(category: str) -> dict[str, str]:
    labels = {
        "browser_session": "Authenticated browser session",
        "cli_auth": "CLI authentication",
        "credential": "Vault credential",
        "worker_runtime": "Worker runtime",
        "model": "Model availability",
        "readiness": "Readiness check",
        "unknown": "Unknown blocker",
    }
    details = {
        "browser_session": (
            "The worker and site account can exist, but browser-backed tools still need a live "
            "authenticated browser profile for rendered UI work."
        ),
        "cli_auth": "A configured CLI tool is installed but needs a local login or token refresh.",
        "credential": "A required key-vault credential reference is missing or unavailable.",
        "worker_runtime": "The configured worker is missing, disabled, offline, or not exposing the required capability.",
        "model": "The configured model or provider backend is not currently usable.",
        "readiness": "One or more readiness checks are failing.",
        "unknown": "The control plane did not provide a specific blocker category.",
    }
    normalized = category if category in labels else "unknown"
    return {"label": labels[normalized], "detail": details[normalized]}


def _probe_status(worker_id: str, probes: dict[str, dict[str, Any]]) -> str:
    probe = probes.get(worker_id) or {}
    status = str(probe.get("probe_status") or "").strip().lower()
    return status or "unknown"


def build_bot_tooling_status(
    *,
    bots: list[dict[str, Any]] | None,
    readiness_payload: dict[str, Any] | None,
    workers: list[dict[str, Any]] | None,
    worker_probes_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded operator view of bot tooling readiness."""
    bot_rows = [row for row in (bots or []) if isinstance(row, dict)]
    readiness_lookup = _readiness_by_bot(readiness_payload)
    workers_lookup = _worker_by_id(workers)
    probe_lookup = _probe_by_worker_id(worker_probes_payload)

    state_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    connection_action_counts: Counter[str] = Counter()
    browser_action_counts: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    blocked_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []

    for bot in bot_rows:
        bot_id = str(bot.get("id") or "").strip()
        enabled = bool(bot.get("enabled", True))
        readiness = readiness_lookup.get(bot_id, {})
        state = str(readiness.get("state") or "").strip().lower()
        if not state:
            state = "disabled" if not enabled else ("ready" if bool(readiness.get("ready")) else "blocked")
        if not enabled:
            state = "disabled"
        state_counts[state] += 1

        tools = _required_tools(bot)
        for tool in tools:
            tool_counts[tool] += 1
        connection_actions = _policy_actions(bot, "connection_action_allowlist")
        owner_approval_actions = _policy_actions(bot, "connection_action_owner_approval_required")
        browser_actions = _policy_actions(bot, "browser_action_allowlist")
        browser_owner_approval_actions = _policy_actions(bot, "browser_action_owner_approval_required")
        for action in connection_actions:
            connection_action_counts[action] += 1
        for action in browser_actions:
            browser_action_counts[action] += 1
        connection_backend_count = _connection_backend_count(bot)
        connection_context = _connection_context_label(bot)
        worker_ids = _worker_ids(bot)
        messages = _failed_messages(readiness)
        category = _blocking_category(messages, tools, worker_ids) if state == "blocked" else ""
        if category:
            blocker_counts[category] += 1

        worker_statuses = []
        for worker_id in worker_ids:
            worker = workers_lookup.get(worker_id, {})
            worker_statuses.append(
                {
                    "worker_id": worker_id,
                    "status": str(worker.get("status") or "missing").strip().lower(),
                    "enabled": bool(worker.get("enabled", False)) if worker else False,
                    "probe_status": _probe_status(worker_id, probe_lookup),
                }
            )

        row = {
            "bot_id": bot_id,
            "name": str(bot.get("name") or bot_id).strip(),
            "role": str(bot.get("role") or "").strip(),
            "project_id": str(bot.get("project_id") or "").strip(),
            "enabled": enabled,
            "state": state,
            "required_tools": tools,
            "connection_actions": connection_actions,
            "owner_approval_actions": owner_approval_actions,
            "browser_actions": browser_actions,
            "browser_owner_approval_actions": browser_owner_approval_actions,
            "connection_backend_count": connection_backend_count,
            "connection_context": connection_context,
            "worker_ids": worker_ids,
            "workers": worker_statuses,
            "blocking_category": category,
            "blocking_messages": messages[:4],
        }
        rows.append(row)
        if state == "blocked":
            blocked_groups[category or "unknown"].append(row)

    rows.sort(
        key=lambda row: (
            0 if row["state"] == "blocked" else 1 if row["state"] == "disabled" else 2,
            row["blocking_category"],
            row["bot_id"],
        )
    )

    return {
        "summary": {
            "total": len(bot_rows),
            "ready": int(state_counts.get("ready", 0)),
            "blocked": int(state_counts.get("blocked", 0)),
            "disabled": int(state_counts.get("disabled", 0)),
            "required_tool_count": int(sum(tool_counts.values())),
            "tooling_bot_count": sum(1 for row in rows if row["required_tools"]),
            "connection_action_bot_count": sum(1 for row in rows if row["connection_actions"]),
            "owner_approval_action_count": sum(len(row["owner_approval_actions"]) for row in rows),
            "browser_action_bot_count": sum(1 for row in rows if row["browser_actions"]),
            "browser_owner_approval_action_count": sum(len(row["browser_owner_approval_actions"]) for row in rows),
            "http_connection_backend_count": sum(row["connection_backend_count"] for row in rows),
        },
        "state_counts": dict(state_counts),
        "required_tools": [
            {"tool": tool, "bot_count": int(count)}
            for tool, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "connection_actions": [
            {"action": action, "bot_count": int(count)}
            for action, count in sorted(connection_action_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "browser_actions": [
            {"action": action, "bot_count": int(count)}
            for action, count in sorted(browser_action_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "blocked_groups": [
            {
                "category": category,
                "label": _blocking_category_view(category)["label"],
                "detail": _blocking_category_view(category)["detail"],
                "count": len(group),
                "bots": group[:8],
            }
            for category, group in sorted(blocked_groups.items(), key=lambda item: (-len(item[1]), item[0]))
        ],
        "blocker_counts": dict(blocker_counts),
        "rows": rows,
    }
