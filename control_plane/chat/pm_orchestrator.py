import json
import re
import uuid
from typing import Any, Dict, List, Optional

from shared.exceptions import BotNotFoundError
from shared.models import Bot, Task, TaskMetadata


class PMOrchestrator:
    """Creates dependency-ordered tasks from a high-level chat assignment."""

    PM_SYSTEM_PROMPT = (
        "You are the NexusAI Project Manager bot. Break the user's request into a "
        "small ordered plan with dependencies and role hints. Return JSON only: "
        '{"steps":[{"id":"step_1","title":"...","instruction":"...","role_hint":"coder",'
        '"depends_on":[]}]}. Keep 2-6 steps.'
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

        for idx, step in enumerate(plan["steps"]):
            step_id = str(step.get("id") or f"step_{idx + 1}")
            role_hint = str(step.get("role_hint") or "").strip().lower()
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
        max_wait_seconds: float = 120.0,
    ) -> Dict[str, Any]:
        import asyncio
        import time

        task_ids = [str(t.get("id")) for t in assignment.get("tasks", []) if t.get("id")]
        deadline = time.monotonic() + max_wait_seconds
        snapshots: Dict[str, Task] = {}

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
            elif task.error and task.error.message:
                lines.append(f"  Error: {task.error.message[:220]}")

        return {
            "summary_text": "\n".join(lines),
            "completed": completed,
            "failed": failed,
            "task_count": len(task_ids),
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
                    }
                )
            if not steps:
                return None
            return {"steps": steps}
        except Exception:
            return None

    def _heuristic_plan(self, instruction: str, bots: List[Bot]) -> Dict[str, Any]:
        role_set = {str(b.role).lower() for b in bots}
        has_tester = any("test" in r for r in role_set)
        has_reviewer = any("review" in r for r in role_set)
        has_research = any("research" in r for r in role_set)

        base_steps: List[Dict[str, Any]] = [
            {
                "id": "step_1",
                "title": "Clarify approach",
                "instruction": f"Define acceptance criteria and approach for: {instruction}",
                "role_hint": "researcher" if has_research else "assistant",
                "depends_on": [],
            },
            {
                "id": "step_2",
                "title": "Implement core changes",
                "instruction": f"Implement the main solution for: {instruction}",
                "role_hint": "coder",
                "depends_on": ["step_1"],
            },
        ]
        if has_tester:
            base_steps.append(
                {
                    "id": "step_3",
                    "title": "Add and run tests",
                    "instruction": "Create coverage for the implementation and validate behavior.",
                    "role_hint": "tester",
                    "depends_on": ["step_2"],
                }
            )
        if has_reviewer:
            depends = ["step_3"] if has_tester else ["step_2"]
            base_steps.append(
                {
                    "id": "step_4",
                    "title": "Review and polish",
                    "instruction": "Review for regressions, edge cases, and quality risks.",
                    "role_hint": "reviewer",
                    "depends_on": depends,
                }
            )
        return {"steps": base_steps}

    def _select_pm_bot(self, bots: List[Bot], requested_pm_bot_id: Optional[str]) -> Optional[Bot]:
        if requested_pm_bot_id:
            for bot in bots:
                if bot.id == requested_pm_bot_id and bot.enabled:
                    return bot
        pm_roles = {"pm", "project_manager", "project manager"}
        for bot in bots:
            if str(bot.role).lower() in pm_roles and bot.enabled:
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
            "researcher": [r"research", r"analyst", r"plan"],
            "assistant": [r"assist", r"general"],
        }

        patterns = role_patterns.get(role_hint, [re.escape(role_hint)] if role_hint else [])
        for bot in candidates:
            role = str(bot.role).lower()
            if any(re.search(p, role) for p in patterns):
                return bot

        candidates.sort(key=lambda b: b.priority, reverse=True)
        return candidates[0]

    def _extract_task_output(self, result: Any) -> str:
        if isinstance(result, dict):
            output = result.get("output")
            if output is not None:
                return str(output)
            return json.dumps(result)
        if result is None:
            return ""
        return str(result)
