from unittest.mock import AsyncMock

from control_plane.chat.pm_orchestrator import PMOrchestrator
from shared.models import Bot, Task


def _bot(*, bot_id: str, name: str, role: str, priority: int = 0, enabled: bool = True) -> Bot:
    return Bot(
        id=bot_id,
        name=name,
        role=role,
        backends=[],
        priority=priority,
        enabled=enabled,
    )


def test_pick_target_bot_avoids_media_planner_for_researcher_role() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="course-image-planner", name="Course Image Planner", role="planner", priority=100),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=80),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="researcher", pm_bot_id="pm-main")
    assert picked.id == "pm-coder"


def test_pick_target_bot_still_selects_research_bot_when_available() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="research-bot", name="Requirements Analyst", role="researcher", priority=40),
        _bot(bot_id="course-image-planner", name="Course Image Planner", role="planner", priority=100),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="researcher", pm_bot_id="pm-main")
    assert picked.id == "research-bot"


def test_truncation_hint_detects_finish_reason_length() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    hint = orchestrator._truncation_hint(
        {
            "output": "partial output",
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        }
    )
    assert "token limit" in hint.lower()


def test_truncation_hint_detects_high_completion_tokens_without_finish_reason() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    hint = orchestrator._truncation_hint(
        {
            "output": "partial output",
            "usage": {"completion_tokens": 4096},
        }
    )
    assert "4096" in hint


def test_normalize_step_kind_infers_repo_change_from_deliverable_paths() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    step_kind = orchestrator._normalize_step_kind(
        "",
        title="Build lesson blocks",
        instruction="Implement the feature",
        role_hint="coder",
        deliverables=["src/lesson_blocks/math_block.py", "tests/lesson_blocks/test_math_block.py"],
    )

    assert step_kind == "repo_change"


def test_parse_plan_json_backfills_step_kind_and_evidence_requirements() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    parsed = orchestrator._parse_plan_json(
        """
        {
          "steps": [
            {
              "id": "step_1",
              "title": "Run tests",
              "instruction": "Execute pytest and report coverage",
              "role_hint": "tester",
              "deliverables": ["coverage.xml"]
            }
          ]
        }
        """
    )

    assert parsed is not None
    assert parsed["steps"][0]["step_kind"] == "test_execution"
    assert parsed["steps"][0]["evidence_requirements"]
    assert "Executed test command output" in parsed["steps"][0]["evidence_requirements"][0]


def test_normalize_evidence_requirements_downgrades_spec_file_commit_claims() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="specification",
        deliverables=["docs/lesson_blocks_design.md", "docs/lesson_blocks_flow.png"],
        evidence_requirements=[
            "Design document stored at docs/lesson_blocks_design.md",
            "Diagram attached to the document",
        ],
    )

    assert requirements[0] == "Proposed repo file artifacts for each listed deliverable"
    assert "Deliverable: path" in requirements[1]


def test_normalize_evidence_requirements_downgrades_planning_link_claims() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="planning",
        deliverables=["GitHub issue tracker entries"],
        evidence_requirements=[
            "URLs of created GitHub issues",
            "Milestone and project board links",
        ],
    )

    assert requirements[0] == "Proposed issue, milestone, or board definitions"
    assert "non-placeholder links" in requirements[1]


def test_normalize_deliverables_for_spec_step_rewrites_placeholders_and_binary_assets() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="specification",
        deliverables=[
            "docs/lesson_blocks_math_geometry_spec.md",
            "docs/lesson_blocks_flow.png",
            "GitHub issue #<generated> with spec summary",
        ],
    )

    assert "docs/lesson_blocks_math_geometry_spec.md" in deliverables
    assert "docs/lesson_blocks_flow.mermaid.md" in deliverables
    assert "Issue definitions (markdown or JSON)" in deliverables
    assert all("png" not in item.lower() for item in deliverables)


def test_normalize_deliverables_for_test_step_removes_release_side_effects() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="test_execution",
        deliverables=[
            "tests/ directory updates",
            "Merged pull request",
            "Git tag vX.Y.Z",
            "Release notes in CHANGELOG.md",
        ],
    )

    assert "tests/ directory updates" in deliverables
    assert all("pull request" not in item.lower() for item in deliverables)
    assert all("git tag" not in item.lower() for item in deliverables)
    assert all("changelog" not in item.lower() for item in deliverables)


def test_build_step_instruction_requires_deliverable_file_format() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Write the design document.",
        step_kind="specification",
        deliverables=["docs/lesson_blocks_design.md"],
        evidence_requirements=["Proposed repo file artifacts for each listed deliverable"],
    )

    assert "Deliverables: docs/lesson_blocks_design.md" in instruction
    assert "Deliverable: path" in instruction
    assert "Never invent placeholders" in instruction


def test_build_step_instruction_mentions_diagram_source_and_concise_spec() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Write the design document.",
        step_kind="specification",
        deliverables=["docs/lesson_blocks_flow.mermaid.md"],
        evidence_requirements=["Proposed repo file artifacts for each listed deliverable"],
    )

    assert "Mermaid or markdown diagram source" in instruction
    assert "Keep the artifact concise" in instruction


async def test_wait_for_completion_labels_chat_preview_truncation() -> None:
    long_output = "A" * 260
    completed_task = Task(
        id="task-1",
        bot_id="pm-coder",
        payload={"title": "Implement code"},
        status="completed",
        result={"output": long_output},
        created_at="2026-03-16T18:22:49+00:00",
        updated_at="2026-03-16T18:23:49+00:00",
    )
    task_manager = type("TaskManager", (), {"get_task": AsyncMock(return_value=completed_task)})()
    orchestrator = PMOrchestrator(
        bot_registry=None,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )

    completion = await orchestrator.wait_for_completion(
        {"tasks": [{"id": "task-1"}]},
        poll_interval_seconds=0.0,
        max_wait_seconds=0.0,
    )

    assert "Output Preview:" in completion["summary_text"]
    assert "preview truncated" in completion["summary_text"].lower()


async def test_wait_for_completion_marks_snapshot_when_timeout_reached() -> None:
    running_task = Task(
        id="task-1",
        bot_id="pm-coder",
        payload={"title": "Implement code"},
        status="running",
        created_at="2026-03-16T18:22:49+00:00",
        updated_at="2026-03-16T18:22:49+00:00",
    )
    task_manager = type("TaskManager", (), {"get_task": AsyncMock(return_value=running_task)})()
    orchestrator = PMOrchestrator(
        bot_registry=None,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )

    completion = await orchestrator.wait_for_completion(
        {"tasks": [{"id": "task-1"}]},
        poll_interval_seconds=0.0,
        max_wait_seconds=0.0,
    )

    assert completion["all_terminal"] is False
    assert "snapshot summary" in completion["summary_text"].lower()
    assert "check the dag or tasks page" in completion["summary_text"].lower()
