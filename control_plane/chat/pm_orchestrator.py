import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from shared.bot_policy import (
    bot_has_explicit_workflow,
    bot_is_project_manager,
    bot_workflow_graph_id,
    derive_allowed_bot_ids,
)
from shared.exceptions import BotNotFoundError
from shared.models import Bot, Task, TaskMetadata

logger = logging.getLogger(__name__)


class PMOrchestrator:
    """Creates dependency-ordered tasks from a high-level chat assignment."""

    PM_SYSTEM_PROMPT = (
        "You are the NexusAI Project Manager bot. Break the user's request into a deterministic "
        "implementation workflow with explicit quality controls. Return JSON only with this shape: "
        '{"global_acceptance_criteria":["..."],"global_quality_gates":["..."],"risks":["..."],'
        '"steps":[{"id":"step_1_code","title":"...","instruction":"...","bot_id":"pm-research-analyst","role_hint":"researcher",'
        '"step_kind":"specification","evidence_requirements":["..."],"depends_on":[],"acceptance_criteria":["..."],'
        '"deliverables":["..."],"quality_gates":["..."]}]}'
        "IMPORTANT: Each step MUST include 'bot_id' with the EXACT bot ID. Do NOT omit bot_id. bot_id is REQUIRED. "
        "Available bot IDs: pm-research-analyst, pm-engineer, pm-coder, pm-tester, "
        "pm-security-reviewer, pm-database-engineer, pm-ui-tester, pm-final-qc. "
        "REQUIRED WORKFLOW ORDER — always follow this topology: "
        "(1) THREE parallel research steps (ids: step_1_code, step_1_data, step_1_online), each bot_id=pm-research-analyst, depends_on=[]: "
        "step_1_code=repo/codebase research, step_1_data=requirements/vault/data context, step_1_online=external references only when needed. "
        "(2) ONE engineering plan step (id: step_2), bot_id=pm-engineer, depends_on=[step_1_code,step_1_data,step_1_online]: "
        "synthesizes research into a concrete plan; MUST produce an implementation_workstreams array for coder fan-out. "
        "(3) ONE OR MORE coder steps (ids: step_3 or step_3_1...step_3_N), bot_id=pm-coder, depends_on=[step_2]: "
        "one step per independent workstream; small tasks use one coder step, large tasks use multiple parallel coder steps. "
        "(4) Tester steps (step_4 or step_4_N), bot_id=pm-tester, each depends_on its paired coder step. "
        "(5) Security reviewer steps (step_5 or step_5_N), bot_id=pm-security-reviewer, each depends_on its paired tester step. "
        "(6) ONE DB engineer step (step_6), bot_id=pm-database-engineer, depends_on all security reviewer steps. "
        "(7) ONE UI tester step (step_7), bot_id=pm-ui-tester, depends_on=[step_6]. OMIT if no UI deliverables. "
        "(8) ONE final QC step (step_8), bot_id=pm-final-qc, depends_on=[step_7] or [step_6] when UI is omitted. "
        "RULES: "
        "Never start with pm-coder, pm-tester, pm-security-reviewer, pm-database-engineer, pm-ui-tester, or pm-final-qc. "
        "Always start with three parallel pm-research-analyst steps followed by pm-engineer. "
        "pm-ui-tester: only when the request includes real UI deliverables or user-facing behavior changes. "
        "pm-final-qc: terminal delivery gate only — never use as a branch retry step. "
        "No operator-owned actions: no CI/CD, commits, PRs, merges, releases unless explicitly requested. "
        "Tester steps: test creation, execution, and behavior validation only — real execution evidence required. "
        "Reviewer steps: concrete findings and final verification only — no merges, tags, or deploys. "
        "Scope: implement exactly what the user asked for — nothing more, nothing less. "
        "Prefer proposed file artifacts over claiming already-committed files. "
        "Use repo context as the source of truth for language, framework, and file extensions. "
        "Match nearby existing files (.razor, .cs, .ts, .py, .cpp) instead of defaulting to Python."
    )

    def __init__(
        self,
        bot_registry: Any,
        scheduler: Any,
        task_manager: Any,
        chat_manager: Any,
    ) -> None:
        self._bot_registry = bot_registry
        self._scheduler = scheduler
        self._task_manager = task_manager
        self._chat_manager = chat_manager

    _TERMINAL_TASK_STATUSES = {"completed", "failed", "retried", "cancelled"}
    _DEFAULT_PM_STAGE_ORDER = [
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

    def _instruction_requests_docs_only_outputs(self, instruction: str) -> bool:
        text = str(instruction or "").strip().lower()
        if not text:
            return False
        has_docs_signal = any(
            marker in text
            for marker in (
                "documentation",
                "markdown",
                ".md",
                "docs/",
                "docs\\",
            )
        )
        has_docs_only_signal = any(
            marker in text
            for marker in (
                "only .md",
                "only md",
                "only markdown",
                "markdown only",
                "docs only",
                "documentation only",
                "document-only",
                "doc-only",
                "document only",
                "only .md documents",
                "only markdown documents",
                "no code edited",
                "no other code edited",
                "shouldn't affect anything with the actual site",
                "shouldnt affect anything with the actual site",
            )
        )
        return has_docs_signal and has_docs_only_signal

    def _requested_output_paths(self, instruction: str) -> List[str]:
        text = str(instruction or "").strip()
        if not text:
            return []
        matches = re.findall(r"(?i)\b(?:docs|documentation)[\\/][A-Za-z0-9_.\-\\/]+", text)
        normalized: List[str] = []
        seen = set()
        for match in matches:
            value = str(match or "").strip().replace("\\", "/").strip("`\"'")
            value = value.rstrip(".,;:)]}")
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _extract_assignment_scope(self, instruction: str) -> Dict[str, Any]:
        docs_only = self._instruction_requests_docs_only_outputs(instruction)
        scope: Dict[str, Any] = {
            "request_text": str(instruction or "").strip(),
            "docs_only": docs_only,
            "requested_output_paths": self._requested_output_paths(instruction),
            "requested_output_extensions": [".md"] if docs_only else [],
            "forbidden_change_domains": ["code", "tests", "database", "ui"] if docs_only else [],
        }
        return scope

    async def orchestrate_assignment(
        self,
        conversation_id: str,
        instruction: str,
        requested_pm_bot_id: Optional[str] = None,
        context_items: Optional[List[str]] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        requested_pm_bot_id = str(requested_pm_bot_id or "").strip()
        if not requested_pm_bot_id:
            raise BotNotFoundError("PM assignment requires an explicit project manager bot selection")
        bots = await self._bot_registry.list()
        pm_bot = self._select_pm_bot(bots, requested_pm_bot_id=requested_pm_bot_id)
        if pm_bot is None:
            raise BotNotFoundError("No PM bot available for assignment")
        if not bot_has_explicit_workflow(pm_bot):
            raise BotNotFoundError(
                f"Selected PM bot '{pm_bot.id}' is missing an explicit workflow configuration"
            )

        return await self._bootstrap_assignment_via_pm_workflow(
            conversation_id=conversation_id,
            instruction=instruction,
            pm_bot=pm_bot,
            context_items=context_items or [],
            project_id=project_id,
            bots=bots,
        )

    def _should_bootstrap_assignment_via_pm_workflow(self, pm_bot: Bot) -> bool:
        workflow = self._bot_workflow(pm_bot)
        if workflow is None:
            return False
        triggers = getattr(workflow, "triggers", None) or []
        return any(bool(getattr(trigger, "enabled", True)) for trigger in triggers)

    async def _bootstrap_assignment_via_pm_workflow(
        self,
        *,
        conversation_id: str,
        instruction: str,
        pm_bot: Bot,
        context_items: List[str],
        project_id: Optional[str],
        bots: List[Bot],
    ) -> Dict[str, Any]:
        orchestration_id = str(uuid.uuid4())
        allowed_bot_ids = derive_allowed_bot_ids(pm_bot.id, bots)
        workflow_graph_id = bot_workflow_graph_id(pm_bot)
        pipeline_name = f"PM Workflow: {str(pm_bot.name or pm_bot.id)}".strip()
        assignment_scope = self._extract_assignment_scope(instruction)
        pm_task = await self._task_manager.create_task(
            bot_id=pm_bot.id,
            payload={
                "title": "PM assignment intake",
                "instruction": str(instruction or "").strip(),
                "role_hint": "pm",
                "step_kind": "planning",
                "deliverables": ["PM workflow output"],
                "evidence_requirements": ["Structured PM output that satisfies the bot output contract"],
                "quality_gates": ["Downstream workflow routing is driven by the selected PM bot configuration"],
                "acceptance_criteria": ["The assignment is decomposed by the selected PM bot configuration"],
                "global_acceptance_criteria": [],
                "global_quality_gates": [],
                "global_risks": [],
                "source": "chat_assign",
                "project_id": project_id,
                "conversation_id": conversation_id,
                "orchestration_id": orchestration_id,
                "context_items": context_items,
                "assignment_request": str(instruction or "").strip(),
                "assignment_scope": assignment_scope,
                "root_pm_bot_id": pm_bot.id,
                "allowed_bot_ids": allowed_bot_ids,
                "workflow_graph_id": workflow_graph_id,
                "run_class": "pm_assignment",
                "pipeline_name": pipeline_name,
                "pipeline_entry_bot_id": pm_bot.id,
            },
            metadata=TaskMetadata(
                source="chat_assign",
                project_id=project_id,
                conversation_id=conversation_id,
                orchestration_id=orchestration_id,
                step_id="pm_assignment_entry",
                pipeline_name=pipeline_name,
                pipeline_entry_bot_id=pm_bot.id,
                root_pm_bot_id=pm_bot.id,
                allowed_bot_ids=allowed_bot_ids,
                workflow_graph_id=workflow_graph_id,
                run_class="pm_assignment",
            ),
        )
        return {
            "orchestration_id": orchestration_id,
            "pm_bot_id": pm_bot.id,
            "instruction": instruction,
            "plan": {
                "global_acceptance_criteria": [],
                "global_quality_gates": [],
                "risks": [],
                "steps": [
                    {
                        "id": "pm_assignment_entry",
                        "title": "PM assignment intake",
                        "instruction": str(instruction or "").strip(),
                        "bot_id": pm_bot.id,
                        "role_hint": str(pm_bot.role or "pm"),
                        "step_kind": "planning",
                        "depends_on": [],
                        "acceptance_criteria": ["The selected PM bot owns assignment decomposition and routing."],
                        "deliverables": ["PM workflow output"],
                        "quality_gates": ["Downstream workflow is driven by bot configuration."],
                        "evidence_requirements": ["Structured PM output that satisfies the bot output contract"],
                    }
                ],
            },
            "tasks": [pm_task.model_dump()],
            "allowed_bot_ids": allowed_bot_ids,
            "workflow_graph_id": workflow_graph_id,
            "pipeline_name": pipeline_name,
        }

    async def wait_for_completion(
        self,
        assignment: Dict[str, Any],
        poll_interval_seconds: float = 0.4,
        max_wait_seconds: float = 900.0,
    ) -> Dict[str, Any]:
        import asyncio
        import time

        task_ids = [str(t.get("id")) for t in assignment.get("tasks", []) if t.get("id")]
        orchestration_id = str(assignment.get("orchestration_id") or "").strip()
        deadline = time.monotonic() + max_wait_seconds
        snapshots: Dict[str, Task] = {}
        all_terminal = False
        last_signature: Optional[tuple[Any, ...]] = None
        last_change_at = time.monotonic()
        settle_window_seconds = max(1.6, poll_interval_seconds * 4.0)
        stage_order = await self._workflow_stage_order_for_assignment(assignment)
        final_qc_required = "pm-final-qc" in stage_order

        async def _current_task_ids() -> List[str]:
            if orchestration_id and hasattr(self._task_manager, "list_tasks"):
                try:
                    tasks = await self._task_manager.list_tasks(orchestration_id=orchestration_id)
                except Exception:
                    tasks = []
                if tasks:
                    ordered = sorted(
                        tasks,
                        key=lambda task: (
                            str(task.created_at or ""),
                            str(task.metadata.step_id if task.metadata else ""),
                            str(task.id or ""),
                        ),
                    )
                    return [str(task.id) for task in ordered if str(task.id or "").strip()]
            return list(task_ids)

        while time.monotonic() < deadline:
            task_ids = await _current_task_ids()
            all_terminal = True
            signature_items: List[tuple[Any, ...]] = []
            for task_id in task_ids:
                task = await self._task_manager.get_task(task_id)
                snapshots[task_id] = task
                signature_items.append(
                    (
                        str(task.id or ""),
                        str(task.bot_id or ""),
                        str(task.status or ""),
                        str(task.updated_at or ""),
                        str(task.metadata.parent_task_id if task.metadata else ""),
                        str(task.metadata.trigger_rule_id if task.metadata else ""),
                    )
                )
                if task.status not in self._TERMINAL_TASK_STATUSES:
                    all_terminal = False
            signature = tuple(sorted(signature_items))
            now = time.monotonic()
            if signature != last_signature:
                last_signature = signature
                last_change_at = now
            if all_terminal and (not task_ids or (now - last_change_at) >= settle_window_seconds):
                break
            await asyncio.sleep(poll_interval_seconds)

        completed = 0
        failed = 0
        observed_bot_ids: List[str] = []
        lines: List[str] = [
            f"Assignment summary ({len(task_ids)} tasks):",
            "",
        ]
        if not all_terminal and task_ids:
            lines.extend(
                [
                    (
                        "Note: This is a snapshot summary. Some tasks were still running or blocked "
                        "when the summary timeout was reached."
                    ),
                    "Check the DAG or Tasks page for the latest final statuses.",
                    "",
                ]
            )
        final_tasks: List[Task] = []
        for task_id in task_ids:
            task = snapshots.get(task_id) or await self._task_manager.get_task(task_id)
            final_tasks.append(task)
            bot_id = str(task.bot_id or "").strip()
            if bot_id and bot_id not in observed_bot_ids:
                observed_bot_ids.append(bot_id)
            if task.status == "completed":
                completed += 1
            if task.status in {"failed", "retried", "cancelled"}:
                failed += 1
            title = ""
            if isinstance(task.payload, dict):
                title = str(task.payload.get("title") or "")
            lines.append(f"- [{task.status}] {title or task_id} ({task.bot_id})")
            if task.status == "completed":
                output = self._extract_task_output(task.result)
                if output:
                    preview = output[:220]
                    suffix = "..." if len(output) > 220 else ""
                    lines.append(f"  Output Preview: {preview}{suffix}")
                    if len(output) > 220:
                        lines.append("  Note: Chat summary preview truncated; open View DAG or Tasks for the full task result.")
                truncation = self._truncation_hint(task.result)
                if truncation:
                    lines.append(f"  Note: {truncation}")
            elif task.error and task.error.message:
                lines.append(f"  Error: {task.error.message[:220]}")

        final_qc_terminal = any(
            str(task.bot_id or "").strip() == "pm-final-qc"
            and str(task.status or "").strip().lower() in self._TERMINAL_TASK_STATUSES
            for task in final_tasks
        )
        final_qc_completed = any(
            str(task.bot_id or "").strip() == "pm-final-qc"
            and str(task.status or "").strip().lower() == "completed"
            for task in final_tasks
        )
        workflow_complete = (not final_qc_required) or final_qc_terminal
        missing_stages = [
            stage_id
            for stage_id in stage_order
            if stage_id and stage_id not in observed_bot_ids
        ]
        if all_terminal and not workflow_complete:
            lines.extend(
                [
                    "",
                    "Workflow stopped before reaching the configured terminal PM stage.",
                    "Expected terminal stage: pm-final-qc.",
                ]
            )
        if missing_stages:
            lines.append(f"Observed stages: {', '.join(observed_bot_ids) if observed_bot_ids else 'none'}")
            lines.append(f"Missing stages: {', '.join(missing_stages)}")

        return {
            "summary_text": "\n".join(lines),
            "completed": completed,
            "failed": failed,
            "task_count": len(task_ids),
            "all_terminal": all_terminal,
            "workflow_complete": workflow_complete,
            "final_qc_required": final_qc_required,
            "final_qc_completed": final_qc_completed,
            "observed_bot_ids": observed_bot_ids,
            "missing_stages": missing_stages,
            "tasks": [task.model_dump() for task in final_tasks],
        }

    async def persist_summary_message(
        self,
        conversation_id: str,
        assignment: Dict[str, Any],
        completion: Dict[str, Any],
    ) -> Any:
        failed = int(completion.get("failed") or 0)
        all_terminal = bool(completion.get("all_terminal"))
        workflow_complete = bool(completion.get("workflow_complete", True))
        final_qc_required = bool(completion.get("final_qc_required"))
        final_qc_completed = bool(completion.get("final_qc_completed"))
        run_status = (
            "passed"
            if failed == 0 and all_terminal and workflow_complete and (not final_qc_required or final_qc_completed)
            else "failed"
        )
        task_count = int(completion.get("task_count") or 0)
        completed = int(completion.get("completed") or 0)
        pm_bot_id = str(assignment.get("pm_bot_id") or "")
        orchestration_id = str(assignment.get("orchestration_id") or "")
        lines = [
            f"PM run {run_status}.",
            f"Assigned Bot: {pm_bot_id}",
            f"Orchestration ID: {orchestration_id}",
            f"Tasks: {task_count} total, {completed} completed, {failed} failed.",
            "Open View DAG or Full Recap for full task-by-task details.",
        ]
        if not all_terminal:
            lines.append("Run summary captured before all tasks reached a terminal state.")
        elif final_qc_required and not final_qc_completed:
            lines.append("Run did not reach a completed Final QC stage, so it cannot be marked as passed.")
        if run_status == "failed":
            try:
                existing_messages = await self._chat_manager.list_messages(conversation_id, limit=500)
                for message in existing_messages:
                    metadata = message.metadata if isinstance(message.metadata, dict) else {}
                    if str(metadata.get("orchestration_id") or "").strip() != orchestration_id:
                        continue
                    if str(metadata.get("mode") or "").strip() != "assign_pending":
                        continue
                    updated_metadata = dict(metadata)
                    updated_metadata.update(
                        {
                            "run_status": "failed",
                            "ingest_allowed": False,
                        }
                    )
                    await self._chat_manager.update_message(
                        message.id,
                        metadata=updated_metadata,
                    )
            except Exception:
                logger.exception("Failed to mark assign_pending message as failed for orchestration %s", orchestration_id)
        return await self._chat_manager.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="\n".join(lines),
            bot_id=pm_bot_id,
            metadata={
                "mode": "pm_run_report",
                "orchestration_id": orchestration_id,
                "task_count": task_count,
                "completed": completed,
                "failed": failed,
                "run_status": run_status,
                "ingest_allowed": run_status == "passed",
                "workflow_complete": workflow_complete,
                "final_qc_required": final_qc_required,
                "final_qc_completed": final_qc_completed,
                "full_summary_text": str(completion.get("summary_text") or ""),
            },
        )

    async def _workflow_stage_order_for_assignment(self, assignment: Dict[str, Any]) -> List[str]:
        pm_bot_id = str(
            assignment.get("pm_bot_id")
            or assignment.get("root_pm_bot_id")
            or ""
        ).strip()
        if not pm_bot_id:
            return []
        if self._bot_registry is None:
            return []

        bot_doc = None
        if hasattr(self._bot_registry, "get"):
            try:
                bot_doc = await self._bot_registry.get(pm_bot_id)
            except Exception:
                bot_doc = None
        if bot_doc is None and hasattr(self._bot_registry, "list"):
            try:
                bots = await self._bot_registry.list()
            except Exception:
                bots = []
            bot_doc = next((bot for bot in bots if str(getattr(bot, "id", "")).strip() == pm_bot_id), None)

        workflow = None
        if isinstance(bot_doc, dict):
            workflow = bot_doc.get("workflow")
        elif bot_doc is not None:
            workflow = getattr(bot_doc, "workflow", None)
        reference_graph = None
        if isinstance(workflow, dict):
            reference_graph = workflow.get("reference_graph")
        elif workflow is not None:
            reference_graph = getattr(workflow, "reference_graph", None)
        nodes = None
        if isinstance(reference_graph, dict):
            nodes = reference_graph.get("nodes")
        elif reference_graph is not None:
            nodes = getattr(reference_graph, "nodes", None)
        stage_order = []
        for node in (nodes or []):
            bot_id = ""
            if isinstance(node, dict):
                bot_id = str(node.get("bot_id") or "").strip()
            else:
                bot_id = str(getattr(node, "bot_id", "") or "").strip()
            if bot_id:
                stage_order.append(bot_id)
        if not stage_order:
            return list(self._DEFAULT_PM_STAGE_ORDER)
        return stage_order

    async def _build_plan(
        self,
        instruction: str,
        pm_bot_id: str,
        bots: List[Bot],
        context_items: List[str],
    ) -> Dict[str, Any]:
        if self._has_standard_pm_pack(bots):
            return self._deterministic_pm_pack_plan(instruction, bots)

        raw_output = ""
        try:
            prompt = self._plan_prompt(instruction, context_items)
            pm_task = Task(
                id=f"pm-plan-{uuid.uuid4()}",
                bot_id=pm_bot_id,
                payload=prompt,
                status="running",
                created_at="",
                updated_at="",
            )
            result = await self._scheduler.schedule(pm_task)
            raw_output = self._extract_task_output(result)
        except Exception:
            raw_output = ""

        parsed = self._parse_plan_json(raw_output)
        if parsed:
            # Validate that implementation plans start with research/specification
            steps = parsed.get("steps", [])
            if steps:
                first_step = steps[0]
                first_bot_id = str(first_step.get("bot_id", "")).lower()
                first_step_kind = str(first_step.get("step_kind", "")).lower()
                first_title = str(first_step.get("title", "")).lower()
                
                # For implementation tasks, first step should be research/specification
                # Acceptable first bots: pm-research-analyst (spec), pm-engineer (planning/architecture)
                # Unacceptable: pm-coder, pm-tester, pm-security-reviewer, etc.
                implementation_bots = {"pm-coder", "pm-tester", "pm-security-reviewer", "pm-database-engineer", "pm-ui-tester", "pm-final-qc"}
                if first_bot_id in implementation_bots:
                    logger.warning(
                        "LLM plan starts with implementation bot %s, falling back to heuristic plan",
                        first_bot_id,
                    )
                    return self._heuristic_plan(instruction, bots)
                
                # If starting with pm-engineer, check if pm-research-analyst should be first
                if first_bot_id == "pm-engineer":
                    has_research_bot = any(str(b.id).lower() == "pm-research-analyst" for b in bots)
                    if has_research_bot:
                        logger.warning(
                            "LLM plan starts with pm-engineer but pm-research-analyst is available, falling back to heuristic plan",
                        )
                        return self._heuristic_plan(instruction, bots)
            
            return parsed
        return self._heuristic_plan(instruction, bots)

    def _has_standard_pm_pack(self, bots: List[Bot]) -> bool:
        required_bot_ids = {
            "pm-research-analyst",
            "pm-engineer",
            "pm-coder",
            "pm-tester",
            "pm-security-reviewer",
            "pm-database-engineer",
            "pm-ui-tester",
            "pm-final-qc",
        }
        enabled_ids = {str(bot.id).strip().lower() for bot in bots if getattr(bot, "enabled", False)}
        return required_bot_ids.issubset(enabled_ids)

    def _bot_workflow(self, bot: Bot) -> Any:
        workflow = getattr(bot, "workflow", None)
        if workflow is not None and getattr(workflow, "triggers", None):
            return workflow
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return workflow
        candidate = routing_rules.get("workflow")
        if candidate is None:
            return workflow
        try:
            from shared.models import BotWorkflow

            parsed = BotWorkflow.model_validate(candidate)
            return parsed if parsed.triggers else workflow
        except Exception:
            return workflow

    def _deterministic_pm_pack_plan(self, instruction: str, bots: List[Bot]) -> Dict[str, Any]:
        enabled_ids = {str(bot.id).strip().lower() for bot in bots if getattr(bot, "enabled", False)}
        steps: List[Dict[str, Any]] = [
            {
                "id": "step_1_code",
                "title": "Research repo implementation patterns and code constraints",
                "instruction": (
                    "Inspect the repository directly for stack, runtime constraints, nearby implementations, existing "
                    "components, file-structure expectations, and coding/test patterns relevant to the request."
                ),
                "bot_id": "pm-research-analyst",
                "role_hint": "researcher",
                "step_kind": "specification",
                "depends_on": [],
                "acceptance_criteria": [
                    "Repo implementation patterns and runtime constraints are identified from concrete files",
                    "Code and test conventions are grounded in the actual repository",
                ],
                "deliverables": [
                    "Repo/runtime constraints summary",
                    "Existing implementation inventory",
                ],
                "evidence_requirements": [
                    "Concrete repo-profile or existing-file evidence",
                    "Relevant file/path inventory tied to the requested work",
                ],
                "quality_gates": ["No unsupported stack or runtime assumptions are introduced"],
            },
            {
                "id": "step_1_data",
                "title": "Research requirements, prior decisions, and data context",
                "instruction": (
                    "Use the user request, prior project context, and available vault knowledge to extract requirements, "
                    "acceptance criteria, dependencies, prior decisions, and data or database considerations relevant to implementation."
                ),
                "bot_id": "pm-research-analyst",
                "role_hint": "researcher",
                "step_kind": "specification",
                "depends_on": [],
                "acceptance_criteria": [
                    "Requirements and prior project constraints are captured clearly",
                    "Relevant data, schema, or state-management concerns are identified when applicable",
                ],
                "deliverables": [
                    "Requirements summary artifact",
                    "Project and data-context summary",
                ],
                "evidence_requirements": [
                    "Requirements artifact with acceptance criteria",
                    "Concrete project, vault, or data-context evidence",
                ],
                "quality_gates": ["No prior project or data constraints are ignored or contradicted"],
            },
            {
                "id": "step_1_online",
                "title": "Research external references when required",
                "instruction": (
                    "Research external documentation, standards, or online references only when the request requires it. "
                    "If no external research is needed, state that explicitly instead of inventing it."
                ),
                "bot_id": "pm-research-analyst",
                "role_hint": "researcher",
                "step_kind": "specification",
                "depends_on": [],
                "acceptance_criteria": [
                    "External references are used only when necessary",
                    "Any online research is relevant, current, and tied back to the requested work",
                ],
                "deliverables": [
                    "External research summary or explicit no-external-research note",
                ],
                "evidence_requirements": [
                    "Current external reference evidence when used",
                    "Explicit statement when external research is not required",
                ],
                "quality_gates": ["No unnecessary or unsupported external assumptions are introduced"],
            },
            {
                "id": "step_2",
                "title": "Plan architecture and implementation sequence",
                "instruction": (
                    "Turn the validated requirements into a concrete engineering plan. Identify affected systems, file areas, "
                    "test strategy, database impact, and the coder workstreams needed for downstream fan-out. "
                    "Produce a structured implementation_workstreams list sized for parallel coders when the scope naturally splits; "
                    "use a single workstream only when the task is genuinely small. "
                    "CRITICAL: Your JSON output MUST include an 'implementation_workstreams' array. Each entry must have: "
                    "'title' (short workstream name), 'instruction' (self-contained implementation instruction for the coder), "
                    "'scope' (list of files to create/modify), 'acceptance_criteria' (list), and 'test_strategy' (string). "
                    "The implementation_workstreams array drives the entire downstream coder fan-out — every coder, tester, "
                    "and security reviewer branch is created from this list. Do not omit it."
                ),
                "bot_id": "pm-engineer",
                "role_hint": "engineer",
                "step_kind": "planning",
                "depends_on": ["step_1_code", "step_1_data", "step_1_online"],
                "acceptance_criteria": [
                    "The implementation plan matches the repo stack and existing architecture",
                    "Impacted files, test strategy, and validation stages are clear",
                    "Implementation workstreams are explicit and ready for coder fan-out",
                ],
                "deliverables": [
                    "Implementation plan artifact",
                    "Implementation workstream list for coder fan-out",
                    "Impacted areas summary",
                    "Validation strategy summary",
                ],
                "evidence_requirements": [
                    "Concrete implementation plan tied to repo structure",
                    "Structured implementation_workstreams list for downstream coder fan-out",
                    "Risk and dependency notes",
                ],
                "quality_gates": ["No plan item introduces an unsupported runtime or framework"],
            },
        ]

        return {
            "steps": steps,
            "global_acceptance_criteria": [
                "The workflow follows the fixed PM sequence with explicit bot IDs",
                "Implementation and validation stay grounded in the repo profile and existing codebase",
            ],
            "global_quality_gates": [
                "No unsupported runtime is introduced unless explicitly authorized by the user",
                "All required validation stages complete before final QC",
            ],
            "risks": [],
        }

    def _plan_prompt(self, instruction: str, context_items: List[str]) -> List[Dict[str, str]]:
        context_blob = "\n".join(context_items).strip()
        user_prompt = f"User request:\n{instruction.strip()}"
        if context_blob:
            user_prompt = f"{user_prompt}\n\nContext:\n{context_blob[:3000]}"
        return [
            {"role": "system", "content": self.PM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _parse_plan_json(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = text.strip()
        candidate = text
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                candidate = parts[1].replace("json", "", 1).strip()
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
                return None
            steps = []
            for idx, s in enumerate(parsed["steps"]):
                if not isinstance(s, dict):
                    continue
                step_id = str(s.get("id") or f"step_{idx + 1}")
                raw_deliverables = self._normalize_string_list(s.get("deliverables"))
                role_hint = str(s.get("role_hint") or "assistant").lower()
                step_kind = self._normalize_step_kind(
                    s.get("step_kind"),
                    title=str(s.get("title") or f"Step {idx + 1}"),
                    instruction=str(s.get("instruction") or ""),
                    role_hint=role_hint,
                    deliverables=raw_deliverables,
                )
                deliverables = self._normalize_deliverables_for_step(step_kind=step_kind, deliverables=raw_deliverables)
                steps.append(
                    {
                        "id": step_id,
                        "title": str(s.get("title") or f"Step {idx + 1}"),
                        "instruction": str(s.get("instruction") or ""),
                        "bot_id": str(s.get("bot_id") or "").strip(),
                        "role_hint": role_hint,
                        "depends_on": [str(x) for x in (s.get("depends_on") or []) if x],
                        "step_kind": step_kind,
                        "evidence_requirements": self._normalize_evidence_requirements(
                            step_kind=step_kind,
                            deliverables=deliverables,
                            evidence_requirements=self._normalize_string_list(s.get("evidence_requirements")),
                        ),
                        "acceptance_criteria": self._normalize_string_list(s.get("acceptance_criteria")),
                        "deliverables": deliverables,
                        "quality_gates": self._normalize_string_list(s.get("quality_gates")),
                    }
                )
            if not steps:
                return None
            return {
                "steps": steps,
                "global_acceptance_criteria": self._normalize_string_list(parsed.get("global_acceptance_criteria")),
                "global_quality_gates": self._normalize_string_list(parsed.get("global_quality_gates")),
                "risks": self._normalize_string_list(parsed.get("risks")),
            }
        except Exception:
            return None

    def _heuristic_plan(self, instruction: str, bots: List[Bot]) -> Dict[str, Any]:
        role_set = {str(b.role).lower() for b in bots}
        has_tester = any("test" in r for r in role_set)
        has_reviewer = any("review" in r or "audit" in r or "security" in r for r in role_set)
        has_research = any("research" in r for r in role_set)
        has_dba = any("dba" in r or "database" in r or "data" in r for r in role_set)
        has_final_qc = any(("final" in r and "qc" in r) or "final-qc" in r for r in role_set)
        has_ui_tester = any("ui" in r for r in role_set)
        needs_database = self._instruction_mentions_database(instruction)
        needs_ui = self._instruction_mentions_ui(instruction)
        pm_bot = self._select_pm_bot(bots, requested_pm_bot_id=None)
        pm_bot_id = pm_bot.id if pm_bot is not None else ""

        base_steps: List[Dict[str, Any]] = [
            {
                "id": "step_1",
                "title": "Write implementation specification",
                "instruction": (
                    "Produce user stories, acceptance criteria, failure cases, and scope boundaries for: "
                    f"{instruction}"
                ),
                "bot_id": self._preferred_bot_id_for_role(
                    bots,
                    role_hint="researcher" if has_research else "assistant",
                    pm_bot_id=pm_bot_id,
                ),
                "role_hint": "researcher" if has_research else "assistant",
                "step_kind": "specification",
                "depends_on": [],
                "acceptance_criteria": [
                    "Stories are implementation-ready and testable",
                    "Acceptance criteria are explicit and non-ambiguous",
                ],
                "deliverables": ["Specification notes", "Story list", "Acceptance criteria list"],
                "evidence_requirements": ["Specification document or requirements artifact"],
                "quality_gates": ["No unresolved scope ambiguity"],
            },
        ]

        implementation_dep = "step_1"
        if needs_database:
            base_steps.append(
                {
                    "id": "step_2",
                    "title": "Plan and apply database changes",
                    "instruction": (
                        "Design required schema/query/data updates for this request, including rollback strategy and "
                        "data-safety checks."
                    ),
                    "bot_id": self._preferred_bot_id_for_role(
                        bots,
                        role_hint="dba" if has_dba else "coder",
                        pm_bot_id=pm_bot_id,
                    ),
                    "role_hint": "dba" if has_dba else "coder",
                    "step_kind": "planning",
                    "depends_on": ["step_1"],
                    "acceptance_criteria": [
                        "Schema/query updates are backward-compatible or have explicit migration plan",
                        "Data integrity and rollback plan are documented",
                    ],
                    "deliverables": ["Migration/query plan", "DB safety checklist"],
                    "evidence_requirements": ["Migration plan or schema artifact"],
                    "quality_gates": ["No destructive operation without rollback path"],
                }
            )
            implementation_dep = "step_2"

        base_steps.append(
            {
                "id": "step_3",
                "title": "Implement core changes",
                "instruction": f"Implement the approved solution for: {instruction}",
                "bot_id": self._preferred_bot_id_for_role(bots, role_hint="coder", pm_bot_id=pm_bot_id),
                "role_hint": "coder",
                "step_kind": "repo_change",
                "depends_on": [implementation_dep],
                "acceptance_criteria": [
                    "Implementation matches stories and acceptance criteria",
                    "Code paths include error handling and edge-case handling",
                ],
                "deliverables": ["Code changes", "Implementation notes"],
                "evidence_requirements": ["Proposed files or code artifacts"],
                "quality_gates": ["No runtime errors in local test run"],
            }
        )

        base_steps.append(
            {
                "id": "step_4",
                "title": "Add and run tests",
                "instruction": "Create and run tests for happy path, edge cases, regressions, and failure handling.",
                "bot_id": self._preferred_bot_id_for_role(
                    bots,
                    role_hint="tester" if has_tester else "coder",
                    pm_bot_id=pm_bot_id,
                ),
                "role_hint": "tester" if has_tester else "coder",
                "step_kind": "test_execution",
                "depends_on": ["step_3"],
                "acceptance_criteria": [
                    "Automated tests cover core behavior and edge cases",
                    "Failing tests are resolved before handoff",
                ],
                "deliverables": ["Test changes", "Test run evidence"],
                "evidence_requirements": ["Executed test command output", "Coverage or pass/fail evidence"],
                "quality_gates": ["All required tests pass"],
            }
        )
        depends = ["step_4"]
        base_steps.append(
            {
                "id": "step_5",
                "title": "Security and quality review",
                "instruction": (
                    "Review for security risks, data exposure, algorithm or logic defects, and performance regressions."
                ),
                "bot_id": self._preferred_bot_id_for_role(
                    bots,
                    role_hint="reviewer" if has_reviewer else "security",
                    pm_bot_id=pm_bot_id,
                ),
                "role_hint": "reviewer" if has_reviewer else "security",
                "step_kind": "review",
                "depends_on": depends,
                "acceptance_criteria": [
                    "No known security leak paths or obvious privilege bypasses",
                    "No unresolved high-severity quality issues",
                ],
                "deliverables": ["Review findings", "Final verification summary"],
                "evidence_requirements": ["Concrete findings tied to changed files or executed evidence"],
                "quality_gates": ["Zero unresolved high-severity findings"],
            }
        )
        if has_final_qc:
            final_qc_dep = "step_5"
            if has_ui_tester and needs_ui:
                base_steps.append(
                    {
                        "id": "step_5b",
                        "title": "UI / frontend testing",
                        "instruction": (
                            "Verify all user-facing deliverables render correctly. Test component behaviour, "
                            "visual layout, accessibility, and interaction flows as applicable."
                        ),
                        "bot_id": self._preferred_bot_id_for_role(
                            bots,
                            role_hint="ui-tester",
                            pm_bot_id=pm_bot_id,
                        ),
                        "role_hint": "ui-tester",
                        "step_kind": "test_execution",
                        "depends_on": ["step_5"],
                        "acceptance_criteria": [
                            "All UI deliverables render without errors",
                            "Interaction and layout tests pass",
                        ],
                        "deliverables": ["UI test results", "Screenshot or render evidence"],
                        "evidence_requirements": ["UI test run output or render screenshots"],
                        "quality_gates": ["No blocking UI rendering or interaction failures"],
                    }
                )
                final_qc_dep = "step_5b"
            base_steps.append(
                {
                    "id": "step_6",
                    "title": "Final delivery verification",
                    "instruction": (
                        "Perform the final evidence-backed QC pass. Confirm deliverables are present, tests or review gates "
                        "are satisfied, and the implementation is ready for operator review."
                    ),
                    "bot_id": self._preferred_bot_id_for_role(bots, role_hint="final-qc", pm_bot_id=pm_bot_id),
                    "role_hint": "final-qc",
                    "step_kind": "review",
                    "depends_on": [final_qc_dep],
                    "acceptance_criteria": [
                        "All required deliverables are present with evidence-backed validation",
                        "The final summary identifies any remaining operator-owned actions without claiming completion of them",
                    ],
                    "deliverables": ["Final verification summary", "Suggested commit message"],
                    "evidence_requirements": ["Concrete findings tied to changed files or execution evidence"],
                    "quality_gates": ["No unresolved blocking issues remain"],
                }
            )
        return {
            "steps": base_steps,
            "global_acceptance_criteria": [
                "Implementation matches user request and acceptance criteria",
                "No known regressions in touched behavior",
            ],
            "global_quality_gates": [
                "Tests pass",
                "Security/quality review complete",
            ],
            "risks": [],
        }

    def _select_pm_bot(self, bots: List[Bot], requested_pm_bot_id: Optional[str]) -> Optional[Bot]:
        selected_id = str(requested_pm_bot_id or "").strip()
        if not selected_id:
            return None
        for bot in bots:
            if bot.id != selected_id:
                continue
            if not bot.enabled:
                raise BotNotFoundError(f"Selected PM bot '{selected_id}' is disabled")
            if not bot_is_project_manager(bot):
                raise BotNotFoundError(
                    f"Selected bot '{selected_id}' is not configured as a project manager"
                )
            return bot
        return None

    def _get_bot_by_id(self, bots: List[Bot], bot_id: str) -> Optional[Bot]:
        """Get a bot by its exact ID. Returns None if not found."""
        for bot in bots:
            if bot.id == bot_id:
                return bot
        return None

    def _preferred_bot_id_for_role(self, bots: List[Bot], role_hint: str, pm_bot_id: str) -> str:
        preferred_ids = {
            "researcher": "pm-research-analyst",
            "assistant": "pm-research-analyst",
            "planner": "pm-engineer",
            "planning": "pm-engineer",
            "engineer": "pm-engineer",
            "coder": "pm-coder",
            "tester": "pm-tester",
            "qa": "pm-tester",
            "reviewer": "pm-security-reviewer",
            "security": "pm-security-reviewer",
            "security-reviewer": "pm-security-reviewer",
            "dba": "pm-database-engineer",
            "database": "pm-database-engineer",
            "dba-sql": "pm-database-engineer",
            "ui": "pm-ui-tester",
            "ui-tester": "pm-ui-tester",
            "final-qc": "pm-final-qc",
            "final_qc": "pm-final-qc",
        }
        preferred_id = preferred_ids.get(str(role_hint or "").strip().lower())
        if preferred_id:
            preferred_bot = self._get_bot_by_id(bots, preferred_id)
            if preferred_bot is not None and preferred_bot.enabled and preferred_bot.id != pm_bot_id:
                return preferred_bot.id
        return self._pick_target_bot(bots, role_hint=role_hint, pm_bot_id=pm_bot_id).id

    def _pick_target_bot(self, bots: List[Bot], role_hint: str, pm_bot_id: str) -> Bot:
        enabled = [b for b in bots if b.enabled]
        non_pm = [b for b in enabled if b.id != pm_bot_id]
        candidates = non_pm or enabled
        if not candidates:
            raise BotNotFoundError("No enabled bots available for assignment tasks")

        role_hint = role_hint.lower()
        
        # Map role_hint to canonical bot role values for exact matching
        role_exact_matches = {
            "coder": ["coder", "developer", "engineer"],
            "tester": ["tester", "qa"],
            "reviewer": ["reviewer"],
            "researcher": ["researcher", "analyst"],
            "security": ["security", "security-reviewer"],
            "dba": ["dba", "dba-sql", "database"],
            "qa": ["qa", "tester"],
            "assistant": ["assistant"],
            "planner": ["planner"],
            "final-qc": ["final-qc", "final_qc"],
        }
        
        role_patterns = {
            "coder": [r"code", r"dev", r"implement"],
            "tester": [r"test", r"qa"],
            "reviewer": [r"review", r"audit"],
            "researcher": [r"research", r"analyst", r"requirements?", r"spec"],
            "security": [r"security", r"audit", r"review"],
            "dba": [r"\bdba\b", r"database", r"data", r"sql", r"migration"],
            "qa": [r"\bqa\b", r"test", r"quality"],
            "assistant": [r"assist", r"general"],
            "final-qc": [r"final[_\s-]*qc", r"final[_\s-]*review", r"delivery[_\s-]*gate"],
        }

        def _bot_signature(bot: Bot) -> str:
            return f"{bot.id} {bot.name} {bot.role}".lower()

        def _is_media_planner(bot: Bot) -> bool:
            signature = _bot_signature(bot)
            is_media = any(token in signature for token in ("image", "asset", "thumbnail", "media", "art"))
            is_planner = any(token in signature for token in ("planner", "planning", "plan"))
            return is_media and is_planner

        def _skip_for_generic_step(bot: Bot) -> bool:
            if role_hint in {"coder", "tester", "reviewer", "researcher", "security", "dba", "qa", "assistant", "planner", "planning", "final-qc"}:
                return _is_media_planner(bot)
            return False

        def _bot_role_matches_exact(bot: Bot, hint: str) -> bool:
            """Check if bot's role field exactly matches the role_hint."""
            bot_role = str(bot.role or "").lower().strip()
            exact_roles = role_exact_matches.get(hint, [])
            return bot_role in exact_roles

        # Priority 1: Exact role match
        for bot in candidates:
            if _skip_for_generic_step(bot):
                continue
            if _bot_role_matches_exact(bot, role_hint):
                return bot

        # Priority 2: Pattern match on id/name/role (but exclude database bots for coder role)
        patterns = role_patterns.get(role_hint, [re.escape(role_hint)] if role_hint else [])
        for bot in candidates:
            if _skip_for_generic_step(bot):
                continue
            # Skip database engineer bots for non-DBA roles
            if role_hint in {"coder", "tester", "reviewer", "researcher", "security", "qa"}:
                bot_role = str(bot.role or "").lower()
                if "dba" in bot_role or "database" in bot_role:
                    continue
            signature = _bot_signature(bot)
            if any(re.search(p, signature) for p in patterns):
                return bot

        # If a pure researcher bot is unavailable, prefer a general coding/PM bot over domain-specific planners.
        if role_hint in {"researcher", "assistant"}:
            fallback_patterns = [r"coder", r"dev", r"engineer", r"\bpm\b", r"manager", r"orchestrator"]
            for bot in candidates:
                if _skip_for_generic_step(bot):
                    continue
                signature = _bot_signature(bot)
                if any(re.search(p, signature) for p in fallback_patterns):
                    return bot

        generic_candidates = [b for b in candidates if not _skip_for_generic_step(b)]
        pool = generic_candidates or candidates
        pool.sort(key=lambda b: b.priority, reverse=True)
        return pool[0]

    def _normalize_string_list(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        result: List[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    def _normalize_step_kind(
        self,
        value: Any,
        *,
        title: str,
        instruction: str,
        role_hint: str,
        deliverables: List[str],
    ) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "spec": "specification",
            "requirements": "specification",
            "requirement": "specification",
            "design": "planning",
            "architecture": "planning",
            "implementation": "repo_change",
            "implement": "repo_change",
            "coding": "repo_change",
            "code": "repo_change",
            "testing": "test_execution",
            "tests": "test_execution",
            "qa": "test_execution",
            "reviewer": "review",
            "security_review": "review",
            "release_review": "release",
            "ship": "release",
            "merge": "release",
        }
        normalized = aliases.get(normalized, normalized)
        valid = {"specification", "planning", "repo_change", "test_execution", "review", "release"}
        if self._looks_like_issue_planning_step(
            title=title,
            instruction=instruction,
            role_hint=role_hint,
            deliverables=deliverables,
        ):
            return "planning"
        if normalized in valid:
            return normalized
        return self._infer_step_kind(
            title=title,
            instruction=instruction,
            role_hint=role_hint,
            deliverables=deliverables,
        )

    def _infer_step_kind(
        self,
        *,
        title: str,
        instruction: str,
        role_hint: str,
        deliverables: List[str],
    ) -> str:
        haystack = self._step_kind_haystack(
            title=title,
            instruction=instruction,
            role_hint=role_hint,
            deliverables=deliverables,
        )
        role = str(role_hint or "").lower()

        if self._looks_like_issue_planning_step(
            title=title,
            instruction=instruction,
            role_hint=role_hint,
            deliverables=deliverables,
        ):
            return "planning"
        if role in {"tester", "qa"}:
            return "test_execution"
        if role in {"reviewer", "security", "security-reviewer"}:
            return "review"
        if role in {"researcher", "analyst"}:
            return "specification"
        if role in {"coder", "developer", "engineer"} and any("/" in item or "." in item for item in deliverables):
            return "repo_change"
        if any(token in haystack for token in ("release", "merge", "deploy", "ship", "tag", "cutover")):
            return "release"
        if any(token in haystack for token in ("test", "coverage", "qa", "pytest", "integration", "verification")):
            return "test_execution"
        if any(token in haystack for token in ("review", "audit", "security", "findings", "approval")):
            return "review"
        if any(token in haystack for token in ("spec", "requirement", "acceptance criteria", "user story")):
            return "specification"
        if any(
            marker in haystack
            for marker in ("implement", "code", "file", "component", "api route", "refactor", "patch", "fix")
        ) or any("/" in item or "." in item for item in deliverables):
            return "repo_change"
        if any(token in haystack for token in ("plan", "design", "architecture", "migration", "rollback")):
            return "planning"
        return "planning"

    def _step_kind_haystack(
        self,
        *,
        title: str,
        instruction: str,
        role_hint: str,
        deliverables: List[str],
    ) -> str:
        return " ".join(
            [
                str(title or ""),
                str(instruction or ""),
                str(role_hint or ""),
                " ".join(deliverables),
            ]
        ).lower()

    def _looks_like_issue_planning_step(
        self,
        *,
        title: str,
        instruction: str,
        role_hint: str,
        deliverables: List[str],
    ) -> bool:
        haystack = self._step_kind_haystack(
            title=title,
            instruction=instruction,
            role_hint=role_hint,
            deliverables=deliverables,
        )
        return any(
            token in haystack
            for token in (
                "issue",
                "issues",
                "milestone",
                "project board",
                "issue tracker",
                "tracked git issues",
                "tracking issue",
                "tracking issues",
                "roadmap",
                "planning artifact",
            )
        )

    def _default_evidence_requirements(self, step_kind: str) -> List[str]:
        defaults = {
            "specification": [
                "Specification artifact with explicit acceptance criteria",
                "Requirements or scope assumptions documented",
            ],
            "planning": [
                "Implementation or migration plan artifact",
                "Risk, rollback, or dependency notes",
            ],
            "repo_change": [
                "Proposed repo file artifacts or code patches",
                "Concrete changed-file evidence tied to deliverables",
            ],
            "test_execution": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
            "review": [
                "Concrete findings tied to changed files, diffs, or execution artifacts",
                "Merge-readiness verdict backed by evidence",
            ],
            "release": [
                "Pull request, merge, or release artifact",
                "Version, tag, commit, or deployment evidence",
            ],
        }
        return list(defaults.get(step_kind, defaults["planning"]))

    def _is_test_source_file(self, value: str) -> bool:
        text = str(value or "").strip().replace("\\", "/").lower()
        if not self._looks_like_repo_file(text):
            return False
        if text.startswith("reports/") or text.startswith("coverage/"):
            return False
        leaf = text.rsplit("/", 1)[-1]
        return (
            text.startswith("tests/")
            or "/tests/" in text
            or ".tests/" in text
            or "/tests." in text
            or leaf.startswith("test_")
            or ".test." in leaf
            or ".spec." in leaf
        )

    def _is_execution_artifact_file(self, value: str) -> bool:
        text = str(value or "").strip().replace("\\", "/").lower()
        if not self._looks_like_repo_file(text):
            return False
        return (
            text.startswith("reports/")
            or text.startswith("coverage/")
            or text.endswith((".xml", ".html", ".txt", ".json", ".log"))
        )

    def _expand_test_execution_steps(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list):
            return plan

        expanded: List[Dict[str, Any]] = []
        step_id_map: Dict[str, List[str]] = {}

        for idx, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                continue
            original_step_id = str(step.get("id") or f"step_{idx + 1}")
            step_kind = self._normalize_step_kind(
                step.get("step_kind"),
                title=str(step.get("title") or ""),
                instruction=str(step.get("instruction") or ""),
                role_hint=str(step.get("role_hint") or ""),
                deliverables=self._normalize_string_list(step.get("deliverables")),
            )
            deliverables = self._normalize_string_list(step.get("deliverables"))
            test_source_files = [item for item in deliverables if self._is_test_source_file(item)]
            execution_artifacts = [item for item in deliverables if self._is_execution_artifact_file(item) and item not in test_source_files]
            passthrough_deliverables = [
                item for item in deliverables if item not in test_source_files and item not in execution_artifacts
            ]

            # Detect whether all repo-like deliverables are documentation files.
            # If so, this step should never trigger the internal test runner — convert it
            # to a "review" step so it is treated as a validation/QC task instead.
            repo_like_deliverables = [item for item in deliverables if self._looks_like_repo_file(item)]
            all_deliverables_are_docs = bool(repo_like_deliverables) and all(
                str(item).lower().endswith((".md", ".mdx", ".rst", ".adoc", ".txt"))
                or str(item).lower().startswith("docs/")
                for item in repo_like_deliverables
            )

            if step_kind != "test_execution" or not test_source_files:
                if step_kind == "test_execution" and all_deliverables_are_docs:
                    # Convert to review so the internal test runner never fires for docs workstreams
                    converted_step = dict(step)
                    converted_step["step_kind"] = "review"
                    if str(converted_step.get("role_hint") or "").strip().lower() in {"tester", "qa"}:
                        converted_step["role_hint"] = "reviewer"
                    expanded.append(converted_step)
                else:
                    expanded.append(step)
                step_id_map[original_step_id] = [original_step_id]
                continue

            create_step_id = f"{original_step_id}_create_tests"
            execute_step_id = f"{original_step_id}_execute_tests"
            role_hint = str(step.get("role_hint") or "").strip().lower()
            create_role = "coder" if role_hint in {"tester", "qa", ""} else role_hint

            create_step = {
                "id": create_step_id,
                "title": f"Create test files for {str(step.get('title') or 'test execution').strip()}",
                "instruction": (
                    "Create the automated test files needed for this feature. "
                    "Do not claim test execution in this step."
                ),
                "role_hint": create_role,
                "step_kind": "repo_change",
                "depends_on": [str(dep) for dep in (step.get("depends_on") or []) if str(dep).strip()],
                "acceptance_criteria": self._normalize_string_list(step.get("acceptance_criteria")) or [
                    "Test files cover the intended behavior and edge cases"
                ],
                "deliverables": test_source_files,
                "quality_gates": ["Test sources are ready for execution"],
                "evidence_requirements": [
                    "Proposed repo file artifacts or patches for changed files",
                    "Concrete changed-file evidence tied to deliverables",
                ],
            }

            execute_deliverables = execution_artifacts or passthrough_deliverables
            execute_step = {
                "id": execute_step_id,
                "title": str(step.get("title") or "Execute automated tests"),
                "instruction": str(step.get("instruction") or "Execute the automated tests and return real results."),
                "role_hint": role_hint or "tester",
                "step_kind": "test_execution",
                "depends_on": [create_step_id],
                "acceptance_criteria": self._normalize_string_list(step.get("acceptance_criteria")),
                "deliverables": execute_deliverables,
                "quality_gates": self._normalize_string_list(step.get("quality_gates")),
                "evidence_requirements": [
                    "Executed test command output",
                    "Pass/fail or coverage evidence from the test run",
                ],
            }

            expanded.extend([create_step, execute_step])
            step_id_map[original_step_id] = [create_step_id, execute_step_id]

        for step in expanded:
            depends = []
            for dep in step.get("depends_on") or []:
                mapped = step_id_map.get(str(dep), [str(dep)])
                depends.extend(mapped[-1:])
            step["depends_on"] = depends

        return {
            **plan,
            "steps": expanded,
        }

    def _looks_like_repo_file(self, value: str) -> bool:
        text = str(value or "").strip().replace("\\", "/").strip("`")
        if not text or " " in text:
            return False
        if "/" in text:
            leaf = text.rsplit("/", 1)[-1]
            return "." in leaf
        return "." in text and not text.lower().startswith("http")

    def _non_repo_artifact_label(self, *, step_kind: str, value: str) -> str:
        lowered = str(value or "").strip().lower()
        if step_kind == "specification":
            if "report" in lowered or "summary" in lowered or "guide" in lowered:
                return "Research report artifact"
            if "checklist" in lowered:
                return "Research checklist artifact"
            return "Research artifact"
        if step_kind == "planning":
            if "plan" in lowered:
                return "Implementation plan artifact"
            if "risk" in lowered:
                return "Risk summary artifact"
            return "Planning artifact"
        if step_kind == "test_execution":
            if "coverage" in lowered or "test" in lowered or "validation" in lowered:
                return "Validation results artifact"
            return "Execution artifact"
        if step_kind == "review":
            if "final" in lowered or "summary" in lowered:
                return "Review summary artifact"
            return "Review findings artifact"
        return "Artifact"

    def _normalize_deliverables_for_step(self, *, step_kind: str, deliverables: List[str]) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        for item in deliverables:
            text = str(item or "").strip()
            if not text:
                continue
            lowered = text.lower()

            if step_kind in {"specification", "planning"}:
                if "issue" in lowered:
                    text = "Issue definitions (markdown or JSON)"
                    lowered = text.lower()
                elif "project board" in lowered:
                    text = "Project board proposal (markdown)"
                    lowered = text.lower()
                elif "milestone" in lowered:
                    text = "Milestone definition (markdown)"
                    lowered = text.lower()
                elif "pull request" in lowered:
                    continue
                elif "readme.md" in lowered and "placeholder" in lowered:
                    text = "README.md update proposal"
                    lowered = text.lower()

            if step_kind == "repo_change" and any(token in lowered for token in ("feature branch", "commit sha", "commit hash")):
                continue
            if step_kind == "repo_change" and "pull request" in lowered and ("<" in lowered or "placeholder" in lowered):
                continue

            if step_kind == "test_execution" and any(
                token in lowered for token in ("merged pull request", "git tag", "release notes", "changelog", "merge")
            ):
                continue
            if step_kind == "test_execution" and any(
                token in lowered for token in ("github actions run", "ci run", "workflow run", "run #")
            ):
                text = "Test run log artifact"
                lowered = text.lower()

            if step_kind == "review":
                if "pull request" in lowered and ("<" in lowered or "placeholder" in lowered):
                    text = "Review findings (markdown or JSON)"
                    lowered = text.lower()
                elif "release_notes" in lowered or "release notes" in lowered:
                    text = "Documentation update proposal"
                    lowered = text.lower()

            if step_kind == "release":
                if "git tag" in lowered and ("vx.y.z" in lowered or "<" in lowered):
                    text = "Release tag proposal"
                    lowered = text.lower()
                elif "pull request" in lowered and ("<" in lowered or "placeholder" in lowered):
                    text = "Release readiness summary"
                    lowered = text.lower()

            if step_kind in {"specification", "planning", "test_execution", "review"} and self._looks_like_repo_file(text):
                text = self._non_repo_artifact_label(step_kind=step_kind, value=text)
                lowered = text.lower()

            if step_kind in {"specification", "planning", "repo_change"} and re.search(
                r"\.(png|jpg|jpeg|gif|webp|svg)\b",
                lowered,
            ):
                text = re.sub(
                    r"\.(png|jpg|jpeg|gif|webp|svg)\b",
                    ".mermaid.md",
                    text,
                    flags=re.IGNORECASE,
                )

            if text not in seen:
                seen.add(text)
                normalized.append(text)
        return normalized or deliverables

    def _instruction_explicitly_requests_operator_actions(self, instruction: str) -> bool:
        text = str(instruction or "").lower()
        explicit_tokens = (
            "ci/cd",
            "ci cd",
            "github actions",
            "workflow",
            ".github/workflows",
            "project board",
            "milestone",
            "issue tracker",
            "issue definitions",
            "create github issues",
            "pull request",
            "merge",
            "deploy",
            "release",
            "git tag",
            "changelog",
            "commit and push",
            "push to github",
        )
        return any(token in text for token in explicit_tokens)

    def _step_mentions_operator_actions(self, step: Dict[str, Any]) -> bool:
        haystack = " ".join(
            [
                str(step.get("title") or ""),
                str(step.get("instruction") or ""),
                " ".join(self._normalize_string_list(step.get("deliverables"))),
                " ".join(self._normalize_string_list(step.get("acceptance_criteria"))),
                " ".join(self._normalize_string_list(step.get("quality_gates"))),
                " ".join(self._normalize_string_list(step.get("evidence_requirements"))),
            ]
        ).lower()
        tokens = (
            "github issue",
            "issue definitions",
            "project board",
            "milestone",
            "github actions",
            "workflow",
            ".github/workflows",
            "ci run",
            "ci pipeline",
            "pull request",
            "merged",
            "merge",
            "deploy",
            "release",
            "git tag",
            "changelog",
            "approval",
        )
        return any(token in haystack for token in tokens)

    def _sanitize_list_for_operator_scope(self, values: List[str], *, step_kind: str) -> List[str]:
        blocked_tokens = (
            "github issue",
            "issue definitions",
            "project board",
            "milestone",
            "github actions",
            "workflow run",
            "ci run",
            "ci pipeline",
            ".github/workflows",
            "pull request",
            "merged",
            "merge",
            "deploy",
            "release",
            "git tag",
            "changelog",
            "approval",
        )
        normalized: List[str] = []
        for item in values:
            text = str(item or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if any(token in lowered for token in blocked_tokens):
                continue
            if step_kind == "test_execution" and re.search(r"\bci\b", lowered):
                continue
            if step_kind == "test_execution" and lowered.endswith(("ci.yml", "ci.yaml")):
                continue
            normalized.append(text)
        return normalized

    def _sanitize_text_for_operator_scope(self, text: str, *, step_kind: str, fallback: str) -> str:
        value = str(text or "").strip()
        lowered = value.lower()
        replacements = {
            "deployment readiness": "final verification",
            "ci/cd": "",
            "ci cd": "",
            "github actions": "",
            "workflow": "",
            "pull request": "",
            "merge": "",
            "deploy": "",
            "release": "",
            "git tag": "",
            "changelog": "",
        }
        for source, target in replacements.items():
            value = re.sub(source, target, value, flags=re.IGNORECASE)
        value = re.sub(r"\s*&\s*", " and ", value)
        value = re.sub(r"\s{2,}", " ", value)
        value = re.sub(r"\(\s*\)", "", value)
        value = value.strip(" -,:;")
        if step_kind == "test_execution" and any(token in lowered for token in ("ci", "workflow", "pipeline")):
            return fallback
        if step_kind in {"review", "release"} and any(
            token in lowered for token in ("release", "deploy", "merge", "tag", "changelog")
        ):
            return fallback
        return value or fallback

    def _sanitize_plan_for_operator_scope(self, plan: Dict[str, Any], *, instruction: str) -> Dict[str, Any]:
        if self._instruction_explicitly_requests_operator_actions(instruction):
            return plan

        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list):
            return plan

        original_steps: Dict[str, Dict[str, Any]] = {
            str(step.get("id") or f"step_{idx + 1}"): step
            for idx, step in enumerate(raw_steps)
            if isinstance(step, dict)
        }
        sanitized_steps: List[Dict[str, Any]] = []

        for idx, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or f"step_{idx + 1}")
            role_hint = str(step.get("role_hint") or "").strip().lower()
            step_kind = self._normalize_step_kind(
                step.get("step_kind"),
                title=str(step.get("title") or ""),
                instruction=str(step.get("instruction") or ""),
                role_hint=role_hint,
                deliverables=self._normalize_string_list(step.get("deliverables")),
            )

            if step_kind == "planning" and self._looks_like_issue_planning_step(
                title=str(step.get("title") or ""),
                instruction=str(step.get("instruction") or ""),
                role_hint=role_hint,
                deliverables=self._normalize_string_list(step.get("deliverables")),
            ):
                continue

            sanitized = dict(step)
            if step_kind == "release":
                sanitized["step_kind"] = "review"
                sanitized["role_hint"] = "reviewer" if role_hint not in {"security", "security-reviewer"} else role_hint
                sanitized["title"] = "Finalize verification summary"
                sanitized["instruction"] = (
                    "Summarize concrete review findings, residual risks, and handoff notes for the operator. "
                    "Return a final verification summary only."
                )
                sanitized["deliverables"] = ["Review findings", "Final verification summary"]
                sanitized["evidence_requirements"] = ["Concrete findings tied to changed files or executed evidence"]
                sanitized["quality_gates"] = ["Zero unresolved high-severity findings"]
                sanitized_steps.append(sanitized)
                continue

            sanitized["title"] = self._sanitize_text_for_operator_scope(
                str(step.get("title") or ""),
                step_kind=step_kind,
                fallback="Add and run tests" if step_kind == "test_execution" else "Final verification review",
            )
            sanitized["instruction"] = self._sanitize_text_for_operator_scope(
                str(step.get("instruction") or ""),
                step_kind=step_kind,
                fallback=(
                    "Create automated tests, run them in the repo workspace, and return real results."
                    if step_kind == "test_execution"
                    else "Review the implementation for concrete code, security, and data-handling risks."
                ),
            )
            sanitized["deliverables"] = self._sanitize_list_for_operator_scope(
                self._normalize_string_list(step.get("deliverables")),
                step_kind=step_kind,
            )
            sanitized["acceptance_criteria"] = self._sanitize_list_for_operator_scope(
                self._normalize_string_list(step.get("acceptance_criteria")),
                step_kind=step_kind,
            )
            sanitized["quality_gates"] = self._sanitize_list_for_operator_scope(
                self._normalize_string_list(step.get("quality_gates")),
                step_kind=step_kind,
            )
            sanitized["evidence_requirements"] = self._sanitize_list_for_operator_scope(
                self._normalize_string_list(step.get("evidence_requirements")),
                step_kind=step_kind,
            )

            if step_kind == "review":
                sanitized["deliverables"] = sanitized["deliverables"] or ["Review findings", "Final verification summary"]
            elif step_kind == "test_execution":
                sanitized["deliverables"] = sanitized["deliverables"] or ["Test run log artifact"]

            if step_kind == "review" and self._step_mentions_operator_actions(step):
                sanitized["instruction"] = (
                    "Review the implementation for concrete code defects, security risks, data leakage, and regressions. "
                    "Return findings and a final verification summary only."
                )

            sanitized_steps.append(sanitized)

        surviving_ids = {str(step.get("id") or "") for step in sanitized_steps}

        def _resolve_dep(dep_id: str, seen: Optional[set[str]] = None) -> List[str]:
            dep = str(dep_id or "").strip()
            if not dep:
                return []
            if dep in surviving_ids:
                return [dep]
            if seen is None:
                seen = set()
            if dep in seen:
                return []
            seen.add(dep)
            original = original_steps.get(dep)
            if not isinstance(original, dict):
                return []
            resolved: List[str] = []
            for parent in original.get("depends_on") or []:
                resolved.extend(_resolve_dep(str(parent), seen))
            return resolved

        for step in sanitized_steps:
            depends: List[str] = []
            for dep in step.get("depends_on") or []:
                depends.extend(_resolve_dep(str(dep)))
            deduped: List[str] = []
            for dep in depends:
                if dep and dep not in deduped:
                    deduped.append(dep)
            step["depends_on"] = deduped

        return {
            **plan,
            "steps": sanitized_steps,
        }

    def _normalize_evidence_requirements(
        self,
        *,
        step_kind: str,
        deliverables: List[str],
        evidence_requirements: List[str],
    ) -> List[str]:
        normalized = list(evidence_requirements or self._default_evidence_requirements(step_kind))
        deliverable_text = " ".join(deliverables).lower()
        evidence_text = " ".join(normalized).lower()
        has_repo_files = any(self._looks_like_repo_file(item) for item in deliverables)
        mentions_links = any(token in f"{deliverable_text} {evidence_text}" for token in ("github issue", "milestone", "project board", "url", "link"))
        mentions_planning_links = any(
            token in f"{deliverable_text} {evidence_text}"
            for token in ("github issue", "issue definitions", "milestone", "project board", "tracking issue", "roadmap")
        )
        mentions_git_side_effects = any(token in evidence_text for token in ("commit sha", "pull request", "pr ", "approved", "ci", "merged"))
        mentions_ci_links = any(token in f"{deliverable_text} {evidence_text}" for token in ("github actions", "ci run", "workflow run", "run logs"))

        if step_kind in {"specification", "planning"} and has_repo_files:
            return [
                "Proposed repo file artifacts for each listed deliverable",
                "Use `Deliverable: path` plus fenced content for each file output",
            ]
        if step_kind in {"specification", "planning"} and mentions_planning_links:
            return [
                "Proposed issue, milestone, or board definitions",
                "Only include live non-placeholder links if they actually exist",
            ]
        if step_kind == "repo_change" and mentions_git_side_effects:
            return [
                "Proposed repo file artifacts or patches for changed files",
                "Only include non-placeholder commit or pull request evidence if it actually exists",
            ]
        if step_kind == "test_execution" and mentions_ci_links:
            return [
                "Executed test command output",
                "Coverage report file or test run log artifact",
            ]
        return normalized

    def _build_step_instruction(
        self,
        *,
        base_instruction: str,
        step_kind: str,
        deliverables: List[str],
        evidence_requirements: List[str],
        context_items: Optional[List[str]] = None,
        role_hint: str = "",
    ) -> str:
        lines = [str(base_instruction or "").strip()]
        has_repo_profile_context = any(
            "[repo-profile]" in str(item or "").lower()
            for item in (context_items or [])
        )
        
        # Inject context items (repo profile, vault items, etc.) at the top
        if context_items:
            context_blob = "\n".join(str(item) for item in context_items if item).strip()
            if context_blob:
                lines.insert(0, "")
                lines.insert(0, context_blob)
                lines.insert(0, "Context:")
        
        if deliverables:
            lines.append("Deliverables: " + "; ".join(deliverables))
        if evidence_requirements:
            lines.append("Evidence requirements: " + "; ".join(evidence_requirements))
        if has_repo_profile_context:
            lines.append(
                "The repo-profile context above is authoritative. Do not say the stack is unknown, assumed, or inferred when it is already provided."
            )
            lines.append(
                "Do not introduce a different language, framework, or runtime than the repo profile unless the user explicitly authorizes that additional runtime."
            )

        if any(self._looks_like_repo_file(item) for item in deliverables):
            lines.append(
                "For each repo file deliverable, return the full file content in this exact format: "
                "`Deliverable: path` on its own line followed by a fenced code block."
            )
            lines.append(
                "If the bot is constrained to JSON output, include the same full file contents in an `artifacts` array "
                "using objects shaped like `{path, content}` for every created or modified deliverable."
            )
            lines.append(
                "Choose languages, frameworks, and file extensions to match the repo context and nearby existing files. "
                "Do not default to Python when the repo points to Razor, C#, TypeScript, C++, or another established stack."
            )
            lines.append(
                "The repo profile is authoritative. Do not let spec assumptions or requested examples introduce a new runtime "
                "that the repo does not already declare. Runtime-mismatched repo files will fail validation."
            )
        elif step_kind in {"specification", "planning", "test_execution", "review"}:
            lines.append(
                "This is a non-repo step. Do not return repo file deliverables, committed file contents, or any artifact entries "
                "with repo-style `path` values such as `docs/...`, `src/...`, or other workspace paths."
            )
            lines.append(
                "Keep the output in the structured JSON fields for this bot. If you include `artifacts`, they must be report-style "
                "records without repo file paths and must not represent files to commit."
            )
        elif step_kind == "repo_change":
            lines.append(
                "This is a repo-change step. You MUST determine the exact changed file paths from the repo context and return "
                "the full file content for every created or modified repo file."
            )
            lines.append(
                "If the bot returns JSON, include every created or modified repo file in a non-empty `artifacts` array using "
                "objects shaped like `{path, content}`."
            )
            lines.append(
                "Do not return only summaries, plans, issue lists, CI workflow proposals, or generic implementation notes. "
                "Return the actual repo file artifacts needed to apply the change."
            )
            lines.append(
                "Choose languages, frameworks, and file extensions to match the repo context and nearby existing files. "
                "Do not introduce a new runtime unless the user explicitly authorized it."
            )
        # Namespace / package injection hint for coder steps.
        # Prevents the bot from hallucinating namespace names that don't exist in the repo.
        _is_coder_step = str(role_hint or "").strip().lower() in {
            "coder", "developer", "coding", "engineer", "implementation",
        } or step_kind in {"repo_change", "coding", "implementation"}
        if _is_coder_step:
            lines.append(
                "NAMESPACE / PACKAGE INTEGRITY: Before declaring any namespace, package, module, "
                "or import path in generated code, use repo_search to find an existing file in the "
                "same directory or adjacent directories and copy its exact namespace/package declaration. "
                "Never invent or guess a namespace. If you cannot confirm the namespace from an existing "
                "file, leave a TODO comment and state what you searched for."
            )
        if any(item.lower().endswith(".mermaid.md") for item in deliverables):
            lines.append(
                "For diagram deliverables, return Mermaid or markdown diagram source as text. Do not attempt to return binary image data."
            )
        if step_kind == "planning":
            lines.append(
                "If live GitHub or project-board access is unavailable, return proposed issue/milestone/board definitions only and do not claim creation."
            )
        if step_kind == "specification":
            lines.append(
                "Produce a complete, implementation-ready artifact. Use structured sections and clear examples. "
                "Include all necessary detail - completeness is more important than brevity."
            )
            if any(self._looks_like_repo_file(item) for item in deliverables):
                lines.append(
                    "CRITICAL: You MUST begin EACH file deliverable with 'Deliverable: path' on its own line, "
                    "followed by a fenced code block containing the full file content. "
                    "Example:\n"
                    "Deliverable: docs/SPEC.md\n"
                    "```markdown\n"
                    "# Specification Title\n"
                    "Content here...\n"
                    "```\n"
                    "Do NOT skip this format. Do NOT just describe what you would write."
                )
        if step_kind == "test_execution":
            lines.append(
                "Return only real executed command output and concrete artifact paths. Do not provide mocked, representative, or checklist-only test reports."
            )
            lines.append(
                "Include an `Executed Commands` section with the commands run, exit codes, and short stdout/stderr excerpts. "
                "If a report file deliverable exists, return it as `Deliverable: path` followed by a fenced code block containing the artifact content."
            )
            lines.append(
                "If the bot returns JSON, include any generated reports or logs in an `artifacts` array with `{path, content}` objects."
            )
        if step_kind in {"review", "release"}:
            lines.append(
                "Do not provide a generic checklist. Return only concrete findings or release evidence backed by actual files, diffs, links, SHAs, or command output."
            )
        lines.append(
            "Never invent placeholders, fake SHAs, fake URLs, fake approvals, or simulated CI/release status."
        )
        return "\n\n".join(line for line in lines if line)

    def _instruction_mentions_database(self, instruction: str) -> bool:
        text = str(instruction or "").lower()
        keywords = (
            "database",
            "db ",
            "db.",
            "migration",
            "schema",
            "sql",
            "table",
            "query",
            "index",
            "postgres",
            "sqlite",
            "mysql",
        )
        return any(keyword in text for keyword in keywords)

    def _instruction_mentions_ui(self, instruction: str) -> bool:
        text = str(instruction or "").lower()
        keywords = (
            "frontend",
            "front-end",
            "ui ",
            " ui",
            "component",
            "template",
            "render",
            " css",
            "html",
            "react",
            "vue",
            "angular",
            " page",
            " view",
            "interface",
            "layout",
            "widget",
            "screen",
            "modal",
            "form",
        )
        return any(keyword in text for keyword in keywords)

    def _extract_task_output(self, result: Any) -> str:
        if isinstance(result, dict):
            output = result.get("output")
            if output is not None:
                return str(output)
            return json.dumps(result)
        if result is None:
            return ""
        return str(result)

    def _truncation_hint(self, result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        finish_reason = str(result.get("finish_reason") or "").strip().lower()
        if finish_reason in {"length", "max_tokens", "max_output_tokens", "token_limit", "max_new_tokens"}:
            return "Model output likely hit token limit and may be incomplete."
        # Don't flag based on token count alone - models can legitimately produce long outputs
        # Only show truncation warning if finish_reason explicitly indicates truncation
        return ""
