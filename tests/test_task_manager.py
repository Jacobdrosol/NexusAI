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
