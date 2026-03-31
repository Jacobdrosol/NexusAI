"""
Integration tests for PM workflow trigger routing.

Covers the full PM bot topology:
  pm-engineer → [fan-out N] → pm-coder → pm-tester → pm-security-reviewer
                                                             ↓ join (fanout_id)
                                               pm-database-engineer → pm-ui-tester → pm-final-qc

Backward routes:
  pm-tester      (fail)              → pm-coder
  pm-security    (fail)              → pm-coder
  pm-ui-tester   (ui_data_issue)     → pm-database-engineer
  pm-ui-tester   (ui_config_issue)   → pm-database-engineer
  pm-ui-tester   (ui_render_issue)   → pm-engineer
  pm-ui-tester   (environment_blocker)→ pm-engineer
  pm-final-qc    (fail)              → pm-engineer

Each test uses a StubScheduler that returns controlled results per bot,
then waits for the terminal task to complete and asserts task cardinality.
"""

import asyncio
import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _workstream(title: str) -> dict:
    return {
        "title": title,
        "instruction": f"Implement {title}",
        "scope": [],
        "acceptance_criteria": [],
        "test_strategy": "unit tests",
    }


def _engineer_result(*titles: str) -> dict:
    return {
        "status": "pass",
        "change_summary": "plan ready",
        "files_touched": [],
        "artifacts": [],
        "risks": [],
        "handoff_notes": "proceed",
        "implementation_workstreams": [_workstream(t) for t in titles],
    }


_CODER_PASS = {
    "status": "pass",
    "change_summary": "implemented",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "ready for testing",
}

_CODER_FAIL = {
    "status": "fail",
    "change_summary": "failed",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "could not implement",
}

_TESTER_PASS = {
    "status": "pass",
    "failure_type": "pass",
    "change_summary": "all tests pass",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "ready for security",
}

_TESTER_FAIL = {
    "status": "fail",
    "failure_type": "test_failure",
    "change_summary": "tests failed",
    "files_touched": [],
    "findings": ["test_failure: unit tests failed"],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "fix the test failures",
}

_SECURITY_PASS = {
    "status": "pass",
    "outcome": "pass",
    "failure_type": "pass",
    "change_summary": "no issues found",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "ready for db",
}

_SECURITY_SKIP = {
    "status": "completed",
    "outcome": "skip",
    "failure_type": "not_applicable",
    "change_summary": "docs-only branch has no security-sensitive runtime changes",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "security review not applicable; continue forward",
}

_SECURITY_FAIL = {
    "status": "fail",
    "failure_type": "security_issue",
    "change_summary": "security issue found",
    "files_touched": [],
    "findings": ["security_issue: vulnerability detected"],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "fix the security vulnerability",
}

_DB_PASS = {
    "status": "pass",
    "change_summary": "db schema ok",
    "files_touched": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "ready for ui test",
}

_UI_SKIP = {
    "status": "skip",
    "failure_type": "skip",
    "change_summary": "no UI deliverables",
    "files_touched": [],
    "findings": [],
    "evidence": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "skipping UI test",
}

_UI_PASS = {
    "status": "pass",
    "failure_type": "pass",
    "change_summary": "UI looks good",
    "files_touched": [],
    "findings": [],
    "evidence": [],
    "artifacts": [],
    "risks": [],
    "handoff_notes": "ready for final qc",
}

_QC_PASS = {
    "status": "pass",
    "change_summary": "all clear",
    "files_touched": [],
    "artifacts": [],
    "delivery_checklist": [],
    "operator_actions": [],
    "risks": [],
    "handoff_notes": "done",
}

_QC_FAIL = {
    "status": "fail",
    "change_summary": "missing deliverables",
    "files_touched": [],
    "artifacts": [],
    "delivery_checklist": [],
    "operator_actions": [],
    "risks": [],
    "handoff_notes": "re-plan needed",
}


async def _make_bot_registry(tmp_path, suffix: str = ""):
    """Create and register all 7 PM bots (matching their YAML trigger configs)."""
    from control_plane.registry.bot_registry import BotRegistry
    from shared.models import Bot

    bot_registry = BotRegistry(db_path=str(tmp_path / f"pm-bots{suffix}.db"))

    await bot_registry.register(Bot(
        id="pm-engineer",
        name="PM Code Engineer",
        role="engineer",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "fanout-to-coders",
                    "event": "task_completed",
                    "target_bot_id": "pm-coder",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "pass",
                    "fan_out_field": "implementation_workstreams",
                    "fan_out_alias": "workstream",
                },
                {
                    "id": "fail-halt",
                    "event": "task_completed",
                    "target_bot_id": "pm-orchestrator",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "fail",
                },
                {
                    "id": "blocked-halt",
                    "event": "task_completed",
                    "target_bot_id": "pm-orchestrator",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "blocked",
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-coder",
        name="PM Coder",
        role="coder",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "pass-to-tester",
                    "event": "task_completed",
                    "target_bot_id": "pm-tester",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "pass",
                },
                {
                    "id": "blocked-to-engineer",
                    "event": "task_completed",
                    "target_bot_id": "pm-engineer",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "blocked",
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-tester",
        name="PM Tester",
        role="tester",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "pass-to-security",
                    "event": "task_completed",
                    "target_bot_id": "pm-security-reviewer",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "pass",
                },
                {
                    "id": "fail-back-to-coder",
                    "event": "task_completed",
                    "target_bot_id": "pm-coder",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "fail",
                    "payload_template": {
                        "upstream_failure_type": "{{source_result.failure_type}}",
                        "upstream_findings": "{{source_result.findings}}",
                        "upstream_handoff_notes": "{{source_result.handoff_notes}}",
                        "fanout_id": "{{source_payload.fanout_id}}",
                        "fanout_count": "{{source_payload.fanout_count}}",
                        "workstream": "{{source_payload.workstream}}",
                        "instruction": "{{source_payload.instruction}}",
                    },
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-security-reviewer",
        name="PM Security Reviewer",
        role="security-reviewer",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "security-skip-join-database",
                    "event": "task_completed",
                    "target_bot_id": "pm-database-engineer",
                    "condition": "has_result",
                    "result_field": "outcome",
                    "result_equals": "skip",
                    "join_group_field": "fanout_id",
                },
                {
                    "id": "join-pass-to-db",
                    "event": "task_completed",
                    "target_bot_id": "pm-database-engineer",
                    "condition": "has_result",
                    "result_field": "outcome",
                    "result_equals": "pass",
                    "join_group_field": "fanout_id",
                },
                {
                    "id": "fail-back-to-coder",
                    "event": "task_completed",
                    "target_bot_id": "pm-coder",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "fail",
                    "payload_template": {
                        "upstream_failure_type": "{{source_result.failure_type}}",
                        "upstream_findings": "{{source_result.findings}}",
                        "upstream_handoff_notes": "{{source_result.handoff_notes}}",
                        "fanout_id": "{{source_payload.fanout_id}}",
                        "fanout_count": "{{source_payload.fanout_count}}",
                        "workstream": "{{source_payload.workstream}}",
                        "instruction": "{{source_payload.instruction}}",
                    },
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-database-engineer",
        name="PM Database Engineer",
        role="dba-sql",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "pass-to-ui",
                    "event": "task_completed",
                    "target_bot_id": "pm-ui-tester",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "pass",
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-ui-tester",
        name="PM UI Tester",
        role="ui-tester",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "pass-to-final-qc",
                    "event": "task_completed",
                    "target_bot_id": "pm-final-qc",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "pass",
                },
                {
                    "id": "skip-to-final-qc",
                    "event": "task_completed",
                    "target_bot_id": "pm-final-qc",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "skip",
                },
                {
                    "id": "fail-data-back-to-db",
                    "event": "task_completed",
                    "target_bot_id": "pm-database-engineer",
                    "condition": "has_result",
                    "result_field": "failure_type",
                    "result_equals": "ui_data_issue",
                },
                {
                    "id": "fail-config-back-to-db",
                    "event": "task_completed",
                    "target_bot_id": "pm-database-engineer",
                    "condition": "has_result",
                    "result_field": "failure_type",
                    "result_equals": "ui_config_issue",
                },
                {
                    "id": "fail-render-back-to-engineer",
                    "event": "task_completed",
                    "target_bot_id": "pm-engineer",
                    "condition": "has_result",
                    "result_field": "failure_type",
                    "result_equals": "ui_render_issue",
                },
                {
                    "id": "fail-env-back-to-engineer",
                    "event": "task_completed",
                    "target_bot_id": "pm-engineer",
                    "condition": "has_result",
                    "result_field": "failure_type",
                    "result_equals": "environment_blocker",
                },
            ]
        },
    ))

    await bot_registry.register(Bot(
        id="pm-final-qc",
        name="PM Final QC",
        role="final-qc",
        backends=[],
        workflow={
            "triggers": [
                {
                    "id": "fail-back-to-engineer",
                    "event": "task_completed",
                    "target_bot_id": "pm-engineer",
                    "condition": "has_result",
                    "result_field": "status",
                    "result_equals": "fail",
                },
            ]
        },
    ))

    return bot_registry


async def _wait_for_terminal(tm, terminal_bot_id: str, expected_total: int, *, timeout: int = 240) -> list:
    """
    Wait until the terminal bot has a completed task, then wait for the full
    expected task count with all tasks completed.  Returns the task list.
    """
    for _ in range(timeout):
        tasks = await tm.list_tasks()
        if any(t.bot_id == terminal_bot_id and t.status == "completed" for t in tasks):
            break
        await asyncio.sleep(0.1)

    for _ in range(timeout):
        tasks = await tm.list_tasks()
        if len(tasks) == expected_total and all(t.status == "completed" for t in tasks):
            break
        await asyncio.sleep(0.1)

    return await tm.list_tasks()


async def _wait_for_quiescent(tm, *, timeout: int = 240, stable_rounds: int = 5) -> list:
    last_signature = None
    stable = 0
    for _ in range(timeout):
        tasks = await tm.list_tasks()
        if any(task.status in {"queued", "running", "blocked"} for task in tasks):
            stable = 0
            last_signature = None
            await asyncio.sleep(0.1)
            continue
        signature = tuple(sorted((task.id, task.bot_id, task.status) for task in tasks))
        if signature == last_signature:
            stable += 1
        else:
            last_signature = signature
            stable = 1
        if stable >= stable_rounds:
            return tasks
        await asyncio.sleep(0.1)
    return await tm.list_tasks()


def _counts(tasks) -> dict:
    c: dict = {}
    for t in tasks:
        c[t.bot_id] = c.get(t.bot_id, 0) + 1
    return c


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — single workstream, all pass, UI skip → 7 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_single_workstream_all_pass_ui_skip(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: core feature")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test1")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test1.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add feature X"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    expected_total = 7
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks), f"Not all completed: {[(t.bot_id, t.status) for t in tasks]}"

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 1
    assert c.get("pm-tester", 0) == 1
    assert c.get("pm-security-reviewer", 0) == 1
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — two workstreams fan-out, all pass, UI skip → 10 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_two_workstream_fan_out_and_join(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: frontend", "WS2: backend")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test2")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test2.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add feature X with two parts"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # 1 engineer + 2 coders + 2 testers + 2 security reviewers + 1 db + 1 ui + 1 qc = 10
    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    # Verify the join fired with the right counts
    db_tasks = [t for t in tasks if t.bot_id == "pm-database-engineer"]
    assert len(db_tasks) == 1
    db_payload = db_tasks[0].payload
    assert isinstance(db_payload, dict)
    assert db_payload.get("join_expected_count") == 2
    assert db_payload.get("join_count") == 2


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 — tester fails once then passes (backward loop) → 9 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_tester_fails_once_then_passes(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    tester_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: core")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                tester_call_count["n"] += 1
                # First tester call fails, second passes
                return _TESTER_FAIL if tester_call_count["n"] == 1 else _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test3")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test3.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add feature with test retry"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # 1 engineer + 2 coders + 2 testers + 1 security + 1 db + 1 ui + 1 qc = 9
    expected_total = 9
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2       # original + retry after tester fail
    assert c.get("pm-tester", 0) == 2       # first fails, second passes
    assert c.get("pm-security-reviewer", 0) == 1
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    # Verify the backward loop: the failed tester task exists
    tester_tasks = [t for t in tasks if t.bot_id == "pm-tester"]
    statuses = sorted(t.result.get("status") for t in tester_tasks if isinstance(t.result, dict))
    assert "fail" in statuses
    assert "pass" in statuses


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 — security reviewer fails once then passes (backward loop) → 10 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_security_fails_once_then_passes(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    security_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: core")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                security_call_count["n"] += 1
                # First security review fails, second passes
                return _SECURITY_FAIL if security_call_count["n"] == 1 else _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test4")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test4.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add feature with security retry"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # 1 engineer + 2 coders + 2 testers + 2 security + 1 db + 1 ui + 1 qc = 10
    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2       # original + retry after security fail
    assert c.get("pm-tester", 0) == 2       # original + second pass after coder retry
    assert c.get("pm-security-reviewer", 0) == 2  # first fails, second passes
    assert c.get("pm-database-engineer", 0) == 1  # join fires once second security passes
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    # Verify security reviewer results: one fail, one pass
    sec_tasks = [t for t in tasks if t.bot_id == "pm-security-reviewer"]
    statuses = sorted(t.result.get("status") for t in sec_tasks if isinstance(t.result, dict))
    assert statuses == ["fail", "pass"]


# ──────────────────────────────────────────────────────────────────────────────
# Test 5 — UI tester reports ui_data_issue, routes back to DB engineer → 9 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_ui_tester_data_issue_routes_back_to_db(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    ui_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: data pipeline")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                ui_call_count["n"] += 1
                if ui_call_count["n"] == 1:
                    return {
                        "status": "fail",
                        "failure_type": "ui_data_issue",
                        "change_summary": "data binding mismatch",
                        "files_touched": [],
                        "findings": ["API response format mismatch"],
                        "evidence": [],
                        "artifacts": [],
                        "risks": [],
                        "handoff_notes": "fix db layer",
                    }
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test5")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test5.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add data pipeline feature"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # 1 engineer + 1 coder + 1 tester + 1 security + 2 db + 2 ui + 1 qc = 9
    expected_total = 9
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 1
    assert c.get("pm-tester", 0) == 1
    assert c.get("pm-security-reviewer", 0) == 1
    assert c.get("pm-database-engineer", 0) == 2  # original + re-run after ui_data_issue
    assert c.get("pm-ui-tester", 0) == 2           # first fails, second skips
    assert c.get("pm-final-qc", 0) == 1

    # Confirm first UI tester reported the data issue
    ui_tasks = [t for t in tasks if t.bot_id == "pm-ui-tester"]
    failure_types = [t.result.get("failure_type") for t in ui_tasks if isinstance(t.result, dict)]
    assert "ui_data_issue" in failure_types


# ──────────────────────────────────────────────────────────────────────────────
# Test 6 — UI tester reports ui_render_issue, routes back to engineer → 14 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_ui_tester_render_issue_routes_back_to_engineer(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    ui_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: render fix")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                ui_call_count["n"] += 1
                if ui_call_count["n"] == 1:
                    return {
                        "status": "fail",
                        "failure_type": "ui_render_issue",
                        "change_summary": "component rendering broken",
                        "files_touched": [],
                        "findings": ["Header component crashes"],
                        "evidence": [],
                        "artifacts": [],
                        "risks": [],
                        "handoff_notes": "re-plan UI layer",
                    }
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test6")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test6.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "fix render issue"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # Full pipeline runs twice (engineer re-plans after ui_render_issue):
    # Pass 1: eng1, coder1, tester1, security1, db1, ui1(fail→render) → triggers eng2 — 6 tasks, no QC
    # Pass 2: eng2, coder2, tester2, security2, db2, ui2(skip) → qc1 — 7 tasks
    # Total: 6 + 7 = 13
    expected_total = 13
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 2
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 2
    assert c.get("pm-ui-tester", 0) == 2
    assert c.get("pm-final-qc", 0) == 1  # only the second pass reaches QC


# ──────────────────────────────────────────────────────────────────────────────
# Test 7 — final QC fails once, triggers engineer re-plan → 14 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_final_qc_fails_triggers_engineer_replan(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    qc_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: feature")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                qc_call_count["n"] += 1
                # First QC fails (triggers re-plan), second passes
                return _QC_FAIL if qc_call_count["n"] == 1 else _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test7")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test7.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add feature with qc retry"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    # Full pipeline runs twice:
    # Pass 1: eng1, coder1, tester1, security1, db1, ui1, qc1(fail) → re-plan eng2
    # Pass 2: eng2, coder2, tester2, security2, db2, ui2, qc2(pass)
    # Total: 7 + 7 = 14
    expected_total = 14
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 2
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 2
    assert c.get("pm-ui-tester", 0) == 2
    assert c.get("pm-final-qc", 0) == 2

    # Verify QC results: one fail, one pass
    qc_tasks = [t for t in tasks if t.bot_id == "pm-final-qc"]
    statuses = sorted(t.result.get("status") for t in qc_tasks if isinstance(t.result, dict))
    assert statuses == ["fail", "pass"]


# ──────────────────────────────────────────────────────────────────────────────
# Test 8 — UI tester passes (no skip), full pass path → 7 tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pm_workflow_ui_tester_pass_routes_to_final_qc(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: UI feature")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_PASS   # pass, not skip
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test8")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test8.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "add UI feature"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    expected_total = 7
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-final-qc", 0) == 1
    # UI tester should have status=pass in result
    ui_tasks = [t for t in tasks if t.bot_id == "pm-ui-tester"]
    assert len(ui_tasks) == 1
    assert ui_tasks[0].result.get("status") == "pass"


@pytest.mark.anyio
async def test_pm_workflow_mixed_security_pass_and_skip_still_joins_to_db(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    security_call_count = {"n": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: library comparison", "WS2: implementation guide")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                security_call_count["n"] += 1
                return _SECURITY_SKIP if security_call_count["n"] == 1 else _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-test9")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-test9.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "create docs with mixed review applicability"},
        metadata=TaskMetadata(source="chat_assign"),
    )

    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1


@pytest.mark.anyio
async def test_pm_assignment_dynamic_routing_routes_database_workstreams_through_coder_then_joined_db(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result(
                    "Database Schema Migration",
                    "React Frontend Settings Page",
                )
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-dynamic-routing")
    await bot_registry.register(Bot(
        id="pm-orchestrator",
        name="PM Orchestrator",
        role="pm",
        backends=[],
        system_prompt=(
            "Create exactly three research branches by default, then join into one pm-engineer. "
            "Route every implementation workstream through pm-coder first, including database/schema/migration work. "
            "Every coder branch must go through pm-tester and pm-security-reviewer before a single joined "
            "pm-database-engineer, then one pm-ui-tester, then one terminal pm-final-qc."
        ),
    ))
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-dynamic.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "implement the mixed database and frontend workstreams"},
        metadata=TaskMetadata(
            source="bot_trigger",
            orchestration_id="orch-dynamic-routing",
            root_pm_bot_id="pm-orchestrator",
            run_class="pm_assignment",
        ),
    )

    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    db_task = next(task for task in tasks if task.bot_id == "pm-database-engineer")
    coder_tasks = [task for task in tasks if task.bot_id == "pm-coder"]
    ui_task = next(task for task in tasks if task.bot_id == "pm-ui-tester")
    coder_titles = {str(task.payload.get("title") or "") for task in coder_tasks}
    assert "Database Schema Migration" in coder_titles
    assert "React Frontend Settings Page" in coder_titles
    assert db_task.payload.get("join_count") == 2
    assert ui_task.metadata.parent_task_id == db_task.id


@pytest.mark.anyio
async def test_pm_assignment_generic_workstreams_join_to_single_db_ui_and_final_qc(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result(
                    "Backend API Extension",
                    "Webhook Scheduler Trigger",
                )
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-global-stages")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-global-stages.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "implement two generic backend workstreams"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-global-stages",
            run_class="pm_assignment",
        ),
    )

    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1


@pytest.mark.anyio
async def test_pm_assignment_ui_data_issue_retries_db_then_ui_without_duplicate_global_stages(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        def __init__(self):
            self._ui_runs = 0

        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("React Frontend Settings Page")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                self._ui_runs += 1
                if self._ui_runs == 1:
                    return {
                        "status": "fail",
                        "failure_type": "ui_data_issue",
                        "change_summary": "UI needs corrected data",
                        "files_touched": [],
                        "findings": ["ui_data_issue: branch needs database repair"],
                        "evidence": [],
                        "artifacts": [],
                        "risks": [],
                        "handoff_notes": "Fix the underlying data path and validate again.",
                    }
                return _UI_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-ui-repair")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-ui-repair.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "implement the frontend settings page"},
        metadata=TaskMetadata(
            source="bot_trigger",
            orchestration_id="orch-ui-repair",
            root_pm_bot_id="pm-orchestrator",
            run_class="pm_assignment",
        ),
    )

    expected_total = 9
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 1
    assert c.get("pm-tester", 0) == 1
    assert c.get("pm-database-engineer", 0) == 2
    assert c.get("pm-ui-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    ui_tasks = [task for task in tasks if task.bot_id == "pm-ui-tester"]
    db_tasks = [task for task in tasks if task.bot_id == "pm-database-engineer"]
    parent_task_ids = {task.metadata.parent_task_id for task in ui_tasks}
    assert len(db_tasks) == 2
    assert {task.id for task in db_tasks}.issubset(parent_task_ids)


@pytest.mark.anyio
async def test_pm_assignment_security_retry_still_converges_to_db_ui_and_final_qc(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    security_runs = {"count": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("Backend API Extension")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                security_runs["count"] += 1
                return _SECURITY_FAIL if security_runs["count"] == 1 else _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                return _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-dynamic-security-retry")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-dynamic-security-retry.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "implement the backend API extension"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-dynamic-security-retry",
            run_class="pm_assignment",
        ),
    )

    expected_total = 10
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 1
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 1
    assert c.get("pm-ui-tester", 0) == 1
    assert c.get("pm-final-qc", 0) == 1

    security_tasks = [task for task in tasks if task.bot_id == "pm-security-reviewer"]
    assert sorted(str(task.result.get("status") or "") for task in security_tasks if isinstance(task.result, dict)) == [
        "fail",
        "pass",
    ]


@pytest.mark.anyio
async def test_pm_assignment_final_qc_retry_reruns_terminal_stage_once(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    qc_runs = {"count": 0}

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("Backend API Extension")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_PASS
            if task.bot_id == "pm-security-reviewer":
                return _SECURITY_PASS
            if task.bot_id == "pm-database-engineer":
                return _DB_PASS
            if task.bot_id == "pm-ui-tester":
                return _UI_SKIP
            if task.bot_id == "pm-final-qc":
                qc_runs["count"] += 1
                return _QC_FAIL if qc_runs["count"] == 1 else _QC_PASS
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-dynamic-final-qc-retry")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-dynamic-final-qc-retry.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "implement the backend API extension"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-dynamic-final-qc-retry",
            run_class="pm_assignment",
        ),
    )

    expected_total = 14
    tasks = await _wait_for_terminal(tm, "pm-final-qc", expected_total)

    assert len(tasks) == expected_total, f"Expected {expected_total} tasks, got {len(tasks)}: {_counts(tasks)}"
    assert all(t.status == "completed" for t in tasks)

    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 2
    assert c.get("pm-coder", 0) == 2
    assert c.get("pm-tester", 0) == 2
    assert c.get("pm-security-reviewer", 0) == 2
    assert c.get("pm-database-engineer", 0) == 2
    assert c.get("pm-ui-tester", 0) == 2
    assert c.get("pm-final-qc", 0) == 2

    qc_tasks = [task for task in tasks if task.bot_id == "pm-final-qc"]
    assert sorted(str(task.result.get("status") or "") for task in qc_tasks if isinstance(task.result, dict)) == [
        "fail",
        "pass",
    ]


def test_pm_workstream_route_classifier_falls_back_to_keyword_matching_without_root_prompt():
    from control_plane.task_manager.task_manager import TaskManager

    tm = TaskManager(scheduler=object(), db_path=":memory:")

    route = tm._classify_pm_workstream_route(
        {
            "title": "Database Schema Migration",
            "instruction": "Apply the migration and update the SQL schema.",
        },
        default_target_bot_id="pm-coder",
        policy={"consulted_root_system_prompt": False},
    )

    assert route["route_kind"] == "database_coder_branch"
    assert route["target_bot_id"] == "pm-coder"


def test_pm_workstream_route_classifier_preserves_docs_only_lane_even_with_database_keywords():
    from control_plane.task_manager.task_manager import TaskManager

    tm = TaskManager(scheduler=object(), db_path=":memory:")

    route = tm._classify_pm_workstream_route(
        {
            "title": "Database Schema Migration Guide",
            "instruction": "Write only markdown documentation in docs/database and do not edit code.",
            "assignment_scope": {
                "docs_only": True,
                "requested_output_paths": ["docs/database"],
            },
        },
        default_target_bot_id="pm-coder",
        policy={"consulted_root_system_prompt": True},
    )

    assert route["route_kind"] == "generic_coder"
    assert route["target_bot_id"] == "pm-coder"


def test_pm_assignment_workstream_budget_caps_oversized_engineer_plan_and_preserves_key_lanes():
    from control_plane.task_manager.task_manager import TaskManager

    tm = TaskManager(scheduler=object(), db_path=":memory:")

    payloads = [
        {"title": "API submission endpoint", "instruction": "Extend the API controller for presigned URL submission."},
        {"title": "Security scanning layer", "instruction": "Add virus scanning and file validation."},
        {"title": "AI grading worker", "instruction": "Build the background worker and queue polling flow."},
        {"title": "Webhook scheduler trigger", "instruction": "Create webhook and scheduler trigger handling."},
        {"title": "Metrics and alerts", "instruction": "Add monitoring, metrics, and alert coverage."},
        {"title": "Fallback operational logging", "instruction": "Add extra logging and follow-up diagnostics."},
        {"title": "Second generic backend branch", "instruction": "Implement another backend branch for the same feature."},
    ]
    routes = [{"route_kind": "generic_coder"} for _ in payloads]

    trimmed_payloads, trimmed_routes, budget = tm._pm_assignment_workstream_budget(payloads, routes)

    assert len(trimmed_payloads) == 5
    assert len(trimmed_routes) == 5
    assert [payload["title"] for payload in trimmed_payloads] == [
        "API submission endpoint",
        "Security scanning layer",
        "AI grading worker",
        "Webhook scheduler trigger",
        "Metrics and alerts",
    ]
    assert budget["reason"] == "pm_assignment_workstream_cap"
    assert budget["original_count"] == 7
    assert budget["kept_count"] == 5
    assert budget["preserved_lanes"] == ["api", "security", "worker", "trigger", "operations"]


def test_pm_assignment_workstream_budget_preserves_specialist_routes():
    from control_plane.task_manager.task_manager import TaskManager

    tm = TaskManager(scheduler=object(), db_path=":memory:")

    payloads = [
        {"title": "Database migration", "instruction": "Create the migration."},
        {"title": "Backend API extension", "instruction": "Extend the API controller."},
        {"title": "Frontend admin page", "instruction": "Build the Razor admin page."},
        {"title": "Webhook scheduler trigger", "instruction": "Create the callback and scheduler."},
        {"title": "Security scanning layer", "instruction": "Add virus scanning and file validation."},
        {"title": "AI grading worker", "instruction": "Build the worker."},
    ]
    routes = [
        {"route_kind": "database_coder_branch"},
        {"route_kind": "generic_coder"},
        {"route_kind": "ui_coder_validation"},
        {"route_kind": "generic_coder"},
        {"route_kind": "generic_coder"},
        {"route_kind": "generic_coder"},
    ]

    trimmed_payloads, trimmed_routes, budget = tm._pm_assignment_workstream_budget(payloads, routes)

    assert len(trimmed_payloads) == 5
    assert [route["route_kind"] for route in trimmed_routes] == [
        "database_coder_branch",
        "generic_coder",
        "ui_coder_validation",
        "generic_coder",
        "generic_coder",
    ]
    assert [payload["title"] for payload in trimmed_payloads] == [
        "Database migration",
        "Backend API extension",
        "Frontend admin page",
        "Webhook scheduler trigger",
        "Security scanning layer",
    ]
    assert budget["preserved_lanes"][:4] == ["database", "api", "ui", "trigger"]


@pytest.mark.anyio
async def test_pm_assignment_research_fanout_is_capped_to_three_by_default(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-orchestrator":
                steps = []
                for idx, title in enumerate(
                    [
                        "Repository implementation patterns",
                        "Requirements and data context",
                        "External docs and standards",
                        "Additional repo search",
                        "Follow-on code scan",
                        "Extra docs review",
                        "Risk sweep",
                        "Constraint recap",
                    ],
                    start=1,
                ):
                    steps.append(
                        {
                            "id": f"step_1_{idx}",
                            "title": title,
                            "instruction": f"Research {title.lower()}",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        }
                    )
                return {"status": "pass", "steps": steps}
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-research-cap-default")
    await bot_registry.register(
        Bot(
            id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "pm-to-research",
                        "event": "task_completed",
                        "target_bot_id": "pm-research-analyst",
                        "condition": "has_result",
                        "fan_out_field": "source_result.steps",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(id="pm-research-analyst", name="PM Research Analyst", role="researcher", backends=[])
    )
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-research-cap-default.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-orchestrator",
        payload={"instruction": "plan the assignment"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-research-cap-default",
            run_class="pm_assignment",
            root_pm_bot_id="pm-orchestrator",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    c = _counts(tasks)
    assert c.get("pm-orchestrator", 0) == 1
    assert c.get("pm-research-analyst", 0) == 3
    research_tasks = [task for task in tasks if task.bot_id == "pm-research-analyst"]
    assert sorted(str(task.payload.get("title") or "") for task in research_tasks) == sorted([
        "Repository implementation patterns",
        "Requirements and data context",
        "External docs and standards",
    ])
    assert all((task.payload.get("pm_fanout_budget") or {}).get("original_count") == 8 for task in research_tasks)
    await tm.close()


@pytest.mark.anyio
async def test_pm_assignment_research_fanout_allows_sharded_steps_up_to_six(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-orchestrator":
                return {
                    "status": "pass",
                    "steps": [
                        {
                            "id": "step_1_code_part_1",
                            "title": "Repo search part 1",
                            "instruction": "Inspect repo files batch 1",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_code_part_2",
                            "title": "Repo search part 2",
                            "instruction": "Inspect repo files batch 2",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_data_part_1",
                            "title": "Requirements batch 1",
                            "instruction": "Collect requirements chunk 1",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_data_part_2",
                            "title": "Requirements batch 2",
                            "instruction": "Collect requirements chunk 2",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_online_part_1",
                            "title": "External docs part 1",
                            "instruction": "Review current docs segment 1",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_online_part_2",
                            "title": "External docs part 2",
                            "instruction": "Review current docs segment 2",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_1_online_part_3",
                            "title": "External docs part 3",
                            "instruction": "Review current docs segment 3",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                    ],
                }
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-research-cap-split")
    await bot_registry.register(
        Bot(
            id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "pm-to-research",
                        "event": "task_completed",
                        "target_bot_id": "pm-research-analyst",
                        "condition": "has_result",
                        "fan_out_field": "source_result.steps",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(id="pm-research-analyst", name="PM Research Analyst", role="researcher", backends=[])
    )
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-research-cap-split.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-orchestrator",
        payload={"instruction": "plan the assignment"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-research-cap-split",
            run_class="pm_assignment",
            root_pm_bot_id="pm-orchestrator",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    research_tasks = [task for task in tasks if task.bot_id == "pm-research-analyst"]
    assert len(research_tasks) == 6
    assert all((task.payload.get("pm_fanout_budget") or {}).get("split_required") is True for task in research_tasks)
    await tm.close()


@pytest.mark.anyio
async def test_pm_assignment_research_trigger_filters_mixed_plan_steps_to_research_only(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-orchestrator":
                return {
                    "status": "pass",
                    "steps": [
                        {
                            "id": "step_1",
                            "title": "Specification & Gap Analysis",
                            "instruction": "Review the repository structure and produce a concise gap analysis.",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_2",
                            "title": "Database Schema Design & Migration",
                            "instruction": "Create the required schema extensions and migration script.",
                            "bot_id": "pm-database-engineer",
                            "role_hint": "database_engineer",
                            "step_kind": "implementation",
                        },
                        {
                            "id": "step_3",
                            "title": "Backend API Extension",
                            "instruction": "Extend the submission controller and webhook flow.",
                            "bot_id": "pm-engineer",
                            "role_hint": "backend_developer",
                            "step_kind": "implementation",
                        },
                        {
                            "id": "step_4",
                            "title": "Final Quality Check & Sign-off",
                            "instruction": "Run end-to-end verification and record sign-off.",
                            "bot_id": "pm-final-qc",
                            "role_hint": "quality_controller",
                            "step_kind": "validation",
                        },
                    ],
                }
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-research-filter-mixed-steps")
    await bot_registry.register(
        Bot(
            id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "pm-to-research",
                        "event": "task_completed",
                        "target_bot_id": "pm-research-analyst",
                        "condition": "has_result",
                        "fan_out_field": "source_result.steps",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(id="pm-research-analyst", name="PM Research Analyst", role="researcher", backends=[])
    )
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-research-filter-mixed-steps.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-orchestrator",
        payload={"instruction": "plan the assignment"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-research-filter-mixed-steps",
            run_class="pm_assignment",
            root_pm_bot_id="pm-orchestrator",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    research_tasks = [task for task in tasks if task.bot_id == "pm-research-analyst"]
    assert len(research_tasks) == 1
    assert str(research_tasks[0].payload.get("title") or "") == "Specification & Gap Analysis"
    budget = research_tasks[0].payload.get("pm_fanout_budget") or {}
    assert budget.get("reason") == "pm_assignment_research_trigger_filter"
    assert budget.get("original_count") == 4
    assert budget.get("kept_count") == 1
    await tm.close()


@pytest.mark.anyio
async def test_pm_assignment_research_trigger_filters_orchestration_finalization_even_if_labeled_research(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-orchestrator":
                return {
                    "status": "pass",
                    "steps": [
                        {
                            "id": "step_1",
                            "title": "Repository implementation patterns",
                            "instruction": "Review the repository structure and implementation patterns.",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_2",
                            "title": "Requirements and data context",
                            "instruction": "Collect requirements and data assumptions.",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "analysis",
                        },
                        {
                            "id": "step_3",
                            "title": "External docs and standards",
                            "instruction": "Review external docs and current standards.",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        },
                        {
                            "id": "step_4",
                            "title": "Final quality check and sign-off",
                            "instruction": "Perform orchestration finalization and sign-off.",
                            "bot_id": "pm-research-analyst",
                            "role_hint": "researcher",
                            "step_kind": "orchestration_finalization",
                        },
                    ],
                }
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-research-filter-finalization")
    await bot_registry.register(
        Bot(
            id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "pm-to-research",
                        "event": "task_completed",
                        "target_bot_id": "pm-research-analyst",
                        "condition": "has_result",
                        "fan_out_field": "source_result.steps",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(id="pm-research-analyst", name="PM Research Analyst", role="researcher", backends=[])
    )
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-research-filter-finalization.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-orchestrator",
        payload={"instruction": "plan the assignment"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-research-filter-finalization",
            run_class="pm_assignment",
            root_pm_bot_id="pm-orchestrator",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    research_tasks = [task for task in tasks if task.bot_id == "pm-research-analyst"]
    assert len(research_tasks) == 3
    assert sorted(str(task.payload.get("title") or "") for task in research_tasks) == sorted([
        "External docs and standards",
        "Repository implementation patterns",
        "Requirements and data context",
    ])
    budget = research_tasks[0].payload.get("pm_fanout_budget") or {}
    assert budget.get("reason") == "pm_assignment_research_trigger_filter"
    assert budget.get("original_count") == 4
    assert budget.get("kept_count") == 3
    await tm.close()


@pytest.mark.anyio
async def test_pm_assignment_research_fanout_cap_applies_when_steps_use_role_hints_without_bot_ids(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-orchestrator":
                steps = []
                for idx, title in enumerate(
                    [
                        "Repository implementation patterns",
                        "Requirements and data context",
                        "External docs and standards",
                        "Additional repo search",
                        "Follow-on code scan",
                        "Extra docs review",
                    ],
                    start=1,
                ):
                    steps.append(
                        {
                            "id": f"step_1_{idx}",
                            "title": title,
                            "instruction": f"Research {title.lower()}",
                            "role_hint": "researcher",
                            "step_kind": "specification",
                        }
                    )
                return {"status": "pass", "steps": steps}
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-research-cap-role-hints")
    await bot_registry.register(
        Bot(
            id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "pm-to-research",
                        "event": "task_completed",
                        "target_bot_id": "pm-research-analyst",
                        "condition": "has_result",
                        "fan_out_field": "source_result.steps",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(id="pm-research-analyst", name="PM Research Analyst", role="researcher", backends=[])
    )
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-research-cap-role-hints.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-orchestrator",
        payload={"instruction": "plan the assignment"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-research-cap-role-hints",
            run_class="pm_assignment",
            root_pm_bot_id="pm-orchestrator",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    research_tasks = [task for task in tasks if task.bot_id == "pm-research-analyst"]
    assert len(research_tasks) == 3
    assert all((task.payload.get("pm_fanout_budget") or {}).get("original_count") == 6 for task in research_tasks)
    await tm.close()


@pytest.mark.anyio
async def test_pm_assignment_loop_guard_stops_retargeting_same_bot_forever(tmp_path, monkeypatch):
    import control_plane.task_manager.task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    original_settings_int = task_manager_module._settings_int

    def _settings_int(name: str, default: int) -> int:
        if name == "workflow_route_repeat_limit":
            return 3
        return original_settings_int(name, default)

    monkeypatch.setattr(task_manager_module, "_settings_int", _settings_int)

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "pm-engineer":
                return _engineer_result("WS1: looping branch")
            if task.bot_id == "pm-coder":
                return _CODER_PASS
            if task.bot_id == "pm-tester":
                return _TESTER_FAIL
            return {"status": "pass"}

    bot_registry = await _make_bot_registry(tmp_path, "-repeat-guard")
    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pm-routing-repeat-guard.db"),
        bot_registry=bot_registry,
    )

    await tm.create_task(
        bot_id="pm-engineer",
        payload={"instruction": "keep retrying forever"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="orch-repeat-guard",
            run_class="pm_assignment",
        ),
    )

    tasks = await _wait_for_quiescent(tm)
    c = _counts(tasks)
    assert c.get("pm-engineer", 0) == 2
    assert c.get("pm-coder", 0) == 6
    assert c.get("pm-tester", 0) == 6
    assert c.get("pm-security-reviewer", 0) == 0
    assert c.get("pm-final-qc", 0) == 0
    await tm.close()
