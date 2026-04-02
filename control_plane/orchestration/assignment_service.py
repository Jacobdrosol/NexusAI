from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Optional, Set

from control_plane.connections.resolver import ConnectionResolver
from control_plane.orchestration.run_store import OrchestrationRunStore
from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import Bot, Task


def _graph_completeness_report(*, graph: Dict[str, Any], tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Evaluate pipeline completeness. Returns None if the evaluator is unavailable."""
    try:
        from control_plane.orchestration.graph_completeness import GraphCompletenessEvaluator
        return GraphCompletenessEvaluator.for_pm_software_delivery().evaluate(
            graph=graph, tasks=tasks
        ).to_dict()
    except Exception:
        return None


_DEFAULT_STAGE_ORDER = [
    "pm-orchestrator",
    "pm-research-analyst",
    "pm-engineer",
    "pm-coder",
    "pm-tester",
    "pm-security-reviewer",
    "pm-database-engineer",
    "pm-ui-tester",
    "pm-final-qc",
]


def _normalize_node_override(raw: Any) -> Dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    bindings: List[Dict[str, Any]] = []
    raw_bindings = payload.get("connection_bindings")
    if isinstance(raw_bindings, list):
        for item in raw_bindings:
            if not isinstance(item, dict):
                continue
            slot = str(item.get("slot") or "").strip()
            project_connection_id = str(item.get("project_connection_id") or "").strip()
            if not slot or not project_connection_id:
                continue
            bindings.append({"slot": slot, "project_connection_id": project_connection_id})
    return {
        "skip": bool(payload.get("skip", False)),
        "instructions": str(payload.get("instructions") or "").strip(),
        "connection_bindings": bindings,
        "execution_mode": str(payload.get("execution_mode") or "").strip(),
        "policy_overrides": payload.get("policy_overrides") if isinstance(payload.get("policy_overrides"), dict) else {},
    }


def _normalize_overrides(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        node_id = str(key or "").strip()
        if not node_id:
            continue
        result[node_id] = _normalize_node_override(value)
    return result


def _workflow_reference_nodes(pm_bot: Bot) -> List[Dict[str, Any]]:
    workflow = getattr(pm_bot, "workflow", None)
    reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
    nodes = getattr(reference_graph, "nodes", None) if reference_graph is not None else None
    result: List[Dict[str, Any]] = []
    for item in nodes or []:
        bot_id = str(getattr(item, "bot_id", "") or "").strip()
        if not bot_id:
            continue
        result.append(
            {
                "id": bot_id,
                "bot_id": bot_id,
                "title": str(getattr(item, "title", "") or bot_id),
                "stage_kind": str(getattr(item, "stage_kind", "") or ""),
                "status": "queued",
                "kind": "stage",
                "depends_on": [],
            }
        )
    if result:
        return result
    return [
        {
            "id": stage_id,
            "bot_id": stage_id,
            "title": stage_id,
            "stage_kind": "stage",
            "status": "queued",
            "kind": "stage",
            "depends_on": [],
        }
        for stage_id in _DEFAULT_STAGE_ORDER
    ]


def _workflow_reference_edges(pm_bot: Bot, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    workflow = getattr(pm_bot, "workflow", None)
    reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
    edges = getattr(reference_graph, "edges", None) if reference_graph is not None else None
    result: List[Dict[str, Any]] = []
    for item in edges or []:
        source = str(getattr(item, "source_bot_id", "") or "").strip()
        target = str(getattr(item, "target_bot_id", "") or "").strip()
        if not source or not target:
            continue
        result.append(
            {
                "source": source,
                "target": target,
                "kind": str(getattr(item, "route_kind", "") or "forward"),
                "trigger_id": str(getattr(item, "trigger_id", "") or ""),
                "title": str(getattr(item, "title", "") or ""),
            }
        )
    if result:
        return result
    synthetic: List[Dict[str, Any]] = []
    previous = None
    for node in nodes:
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        if previous:
            synthetic.append({"source": previous, "target": node_id, "kind": "forward"})
        previous = node_id
    return synthetic


class AssignmentService:
    def __init__(
        self,
        *,
        chat_manager: Any,
        bot_registry: Any,
        task_manager: Any,
        pm_orchestrator: Any,
        run_store: OrchestrationRunStore,
        connection_resolver: ConnectionResolver,
    ) -> None:
        self._chat_manager = chat_manager
        self._bot_registry = bot_registry
        self._task_manager = task_manager
        self._pm_orchestrator = pm_orchestrator
        self._run_store = run_store
        self._connection_resolver = connection_resolver

    async def preview(
        self,
        *,
        conversation_id: str,
        pm_bot_id: str,
        instruction: str,
        node_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        conversation = await self._chat_manager.get_conversation(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"Conversation not found: {conversation_id}")
        bot = await self._bot_registry.get(pm_bot_id)
        if bot is None:
            raise BotNotFoundError(f"Bot not found: {pm_bot_id}")
        nodes = _workflow_reference_nodes(bot)
        edges = _workflow_reference_edges(bot, nodes)
        normalized_overrides = _normalize_overrides(node_overrides)
        await self._validate_project_bindings(
            project_id=str(conversation.project_id or "").strip(),
            node_overrides=normalized_overrides,
        )
        run = await self._run_store.create_run(
            conversation_id=conversation_id,
            project_id=conversation.project_id,
            pm_bot_id=pm_bot_id,
            instruction=instruction,
            graph_snapshot={"nodes": nodes, "edges": edges},
            node_overrides=normalized_overrides,
            metadata={"mode": "preview"},
        )
        return {
            "run_id": run["id"],
            "assignment_id": run["assignment_id"],
            "conversation_id": conversation_id,
            "project_id": conversation.project_id,
            "pm_bot_id": pm_bot_id,
            "instruction": instruction,
            "graph": run["graph_snapshot"],
            "node_overrides": run["node_overrides"],
            "project_connections": self._connection_resolver.list_project_connections(str(conversation.project_id or "").strip()),
        }

    async def create_assignment(
        self,
        *,
        conversation_id: str,
        instruction: str,
        pm_bot_id: str,
        run_id: Optional[str] = None,
        node_overrides: Optional[Dict[str, Any]] = None,
        context_items: Optional[List[str]] = None,
        conversation_brief: str = "",
        conversation_transcript: str = "",
        conversation_message_count: int = 0,
        conversation_transcript_strategy: str = "",
        assignment_memory_hits: Optional[List[Dict[str, Any]]] = None,
        assignment_memory_hit_count: int = 0,
    ) -> Dict[str, Any]:
        conversation = await self._chat_manager.get_conversation(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"Conversation not found: {conversation_id}")
        normalized_overrides = _normalize_overrides(node_overrides)
        await self._validate_project_bindings(
            project_id=str(conversation.project_id or "").strip(),
            node_overrides=normalized_overrides,
        )
        run_payload = await self._resolve_or_create_run(
            conversation_id=conversation_id,
            pm_bot_id=pm_bot_id,
            instruction=instruction,
            run_id=run_id,
            node_overrides=normalized_overrides,
            project_id=conversation.project_id,
        )
        assignment = await self._pm_orchestrator.orchestrate_assignment(
            conversation_id=conversation_id,
            instruction=instruction,
            requested_pm_bot_id=pm_bot_id,
            context_items=context_items or [],
            conversation_brief=conversation_brief,
            conversation_transcript=conversation_transcript,
            conversation_message_count=conversation_message_count,
            conversation_transcript_strategy=conversation_transcript_strategy,
            assignment_memory_hits=assignment_memory_hits,
            assignment_memory_hit_count=assignment_memory_hit_count,
            project_id=conversation.project_id,
            node_overrides=normalized_overrides,
            orchestration_run_id=str(run_payload.get("id") or ""),
            assignment_id=str(run_payload.get("assignment_id") or ""),
        )
        await self._run_store.bind_orchestration_id(str(run_payload.get("id") or ""), str(assignment.get("orchestration_id") or ""))
        response = dict(assignment or {})
        response["run_id"] = str(run_payload.get("id") or "")
        response["assignment_id"] = str(response.get("assignment_id") or run_payload.get("assignment_id") or "")
        response["orchestration_run_id"] = str(response.get("orchestration_run_id") or run_payload.get("id") or "")
        response["node_overrides"] = normalized_overrides
        return response

    async def get_graph(
        self,
        *,
        run_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        run = None
        if run_id:
            run = await self._run_store.get_run(run_id)
        elif orchestration_id:
            run = await self._run_store.get_run_by_orchestration(orchestration_id)
        if run is None:
            raise ValueError("run not found")
        tasks: List[Task] = []
        if run.get("orchestration_id"):
            tasks = await self._task_manager.list_tasks(orchestration_id=str(run.get("orchestration_id") or ""), limit=1000)
            await self._run_store.sync_graph_from_tasks(str(run.get("id") or ""), tasks)
            run = await self._run_store.get_run(str(run.get("id") or "")) or run
        return {
            "run_id": run.get("id"),
            "assignment_id": run.get("assignment_id"),
            "orchestration_id": run.get("orchestration_id"),
            "state": run.get("state"),
            "archived": bool(run.get("archived")),
            "graph": run.get("graph_snapshot") if isinstance(run.get("graph_snapshot"), dict) else {"nodes": [], "edges": []},
            "node_overrides": run.get("node_overrides") if isinstance(run.get("node_overrides"), dict) else {},
            "tasks": [task.model_dump() for task in tasks],
            "completeness_report": _graph_completeness_report(
                graph=run.get("graph_snapshot") if isinstance(run.get("graph_snapshot"), dict) else {"nodes": [], "edges": []},
                tasks=[task.model_dump() for task in tasks],
            ),
        }

    async def splice_and_rerun(
        self,
        *,
        run_id: str,
        from_node_id: str,
        override_patch: Optional[Dict[str, Any]] = None,
        context_items: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        parent = await self._run_store.get_run(run_id)
        if parent is None:
            raise ValueError("run not found")
        parent_overrides = parent.get("node_overrides") if isinstance(parent.get("node_overrides"), dict) else {}
        merged = copy.deepcopy(parent_overrides)
        for key, value in _normalize_overrides(override_patch).items():
            merged[str(key)] = value

        # Preserve upstream results by defaulting upstream stages to deterministic skip.
        graph_snapshot = (
            parent.get("graph_snapshot")
            if isinstance(parent.get("graph_snapshot"), dict)
            else {"nodes": [], "edges": []}
        )
        upstream = self._upstream_nodes(
            graph=graph_snapshot,
            from_node_id=from_node_id,
        )
        node_lookup = self._graph_node_lookup(graph_snapshot)
        for node_id in upstream:
            node_doc = node_lookup.get(node_id) if isinstance(node_lookup, dict) else None
            override_keys = self._node_override_keys(node_doc, fallback_id=node_id)
            for key in override_keys:
                current = merged.get(key) if isinstance(merged.get(key), dict) else {}
                current["skip"] = True
                current.setdefault("execution_mode", "preserve_upstream")
                merged[key] = current

        child = await self._run_store.create_splice_child(
            run_id=run_id,
            spliced_from_node_id=from_node_id,
            node_overrides=merged,
            metadata={"action": "splice"},
        )
        assignment = await self.create_assignment(
            conversation_id=str(child.get("conversation_id") or ""),
            instruction=str(child.get("instruction") or ""),
            pm_bot_id=str(child.get("pm_bot_id") or ""),
            run_id=str(child.get("id") or ""),
            node_overrides=merged,
            context_items=context_items,
        )
        return {"child_run": child, "assignment": assignment}

    async def rerun_node(
        self,
        *,
        orchestration_id: str,
        node_id: str,
        payload_override: Optional[Any] = None,
    ) -> Dict[str, Any]:
        tasks = await self._task_manager.list_tasks(orchestration_id=orchestration_id, limit=1000)
        target_task = self._find_node_task(tasks, node_id)
        if target_task is None:
            raise ValueError(f"node task not found: {node_id}")
        rerun = await self._task_manager.retry_task(target_task.id, payload_override=payload_override)
        return {"orchestration_id": orchestration_id, "node_id": node_id, "task": rerun.model_dump()}

    async def list_lineage(self, run_id: str) -> Dict[str, Any]:
        lineage = await self._run_store.list_lineage(run_id)
        return {"run_id": run_id, "lineage": lineage}

    async def _resolve_or_create_run(
        self,
        *,
        conversation_id: str,
        pm_bot_id: str,
        instruction: str,
        run_id: Optional[str],
        node_overrides: Dict[str, Any],
        project_id: Optional[str],
    ) -> Dict[str, Any]:
        if run_id:
            existing = await self._run_store.get_run(run_id)
            if existing is not None:
                return existing
        bot = await self._bot_registry.get(pm_bot_id)
        nodes = _workflow_reference_nodes(bot)
        edges = _workflow_reference_edges(bot, nodes)
        return await self._run_store.create_run(
            conversation_id=conversation_id,
            project_id=project_id,
            pm_bot_id=pm_bot_id,
            instruction=instruction,
            graph_snapshot={"nodes": nodes, "edges": edges},
            node_overrides=node_overrides,
            metadata={"mode": "direct_assign"},
        )

    async def _validate_project_bindings(self, *, project_id: str, node_overrides: Dict[str, Dict[str, Any]]) -> None:
        if not project_id:
            for node_id, override in node_overrides.items():
                if override.get("connection_bindings"):
                    raise ValueError(f"node '{node_id}' requested project connection bindings but conversation is not attached to a project")
            return
        for node_id, override in node_overrides.items():
            bindings = override.get("connection_bindings") if isinstance(override, dict) else []
            if not isinstance(bindings, list):
                continue
            for item in bindings:
                if not isinstance(item, dict):
                    continue
                try:
                    connection_id = int(str(item.get("project_connection_id") or "").strip())
                except Exception:
                    raise ValueError(f"node '{node_id}' has an invalid project_connection_id binding")
                connection = self._connection_resolver.get_project_connection(project_id, connection_id)
                if connection is None:
                    raise ValueError(
                        f"node '{node_id}' requested connection '{connection_id}' that is not attached to project '{project_id}'"
                    )
                if not bool(connection.get("enabled")):
                    raise ValueError(
                        f"node '{node_id}' requested connection '{connection_id}' but it is disabled"
                    )

    def _find_node_task(self, tasks: List[Task], node_id: str) -> Optional[Task]:
        target = str(node_id or "").strip()
        if not target:
            return None
        if target.startswith("task:"):
            lookup = target[5:]
            for task in tasks:
                if str(task.id) == lookup:
                    return task
        scored: List[Task] = []
        for task in tasks:
            step_id = str(task.metadata.step_id if task.metadata else "").strip()
            bot_id = str(task.bot_id or "").strip()
            if target in {step_id, bot_id}:
                scored.append(task)
        if not scored:
            return None
        return sorted(scored, key=lambda item: (str(item.updated_at or ""), str(item.id or "")), reverse=True)[0]

    def _upstream_nodes(self, *, graph: Dict[str, Any], from_node_id: str) -> Set[str]:
        nodes = graph.get("nodes") if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else []
        edges = graph.get("edges") if isinstance(graph, dict) and isinstance(graph.get("edges"), list) else []
        valid_nodes = {
            str(item.get("id") or "").strip()
            for item in nodes
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        alias_map: Dict[str, Set[str]] = {}
        for item in nodes:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "").strip()
            if not node_id:
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            aliases = {
                node_id,
                str(item.get("bot_id") or "").strip(),
                str(item.get("stage_key") or "").strip(),
                str(item.get("step_id") or "").strip(),
                str(details.get("step_id") or "").strip(),
                str(details.get("task_id") or "").strip(),
            }
            for alias in aliases:
                normalized = str(alias or "").strip()
                if not normalized:
                    continue
                alias_map.setdefault(normalized, set()).add(node_id)
        target = str(from_node_id or "").strip()
        if not target:
            return set()
        if target in valid_nodes:
            targets = {target}
        else:
            targets = alias_map.get(target) or set()
        if not targets:
            return set()
        reverse_adj: Dict[str, Set[str]] = {}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or "").strip()
            dest = str(edge.get("target") or "").strip()
            if source and dest:
                reverse_adj.setdefault(dest, set()).add(source)
        visited: Set[str] = set()
        stack = list(targets)
        while stack:
            current = stack.pop()
            for parent in reverse_adj.get(current, set()):
                if parent in visited:
                    continue
                visited.add(parent)
                stack.append(parent)
        return visited

    def _graph_node_lookup(self, graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        nodes = graph.get("nodes") if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else []
        lookup: Dict[str, Dict[str, Any]] = {}
        for item in nodes:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "").strip()
            if not node_id:
                continue
            lookup[node_id] = item
        return lookup

    def _node_override_keys(self, node_doc: Optional[Dict[str, Any]], *, fallback_id: str) -> Set[str]:
        keys: Set[str] = set()
        if isinstance(node_doc, dict):
            details = node_doc.get("details") if isinstance(node_doc.get("details"), dict) else {}
            for candidate in (
                node_doc.get("bot_id"),
                node_doc.get("stage_key"),
                node_doc.get("step_id"),
                details.get("step_id"),
                node_doc.get("id"),
            ):
                normalized = str(candidate or "").strip()
                if not normalized or normalized.startswith("task:"):
                    continue
                keys.add(normalized)
        fallback = str(fallback_id or "").strip()
        if fallback and not fallback.startswith("task:"):
            keys.add(fallback)
        return keys
