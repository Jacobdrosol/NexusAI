import pytest
from control_plane.chat.pm_orchestrator import PMOrchestrator
from control_plane.task_manager.task_manager import TaskManager, _TaskPolicyViolation
from shared.models import Task, TaskMetadata


@pytest.mark.anyio
async def test_scope_lock_prevents_out_of_scope_artifacts():
    """
    Verify that the TaskManager's artifact recording step enforces the scope_lock.
    A task that tries to produce a forbidden artifact (.py file for a docs-only request)
    should raise a _TaskPolicyViolation.
    """

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "ok"}

    tm = TaskManager(StubScheduler(), db_path=":memory:")

    scope_lock = {
        "domains": ["documentation"],
        "allowed_artifacts": ["*.md"],
        "forbidden_keywords": [".py", ".js", ".json"],
        "raw_instruction": "docs only for math blocks",
    }

    task_payload = {
        "instruction": "Create math block docs",
        "assignment_scope": {
            "scope_lock": scope_lock,
            "docs_only": True
        }
    }
    
    task = Task(
        id="test-task-1",
        bot_id="pm-coder",
        payload=task_payload,
        status="completed",
        created_at="",
        updated_at="",
        result={
            "artifacts": [
                {"path": "docs/math/geometry.md", "content": "# Geometry"},
                {"path": "scripts/helper.py", "content": "print('hello')"}, # Forbidden
            ]
        }
    )

    with pytest.raises(_TaskPolicyViolation) as excinfo:
        await tm._record_artifacts_for_task(task)
    
    assert "violates scope lock" in str(excinfo.value)
    assert excinfo.value.code == "scope_violation_forbidden"


def test_scope_lock_extraction_from_instruction():
    """
    Verify that the PMOrchestrator correctly extracts a scope_lock from a
    user's instruction.
    """
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    
    instruction = "I want documentation only for geometry, calculus, and mathematics blocks. No code!"
    
    scope = orchestrator._extract_assignment_scope(instruction)
    scope_lock = scope.get("scope_lock")

    assert scope_lock is not None
    assert "math" in scope_lock["domains"]
    assert "geometry" in scope_lock["domains"]
    assert "*.md" in scope_lock["allowed_artifacts"]
    assert ".py" in scope_lock["forbidden_keywords"]
    assert "code" in scope_lock["forbidden_keywords"]


@pytest.mark.anyio
async def test_pm_prompt_includes_scope_lock_directive():
    """
    Verify the PM_SYSTEM_PROMPT now contains instructions to adhere to the scope_lock.
    """
    assert "scope_lock" in PMOrchestrator.PM_SYSTEM_PROMPT
    assert "non-negotiable directive" in PMOrchestrator.PM_SYSTEM_PROMPT
    assert "STRICTLY adheres to this scope_lock" in PMOrchestrator.PM_SYSTEM_PROMPT

