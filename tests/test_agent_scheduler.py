from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from control_plane.agent_scheduler.engine import AgentScheduleEngine


class _FakeTaskManager:
    def __init__(self, *, status: str = "queued", error_message: str | None = None, create_failures: int = 0) -> None:
        self.task = SimpleNamespace(
            id="scheduled-task-1",
            status=status,
            error=SimpleNamespace(message=error_message) if error_message else None,
        )
        self.create_calls = []
        self.create_failures = create_failures

    async def create_task(self, **kwargs):
        self.create_calls.append(kwargs)
        if len(self.create_calls) <= self.create_failures:
            raise RuntimeError("task persistence unavailable")
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


def test_schedule_payload_validation_normalizes_without_persisting() -> None:
    payload = _schedule_payload()
    payload.update(
        {
            "status": "paused",
            "project_id": "acme",
            "task_payload": {"scope": "read_only"},
            "metadata": {"mutation_safe": True},
        }
    )

    normalized = AgentScheduleEngine.validate_schedule_payload(payload)

    assert normalized["status"] == "paused"
    assert normalized["project_id"] == "acme"
    assert normalized["task_payload"] == {"scope": "read_only"}
    assert normalized["metadata"]["mutation_safe"] is True
    assert normalized["metadata"]["task_payload"] == {"scope": "read_only"}
    assert normalized["next_run_at"]


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
async def test_schedule_run_records_pipeline_orchestration_from_created_task(tmp_path):
    task_manager = _FakeTaskManager()
    task_manager.task.metadata = SimpleNamespace(orchestration_id="scheduled-course-pipeline")
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "pipeline-schedules.db"),
    )
    schedule = await engine.create_schedule(_schedule_payload())

    await engine.trigger_schedule(schedule["id"])

    run = (await engine.list_runs(schedule["id"]))[0]
    assert run["status"] == "running"
    assert run["task_id"] == task_manager.task.id
    assert run["orchestration_id"] == "scheduled-course-pipeline"


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
async def test_schedule_dispatch_merges_structured_task_payload(tmp_path):
    task_manager = _FakeTaskManager()
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule(
        {
            **_schedule_payload(),
            "task_payload": {"artifact": "draft lesson", "acceptance_criteria": "No factual errors", "instruction": "ignored"},
        }
    )

    await engine.trigger_schedule(schedule["id"])

    payload = task_manager.create_calls[0]["payload"]
    assert payload["artifact"] == "draft lesson"
    assert payload["acceptance_criteria"] == "No factual errors"
    assert payload["instruction"] == "Reply with exactly OK."


@pytest.mark.anyio
async def test_schedule_dispatch_materializes_bounded_system_payload(tmp_path):
    task_manager = _FakeTaskManager()

    async def materialize(schedule):
        assert schedule["id"]
        return {"monitoring_events": "sanitized fleet snapshot"}

    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
        payload_materializer=materialize,
    )
    schedule = await engine.create_schedule(
        {
            **_schedule_payload(),
            "task_payload": {"monitoring_events": "dispatch placeholder"},
        }
    )

    await engine.trigger_schedule(schedule["id"])

    payload = task_manager.create_calls[0]["payload"]
    assert payload["monitoring_events"] == "sanitized fleet snapshot"
    assert payload["source"] == "agent_schedule"


@pytest.mark.anyio
async def test_schedule_preview_materializes_payload_without_creating_a_task_or_run(tmp_path):
    task_manager = _FakeTaskManager()

    async def materialize(schedule):
        assert schedule["id"]
        return {"revision_items": "sanitized CSV snapshot"}

    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
        payload_materializer=materialize,
    )
    schedule = await engine.create_schedule(
        {**_schedule_payload(), "task_payload": {"artifact": "draft only"}}
    )

    preview = await engine.preview_schedule_payload(schedule["id"])

    assert preview["schedule"]["id"] == schedule["id"]
    assert preview["task_payload"] == {
        "artifact": "draft only",
        "revision_items": "sanitized CSV snapshot",
    }
    assert task_manager.create_calls == []
    assert await engine.list_runs(schedule["id"]) == []


@pytest.mark.anyio
async def test_schedule_retries_only_a_failed_pre_dispatch_attempt(tmp_path, monkeypatch):
    import control_plane.agent_scheduler.engine as scheduler_module

    started_at = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    current_time = started_at
    monkeypatch.setattr(scheduler_module, "_now", lambda: current_time)
    task_manager = _FakeTaskManager(create_failures=1)
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule(
        {
            **_schedule_payload(),
            "status": "active",
            "retry_max": 1,
            "retry_backoff_seconds": 30,
        }
    )

    await engine.trigger_schedule(schedule["id"])
    failed_run = (await engine.list_runs(schedule["id"]))[0]
    assert failed_run["status"] == "failed"
    assert failed_run["task_id"] is None
    assert failed_run["retry_not_before"] == "2026-07-19T12:00:30+00:00"

    current_time = started_at + timedelta(seconds=29)
    monkeypatch.setattr(scheduler_module, "_now", lambda: current_time)
    await engine.tick_once()
    assert len(task_manager.create_calls) == 1

    current_time = started_at + timedelta(seconds=30)
    monkeypatch.setattr(scheduler_module, "_now", lambda: current_time)
    await engine.tick_once()

    retried_run = (await engine.list_runs(schedule["id"]))[0]
    assert len(task_manager.create_calls) == 2
    assert retried_run["status"] == "running"
    assert retried_run["attempt"] == 1
    assert retried_run["task_id"] == task_manager.task.id


@pytest.mark.anyio
async def test_schedule_run_schema_migrates_existing_run_history(tmp_path):
    db_path = tmp_path / "schedules.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE agent_schedule_runs (
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                scheduled_for TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                status TEXT NOT NULL, orchestration_id TEXT, task_id TEXT,
                error_json TEXT, attempt INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            )
            """
        )

    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(db_path),
    )
    await engine._ensure_db()

    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(agent_schedule_runs)")}
    assert "retry_not_before" in columns
    assert "manual" in columns


@pytest.mark.anyio
async def test_schedule_retry_policy_is_bounded_for_creates_and_updates(tmp_path):
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(tmp_path / "schedules.db"),
    )

    with pytest.raises(ValueError, match="retry_max"):
        await engine.create_schedule({**_schedule_payload(), "retry_max": 6})
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        await engine.create_schedule({**_schedule_payload(), "retry_backoff_seconds": 4})

    schedule = await engine.create_schedule(_schedule_payload())
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        await engine.update_schedule(schedule["id"], {"retry_backoff_seconds": 3601})


@pytest.mark.anyio
async def test_schedule_forbids_overlapping_runs_by_default(tmp_path, monkeypatch):
    import control_plane.agent_scheduler.engine as scheduler_module

    started_at = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "_now", lambda: started_at)
    task_manager = _FakeTaskManager()
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule(_schedule_payload())

    first_run = await engine.trigger_schedule(schedule["id"])
    monkeypatch.setattr(scheduler_module, "_now", lambda: started_at + timedelta(seconds=1))
    skipped_run = await engine.trigger_schedule(schedule["id"])

    assert schedule["overlap_policy"] == "forbid"
    assert first_run["status"] == "queued"
    assert skipped_run["status"] == "skipped"
    assert skipped_run["finished_at"] == "2026-07-19T12:00:01+00:00"
    assert skipped_run["error"] == {
        "reason": "overlap_prevented",
        "message": "Skipped because a previous run for this schedule is still active.",
        "active_run_id": first_run["id"],
    }
    assert len(task_manager.create_calls) == 1
    assert (await engine.get_schedule(schedule["id"]))["last_run_status"] == "skipped"


@pytest.mark.anyio
async def test_schedule_can_explicitly_allow_overlapping_runs(tmp_path, monkeypatch):
    import control_plane.agent_scheduler.engine as scheduler_module

    started_at = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "_now", lambda: started_at)
    task_manager = _FakeTaskManager()
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule({**_schedule_payload(), "overlap_policy": "allow"})

    await engine.trigger_schedule(schedule["id"])
    monkeypatch.setattr(scheduler_module, "_now", lambda: started_at + timedelta(seconds=1))
    second_run = await engine.trigger_schedule(schedule["id"])

    assert (await engine.get_schedule(schedule["id"]))["overlap_policy"] == "allow"
    assert second_run["status"] == "queued"
    assert len(task_manager.create_calls) == 2


@pytest.mark.anyio
async def test_schedule_rejects_invalid_overlap_policy(tmp_path):
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(tmp_path / "schedules.db"),
    )

    with pytest.raises(ValueError, match="overlap_policy"):
        await engine.create_schedule({**_schedule_payload(), "overlap_policy": "queue"})

    schedule = await engine.create_schedule(_schedule_payload())
    updated = await engine.update_schedule(schedule["id"], {"overlap_policy": "allow"})
    assert updated is not None
    assert updated["overlap_policy"] == "allow"
    with pytest.raises(ValueError, match="overlap_policy"):
        await engine.update_schedule(schedule["id"], {"overlap_policy": "queue"})


@pytest.mark.anyio
async def test_schedule_tick_uses_cron_window_and_dispatches_it_once(tmp_path, monkeypatch):
    import control_plane.agent_scheduler.engine as scheduler_module

    task_manager = _FakeTaskManager()
    db_path = str(tmp_path / "schedules.db")
    created_at = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "_now", lambda: created_at)
    first_engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=db_path,
    )
    schedule = await first_engine.create_schedule(
        {**_schedule_payload(), "cron_expression": "*/5 * * * *", "status": "active"}
    )
    expected_window = "2026-07-19T12:05:00+00:00"
    assert schedule["next_run_at"] == expected_window

    monkeypatch.setattr(scheduler_module, "_now", lambda: created_at + timedelta(minutes=5, seconds=1))
    second_engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=db_path,
    )

    first_run, first_created = await first_engine._create_run(
        schedule,
        scheduled_for=expected_window,
        manual=False,
    )
    duplicate_run, duplicate_created = await second_engine._create_run(
        schedule,
        scheduled_for=expected_window,
        manual=False,
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate_run["id"] == first_run["id"]

    await first_engine._dispatch_run(schedule, first_run)
    await first_engine.tick_once()
    runs = await first_engine.list_runs(schedule["id"])
    assert len(runs) == 1
    assert runs[0]["scheduled_for"] == expected_window
    assert runs[0]["manual"] is False
    assert len(task_manager.create_calls) == 1


@pytest.mark.anyio
async def test_schedule_run_persists_manual_origin(tmp_path):
    task_manager = _FakeTaskManager()
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=task_manager,
        db_path=str(tmp_path / "schedules.db"),
    )
    schedule = await engine.create_schedule({**_schedule_payload(), "overlap_policy": "allow"})

    manual_run = await engine.trigger_schedule(schedule["id"])
    scheduled_for = (datetime.fromisoformat(manual_run["scheduled_for"]) + timedelta(seconds=1)).isoformat()
    scheduled_run, scheduled_created = await engine._create_run(
        schedule,
        scheduled_for=scheduled_for,
        manual=False,
    )

    assert manual_run["manual"] is True
    assert scheduled_created is True
    assert scheduled_run["manual"] is False
    persisted_by_id = {
        run["id"]: run
        for run in await engine.list_runs(schedule["id"])
    }
    assert persisted_by_id[manual_run["id"]]["manual"] is True
    assert persisted_by_id[scheduled_run["id"]]["manual"] is False


@pytest.mark.anyio
async def test_schedule_tick_prunes_only_excess_terminal_history(tmp_path, monkeypatch):
    import control_plane.agent_scheduler.engine as scheduler_module

    started_at = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    current_time = started_at
    monkeypatch.setattr(scheduler_module, "_now", lambda: current_time)
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(tmp_path / "schedules.db"),
        terminal_run_retention_per_schedule=2,
        terminal_run_prune_batch_size=10,
    )
    schedule = await engine.create_schedule({**_schedule_payload(), "overlap_policy": "allow"})

    terminal_run_ids = []
    for offset in range(4):
        current_time = started_at + timedelta(minutes=offset + 1)
        run, created = await engine._create_run(
            schedule,
            scheduled_for=(started_at + timedelta(minutes=offset + 1)).isoformat(),
            manual=False,
        )
        assert created is True
        terminal_run_ids.append(run["id"])
        await engine._set_run_status(run["id"], "completed", finished_at=current_time.isoformat())

    current_time = started_at + timedelta(minutes=6)
    active_run, created = await engine._create_run(
        schedule,
        scheduled_for=current_time.isoformat(),
        manual=False,
    )
    assert created is True
    assert active_run["status"] == "queued"

    await engine.tick_once()

    persisted = await engine.list_runs(schedule["id"], limit=20)
    persisted_by_id = {run["id"]: run for run in persisted}
    assert set(persisted_by_id) == set(terminal_run_ids[-2:]) | {active_run["id"]}
    assert persisted_by_id[active_run["id"]]["status"] == "queued"


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


@pytest.mark.anyio
async def test_scheduler_rejects_equivalent_schedule_creates_and_updates(tmp_path):
    engine = AgentScheduleEngine(
        assignment_service=object(),
        task_manager=_FakeTaskManager(),
        db_path=str(tmp_path / "schedules.db"),
    )
    first = await engine.create_schedule({**_schedule_payload(), "project_id": "acme"})

    with pytest.raises(ValueError, match="schedule_duplicate_exists"):
        await engine.create_schedule(
            {**_schedule_payload(), "name": "Renamed duplicate", "status": "active", "project_id": "acme"}
        )

    second = await engine.create_schedule(
        {**_schedule_payload(), "name": "Different cadence", "cron_expression": "30 * * * *", "project_id": "acme"}
    )
    duplicate = await engine.find_equivalent_schedule({**_schedule_payload(), "project_id": "acme"})
    assert duplicate == {"schedule_id": first["id"], "status": "paused"}

    with pytest.raises(ValueError, match="schedule_duplicate_exists"):
        await engine.update_schedule(second["id"], {"cron_expression": "0 * * * *"})
