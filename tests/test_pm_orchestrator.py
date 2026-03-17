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
