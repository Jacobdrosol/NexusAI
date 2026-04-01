from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from shared.models import Bot, BotExecutionPolicy


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
    if capabilities is not None and bool(getattr(capabilities, "is_pipeline_entry", False)):
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
    if bot_is_project_manager(bot) and not bot_has_explicit_workflow(bot):
        errors.append(f"Bot '{bot.id}' is marked as a project manager but has no explicit workflow triggers.")
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
