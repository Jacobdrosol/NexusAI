"""Unit tests for DependencyEngine."""

from control_plane.scheduler.dependency_engine import DependencyEngine
from shared.models import Task


def _task(task_id: str, status: str, depends_on=None) -> Task:
    return Task(
        id=task_id,
        bot_id="bot1",
        payload={},
        depends_on=depends_on or [],
        status=status,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_dependency_engine_ready_when_all_dependencies_completed():
    task = _task("t2", "blocked", depends_on=["t1"])
    tasks = {"t1": _task("t1", "completed"), "t2": task}
    assert DependencyEngine.is_ready(task, tasks) is True


def test_dependency_engine_not_ready_when_dependency_missing_or_incomplete():
    task = _task("t2", "blocked", depends_on=["t1", "t3"])
    tasks = {"t1": _task("t1", "running"), "t2": task}
    assert DependencyEngine.is_ready(task, tasks) is False
