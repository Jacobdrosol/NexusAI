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
        '"step_kind":"specification|planning|repo_change|test_execution|review|release",'
        '"evidence_requirements":["..."],"depends_on":[],"acceptance_criteria":["..."],'
        '"deliverables":["..."],"quality_gates":["..."]}]}. '
        "Keep 3-6 steps. Include stories/specification first, implementation next, and testing/review gates before completion. "
        "Do not classify a step as test_execution, review, or release unless it can produce execution-backed evidence rather than generic advice. "
        "Do not claim committed files, created issues, merged pull requests, CI passes, approvals, or releases unless those side effects are actually available with live evidence. "
        "When a step deliverable is a repo file, prefer proposed file artifacts that can be applied later rather than claiming the file is already committed."
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
        plan = self._expand_test_execution_steps(plan)

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
            raw_deliverables = self._normalize_string_list(step.get("deliverables"))
            quality_gates = self._normalize_string_list(step.get("quality_gates"))
            step_kind = self._normalize_step_kind(
                step.get("step_kind"),
                title=str(step.get("title") or ""),
                instruction=str(step.get("instruction") or ""),
                role_hint=role_hint,
                deliverables=raw_deliverables,
            )
            deliverables = self._normalize_deliverables_for_step(step_kind=step_kind, deliverables=raw_deliverables)
            evidence_requirements = self._normalize_evidence_requirements(
                step_kind=step_kind,
                deliverables=deliverables,
                evidence_requirements=self._normalize_string_list(step.get("evidence_requirements")),
            )
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
                    "instruction": self._build_step_instruction(
                        base_instruction=str(step.get("instruction") or instruction),
                        step_kind=step_kind,
                        deliverables=deliverables,
                        evidence_requirements=evidence_requirements,
                    ),
                    "role_hint": role_hint or "assistant",
                    "step_kind": step_kind,
                    "evidence_requirements": evidence_requirements,
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
                    "Review for security risks, data exposure, performance regressions, and deployment readiness."
                ),
                "role_hint": "reviewer" if has_reviewer else "security",
                "step_kind": "review",
                "depends_on": depends,
                "acceptance_criteria": [
                    "No known security leak paths or obvious privilege bypasses",
                    "No unresolved high-severity quality issues",
                ],
                "deliverables": ["Review findings", "Final release recommendation"],
                "evidence_requirements": ["Concrete findings tied to changed files or executed evidence"],
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
        haystack = " ".join(
            [
                str(title or ""),
                str(instruction or ""),
                str(role_hint or ""),
                " ".join(deliverables),
            ]
        ).lower()
        role = str(role_hint or "").lower()

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

            if step_kind != "test_execution" or not test_source_files:
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
        mentions_git_side_effects = any(token in evidence_text for token in ("commit sha", "pull request", "pr ", "approved", "ci", "merged"))

        if step_kind in {"specification", "planning"} and has_repo_files:
            return [
                "Proposed repo file artifacts for each listed deliverable",
                "Use `Deliverable: path` plus fenced content for each file output",
            ]
        if step_kind == "planning" and mentions_links:
            return [
                "Proposed issue, milestone, or board definitions",
                "Only include live non-placeholder links if they actually exist",
            ]
        if step_kind == "repo_change" and mentions_git_side_effects:
            return [
                "Proposed repo file artifacts or patches for changed files",
                "Only include non-placeholder commit or pull request evidence if it actually exists",
            ]
        return normalized

    def _build_step_instruction(
        self,
        *,
        base_instruction: str,
        step_kind: str,
        deliverables: List[str],
        evidence_requirements: List[str],
    ) -> str:
        lines = [str(base_instruction or "").strip()]
        if deliverables:
            lines.append("Deliverables: " + "; ".join(deliverables))
        if evidence_requirements:
            lines.append("Evidence requirements: " + "; ".join(evidence_requirements))

        if any(self._looks_like_repo_file(item) for item in deliverables):
            lines.append(
                "For each repo file deliverable, return the full file content in this exact format: "
                "`Deliverable: path` on its own line followed by a fenced code block."
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
                "Keep the artifact concise and implementation-ready. Prefer structured sections, compact examples, and no unnecessary narrative so the response fits within token limits."
            )
        if step_kind == "test_execution":
            lines.append(
                "Return only real executed command output and concrete artifact paths. Do not provide mocked, representative, or checklist-only test reports."
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
