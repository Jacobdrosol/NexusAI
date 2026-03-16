import json
import re
import uuid
from typing import Any, Dict, List, Optional

from shared.exceptions import BotNotFoundError
from shared.models import Bot, Task, TaskMetadata


class PMOrchestrator:
    """Creates dependency-ordered tasks from a high-level chat assignment."""

    PM_SYSTEM_PROMPT = (
        "You are the NexusAI Project Manager bot. Break the user's request into a deterministic "
        "implementation workflow with explicit quality controls. Return JSON only with this shape: "
        '{"global_acceptance_criteria":["..."],"global_quality_gates":["..."],"risks":["..."],'
        '"steps":[{"id":"step_1","title":"...","instruction":"...","role_hint":"coder",'
        '"depends_on":[],"acceptance_criteria":["..."],"deliverables":["..."],"quality_gates":["..."]}]}. '
        "Keep 3-6 steps. Include stories/specification first, implementation next, and testing/review gates before completion."
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

    async def orchestrate_assignment(
        self,
        conversation_id: str,
        instruction: str,
        requested_pm_bot_id: Optional[str] = None,
        context_items: Optional[List[str]] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        bots = await self._bot_registry.list()
        pm_bot = self._select_pm_bot(bots, requested_pm_bot_id=requested_pm_bot_id)
        if pm_bot is None:
            raise BotNotFoundError("No PM bot available for assignment")

        plan = await self._build_plan(
            instruction=instruction,
            pm_bot_id=pm_bot.id,
            bots=bots,
            context_items=context_items or [],
        )

        orchestration_id = str(uuid.uuid4())
        task_ids_by_step: Dict[str, str] = {}
        created_tasks: List[Task] = []
        global_acceptance_criteria = self._normalize_string_list(plan.get("global_acceptance_criteria"))
        global_quality_gates = self._normalize_string_list(plan.get("global_quality_gates"))
        global_risks = self._normalize_string_list(plan.get("risks"))

        for idx, step in enumerate(plan["steps"]):
            step_id = str(step.get("id") or f"step_{idx + 1}")
            role_hint = str(step.get("role_hint") or "").strip().lower()
            acceptance_criteria = self._normalize_string_list(step.get("acceptance_criteria"))
            deliverables = self._normalize_string_list(step.get("deliverables"))
            quality_gates = self._normalize_string_list(step.get("quality_gates"))
            target_bot = self._pick_target_bot(bots, role_hint=role_hint, pm_bot_id=pm_bot.id)
            depends_ids = [
                task_ids_by_step[d]
                for d in (step.get("depends_on") or [])
                if isinstance(d, str) and d in task_ids_by_step
            ]
            task = await self._task_manager.create_task(
                bot_id=target_bot.id,
                payload={
                    "title": str(step.get("title") or step_id),
                    "instruction": str(step.get("instruction") or instruction),
                    "role_hint": role_hint or "assistant",
                    "step_number": idx + 1,
                    "step_count": len(plan["steps"]),
                    "depends_on_steps": [str(dep) for dep in (step.get("depends_on") or []) if str(dep).strip()],
                    "acceptance_criteria": acceptance_criteria,
                    "deliverables": deliverables,
                    "quality_gates": quality_gates,
                    "global_acceptance_criteria": global_acceptance_criteria,
                    "global_quality_gates": global_quality_gates,
                    "global_risks": global_risks,
                    "source": "chat_assign",
                    "project_id": project_id,
                    "conversation_id": conversation_id,
                    "orchestration_id": orchestration_id,
                },
                metadata=TaskMetadata(
                    source="chat_assign",
                    project_id=project_id,
                    conversation_id=conversation_id,
                    orchestration_id=orchestration_id,
                    step_id=step_id,
                ),
                depends_on=depends_ids,
            )
            task_ids_by_step[step_id] = task.id
            created_tasks.append(task)

        return {
            "orchestration_id": orchestration_id,
            "pm_bot_id": pm_bot.id,
            "instruction": instruction,
            "plan": plan,
            "tasks": [t.model_dump() for t in created_tasks],
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
        deadline = time.monotonic() + max_wait_seconds
        snapshots: Dict[str, Task] = {}
        all_terminal = False

        while time.monotonic() < deadline:
            all_terminal = True
            for task_id in task_ids:
                task = await self._task_manager.get_task(task_id)
                snapshots[task_id] = task
                if task.status not in {"completed", "failed", "retried"}:
                    all_terminal = False
            if all_terminal:
                break
            await asyncio.sleep(poll_interval_seconds)

        completed = 0
        failed = 0
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
        for task_id in task_ids:
            task = snapshots.get(task_id) or await self._task_manager.get_task(task_id)
            if task.status == "completed":
                completed += 1
            if task.status in {"failed", "retried"}:
                failed += 1
            title = ""
            if isinstance(task.payload, dict):
                title = str(task.payload.get("title") or "")
            lines.append(f"- [{task.status}] {title or task_id} ({task.bot_id})")
            if task.status == "completed":
                output = self._extract_task_output(task.result)
                if output:
                    lines.append(f"  Output: {output[:220]}")
                truncation = self._truncation_hint(task.result)
                if truncation:
                    lines.append(f"  Note: {truncation}")
            elif task.error and task.error.message:
                lines.append(f"  Error: {task.error.message[:220]}")

        return {
            "summary_text": "\n".join(lines),
            "completed": completed,
            "failed": failed,
            "task_count": len(task_ids),
            "all_terminal": all_terminal,
            "tasks": [snapshots[t].model_dump() if t in snapshots else None for t in task_ids],
        }

    async def persist_summary_message(
        self,
        conversation_id: str,
        assignment: Dict[str, Any],
        completion: Dict[str, Any],
    ) -> Any:
        return await self._chat_manager.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=str(completion.get("summary_text") or ""),
            bot_id=str(assignment.get("pm_bot_id") or ""),
            metadata={
                "mode": "assign_summary",
                "orchestration_id": assignment.get("orchestration_id"),
                "task_count": completion.get("task_count"),
                "completed": completion.get("completed"),
                "failed": completion.get("failed"),
            },
        )

    async def _build_plan(
        self,
        instruction: str,
        pm_bot_id: str,
        bots: List[Bot],
        context_items: List[str],
    ) -> Dict[str, Any]:
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
            return parsed
        return self._heuristic_plan(instruction, bots)

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
                steps.append(
                    {
                        "id": step_id,
                        "title": str(s.get("title") or f"Step {idx + 1}"),
                        "instruction": str(s.get("instruction") or ""),
                        "role_hint": str(s.get("role_hint") or "assistant").lower(),
                        "depends_on": [str(x) for x in (s.get("depends_on") or []) if x],
                        "acceptance_criteria": self._normalize_string_list(s.get("acceptance_criteria")),
                        "deliverables": self._normalize_string_list(s.get("deliverables")),
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
        needs_database = self._instruction_mentions_database(instruction)

        base_steps: List[Dict[str, Any]] = [
            {
                "id": "step_1",
                "title": "Write implementation specification",
                "instruction": (
                    "Produce user stories, acceptance criteria, failure cases, and scope boundaries for: "
                    f"{instruction}"
                ),
                "role_hint": "researcher" if has_research else "assistant",
                "depends_on": [],
                "acceptance_criteria": [
                    "Stories are implementation-ready and testable",
                    "Acceptance criteria are explicit and non-ambiguous",
                ],
                "deliverables": ["Specification notes", "Story list", "Acceptance criteria list"],
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
                    "role_hint": "dba" if has_dba else "coder",
                    "depends_on": ["step_1"],
                    "acceptance_criteria": [
                        "Schema/query updates are backward-compatible or have explicit migration plan",
                        "Data integrity and rollback plan are documented",
                    ],
                    "deliverables": ["Migration/query plan", "DB safety checklist"],
                    "quality_gates": ["No destructive operation without rollback path"],
                }
            )
            implementation_dep = "step_2"

        base_steps.append(
            {
                "id": "step_3",
                "title": "Implement core changes",
                "instruction": f"Implement the approved solution for: {instruction}",
                "role_hint": "coder",
                "depends_on": [implementation_dep],
                "acceptance_criteria": [
                    "Implementation matches stories and acceptance criteria",
                    "Code paths include error handling and edge-case handling",
                ],
                "deliverables": ["Code changes", "Implementation notes"],
                "quality_gates": ["No runtime errors in local test run"],
            }
        )

        base_steps.append(
            {
                "id": "step_4",
                "title": "Add and run tests",
                "instruction": "Create and run tests for happy path, edge cases, regressions, and failure handling.",
                "role_hint": "tester" if has_tester else "coder",
                "depends_on": ["step_3"],
                "acceptance_criteria": [
                    "Automated tests cover core behavior and edge cases",
                    "Failing tests are resolved before handoff",
                ],
                "deliverables": ["Test changes", "Test run evidence"],
                "quality_gates": ["All required tests pass"],
            }
        )
        depends = ["step_4"]
        base_steps.append(
            {
                "id": "step_5",
                "title": "Security and quality review",
                "instruction": (
                    "Review for security risks, data exposure, performance regressions, and deployment readiness."
                ),
                "role_hint": "reviewer" if has_reviewer else "security",
                "depends_on": depends,
                "acceptance_criteria": [
                    "No known security leak paths or obvious privilege bypasses",
                    "No unresolved high-severity quality issues",
                ],
                "deliverables": ["Review findings", "Final release recommendation"],
                "quality_gates": ["Zero unresolved high-severity findings"],
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
        if requested_pm_bot_id:
            for bot in bots:
                if bot.id == requested_pm_bot_id and bot.enabled:
                    return bot
        pm_patterns = [
            r"\bpm\b",
            r"project[_\s-]*manager",
            r"program[_\s-]*manager",
            r"orchestrator",
            r"planner",
        ]
        for bot in bots:
            role = str(bot.role or "").lower()
            name = str(bot.name or "").lower()
            if bot.enabled and any(re.search(pattern, role) or re.search(pattern, name) for pattern in pm_patterns):
                return bot
        return None

    def _pick_target_bot(self, bots: List[Bot], role_hint: str, pm_bot_id: str) -> Bot:
        enabled = [b for b in bots if b.enabled]
        non_pm = [b for b in enabled if b.id != pm_bot_id]
        candidates = non_pm or enabled
        if not candidates:
            raise BotNotFoundError("No enabled bots available for assignment tasks")

        role_hint = role_hint.lower()
        role_patterns = {
            "coder": [r"code", r"dev", r"engineer", r"implement"],
            "tester": [r"test", r"qa"],
            "reviewer": [r"review", r"audit"],
            "researcher": [r"research", r"analyst", r"requirements?", r"spec"],
            "security": [r"security", r"audit", r"review"],
            "dba": [r"\bdba\b", r"database", r"data", r"sql", r"migration"],
            "qa": [r"\bqa\b", r"test", r"quality"],
            "assistant": [r"assist", r"general"],
        }

        def _bot_signature(bot: Bot) -> str:
            return f"{bot.id} {bot.name} {bot.role}".lower()

        def _is_media_planner(bot: Bot) -> bool:
            signature = _bot_signature(bot)
            is_media = any(token in signature for token in ("image", "asset", "thumbnail", "media", "art"))
            is_planner = any(token in signature for token in ("planner", "planning", "plan"))
            return is_media and is_planner

        def _skip_for_generic_step(bot: Bot) -> bool:
            if role_hint in {"coder", "tester", "reviewer", "researcher", "security", "dba", "qa", "assistant"}:
                return _is_media_planner(bot)
            return False

        patterns = role_patterns.get(role_hint, [re.escape(role_hint)] if role_hint else [])
        for bot in candidates:
            if _skip_for_generic_step(bot):
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
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return ""
        completion = usage.get("completion_tokens")
        try:
            if int(completion) >= 4096:
                return "Model output may be truncated (completion_tokens reached 4096)."
        except Exception:
            return ""
        return ""
