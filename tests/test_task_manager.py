"""Unit tests for TaskManager."""
import pytest
from unittest.mock import AsyncMock


@pytest.mark.anyio
async def test_create_and_get_task():
    from control_plane.task_manager.task_manager import TaskManager
    mock_scheduler = AsyncMock()
    mock_scheduler.schedule.return_value = {"answer": "42"}
    tm = TaskManager(mock_scheduler)
    task = await tm.create_task(bot_id="bot1", payload={"q": "hello"})
    assert task.bot_id == "bot1"
    assert task.status == "queued"


@pytest.mark.anyio
async def test_task_runs_and_completes():
    import asyncio
    from control_plane.task_manager.task_manager import TaskManager
    mock_scheduler = AsyncMock()
    mock_scheduler.schedule.return_value = {"answer": "42"}
    tm = TaskManager(mock_scheduler)
    task = await tm.create_task(bot_id="bot1", payload={"q": "hello"})
    # Poll until task completes (up to 2 seconds)
    for _ in range(20):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)
    assert updated.status == "completed"
    assert updated.result == {"answer": "42"}


@pytest.mark.anyio
async def test_task_fails_on_scheduler_error():
    import asyncio
    from control_plane.task_manager.task_manager import TaskManager
    from shared.exceptions import NoViableBackendError
    mock_scheduler = AsyncMock()
    mock_scheduler.schedule.side_effect = NoViableBackendError("no backends")
    tm = TaskManager(mock_scheduler)
    task = await tm.create_task(bot_id="bot1", payload={})
    # Poll until task fails (up to 2 seconds)
    for _ in range(20):
        updated = await tm.get_task(task.id)
        if updated.status == "failed":
            break
        await asyncio.sleep(0.1)
    assert updated.status == "failed"
    assert updated.error is not None


@pytest.mark.anyio
async def test_dependent_task_unblocks_after_dependency_completes():
    import asyncio
    from control_plane.task_manager.task_manager import TaskManager

    async def slow_then_fast_schedule(task):
        if task.payload.get("kind") == "root":
            await asyncio.sleep(0.2)
        return {"ok": task.payload.get("kind")}

    class StubScheduler:
        async def schedule(self, task):
            return await slow_then_fast_schedule(task)

    tm = TaskManager(StubScheduler())

    root = await tm.create_task(bot_id="bot1", payload={"kind": "root"})
    dependent = await tm.create_task(
        bot_id="bot1",
        payload={"kind": "child"},
        depends_on=[root.id],
    )

    dep_initial = await tm.get_task(dependent.id)
    assert dep_initial.status == "blocked"

    for _ in range(40):
        dep_latest = await tm.get_task(dependent.id)
        if dep_latest.status == "completed":
            break
        await asyncio.sleep(0.1)

    assert dep_latest.status == "completed"
    assert dep_latest.result == {"ok": "child"}


@pytest.mark.anyio
async def test_bot_trigger_creates_follow_on_run_and_artifacts(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "answer": f"done:{task.bot_id}",
                "artifacts": [{"label": "summary.txt", "path": "runs/summary.txt", "content": "ok"}],
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    await bot_registry.register(Bot(id="bot-a", name="Bot A", role="assistant", backends=[], workflow={
        "triggers": [
            {
                "id": "handoff",
                "event": "task_completed",
                "target_bot_id": "bot-b",
                "condition": "has_result",
            }
        ]
    }))
    await bot_registry.register(Bot(id="bot-b", name="Bot B", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="bot-a", payload={"instruction": "start"})

    for _ in range(40):
        tasks = await tm.list_tasks()
        if len(tasks) >= 2 and all(t.status == "completed" for t in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    assert len(tasks) == 2
    triggered = next(t for t in tasks if t.id != root.id)
    assert triggered.bot_id == "bot-b"
    assert triggered.metadata is not None
    assert triggered.metadata.parent_task_id == root.id
    assert triggered.metadata.trigger_rule_id == "handoff"

    runs = await tm.list_bot_runs("bot-a")
    assert len(runs) == 1
    assert runs[0].status == "completed"

    artifacts = await tm.list_bot_run_artifacts("bot-a")
    labels = {artifact.label for artifact in artifacts}
    assert "Task Payload" in labels
    assert "Task Result" in labels
    assert "Run Report" in labels
    assert "summary.txt" in labels


@pytest.mark.anyio
async def test_qc_bot_can_route_failures_back_to_source_bot(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "worker-bot":
                return {"answer": "draft ready"}
            if task.bot_id == "qc-bot":
                return {"qc_status": "fail", "issues": ["missing citation"]}
            return {"answer": f"redo:{task.bot_id}"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "bots-qc.db"))
    await bot_registry.register(Bot(id="worker-bot", name="Worker", role="assistant", backends=[], workflow={
        "triggers": [
            {
                "id": "send-to-qc",
                "event": "task_completed",
                "target_bot_id": "qc-bot",
                "condition": "has_result",
            }
        ]
    }))
    await bot_registry.register(Bot(id="qc-bot", name="QC", role="quality", backends=[], workflow={
        "triggers": [
            {
                "id": "return-for-fix",
                "event": "task_completed",
                "target_bot_id": "{{source_bot_id}}",
                "condition": "has_result",
                "result_field": "qc_status",
                "result_equals": "fail",
            }
        ]
    }))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "tasks-qc.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="worker-bot", payload={"instruction": "write draft"})

    for _ in range(50):
        tasks = await tm.list_tasks()
        if len(tasks) >= 3 and all(t.status == "completed" for t in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    assert len(tasks) >= 3
    qc_tasks = [t for t in tasks if t.bot_id == "qc-bot"]
    assert qc_tasks
    qc_task = qc_tasks[0]
    retry_task = next(t for t in tasks if t.id not in {root.id, qc_task.id} and t.bot_id == "worker-bot")
    assert retry_task.bot_id == "worker-bot"
    assert retry_task.metadata is not None
    assert retry_task.metadata.parent_task_id == qc_task.id
    assert retry_task.metadata.trigger_rule_id == "return-for-fix"
