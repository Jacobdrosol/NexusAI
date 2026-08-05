from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from shared.models import Bot, BotExecutionPolicy


_AUTONOMOUSLY_RESTRICTED_BACKEND_TYPES = frozenset({"browser", "cli", "documentation"})
_SUPERVISION_ACTION_TYPES = frozenset({"pause_schedule", "hold_bot", "configuration_review"})
_REQUIRED_TOOL_BY_ACTION_POLICY = {
    "browser_action_allowlist": "browser-ui",
    "documentation_action_allowlist": "documentation-v1",
}


def bot_execution_policy(bot: Bot) -> BotExecutionPolicy:
    policy = getattr(bot, "execution_policy", None)
    if policy is not None:
        return policy
    return BotExecutionPolicy()


def bot_is_project_manager(bot: Bot) -> bool:
    capabilities = getattr(bot, "assignment_capabilities", None)
    return bool(capabilities and capabilities.is_project_manager)


def bot_is_pipeline_entry(bot: Bot) -> bool:
    capabilities = getattr(bot, "assignment_capabilities", None)
    if capabilities is not None and (
        bool(getattr(capabilities, "is_pipeline_entry", False))
        or bool(getattr(capabilities, "pipeline", False))
        or bool(getattr(capabilities, "is_project_manager", False))
    ):
        return True
    routing = getattr(bot, "routing_rules", None)
    if not isinstance(routing, dict):
        return False
    launch_profile = routing.get("launch_profile")
    if not isinstance(launch_profile, dict):
        return False
    return bool(launch_profile.get("is_pipeline"))


def bot_allows_repo_output(bot: Bot) -> bool:
    return bot_execution_policy(bot).repo_output_mode == "allow"


def bot_allows_run_result_ingest(bot: Bot) -> bool:
    return bool(bot_execution_policy(bot).allow_run_result_ingest)


def bot_can_apply_db_actions(bot: Bot) -> bool:
    return bool(bot_execution_policy(bot).can_apply_db_actions)


def supervision_manager_config(bot: Bot) -> Optional[Dict[str, Any]]:
    """Return a normalized, bounded manager portfolio or ``None`` for normal bots.

    The configuration lives in routing_rules so public deployments can define their
    own portfolios without making the shared bot schema host-specific.  The returned
    value deliberately contains only fields used by the control plane.
    """
    routing = getattr(bot, "routing_rules", None)
    raw = routing.get("supervision_manager") if isinstance(routing, dict) else None
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None

    portfolio = raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {}
    action_policy = raw.get("action_policy") if isinstance(raw.get("action_policy"), dict) else {}

    def _ids(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        result: List[str] = []
        seen: Set[str] = set()
        for item in value:
            normalized = str(item or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    raw_actions = action_policy.get("allow_actions")
    allowed_actions = _ids(raw_actions) if raw_actions is not None else list(_SUPERVISION_ACTION_TYPES)
    return {
        "project_id": str(portfolio.get("project_id") or bot.project_id or "").strip() or None,
        "bot_ids": _ids(portfolio.get("bot_ids")),
        "schedule_ids": _ids(portfolio.get("schedule_ids")),
        "allowed_actions": [
            action for action in allowed_actions if action in _SUPERVISION_ACTION_TYPES
        ],
    }


def bot_autonomous_dispatch_blockers(
    bot: Bot,
    *,
    allowed_restricted_backend_types: Iterable[str] = (),
) -> List[str]:
    """Return the policy reasons a bot cannot run without an operator present."""

    allowed_backend_types = {
        str(backend_type or "").strip().lower()
        for backend_type in allowed_restricted_backend_types
        if str(backend_type or "").strip()
    }
    blockers: List[str] = []
    if bot_allows_repo_output(bot):
        blockers.append("the bot permits repository writes")
    if bot_can_apply_db_actions(bot):
        blockers.append("the bot can apply database actions")
    if bot_is_pipeline_entry(bot):
        blockers.append("the bot can dispatch a pipeline")

    restricted_backends = sorted(
        {
            str(backend.type or "").strip().lower()
            for backend in bot.backends
            if str(backend.type or "").strip().lower()
            in _AUTONOMOUSLY_RESTRICTED_BACKEND_TYPES
            and str(backend.type or "").strip().lower() not in allowed_backend_types
        }
    )
    if restricted_backends:
        blockers.append(
            "the bot uses restricted backend types: " + ", ".join(restricted_backends)
        )
    return blockers


def bot_workflow_graph_id(bot: Bot) -> str:
    workflow = getattr(bot, "workflow", None)
    reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
    if reference_graph is not None and str(reference_graph.graph_id or "").strip():
        return str(reference_graph.graph_id).strip()
    return str(bot.id)


def bot_has_explicit_workflow(bot: Bot) -> bool:
    workflow = getattr(bot, "workflow", None)
    triggers = getattr(workflow, "triggers", None) if workflow is not None else None
    return bool(triggers)


def validate_reference_graph(bot: Bot) -> List[str]:
    workflow = getattr(bot, "workflow", None)
    reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
    if reference_graph is None:
        return []

    errors: List[str] = []
    current_bot_id = str(reference_graph.current_bot_id or "").strip()
    entry_bot_id = str(reference_graph.entry_bot_id or "").strip()
    graph_id = str(reference_graph.graph_id or "").strip()
    node_ids = {str(node.bot_id or "").strip() for node in reference_graph.nodes if str(node.bot_id or "").strip()}
    graph_edges: Set[Tuple[str, str]] = {
        (str(edge.source_bot_id or "").strip(), str(edge.target_bot_id or "").strip())
        for edge in reference_graph.edges
        if str(edge.source_bot_id or "").strip() and str(edge.target_bot_id or "").strip()
    }
    trigger_edges: Set[Tuple[str, str]] = set()
    for trigger in getattr(workflow, "triggers", None) or []:
        source = str(bot.id or "").strip()
        target = str(trigger.target_bot_id or "").strip()
        if source and target:
            trigger_edges.add((source, target))

    if not graph_id:
        errors.append(f"Bot '{bot.id}' workflow.reference_graph.graph_id is required.")
    if current_bot_id != str(bot.id):
        errors.append(
            f"Bot '{bot.id}' workflow.reference_graph.current_bot_id must equal the bot id."
        )
    if not entry_bot_id:
        errors.append(f"Bot '{bot.id}' workflow.reference_graph.entry_bot_id is required.")
    if current_bot_id and current_bot_id not in node_ids:
        errors.append(f"Bot '{bot.id}' reference graph must include a node for the current bot.")
    if entry_bot_id and entry_bot_id not in node_ids:
        errors.append(f"Bot '{bot.id}' reference graph must include a node for the entry bot.")
    for _, target in trigger_edges:
        if target not in node_ids:
            errors.append(
                f"Bot '{bot.id}' reference graph is missing node '{target}' used by a workflow trigger."
            )
    missing_edges = sorted(trigger_edges - graph_edges)
    for source, target in missing_edges:
        errors.append(
            f"Bot '{bot.id}' reference graph is missing edge '{source} -> {target}' required by workflow triggers."
        )
    return errors


def validate_bot_configuration(bot: Bot) -> List[str]:
    errors = validate_reference_graph(bot)
    execution_policy = bot_execution_policy(bot)
    required_tools = {
        str(tool or "").strip()
        for tool in execution_policy.required_worker_tools
        if str(tool or "").strip()
    }
    approval_required = {
        str(action or "").strip()
        for action in execution_policy.browser_action_owner_approval_required
        if str(action or "").strip()
    }
    authorized_browser_actions = {
        str(action or "").strip()
        for action in execution_policy.browser_action_allowlist
        if str(action or "").strip()
    }
    unrecognized_approval_actions = sorted(approval_required - authorized_browser_actions)
    if unrecognized_approval_actions:
        errors.append(
            f"Bot '{bot.id}' requires owner approval for browser actions not present in its allowlist: "
            + ", ".join(unrecognized_approval_actions)
        )
    if authorized_browser_actions and _REQUIRED_TOOL_BY_ACTION_POLICY["browser_action_allowlist"] not in required_tools:
        errors.append(
            f"Bot '{bot.id}' authorizes browser actions but does not require worker tool "
            f"'{_REQUIRED_TOOL_BY_ACTION_POLICY['browser_action_allowlist']}'."
        )
    connection_approval_required = {
        str(action or "").strip()
        for action in execution_policy.connection_action_owner_approval_required
        if str(action or "").strip()
    }
    authorized_connection_actions = {
        str(action or "").strip()
        for action in execution_policy.connection_action_allowlist
        if str(action or "").strip()
    }
    unrecognized_connection_approval_actions = sorted(
        connection_approval_required - authorized_connection_actions
    )
    if unrecognized_connection_approval_actions:
        errors.append(
            f"Bot '{bot.id}' requires owner approval for connection actions not present in its allowlist: "
            + ", ".join(unrecognized_connection_approval_actions)
        )
    authorized_documentation_actions = {
        str(action or "").strip()
        for action in execution_policy.documentation_action_allowlist
        if str(action or "").strip()
    }
    unsupported_documentation_actions = sorted(
        authorized_documentation_actions - {"documentation.create", "documentation.save"}
    )
    if unsupported_documentation_actions:
        errors.append(
            f"Bot '{bot.id}' has unsupported documentation actions: "
            + ", ".join(unsupported_documentation_actions)
        )
    if (
        authorized_documentation_actions
        and _REQUIRED_TOOL_BY_ACTION_POLICY["documentation_action_allowlist"] not in required_tools
    ):
        errors.append(
            f"Bot '{bot.id}' authorizes documentation actions but does not require worker tool "
            f"'{_REQUIRED_TOOL_BY_ACTION_POLICY['documentation_action_allowlist']}'."
        )
    if bot_is_project_manager(bot) and not bot_has_explicit_workflow(bot):
        errors.append(f"Bot '{bot.id}' is marked as a project manager but has no explicit workflow triggers.")
    routing = getattr(bot, "routing_rules", None)
    external_trigger = routing.get("external_trigger") if isinstance(routing, dict) else None
    if isinstance(external_trigger, dict) and bool(external_trigger.get("enabled", False)):
        if external_trigger.get("require_auth") is not True:
            errors.append(
                f"Bot '{bot.id}' external_trigger.require_auth must be true when external triggering is enabled."
            )
        if str(external_trigger.get("auth_token") or "").strip():
            errors.append(
                f"Bot '{bot.id}' external_trigger.auth_token is not permitted; use auth_token_ref for an encrypted vault key."
            )
        if not str(external_trigger.get("auth_token_ref") or "").strip():
            errors.append(
                f"Bot '{bot.id}' external_trigger.auth_token_ref is required when external triggering is enabled."
            )
    specialist = routing.get("specialist") if isinstance(routing, dict) else None
    if isinstance(specialist, dict):
        specialist_kind = str(specialist.get("kind") or "").strip()
        specialist_risk = str(specialist.get("risk_level") or "").strip()
        context_access = getattr(bot, "context_access", None)
        if isinstance(context_access, dict):
            self_serve = context_access.get("can_self_serve") or []
        else:
            self_serve = getattr(context_access, "can_self_serve", None) or []
        self_serve_sources = {
            str(source or "").strip().lower()
            for source in self_serve
            if str(source or "").strip()
        }

        if execution_policy.workspace_context_injection and "repo" not in self_serve_sources:
            errors.append(
                f"Specialist bot '{bot.id}' enables workspace context but does not declare repo self-service access."
            )
        if execution_policy.repo_output_mode == "allow":
            if specialist_kind != "code_implementer" or specialist_risk != "guarded_write":
                errors.append(
                    f"Specialist bot '{bot.id}' may grant repository writes only to a guarded_write code_implementer."
                )
            if not execution_policy.workspace_context_injection or "repo" not in self_serve_sources:
                errors.append(
                    f"Writable specialist bot '{bot.id}' requires injected workspace context and repo self-service access."
                )
            if specialist.get("repo_write_granted") is not True or specialist.get("operator_review_required") is not True:
                errors.append(
                    f"Writable specialist bot '{bot.id}' requires an explicit repository-write grant and operator review marker."
                )

    manager = routing.get("supervision_manager") if isinstance(routing, dict) else None
    if manager is not None:
        if not isinstance(manager, dict):
            errors.append(f"Bot '{bot.id}' supervision_manager must be an object.")
        elif manager.get("enabled") is not True:
            errors.append(f"Bot '{bot.id}' supervision_manager.enabled must be true when the manager profile is present.")
        else:
            config = supervision_manager_config(bot)
            portfolio = manager.get("portfolio")
            action_policy = manager.get("action_policy")
            profile = routing.get("worker_profile") if isinstance(routing, dict) else None
            profile = profile if isinstance(profile, dict) else {}
            task_scope = str(profile.get("task_scope") or "").strip().lower()
            if not isinstance(portfolio, dict):
                errors.append(f"Manager bot '{bot.id}' requires a supervision_manager.portfolio object.")
            elif not config or not (config["bot_ids"] or config["schedule_ids"]):
                errors.append(
                    f"Manager bot '{bot.id}' requires at least one explicit portfolio bot_ids or schedule_ids entry."
                )
            if not isinstance(action_policy, dict):
                errors.append(f"Manager bot '{bot.id}' requires a supervision_manager.action_policy object.")
            else:
                raw_actions = action_policy.get("allow_actions")
                if not isinstance(raw_actions, list) or not raw_actions:
                    errors.append(
                        f"Manager bot '{bot.id}' requires a non-empty action_policy.allow_actions list."
                    )
                else:
                    unsupported = sorted(
                        {
                            str(action or "").strip()
                            for action in raw_actions
                            if str(action or "").strip() not in _SUPERVISION_ACTION_TYPES
                        }
                    )
                    if unsupported:
                        errors.append(
                            f"Manager bot '{bot.id}' uses unsupported supervision actions: "
                            + ", ".join(unsupported)
                        )
            if bool(profile.get("can_edit")) or not task_scope.startswith("read-only-manager-review"):
                errors.append(
                    f"Manager bot '{bot.id}' requires a non-editing worker_profile with a read-only-manager-review task scope."
                )
            if execution_policy.repo_output_mode != "deny" or execution_policy.can_apply_db_actions:
                errors.append(
                    f"Manager bot '{bot.id}' must deny repository output and database actions."
                )
            output_contract = routing.get("output_contract") if isinstance(routing, dict) else None
            if not isinstance(output_contract, dict) or str(output_contract.get("format") or "").strip().lower() != "json_object":
                errors.append(
                    f"Manager bot '{bot.id}' requires an output_contract with format json_object."
                )
            else:
                required_fields = {
                    str(field or "").strip()
                    for field in output_contract.get("required_fields") or []
                    if str(field or "").strip()
                }
                missing_fields = sorted(
                    {"executive_summary", "overall_status", "portfolio", "action_proposals"}
                    - required_fields
                )
                if missing_fields:
                    errors.append(
                        f"Manager bot '{bot.id}' output_contract.required_fields is missing: "
                        + ", ".join(missing_fields)
                    )
    return errors


def derive_allowed_bot_ids(root_bot_id: str, bots: Sequence[Bot]) -> List[str]:
    bot_map: Dict[str, Bot] = {
        str(bot.id).strip(): bot
        for bot in bots
        if str(bot.id).strip()
    }
    allowed: List[str] = []
    seen: Set[str] = set()
    queue: List[str] = [str(root_bot_id or "").strip()]
    while queue:
        current_id = queue.pop(0)
        if not current_id or current_id in seen:
            continue
        seen.add(current_id)
        allowed.append(current_id)
        bot = bot_map.get(current_id)
        workflow = getattr(bot, "workflow", None) if bot is not None else None
        for trigger in getattr(workflow, "triggers", None) or []:
            target = str(trigger.target_bot_id or "").strip()
            if target and target not in seen:
                queue.append(target)
    return allowed


def bot_map_by_id(bots: Iterable[Bot]) -> Dict[str, Bot]:
    return {str(bot.id).strip(): bot for bot in bots if str(bot.id).strip()}
