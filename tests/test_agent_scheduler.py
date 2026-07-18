from types import SimpleNamespace

import pytest

from control_plane.agent_scheduler.engine import AgentScheduleEngine


class _FakeTaskManager:
    def __init__(self, *, status: str = "queued", error_message: str | None = None) -> None:
        self.task = SimpleNamespace(
            id="scheduled-task-1",
            status=status,
            error=SimpleNamespace(message=error_message) if error_message else None,
        )
        self.create_calls = []

    async def create_task(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.task

    async def get_task(self, task_id: str):
        assert task_id == self.task.id
        return self.task


def _schedule_payload() -> dict:
    return {
        "name": "Task completion tracking test",
        "cron_expression": "0 * * * *",
        "timezone": "UTC",
        "prompt": "Reply with exactly OK.",
        "target_bot_id": "qc-bot",
    }


@pytest.mark.anyio
async def test_schedule_run_stays_running_until_linked_task_completes(tmp_path):
    task_manager = _FakeTaskManager()
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule(_schedule_payload())

    await engine.trigger_schedule(schedule["id"])
    running_run = (await engine.list_runs(schedule["id"]))[0]

    assert running_run["status"] == "running"
    assert running_run["task_id"] == task_manager.task.id
    assert (await engine.get_schedule(schedule["id"]))["last_run_status"] == "running"
    assert len(task_manager.create_calls) == 1
    create_call = task_manager.create_calls[0]
    assert create_call["bot_id"] == "qc-bot"
    assert create_call["payload"] == {
        "instruction": "Reply with exactly OK.",
        "source": "agent_schedule",
        "schedule_id": schedule["id"],
        "project_id": None,
        "node_overrides": {},
    }
    assert create_call["metadata"].source == "agent_schedule"

    task_manager.task.status = "completed"
    await engine.tick_once()

    completed_run = (await engine.list_runs(schedule["id"]))[0]
    assert completed_run["status"] == "completed"
    assert completed_run["finished_at"] is not None
    assert (await engine.get_schedule(schedule["id"]))["last_run_status"] == "completed"


@pytest.mark.anyio
async def test_schedule_run_records_linked_task_failure(tmp_path):
    task_manager = _FakeTaskManager(status="failed", error_message="worker backend timed out")
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule(_schedule_payload())

    await engine.trigger_schedule(schedule["id"])
    await engine.tick_once()

    failed_run = (await engine.list_runs(schedule["id"]))[0]
    assert failed_run["status"] == "failed"
    assert failed_run["finished_at"] is not None
    assert failed_run["error"] == {
        "message": "worker backend timed out",
        "task_status": "failed",
    }
    assert (await engine.get_schedule(schedule["id"]))["last_run_status"] == "failed"


@pytest.mark.anyio
async def test_schedule_dispatch_guard_blocks_unsafe_run(tmp_path):
    task_manager = _FakeTaskManager()

    async def reject_unattested_schedule(schedule):
        raise ValueError(f"schedule {schedule['id']} is not attested")

    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
        autonomy_guard=reject_unattested_schedule,
    )
    schedule = await engine.create_schedule(_schedule_payload())

    await engine.trigger_schedule(schedule["id"])

    failed_run = (await engine.list_runs(schedule["id"]))[0]
    assert failed_run["status"] == "failed"
    assert "not attested" in failed_run["error"]["message"]
    assert task_manager.create_calls == []


@pytest.mark.anyio
async def test_schedule_listing_filters_status_and_rejects_invalid_status(tmp_path):
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(tmp_path / "schedules.db"),
    )
    paused = await engine.create_schedule(_schedule_payload())
    active = await engine.create_schedule(
        {
            **_schedule_payload(),
            "name": "Active schedule",
            "status": "active",
            "target_bot_id": "writer-bot",
        }
    )

    active_rows = await engine.list_schedules(status="active")
    all_rows = await engine.list_schedules(target_bot_id="qc-bot")

    assert [row["id"] for row in active_rows] == [active["id"]]
    assert [row["id"] for row in all_rows] == [paused["id"]]
    assert paused["status"] == "paused"
    with pytest.raises(ValueError, match="schedule status"):
        await engine.list_schedules(status="disabled")
    with pytest.raises(ValueError, match="exactly one dispatch target"):
        await engine.update_schedule(paused["id"], {"assignment_pm_bot_id": "pm-bot", "conversation_id": "thread-1"})
    with pytest.raises(ValueError, match="exactly one dispatch target"):
        await engine.update_schedule(paused["id"], {"target_bot_id": ""})
