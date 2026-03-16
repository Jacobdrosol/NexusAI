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
