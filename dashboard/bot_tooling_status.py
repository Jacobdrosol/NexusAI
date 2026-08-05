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


def _safe_reference_label(value: Any) -> str:
    label = str(value or "").strip()
    if not label:
        return ""
    lowered = label.lower()
    suspicious_prefixes = ("sk-", "xoxb-", "xoxp-", "ghp_", "github_pat_", "ya29.", "eyj")
    if any(lowered.startswith(prefix) for prefix in suspicious_prefixes):
        return "[redacted raw credential]"
    if len(label) > 96:
        return label[:93] + "..."
    return label


def _backend_credential_refs(bot: dict[str, Any]) -> list[str]:
    refs = []
    for backend in _as_list(bot.get("backends")):
        if not isinstance(backend, dict):
            continue
        for key in ("api_key_ref", "credential_ref", "auth_token_ref"):
            ref = _safe_reference_label(backend.get(key))
            if ref and ref not in refs:
                refs.append(ref)
    return refs


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


def _disabled_activation_messages(readiness: dict[str, Any]) -> list[str]:
    messages = []
    for check in _as_list(readiness.get("checks")):
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").strip().lower()
        if status not in {"failed", "blocking"}:
            continue
        component = str(check.get("component") or "").strip().lower()
        message = str(check.get("message") or component or "blocking readiness check").strip()
        if not message:
            continue
        if component == "bot" or message.lower() == "bot is disabled.":
            continue
        messages.append(message)
    return messages


def _blocking_category(messages: list[str], required_tools: list[str], worker_ids: list[str]) -> str:
    joined = " ".join(messages).lower()
    if "project policy" in joined or "project chat tool access" in joined or "project does not allow" in joined:
        return "project_policy"
    if "bot policy" in joined or "bot does not allow" in joined or "chat_tool_access" in joined or "tool access disabled" in joined:
        return "bot_policy"
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
        "project_policy": "Project tool policy",
        "bot_policy": "Bot tool policy",
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
        "project_policy": "The scoped project does not currently allow the requested chat or worker tooling.",
        "bot_policy": "The bot configuration does not currently allow the requested chat or worker tooling.",
        "worker_runtime": "The configured worker is missing, disabled, offline, or not exposing the required capability.",
        "model": "The configured model or provider backend is not currently usable.",
        "readiness": "One or more readiness checks are failing.",
        "unknown": "The control plane did not provide a specific blocker category.",
    }
    normalized = category if category in labels else "unknown"
    return {"label": labels[normalized], "detail": details[normalized]}


def _blocking_category_recommended_action(category: str) -> dict[str, str]:
    actions = {
        "browser_session": {
            "label": "restore browser session",
            "level": "critical",
            "detail": "Open the worker browser profile, confirm the site login, then rerun the worker probe.",
        },
        "cli_auth": {
            "label": "refresh CLI login",
            "level": "critical",
            "detail": "Authenticate the configured CLI on the worker host, then rerun readiness.",
        },
        "credential": {
            "label": "configure vault key",
            "level": "critical",
            "detail": "Create or repair the named vault credential reference without storing raw secret material in bot config.",
        },
        "project_policy": {
            "label": "review project policy",
            "level": "warning",
            "detail": "Enable the required project tool policy only if this project should allow that bot capability.",
        },
        "bot_policy": {
            "label": "review bot policy",
            "level": "warning",
            "detail": "Update the bot execution policy or use a bot already scoped for the requested tooling.",
        },
        "worker_runtime": {
            "label": "restore worker runtime",
            "level": "critical",
            "detail": "Start or repair the assigned worker, verify required tools are installed, then rerun the probe.",
        },
        "model": {
            "label": "fix model route",
            "level": "critical",
            "detail": "Choose an enabled model/backend route or repair the provider configuration.",
        },
        "readiness": {
            "label": "inspect readiness",
            "level": "warning",
            "detail": "Open the bot readiness checks and resolve the reported blocker before assigning work.",
        },
        "unknown": {
            "label": "inspect bot",
            "level": "warning",
            "detail": "Open the bot detail and readiness checks because no specific blocker category was reported.",
        },
    }
    return actions.get(category, actions["unknown"])


def _summary_recommended_action(blocker_counts: Counter[str], summary: dict[str, int]) -> dict[str, str]:
    priority = [
        "credential",
        "worker_runtime",
        "browser_session",
        "cli_auth",
        "model",
        "project_policy",
        "bot_policy",
        "readiness",
        "unknown",
    ]
    for category in priority:
        if int(blocker_counts.get(category, 0)):
            action = dict(_blocking_category_recommended_action(category))
            action["detail"] = f"{blocker_counts[category]} bot(s) need this action. {action['detail']}"
            return action
    if int(summary.get("degraded_worker_probe_count", 0)):
        return {
            "label": "rerun degraded probes",
            "level": "warning",
            "detail": f"{summary['degraded_worker_probe_count']} assigned worker probe(s) are degraded.",
        }
    return {
        "label": "continue",
        "level": "ready",
        "detail": "No blocking bot tooling readiness issue is currently reported.",
    }


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
        credential_refs = _backend_credential_refs(bot)
        connection_context = _connection_context_label(bot)
        worker_ids = _worker_ids(bot)
        messages = _failed_messages(readiness)
        disabled_activation_messages = _disabled_activation_messages(readiness) if state == "disabled" else []
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
        row_action = _blocking_category_recommended_action(category) if category else {
            "label": "continue",
            "level": "ready",
            "detail": "No blocking tooling readiness issue is currently reported for this bot.",
        }
        row_category_view = _blocking_category_view(category) if category else {"label": "None", "detail": ""}

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
            "credential_refs": credential_refs,
            "connection_context": connection_context,
            "worker_ids": worker_ids,
            "workers": worker_statuses,
            "blocking_category": category,
            "blocking_category_view": row_category_view,
            "recommended_action": row_action,
            "blocking_messages": messages[:4],
            "disabled_activation_messages": disabled_activation_messages[:4],
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
    worker_statuses = [
        worker
        for row in rows
        if row["enabled"]
        for worker in row["workers"]
        if isinstance(worker, dict)
    ]
    missing_worker_count = sum(1 for worker in worker_statuses if worker.get("status") == "missing")
    offline_worker_count = sum(
        1
        for worker in worker_statuses
        if worker.get("status") != "missing"
        and (
            worker.get("status") not in {"online", "ready", "healthy"}
            or not bool(worker.get("enabled", False))
        )
    )
    degraded_probe_count = sum(1 for worker in worker_statuses if worker.get("probe_status") not in {"ready", "healthy", "unknown"})

    summary = {
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
            "credential_ref_bot_count": sum(1 for row in rows if row["credential_refs"]),
            "backend_credential_ref_count": sum(len(row["credential_refs"]) for row in rows),
            "disabled_activation_blocker_bot_count": sum(1 for row in rows if row["disabled_activation_messages"]),
            "disabled_activation_blocker_count": sum(len(row["disabled_activation_messages"]) for row in rows),
            "worker_assignment_count": len(worker_statuses),
            "missing_worker_assignment_count": missing_worker_count,
            "offline_worker_assignment_count": offline_worker_count,
            "degraded_worker_probe_count": degraded_probe_count,
        }
    return {
        "summary": {
            **summary,
            "recommended_action": _summary_recommended_action(blocker_counts, summary),
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
                "recommended_action": _blocking_category_recommended_action(category),
                "count": len(group),
                "bots": group[:8],
            }
            for category, group in sorted(blocked_groups.items(), key=lambda item: (-len(item[1]), item[0]))
        ],
        "blocker_counts": dict(blocker_counts),
        "rows": rows,
    }
