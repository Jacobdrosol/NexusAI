"""Unit tests for TaskManager."""
import json
from typing import Any, Dict, List, Set

import pytest
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
async def _close_task_managers_after_each_test(monkeypatch):
    from control_plane.task_manager.task_manager import TaskManager

    created: List[TaskManager] = []
    original_init = TaskManager.__init__

    def _tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(TaskManager, "__init__", _tracked_init)
    yield
    for manager in reversed(created):
        await manager.close()


def test_lookup_payload_path_supports_list_indexes():
    from control_plane.task_manager.task_manager import _lookup_payload_path

    payload = {
        "approved_units": [
            {
                "source_payload": {
                    "unit_blueprint": {
                        "unit_number": 1,
                        "title": "Unit 1",
                    }
                }
            }
        ]
    }

    assert _lookup_payload_path(payload, "approved_units.0.source_payload.unit_blueprint.unit_number") == 1
    assert _lookup_payload_path(payload, "approved_units.0.source_payload.unit_blueprint.title") == "Unit 1"
    assert _lookup_payload_path(payload, "approved_units.1.source_payload.unit_blueprint.title") is None


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
async def test_task_manager_respects_max_concurrency(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager.task_manager import TaskManager

    active = 0
    peak = 0

    class StubScheduler:
        async def schedule(self, task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {"task": task.id}

    monkeypatch.setenv("NEXUSAI_TASK_MAX_CONCURRENCY", "2")
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "queue.db"))
    for idx in range(5):
        await tm.create_task(bot_id="bot1", payload={"i": idx})

    for _ in range(80):
        tasks = await tm.list_tasks()
        if len(tasks) == 5 and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.05)

    tasks = await tm.list_tasks()
    assert len(tasks) == 5
    assert all(task.status == "completed" for task in tasks)
    assert peak <= 2


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
async def test_task_manager_auto_retries_transient_errors(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager

    attempts = 0

    class StubScheduler:
        async def schedule(self, task):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Internal Server Error")
            return {"ok": True, "attempts": attempts}

    monkeypatch.setenv("NEXUSAI_TASK_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(task_manager_module, "_settings_int", lambda name, default: 1 if name == "max_task_retries" else default)
    monkeypatch.setattr(task_manager_module, "_settings_float", lambda name, default: 0.01 if name == "task_retry_delay" else default)
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "retry.db"))
    task = await tm.create_task(bot_id="bot1", payload={"q": "hello"})

    for _ in range(80):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"
    assert updated.result["ok"] is True
    assert updated.metadata is not None
    assert updated.metadata.retry_attempt == 1


@pytest.mark.anyio
async def test_task_manager_auto_retries_invalid_structured_output(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager

    attempts = 0

    class StubScheduler:
        async def schedule(self, task):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("no valid JSON object or array found")
            return {"ok": True, "attempts": attempts}

    monkeypatch.setenv("NEXUSAI_TASK_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(task_manager_module, "_settings_int", lambda name, default: 1 if name == "max_task_retries" else default)
    monkeypatch.setattr(task_manager_module, "_settings_float", lambda name, default: 0.01 if name == "task_retry_delay" else default)
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "retry-json.db"))
    task = await tm.create_task(bot_id="bot1", payload={"q": "hello"})

    for _ in range(80):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"
    assert updated.result["ok"] is True
    assert updated.metadata is not None
    assert updated.metadata.retry_attempt == 1


@pytest.mark.anyio
async def test_manual_retry_creates_new_task_with_override(tmp_path):
    import asyncio

    from control_plane.task_manager.task_manager import TaskManager

    class StubScheduler:
        async def schedule(self, task):
            raise RuntimeError("simulated failure")

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "manual-retry.db"))
    original = await tm.create_task(
        bot_id="bot1",
        payload={"q": "hello"},
        metadata=None,
    )
    for _ in range(40):
        current = await tm.get_task(original.id)
        if current.status == "failed":
            break
        await asyncio.sleep(0.1)

    retried = await tm.retry_task(original.id, payload_override={"q": "fixed"})

    assert retried.id != original.id
    assert retried.bot_id == original.bot_id
    assert retried.payload == {"q": "fixed"}
    assert retried.metadata is not None
    assert retried.metadata.retry_attempt == 1
    assert retried.metadata.original_task_id == original.id
    assert retried.metadata.retry_of_task_id == original.id

    original_after_retry = await tm.get_task(original.id)
    assert original_after_retry.status == "retried"
    assert original_after_retry.metadata is not None
    assert original_after_retry.metadata.retried_by_task_id == retried.id
    assert original_after_retry.error is not None
    assert original_after_retry.error.code == "retried"
    assert isinstance(original_after_retry.error.details, dict)
    assert original_after_retry.error.details.get("retried_by_task_id") == retried.id


@pytest.mark.anyio
async def test_cancel_running_task_marks_cancelled(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager.task_manager import TaskManager

    class StubScheduler:
        async def schedule(self, task):
            await asyncio.sleep(1)
            return {"ok": True}

    monkeypatch.setenv("NEXUSAI_TASK_MAX_CONCURRENCY", "1")
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "cancel-running.db"))
    task = await tm.create_task(bot_id="bot1", payload={"q": "hello"})

    for _ in range(20):
        updated = await tm.get_task(task.id)
        if updated.status == "running":
            break
        await asyncio.sleep(0.05)

    cancelled = await tm.cancel_task(task.id)
    assert cancelled.status in {"running", "cancelled"}

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "cancelled":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "cancelled"
    assert updated.error is not None
    assert updated.error.code == "cancelled"


@pytest.mark.anyio
async def test_cancel_queued_task_marks_cancelled(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager.task_manager import TaskManager

    class StubScheduler:
        async def schedule(self, task):
            await asyncio.sleep(0.5)
            return {"ok": task.id}

    monkeypatch.setenv("NEXUSAI_TASK_MAX_CONCURRENCY", "1")
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "cancel-queued.db"))
    first = await tm.create_task(bot_id="bot1", payload={"i": 1})
    second = await tm.create_task(bot_id="bot1", payload={"i": 2})

    for _ in range(20):
        updated_first = await tm.get_task(first.id)
        updated_second = await tm.get_task(second.id)
        if updated_first.status == "running" and updated_second.status == "queued":
            break
        await asyncio.sleep(0.05)

    cancelled = await tm.cancel_task(second.id)
    assert cancelled.status == "cancelled"


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
async def test_create_task_rejects_payloads_that_violate_input_contract(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "input-contract-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["course_brief", "generation_settings"],
                    "non_empty_fields": ["course_brief.topic"],
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "input-contract-tasks.db"), bot_registry=bot_registry)

    with pytest.raises(ValueError, match="missing required fields: generation_settings"):
        await tm.create_task(
            bot_id="outline-bot",
            payload={"course_brief": {"topic": "AP World History"}},
        )


@pytest.mark.anyio
async def test_create_task_rejects_empty_required_input_fields(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "input-contract-empty-bots.db"))
    await bot_registry.register(
        Bot(
            id="lesson-bot",
            name="Lesson",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["lesson"],
                    "non_empty_fields": ["lesson.title", "lesson.blocks"],
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "input-contract-empty.db"), bot_registry=bot_registry)

    with pytest.raises(ValueError, match="non-empty fields: lesson.title, lesson.blocks"):
        await tm.create_task(
            bot_id="lesson-bot",
            payload={"lesson": {"title": "", "blocks": []}},
        )


@pytest.mark.anyio
async def test_payload_transform_bots_defer_required_input_validation_until_after_transform(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ignored": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "payload-transform-bots.db"))
    await bot_registry.register(
        Bot(
            id="course-intake",
            name="Course Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                },
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings"],
                    "template": {
                        "workflow_type": "course_generation",
                        "course_brief": {
                            "topic": "{{payload.topic}}",
                        },
                        "generation_settings": {
                            "allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}",
                        },
                    },
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "payload-transform-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="course-intake",
        payload={"topic": "AP World History", "allowed_lesson_blocks_json": '["AdvancedParagraph"]'},
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"
    assert updated.result["workflow_type"] == "course_generation"
    assert updated.result["course_brief"]["topic"] == "AP World History"


@pytest.mark.anyio
async def test_payload_transform_bots_can_still_enforce_pre_transform_input_validation(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ignored": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "payload-transform-strict-bots.db"))
    await bot_registry.register(
        Bot(
            id="strict-intake",
            name="Strict Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["topic"],
                    "validate_before_transform": True,
                },
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["course_brief"],
                    "template": {
                        "course_brief": {
                            "topic": "{{payload.topic}}",
                        },
                    },
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "payload-transform-strict-tasks.db"), bot_registry=bot_registry)

    with pytest.raises(ValueError, match="missing required fields: topic"):
        await tm.create_task(bot_id="strict-intake", payload={})


@pytest.mark.anyio
async def test_input_transform_bots_defer_required_input_validation_until_after_transform(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"course_brief": {"topic": "AP World History"}}

    bot_registry = BotRegistry(db_path=str(tmp_path / "input-transform-bots.db"))
    await bot_registry.register(
        Bot(
            id="legacy-intake",
            name="Legacy Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                },
                "input_transform": {
                    "enabled": True,
                    "template": {
                        "workflow_type": "course_generation",
                        "course_brief": {
                            "topic": "{{payload.topic}}",
                        },
                        "generation_settings": {
                            "allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}",
                        },
                    },
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "input-transform-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="legacy-intake",
        payload={"topic": "AP World History", "allowed_lesson_blocks_json": '["AdvancedParagraph"]'},
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


@pytest.mark.anyio
async def test_launch_form_contracts_defer_stale_required_input_validation(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "launch-form-bots.db"))
    await bot_registry.register(
        Bot(
            id="launch-form-intake",
            name="Launch Form Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                    "default_payload": {"workflow_type": "course_generation"},
                    "form_fields": [
                        {"key": "topic", "label": "Topic", "required": True, "type": "text"},
                    ],
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "launch-form-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="launch-form-intake",
        payload={"topic": "AP World History"},
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


@pytest.mark.anyio
async def test_saved_launch_entries_defer_stale_required_input_validation(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "saved-launch-bots.db"))
    await bot_registry.register(
        Bot(
            id="saved-launch-intake",
            name="Saved Launch Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                },
                "launch_profile": {
                    "enabled": True,
                    "label": "Run Intake",
                    "payload": {"topic": "AP World History"},
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "saved-launch-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="saved-launch-intake",
        payload={"topic": "AP World History"},
        metadata=TaskMetadata(source="saved_launch_pipeline"),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


@pytest.mark.anyio
async def test_flat_launch_payloads_defer_normalized_input_validation(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "flat-launch-bots.db"))
    await bot_registry.register(
        Bot(
            id="flat-launch-intake",
            name="Flat Launch Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "flat-launch-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="flat-launch-intake",
        payload={
            "topic": "AP World History",
            "subject": "History",
            "goals_json": '["Analyze continuity and change"]',
            "units_json": "[]",
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


@pytest.mark.anyio
async def test_payload_transform_reuses_already_normalized_payload(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ignored": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "normalized-payload-bots.db"))
    await bot_registry.register(
        Bot(
            id="course-intake",
            name="Course Intake",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings"],
                    "non_empty_fields": [
                        "course_brief.topic",
                        "course_brief.units",
                        "generation_settings.allowed_lesson_blocks",
                    ],
                    "template": {
                        "workflow_type": "course_generation",
                        "course_brief": {
                            "topic": "{{payload.topic}}",
                            "units": "{{json:payload.units_json}}",
                        },
                        "generation_settings": {
                            "allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}",
                        },
                    },
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "normalized-payload-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="course-intake",
        payload={
            "workflow_type": "course_generation",
            "course_brief": {"topic": "AP World History", "units": [{"title": "Unit 1"}]},
            "generation_settings": {"allowed_lesson_blocks": ["AdvancedParagraph"]},
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"
    assert updated.result["course_brief"]["topic"] == "AP World History"


@pytest.mark.anyio
async def test_intake_role_bots_skip_pre_run_input_validation(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "intake-role-bots.db"))
    await bot_registry.register(
        Bot(
            id="course-intake",
            name="Course Intake",
            role="course-intake",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                },
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "intake-role-tasks.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="course-intake",
        payload={
            "workflow_type": "course_generation",
            "course_brief": {"topic": "AP World History", "units": [{"title": "Unit 1"}]},
            "generation_settings": {"allowed_lesson_blocks": ["AdvancedParagraph"]},
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.05)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


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
    assert "Execution Report" in labels
    assert "Usage Report" in labels
    assert "summary.txt" in labels


@pytest.mark.anyio
async def test_trigger_dispatch_failure_does_not_fail_parent_task(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"answer": f"done:{task.bot_id}"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "trigger-failure-bots.db"))
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "handoff",
                        "event": "task_completed",
                        "target_bot_id": "bot-b",
                        "condition": "has_result",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="bot-b",
            name="Bot B",
            role="assistant",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "validate_before_transform": True,
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "trigger-failure-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="bot-a", payload={"instruction": "start"})

    for _ in range(40):
        updated = await tm.get_task(root.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(root.id)
    assert updated.status == "completed"

    tasks = await tm.list_tasks()
    assert len(tasks) == 1

    artifacts = []
    for _ in range(40):
        artifacts = await tm.list_bot_run_artifacts("bot-a", task_id=root.id)
        if any(artifact.label == "Trigger Dispatch Error" for artifact in artifacts):
            break
        await asyncio.sleep(0.05)

    trigger_errors = [artifact for artifact in artifacts if artifact.label == "Trigger Dispatch Error"]
    assert len(trigger_errors) == 1
    assert "revision_context" in (trigger_errors[0].content or "")


@pytest.mark.anyio
async def test_trigger_skip_records_diagnostics_when_fan_out_produces_no_payloads(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"unit_blueprint": {"unit_number": 1, "title": "Unit 1"}}

    bot_registry = BotRegistry(db_path=str(tmp_path / "trigger-skip-bots.db"))
    await bot_registry.register(
        Bot(
            id="unit-builder-bot",
            name="Unit Builder",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fanout-lessons",
                        "event": "task_completed",
                        "target_bot_id": "lesson-writer-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.unit_blueprint.lesson_plans",
                        "fan_out_alias": "lesson",
                        "fan_out_index_alias": "lesson_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="lesson-writer-bot", name="Lesson Writer", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "trigger-skip-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="unit-builder-bot", payload={"instruction": "build unit"})

    for _ in range(40):
        updated = await tm.get_task(root.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    assert len([task for task in tasks if task.bot_id == "lesson-writer-bot"]) == 0

    artifacts = []
    for _ in range(40):
        artifacts = await tm.list_bot_run_artifacts("unit-builder-bot", task_id=root.id)
        if any(artifact.label == "Trigger Dispatch Skipped" for artifact in artifacts):
            break
        await asyncio.sleep(0.05)

    skipped = [artifact for artifact in artifacts if artifact.label == "Trigger Dispatch Skipped"]
    assert len(skipped) == 1
    assert '"reason": "fan_out_field_not_list"' in (skipped[0].content or "")
    assert '"fan_out_field": "source_result.unit_blueprint.lesson_plans"' in (skipped[0].content or "")


@pytest.mark.anyio
async def test_dispatch_triggers_falls_back_to_routing_rules_workflow(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "routing-workflow-bots.db"))
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="assistant",
            backends=[],
            workflow=None,
            routing_rules={
                "workflow": {
                    "triggers": [
                        {
                            "id": "handoff",
                            "event": "task_completed",
                            "target_bot_id": "bot-b",
                            "condition": "has_result",
                        }
                    ]
                }
            },
        )
    )
    await bot_registry.register(Bot(id="bot-b", name="Bot B", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "routing-workflow-tasks.db"), bot_registry=bot_registry)
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


@pytest.mark.anyio
async def test_trigger_wrapper_payloads_skip_pre_run_input_validation(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "trigger-wrapper-bots.db"))
    await bot_registry.register(
        Bot(
            id="course-outline",
            name="Course Outline",
            role="course-outline",
            backends=[],
            routing_rules={
                "input_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings", "revision_context"],
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "trigger-wrapper-tasks.db"), bot_registry=bot_registry)
    await tm._validate_task_payload(
        "course-outline",
        {
            "source_bot_id": "course-intake",
            "source_task_id": "root-task",
            "source_payload": {
                "course_brief": {"topic": "AP World History"},
                "generation_settings": {"allowed_lesson_blocks": ["AdvancedParagraph"]},
                "workflow_type": "course_generation",
            },
            "source_result": {
                "course_brief": {"topic": "AP World History"},
                "generation_settings": {"allowed_lesson_blocks": ["AdvancedParagraph"]},
                "workflow_type": "course_generation",
            },
            "instruction": "Build outline",
        },
        metadata=TaskMetadata(source="bot_trigger", parent_task_id="root-task", trigger_rule_id="to-outline"),
    )


@pytest.mark.anyio
async def test_trigger_payload_template_can_reference_source_fields(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {"course_structure": {"units": [{"unit_number": 1, "title": "Unit 1"}]}}
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "templated-trigger-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-unit",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "payload_template": {
                            "instruction": "Build one unit",
                            "units": "{{source_result.course_structure.units}}",
                        },
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="unit-bot", name="Unit", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "templated-trigger-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="outline-bot", payload={"instruction": "outline"})

    for _ in range(40):
        tasks = await tm.list_tasks()
        if len(tasks) >= 2 and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    triggered = next(task for task in tasks if task.id != root.id and task.bot_id == "unit-bot")
    assert triggered.payload["instruction"] == "Build one unit"
    assert triggered.payload["units"] == [{"unit_number": 1, "title": "Unit 1"}]


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
    retry_task = next(t for t in tasks if t.id != root.id and t.bot_id == "worker-bot" and t.metadata and t.metadata.parent_task_id)
    assert retry_task.bot_id == "worker-bot"
    assert retry_task.metadata is not None
    assert retry_task.metadata.parent_task_id in {task.id for task in qc_tasks}
    assert retry_task.metadata.trigger_rule_id == "return-for-fix"


@pytest.mark.anyio
async def test_trigger_can_resolve_target_bot_from_result_field(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "qc-bot":
                return {"qc_status": "fail", "retry_target_bot_id": "writer-bot"}
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "dynamic-target-bots.db"))
    await bot_registry.register(Bot(id="qc-bot", name="QC", role="quality", backends=[], workflow={
        "triggers": [
            {
                "id": "dynamic-retry",
                "event": "task_completed",
                "target_bot_id": "{{result.retry_target_bot_id}}",
                "condition": "has_result",
                "result_field": "qc_status",
                "result_equals": "fail",
            }
        ]
    }))
    await bot_registry.register(Bot(id="writer-bot", name="Writer", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "dynamic-target-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(bot_id="qc-bot", payload={"instruction": "check"})

    for _ in range(40):
        tasks = await tm.list_tasks()
        if len(tasks) >= 2 and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    retry_task = next(task for task in tasks if task.id != root.id)
    assert retry_task.bot_id == "writer-bot"


@pytest.mark.anyio
async def test_trigger_can_fan_out_many_downstream_tasks(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "units": [
                        {"title": "Unit 1"},
                        {"title": "Unit 2"},
                        {"title": "Unit 3"},
                    ]
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "fanout-bots.db"))
    await bot_registry.register(Bot(id="outline-bot", name="Outline", role="assistant", backends=[], workflow={
        "triggers": [
            {
                "id": "fan-out-units",
                "event": "task_completed",
                "target_bot_id": "unit-bot",
                "condition": "has_result",
                "fan_out_field": "source_result.units",
                "fan_out_alias": "unit",
                "fan_out_index_alias": "unit_index",
            }
        ]
    }))
    await bot_registry.register(Bot(id="unit-bot", name="Unit", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "fanout-tasks.db"), bot_registry=bot_registry)
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(50):
        tasks = await tm.list_tasks()
        if len(tasks) >= 4 and all(t.status == "completed" for t in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    unit_tasks = sorted(
        [t for t in tasks if t.bot_id == "unit-bot"],
        key=lambda task: int(task.payload["unit_index"]),
    )
    assert len(unit_tasks) == 3
    assert unit_tasks[0].payload["unit"]["title"] == "Unit 1"
    assert unit_tasks[1].payload["unit_index"] == 1
    assert unit_tasks[2].payload["fanout_count"] == 3
    assert unit_tasks[0].payload["fanout_branch_key"] == "0"
    assert unit_tasks[2].payload["fanout_expected_branch_keys"] == ["0", "1", "2"]


@pytest.mark.anyio
async def test_trigger_can_join_sibling_branch_outputs(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {
                            "unit_number": 1,
                            "title": "Unit 1",
                            "lessons": [
                                {"lesson_number": 1, "title": "Lesson 1"},
                                {"lesson_number": 2, "title": "Lesson 2"},
                            ],
                        }
                    ]
                }
            if task.bot_id == "unit-bot":
                return {
                    "unit_blueprint": {
                        "unit_number": task.payload["unit"]["unit_number"],
                        "title": task.payload["unit"]["title"],
                        "lesson_plans": task.payload["unit"]["lessons"],
                    }
                }
            if task.bot_id == "lesson-bot":
                lesson = task.payload["lesson"]
                return {
                    "approved_lesson": {
                        "lesson_number": lesson["lesson_number"],
                        "title": lesson["title"],
                    }
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "join-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-lessons",
                        "event": "task_completed",
                        "target_bot_id": "lesson-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.unit_blueprint.lesson_plans",
                        "fan_out_alias": "lesson",
                        "fan_out_index_alias": "lesson_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="lesson-bot",
            name="Lesson",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-lessons",
                        "event": "task_completed",
                        "target_bot_id": "unit-packager",
                        "condition": "has_result",
                        "join_group_field": "source_payload.source_result.unit_blueprint.unit_number",
                        "join_expected_field": "source_payload.fanout_count",
                        "join_items_alias": "lesson_bundles",
                        "join_result_field": "source_result.approved_lesson",
                        "join_result_items_alias": "approved_lessons",
                        "join_sort_field": "source_result.approved_lesson.lesson_number",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="unit-packager", name="Packager", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "join-tasks.db"), bot_registry=bot_registry)
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(80):
        tasks = await tm.list_tasks()
        if any(task.bot_id == "unit-packager" and task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    packager_tasks = [task for task in tasks if task.bot_id == "unit-packager"]
    assert len(packager_tasks) == 1
    packager = packager_tasks[0]
    assert packager.status == "completed"
    assert packager.payload["join_expected_count"] == 2
    assert packager.payload["join_count"] == 2
    assert len(packager.payload["join_results"]) == 2
    assert len(packager.payload["join_task_ids"]) == 2
    assert packager.payload["approved_lessons"][0]["lesson_number"] == 1
    assert packager.payload["approved_lessons"][1]["lesson_number"] == 2
    assert len(packager.payload["lesson_bundles"]) == 2
    assert packager.payload["lesson_bundles"][0]["source_result"]["approved_lesson"]["lesson_number"] == 1
    assert packager.payload["lesson_bundles"][1]["source_result"]["approved_lesson"]["lesson_number"] == 2


@pytest.mark.anyio
async def test_nested_fanout_is_isolated_per_parent_branch(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {
                            "unit_number": 1,
                            "title": "Unit 1",
                            "lessons": [
                                {"lesson_number": 1, "title": "U1-L1"},
                                {"lesson_number": 2, "title": "U1-L2"},
                            ],
                        },
                        {
                            "unit_number": 2,
                            "title": "Unit 2",
                            "lessons": [
                                {"lesson_number": 1, "title": "U2-L1"},
                                {"lesson_number": 2, "title": "U2-L2"},
                            ],
                        },
                    ]
                }
            if task.bot_id == "unit-bot":
                return {
                    "unit_blueprint": {
                        "unit_number": task.payload["unit"]["unit_number"],
                        "title": task.payload["unit"]["title"],
                        "lesson_plans": task.payload["unit"]["lessons"],
                    }
                }
            if task.bot_id == "lesson-bot":
                lesson = task.payload["lesson"]
                return {
                    "approved_lesson": {
                        "unit_number": task.payload["source_result"]["unit_blueprint"]["unit_number"],
                        "lesson_number": lesson["lesson_number"],
                        "title": lesson["title"],
                    }
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "nested-fanout-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-lessons",
                        "event": "task_completed",
                        "target_bot_id": "lesson-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.unit_blueprint.lesson_plans",
                        "fan_out_alias": "lesson",
                        "fan_out_index_alias": "lesson_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="lesson-bot", name="Lesson", role="assistant", backends=[]))

    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "nested-fanout-tasks.db"),
        bot_registry=bot_registry,
    )
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(80):
        tasks = await tm.list_tasks()
        lesson_tasks = [task for task in tasks if task.bot_id == "lesson-bot" and task.status == "completed"]
        if len(lesson_tasks) == 4:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    lesson_tasks = [task for task in tasks if task.bot_id == "lesson-bot"]
    assert len(lesson_tasks) == 4

    step_ids = [task.metadata.step_id for task in lesson_tasks if task.metadata and task.metadata.step_id]
    assert len(step_ids) == 4
    assert len(set(step_ids)) == 4

    fanout_ids = {str(task.payload.get("fanout_id")) for task in lesson_tasks if isinstance(task.payload, dict)}
    assert len(fanout_ids) == 2

    lessons_per_unit: Dict[int, int] = {}
    for task in lesson_tasks:
        assert task.payload["fanout_count"] == 2
        unit_number = int(task.payload["source_result"]["unit_blueprint"]["unit_number"])
        lessons_per_unit[unit_number] = lessons_per_unit.get(unit_number, 0) + 1
    assert lessons_per_unit == {1: 2, 2: 2}


@pytest.mark.anyio
async def test_join_without_group_field_dedupes_per_fanout_set(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {
                            "unit_number": 1,
                            "title": "Unit 1",
                            "lessons": [
                                {"lesson_number": 1, "title": "U1-L1"},
                                {"lesson_number": 2, "title": "U1-L2"},
                            ],
                        },
                        {
                            "unit_number": 2,
                            "title": "Unit 2",
                            "lessons": [
                                {"lesson_number": 1, "title": "U2-L1"},
                                {"lesson_number": 2, "title": "U2-L2"},
                            ],
                        },
                    ]
                }
            if task.bot_id == "unit-bot":
                return {
                    "unit_blueprint": {
                        "unit_number": task.payload["unit"]["unit_number"],
                        "title": task.payload["unit"]["title"],
                        "lesson_plans": task.payload["unit"]["lessons"],
                    }
                }
            if task.bot_id == "lesson-bot":
                lesson = task.payload["lesson"]
                return {
                    "approved_lesson": {
                        "unit_number": task.payload["source_result"]["unit_blueprint"]["unit_number"],
                        "lesson_number": lesson["lesson_number"],
                    }
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "join-fanout-scope-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-lessons",
                        "event": "task_completed",
                        "target_bot_id": "lesson-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.unit_blueprint.lesson_plans",
                        "fan_out_alias": "lesson",
                        "fan_out_index_alias": "lesson_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="lesson-bot",
            name="Lesson",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-lessons",
                        "event": "task_completed",
                        "target_bot_id": "unit-packager",
                        "condition": "has_result",
                        "join_expected_field": "fanout_count",
                        "join_items_alias": "lesson_bundles",
                        "join_result_field": "source_result.approved_lesson",
                        "join_result_items_alias": "approved_lessons",
                        "join_sort_field": "source_result.approved_lesson.lesson_number",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="unit-packager", name="Packager", role="assistant", backends=[]))

    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "join-fanout-scope-tasks.db"),
        bot_registry=bot_registry,
    )
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(100):
        tasks = await tm.list_tasks()
        packagers = [task for task in tasks if task.bot_id == "unit-packager" and task.status == "completed"]
        if len(packagers) == 2:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    packagers = [task for task in tasks if task.bot_id == "unit-packager"]
    assert len(packagers) == 2

    join_fanout_ids = {str(task.payload.get("join_fanout_id")) for task in packagers}
    assert len(join_fanout_ids) == 2

    packaged_units: Set[int] = set()
    for packager in packagers:
        assert packager.payload["join_expected_count"] == 2
        assert packager.payload["join_count"] == 2
        approved_lessons = packager.payload["approved_lessons"]
        assert len(approved_lessons) == 2
        unit_numbers = {int(item["unit_number"]) for item in approved_lessons}
        assert len(unit_numbers) == 1
        packaged_units.update(unit_numbers)
    assert packaged_units == {1, 2}


@pytest.mark.anyio
async def test_course_pipeline_cardinality_matches_expected_branch_counts(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    units = [
        {
            "unit_number": unit_number,
            "title": f"Unit {unit_number}",
            "lessons": [
                {
                    "lesson_number": lesson_number,
                    "title": f"U{unit_number}-L{lesson_number}",
                }
                for lesson_number in range(1, 5)
            ],
        }
        for unit_number in range(1, 4)
    ]

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "intake-bot":
                return {"ok": True}
            if task.bot_id == "outline-bot":
                return {"units": units}
            if task.bot_id == "outline-qc-bot":
                return {"qc_status": "pass", "approved_units": task.payload["source_result"]["units"]}
            if task.bot_id == "unit-builder-bot":
                unit = task.payload["unit"]
                return {
                    "unit_blueprint": {
                        "unit_number": unit["unit_number"],
                        "title": unit["title"],
                        "lesson_plans": unit["lessons"],
                    }
                }
            if task.bot_id == "lesson-writer-bot":
                unit_number = task.payload["source_result"]["unit_blueprint"]["unit_number"]
                lesson = task.payload["lesson"]
                return {
                    "lesson_output": {
                        "unit_number": unit_number,
                        "lesson_number": lesson["lesson_number"],
                        "title": lesson["title"],
                    }
                }
            if task.bot_id == "lesson-qc-bot":
                return {
                    "qc_status": "pass",
                    "approved_lesson": task.payload["source_result"]["lesson_output"],
                }
            if task.bot_id == "unit-aggregator-bot":
                return {"approved_unit": {"unit_number": task.payload["join_group"]}}
            if task.bot_id == "image-planner-bot":
                return {"unit_image_plan": {"unit_number": task.payload["source_result"]["approved_unit"]["unit_number"]}}
            if task.bot_id == "unit-question-bank-bot":
                return {
                    "unit_question_bank": {
                        "unit_number": task.payload["source_result"]["approved_unit"]["unit_number"],
                    }
                }
            if task.bot_id == "unit-question-bank-qc-bot":
                return {
                    "qc_status": "pass",
                    "approved_unit_package": {
                        "unit_number": task.payload["source_result"]["unit_question_bank"]["unit_number"],
                    },
                }
            if task.bot_id == "course-aggregator-bot":
                return {"course_package": {"units": task.payload["approved_units"]}}
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "pipeline-counts-bots.db"))
    await bot_registry.register(
        Bot(
            id="intake-bot",
            name="Intake",
            role="intake",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-outline",
                        "event": "task_completed",
                        "target_bot_id": "outline-bot",
                        "condition": "has_result",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-outline-qc",
                        "event": "task_completed",
                        "target_bot_id": "outline-qc-bot",
                        "condition": "has_result",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="outline-qc-bot",
            name="Outline QC",
            role="quality",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-builder-bot",
                        "condition": "has_result",
                        "result_field": "qc_status",
                        "result_equals": "pass",
                        "fan_out_field": "approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-builder-bot",
            name="Unit Builder",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-lessons",
                        "event": "task_completed",
                        "target_bot_id": "lesson-writer-bot",
                        "condition": "has_result",
                        "fan_out_field": "source_result.unit_blueprint.lesson_plans",
                        "fan_out_alias": "lesson",
                        "fan_out_index_alias": "lesson_index",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.fanout_count}}",
                            "unit_blueprint": "{{source_result.unit_blueprint}}",
                        },
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="lesson-writer-bot",
            name="Lesson Writer",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-lesson-qc",
                        "event": "task_completed",
                        "target_bot_id": "lesson-qc-bot",
                        "condition": "has_result",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.course_unit_count}}",
                            "unit_blueprint": "{{source_payload.unit_blueprint}}",
                            "lesson_output": "{{source_result.lesson_output}}",
                        },
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="lesson-qc-bot",
            name="Lesson QC",
            role="quality",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-lessons",
                        "event": "task_completed",
                        "target_bot_id": "unit-aggregator-bot",
                        "condition": "has_result",
                        "result_field": "qc_status",
                        "result_equals": "pass",
                        "join_group_field": "source_payload.unit_blueprint.unit_number",
                        "join_expected_field": "source_payload.fanout_count",
                        "join_items_alias": "lesson_bundles",
                        "join_result_field": "source_result.approved_lesson",
                        "join_result_items_alias": "approved_lessons",
                        "join_sort_field": "source_result.approved_lesson.lesson_number",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.course_unit_count}}",
                            "unit_blueprint": "{{source_payload.unit_blueprint}}",
                        },
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-aggregator-bot",
            name="Unit Aggregator",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-image-planner",
                        "event": "task_completed",
                        "target_bot_id": "image-planner-bot",
                        "condition": "has_result",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.course_unit_count}}",
                            "approved_unit": "{{source_result.approved_unit}}",
                        },
                    },
                    {
                        "id": "to-unit-question-bank",
                        "event": "task_completed",
                        "target_bot_id": "unit-question-bank-bot",
                        "condition": "has_result",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.course_unit_count}}",
                            "approved_unit": "{{source_result.approved_unit}}",
                        },
                    },
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="image-planner-bot", name="Image Planner", role="assistant", backends=[]))
    await bot_registry.register(
        Bot(
            id="unit-question-bank-bot",
            name="Unit Question Bank",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "to-unit-question-bank-qc",
                        "event": "task_completed",
                        "target_bot_id": "unit-question-bank-qc-bot",
                        "condition": "has_result",
                        "payload_template": {
                            "course_unit_count": "{{source_payload.course_unit_count}}",
                            "unit_question_bank": "{{source_result.unit_question_bank}}",
                        },
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-question-bank-qc-bot",
            name="Unit Question Bank QC",
            role="quality",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-approved-units",
                        "event": "task_completed",
                        "target_bot_id": "course-aggregator-bot",
                        "condition": "has_result",
                        "result_field": "qc_status",
                        "result_equals": "pass",
                        "join_expected_field": "source_payload.course_unit_count",
                        "join_items_alias": "approved_units",
                        "join_result_field": "source_result.approved_unit_package",
                        "join_result_items_alias": "approved_unit_packages",
                        "join_sort_field": "source_result.approved_unit_package.unit_number",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="course-aggregator-bot", name="Course Aggregator", role="assistant", backends=[]))

    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "pipeline-counts-tasks.db"),
        bot_registry=bot_registry,
    )
    await tm.create_task(bot_id="intake-bot", payload={"instruction": "start"})

    for _ in range(240):
        tasks = await tm.list_tasks()
        if any(task.bot_id == "course-aggregator-bot" and task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    expected_total = 43
    for _ in range(240):
        tasks = await tm.list_tasks()
        if len(tasks) == expected_total and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    assert len(tasks) == expected_total
    assert all(task.status == "completed" for task in tasks)

    counts = {}
    for task in tasks:
        counts[task.bot_id] = counts.get(task.bot_id, 0) + 1

    assert counts.get("intake-bot", 0) == 1
    assert counts.get("outline-bot", 0) == 1
    assert counts.get("outline-qc-bot", 0) == 1
    assert counts.get("unit-builder-bot", 0) == 3
    assert counts.get("lesson-writer-bot", 0) == 12
    assert counts.get("lesson-qc-bot", 0) == 12
    assert counts.get("unit-aggregator-bot", 0) == 3
    assert counts.get("image-planner-bot", 0) == 3
    assert counts.get("unit-question-bank-bot", 0) == 3
    assert counts.get("unit-question-bank-qc-bot", 0) == 3
    assert counts.get("course-aggregator-bot", 0) == 1

    unit_aggregators = [task for task in tasks if task.bot_id == "unit-aggregator-bot"]
    assert len(unit_aggregators) == 3
    for task in unit_aggregators:
        assert task.payload["join_expected_count"] == 4
        assert task.payload["join_count"] == 4

@pytest.mark.anyio
async def test_trigger_can_fan_out_from_bare_result_field(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {"unit_number": 1, "title": "Unit 1"},
                        {"unit_number": 2, "title": "Unit 2"},
                    ]
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "bare-result-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-approved-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="unit-bot", name="Unit", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "bare-result-tasks.db"), bot_registry=bot_registry)
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(50):
        tasks = await tm.list_tasks()
        if len([task for task in tasks if task.bot_id == "unit-bot"]) == 2:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    unit_tasks = sorted(
        [task for task in tasks if task.bot_id == "unit-bot"],
        key=lambda task: int(task.payload["unit_index"]),
    )
    assert len(unit_tasks) == 2
    assert unit_tasks[0].payload["unit"]["title"] == "Unit 1"
    assert unit_tasks[1].payload["unit"]["title"] == "Unit 2"
    assert unit_tasks[0].metadata.step_id
    assert unit_tasks[1].metadata.step_id
    assert unit_tasks[0].metadata.step_id != unit_tasks[1].metadata.step_id


def test_transform_template_can_read_list_index_paths():
    from control_plane.task_manager.task_manager import _transform_template_value

    payload = {
        "approved_lesson_bundles": [
            {
                "source_payload": {
                    "unit_blueprint": {
                        "unit_number": 3,
                        "title": "Land-Based Empires",
                    }
                }
            }
        ]
    }
    notes = []

    transformed = _transform_template_value(
        {
            "approved_lesson_bundles": "{{payload.approved_lesson_bundles}}",
            "unit_blueprint": "{{payload.approved_lesson_bundles.0.source_payload.unit_blueprint}}",
        },
        payload,
        notes,
    )

    assert transformed["unit_blueprint"]["unit_number"] == 3
    assert transformed["unit_blueprint"]["title"] == "Land-Based Empires"


def test_transform_template_coalesce_supports_literal_fallbacks():
    from control_plane.task_manager.task_manager import _transform_template_value

    payload = {
        "items": [],
        "backup_items": ["fallback-item"],
    }
    notes = []

    transformed = _transform_template_value(
        {
            "title": "{{coalesce:payload.title,'Generated Course'}}",
            "subject": "{{coalesce:payload.subject,''}}",
            "estimated_hours": "{{coalesce:payload.estimated_hours,0}}",
            "badge_enabled": "{{coalesce:payload.badge_enabled,true}}",
            "preferred_items": "{{coalesce:payload.items,payload.backup_items,[]}}",
            "empty_items_default": "{{coalesce:payload.missing_items,[]}}",
        },
        payload,
        notes,
    )

    assert transformed["title"] == "Generated Course"
    assert transformed["subject"] == ""
    assert transformed["estimated_hours"] == 0
    assert transformed["badge_enabled"] is True
    assert transformed["preferred_items"] == ["fallback-item"]
    assert transformed["empty_items_default"] == []


@pytest.mark.anyio
async def test_join_waits_for_latest_successful_branch_results(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {"unit_number": 1, "title": "Unit 1"},
                        {"unit_number": 2, "title": "Unit 2"},
                        {"unit_number": 3, "title": "Unit 3"},
                    ]
                }
            if task.bot_id == "unit-bot":
                if task.payload["unit"]["unit_number"] == 3:
                    await asyncio.sleep(1.0)
                return {"approved_unit": {"unit_number": task.payload["unit"]["unit_number"]}}
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "join-retry-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-units",
                        "event": "task_completed",
                        "target_bot_id": "packager-bot",
                        "condition": "has_result",
                        "join_expected_field": "source_payload.fanout_count",
                        "join_items_alias": "approved_units",
                        "join_sort_field": "source_result.approved_unit.unit_number",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="packager-bot", name="Packager", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "join-retry-tasks.db"), bot_registry=bot_registry)
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    first_two: List[Any] = []
    for _ in range(60):
        tasks = await tm.list_tasks()
        first_two = [task for task in tasks if task.bot_id == "unit-bot" and task.status == "completed"]
        if len(first_two) >= 2:
            break
        await asyncio.sleep(0.1)

    assert len(first_two) >= 2

    join_wait_reports = []
    for _ in range(40):
        artifacts = await tm.list_bot_run_artifacts("unit-bot")
        join_wait_reports = []
        for artifact in artifacts:
            if artifact.label != "Join Waiting" or not artifact.content:
                continue
            try:
                join_wait_reports.append(json.loads(artifact.content))
            except (TypeError, json.JSONDecodeError):
                continue
        if any(
            int(report.get("expected_count") or 0) == 3
            and int(report.get("received_count") or 0) < 3
            for report in join_wait_reports
        ):
            break
        await asyncio.sleep(0.05)

    assert any(
        int(report.get("expected_count") or 0) == 3
        and int(report.get("received_count") or 0) < 3
        for report in join_wait_reports
    )

    await tm.retry_task(first_two[0].id)
    await asyncio.sleep(0.3)

    tasks = await tm.list_tasks()
    assert not any(task.bot_id == "packager-bot" for task in tasks)

    for _ in range(80):
        tasks = await tm.list_tasks()
        if any(task.bot_id == "packager-bot" and task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    packagers = [task for task in tasks if task.bot_id == "packager-bot"]
    assert len(packagers) == 1
    assert packagers[0].payload["join_expected_count"] == 3
    assert packagers[0].payload["join_count"] == 3


@pytest.mark.anyio
async def test_join_can_use_fanout_branch_metadata_when_expected_field_is_missing(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "outline-bot":
                return {
                    "approved_units": [
                        {"unit_number": 1, "title": "Unit 1"},
                        {"unit_number": 2, "title": "Unit 2"},
                    ]
                }
            if task.bot_id == "unit-bot":
                return {
                    "approved_unit": {
                        "unit_number": task.payload["unit"]["unit_number"],
                        "title": task.payload["unit"]["title"],
                    }
                }
            return {"ok": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "join-fanout-meta-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "fan-out-units",
                        "event": "task_completed",
                        "target_bot_id": "unit-bot",
                        "condition": "has_result",
                        "fan_out_field": "approved_units",
                        "fan_out_alias": "unit",
                        "fan_out_index_alias": "unit_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "join-units",
                        "event": "task_completed",
                        "target_bot_id": "packager-bot",
                        "condition": "has_result",
                        "join_expected_field": "source_payload.missing_count",
                        "join_items_alias": "approved_units",
                        "join_result_field": "source_result.approved_unit",
                        "join_result_items_alias": "approved_unit_results",
                        "join_sort_field": "source_result.approved_unit.unit_number",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="packager-bot", name="Packager", role="assistant", backends=[]))

    tm = TaskManager(
        StubScheduler(),
        db_path=str(tmp_path / "join-fanout-meta-tasks.db"),
        bot_registry=bot_registry,
    )
    await tm.create_task(bot_id="outline-bot", payload={"instruction": "build outline"})

    for _ in range(80):
        tasks = await tm.list_tasks()
        if any(task.bot_id == "packager-bot" and task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    packagers = [task for task in tasks if task.bot_id == "packager-bot"]
    assert len(packagers) == 1
    payload = packagers[0].payload
    assert payload["join_expected_count"] == 2
    assert payload["join_count"] == 2
    assert payload["join_expected_branch_keys"] == ["0", "1"]
    assert payload["join_branch_keys"] == ["0", "1"]
    assert payload["join_missing_branch_keys"] == []
    assert len(payload["approved_unit_results"]) == 2


@pytest.mark.anyio
async def test_run_reports_capture_usage_metadata(tmp_path):
    import asyncio

    from control_plane.task_manager.task_manager import TaskManager

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": "done",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "usage.db"))
    task = await tm.create_task(bot_id="usage-bot", payload={"instruction": "go"})

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    artifacts = []
    for _ in range(40):
        artifacts = await tm.list_bot_run_artifacts("usage-bot")
        if any(a.label == "Usage Report" for a in artifacts):
            break
        await asyncio.sleep(0.05)

    usage_artifact = next((a for a in artifacts if a.label == "Usage Report"), None)
    assert usage_artifact is not None
    assert '"prompt_tokens": 11' in str(usage_artifact.content)
    execution_artifact = next((a for a in artifacts if a.label == "Execution Report"), None)
    assert execution_artifact is not None
    assert execution_artifact.metadata["usage"]["total_tokens"] == 18


@pytest.mark.anyio
async def test_output_contract_extracts_json_from_text_result(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": "```json\n{\"status\":\"pass\",\"items\":[{\"title\":\"Unit 1\"}]}\n```",
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "contract-bots.db"))
    await bot_registry.register(
        Bot(
            id="structured-bot",
            name="Structured",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["status", "items"],
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "contract.db"), bot_registry=bot_registry)
    task = await tm.create_task(bot_id="structured-bot", payload={"instruction": "go"})

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["status"] == "pass"
    assert updated.result["items"][0]["title"] == "Unit 1"
    assert updated.result["usage"]["total_tokens"] == 12


@pytest.mark.anyio
async def test_output_contract_fails_when_required_fields_are_missing(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "{\"summary\":\"missing status\"}"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "missing-bots.db"))
    await bot_registry.register(
        Bot(
            id="strict-bot",
            name="Strict",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "format": "json_object",
                    "required_fields": ["qc_status"],
                }
            },
        )
    )

    monkeypatch.setattr(task_manager_module, "_settings_int", lambda name, default: 0 if name == "max_task_retries" else default)
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "missing.db"), bot_registry=bot_registry)
    task = await tm.create_task(bot_id="strict-bot", payload={"instruction": "go"})

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "missing required fields" in updated.error.message


@pytest.mark.anyio
async def test_output_contract_can_transform_payload_deterministically(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ignored": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "transform-bots.db"))
    await bot_registry.register(
        Bot(
            id="transform-bot",
            name="Transform",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["workflow_type", "course_brief", "generation_settings"],
                    "template": {
                        "workflow_type": "course_generation",
                        "normalization_notes": [],
                        "course_brief": {
                            "topic": "{{payload.topic}}",
                            "goals": "{{json:payload.goals_json}}"
                        },
                        "generation_settings": {
                            "allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}"
                        }
                    },
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "transform.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="transform-bot",
        payload={
            "topic": "AP World History",
            "goals_json": "[\"Goal 1\",\"Goal 2\"]",
            "allowed_lesson_blocks_json": "[\"AdvancedParagraph\",\"image\"]",
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["workflow_type"] == "course_generation"
    assert updated.result["course_brief"]["topic"] == "AP World History"
    assert updated.result["course_brief"]["goals"] == ["Goal 1", "Goal 2"]
    assert updated.result["generation_settings"]["allowed_lesson_blocks"] == ["AdvancedParagraph", "image"]


@pytest.mark.anyio
async def test_payload_transform_mode_skips_scheduler_execution(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        def __init__(self):
            self.called = False

        async def schedule(self, task):
            self.called = True
            return {"should_not": "run"}

    scheduler = StubScheduler()
    bot_registry = BotRegistry(db_path=str(tmp_path / "skip-bots.db"))
    await bot_registry.register(
        Bot(
            id="transform-bot",
            name="Transform",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["workflow_type"],
                    "template": {
                        "workflow_type": "course_generation",
                    },
                }
            },
        )
    )

    tm = TaskManager(scheduler, db_path=str(tmp_path / "skip.db"), bot_registry=bot_registry)
    task = await tm.create_task(bot_id="transform-bot", payload={"instruction": "go"})

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["workflow_type"] == "course_generation"
    assert scheduler.called is False


@pytest.mark.anyio
async def test_output_contract_can_backfill_empty_model_output_from_defaults(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": json.dumps(
                    {
                        "course_shell": {
                            "title": "",
                            "subject": "",
                            "audience": "",
                            "level": "",
                            "estimated_hours": 0,
                            "summary": "",
                            "voice": "",
                            "tone": "",
                            "tags": [],
                            "goals": [],
                        },
                        "course_structure": {
                            "units": [],
                        },
                    }
                )
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "defaults-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["course_shell", "course_structure"],
                    "fallback_mode": "missing_only",
                    "defaults_template": {
                        "course_shell": {
                            "title": "{{payload.course_brief.topic}}",
                            "subject": "{{payload.course_brief.subject}}",
                            "audience": "{{payload.course_brief.audience}}",
                            "level": "{{payload.course_brief.level}}",
                            "estimated_hours": "{{payload.course_brief.estimated_hours}}",
                            "summary": "{{payload.course_brief.scope}}",
                            "voice": "{{payload.course_brief.preferred_voice}}",
                            "tone": "{{payload.course_brief.tone}}",
                            "tags": "{{payload.course_brief.tags}}",
                            "goals": "{{payload.course_brief.goals}}",
                        },
                        "course_structure": {
                            "units": "{{payload.course_brief.units}}",
                        },
                    },
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "defaults.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="outline-bot",
        payload={
            "course_brief": {
                "topic": "AP World History",
                "subject": "History",
                "audience": "High School",
                "level": "Advanced",
                "estimated_hours": 100,
                "scope": "Modern world history survey",
                "preferred_voice": "Formal",
                "tone": "Structured",
                "tags": ["History"],
                "goals": ["Goal 1"],
                "units": [{"unit_number": 1, "title": "Unit 1"}],
            }
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["course_shell"]["title"] == "AP World History"
    assert updated.result["course_structure"]["units"] == [{"unit_number": 1, "title": "Unit 1"}]


@pytest.mark.anyio
async def test_output_contract_preserves_non_empty_model_values_over_defaults(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": json.dumps(
                    {
                        "course_shell": {
                            "title": "Generated Title",
                            "subject": "Generated Subject",
                        },
                        "course_structure": {
                            "units": [{"unit_number": 1, "title": "Generated Unit"}],
                        },
                    }
                )
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "defaults-preserve-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["course_shell", "course_structure"],
                    "fallback_mode": "missing_only",
                    "defaults_template": {
                        "course_shell": {
                            "title": "{{payload.course_brief.topic}}",
                            "subject": "{{payload.course_brief.subject}}",
                        },
                        "course_structure": {
                            "units": "{{payload.course_brief.units}}",
                        },
                    },
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "defaults-preserve.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="outline-bot",
        payload={
            "course_brief": {
                "topic": "Fallback Title",
                "subject": "Fallback Subject",
                "units": [{"unit_number": 99, "title": "Fallback Unit"}],
            }
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["course_shell"]["title"] == "Generated Title"
    assert updated.result["course_shell"]["subject"] == "Generated Subject"
    assert updated.result["course_structure"]["units"] == [{"unit_number": 1, "title": "Generated Unit"}]


@pytest.mark.anyio
async def test_output_contract_can_fallback_to_defaults_when_model_output_is_not_json(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "Here is your detailed unit plan: 1. Start with context 2. Add examples"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "defaults-fallback-bots.db"))
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit Builder",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["unit_blueprint"],
                    "fallback_mode": "parse_failure",
                    "defaults_template": {
                        "normalization_notes": [],
                        "unit_blueprint": {
                            "unit_number": "{{payload.unit.unit_number}}",
                            "title": "{{payload.unit.title}}",
                            "overview": "{{payload.unit.description}}",
                            "outcomes": "{{payload.unit.goals}}",
                            "prerequisites": [],
                            "examples": [],
                            "checks_for_understanding": [],
                            "lesson_plans": "{{payload.unit.lessons}}",
                        },
                    },
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "defaults-fallback.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="unit-bot",
        payload={
            "unit": {
                "unit_number": 2,
                "title": "Networks of Exchange",
                "description": "Trade networks and cultural exchange.",
                "goals": ["Explain trade routes"],
                "lessons": [{"lesson_number": 1, "title": "Lesson 1"}],
            }
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["unit_blueprint"]["unit_number"] == 2
    assert updated.result["unit_blueprint"]["lesson_plans"] == [{"lesson_number": 1, "title": "Lesson 1"}]
    assert "fell back to defaults template" in " ".join(updated.result.get("normalization_notes", []))


@pytest.mark.anyio
async def test_output_contract_non_empty_fields_fail_incomplete_output(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": json.dumps(
                    {
                        "course_shell": {"title": "Generated Title"},
                        "course_structure": {"units": []},
                    }
                )
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "non-empty-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["course_shell", "course_structure"],
                    "non_empty_fields": ["course_structure.units"],
                    "fallback_mode": "disabled",
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "non-empty.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="outline-bot",
        payload={"instruction": "build outline"},
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "non-empty fields" in updated.error.message
    assert "course_structure.units" in updated.error.message


@pytest.mark.anyio
async def test_output_contract_error_includes_truncation_hint_when_finish_reason_is_length(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": json.dumps(
                    {
                        "unit_asset_plan": {
                            "title": "",
                            "images": [],
                        }
                    }
                ),
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 4096,
                },
                "finish_reason": "length",
            }

    bot_registry = BotRegistry(db_path=str(tmp_path / "truncate-hint-bots.db"))
    await bot_registry.register(
        Bot(
            id="asset-bot",
            name="Asset Planner",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["unit_asset_plan"],
                    "non_empty_fields": ["unit_asset_plan.title", "unit_asset_plan.images"],
                    "fallback_mode": "disabled",
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "truncate-hint.db"), bot_registry=bot_registry)
    task = await tm.create_task(bot_id="asset-bot", payload={"instruction": "build unit assets"})

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "non-empty fields" in updated.error.message
    assert "likely truncated model output" in updated.error.message


@pytest.mark.anyio
async def test_output_contract_disabled_fallback_mode_does_not_mask_parse_failures(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "not valid json"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "strict-fallback-bots.db"))
    await bot_registry.register(
        Bot(
            id="unit-bot",
            name="Unit Builder",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "model_output",
                    "format": "json_object",
                    "required_fields": ["unit_blueprint"],
                    "fallback_mode": "disabled",
                    "defaults_template": {
                        "unit_blueprint": {
                            "unit_number": "{{payload.unit.unit_number}}",
                            "title": "{{payload.unit.title}}",
                        },
                    },
                }
            },
        )
    )

    monkeypatch.setattr(task_manager_module, "_settings_int", lambda name, default: 0 if name == "max_task_retries" else default)
    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "strict-fallback.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="unit-bot",
        payload={"unit": {"unit_number": 2, "title": "Networks of Exchange"}},
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "valid JSON object or array" in updated.error.message


@pytest.mark.anyio
async def test_chat_assign_task_retries_when_result_is_truncated(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        def __init__(self):
            self.calls = 0

        async def schedule(self, task):
            self.calls += 1
            if self.calls == 1:
                return {
                    "output": "partial file body",
                    "usage": {"completion_tokens": 4096},
                    "finish_reason": "length",
                }
            return {
                "output": "complete file body.",
                "usage": {"completion_tokens": 512},
                "finish_reason": "stop",
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 1 if name == "max_task_retries" else default,
    )
    monkeypatch.setattr(
        task_manager_module,
        "_settings_float",
        lambda name, default: 0.0 if name == "task_retry_delay" else default,
    )

    scheduler = StubScheduler()
    tm = TaskManager(scheduler, db_path=str(tmp_path / "chat-assign-retry.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={"instruction": "generate files"},
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert scheduler.calls == 2
    assert updated.metadata is not None
    assert updated.metadata.retry_attempt == 1
    assert updated.result is not None
    assert updated.result["output"] == "complete file body."


@pytest.mark.anyio
async def test_chat_assign_tester_output_fails_when_it_admits_not_executed(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Acceptance Criteria Checklist\n"
                    "Pending - not executed\n"
                    "The validation environment does not have the actual source code.\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-unverified.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={"instruction": "run tests", "role_hint": "tester"},
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "unverified" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_repo_change_fails_without_file_evidence(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Implementation plan\n"
                    "1. Create src/lesson_blocks/math_block.py\n"
                    "2. Add tests/lesson_blocks/test_math_block.py\n"
                    "Run the following commands to create the files.\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-no-files.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "implement lesson blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": [
                "src/lesson_blocks/math_block.py",
                "tests/lesson_blocks/test_math_block.py",
            ],
            "evidence_requirements": ["Proposed repo file artifacts"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "changed-file evidence" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_repo_change_succeeds_with_extracted_file_candidates(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: src/lesson_blocks/math_block.py\n"
                    "```python\n"
                    "def add(a, b):\n"
                    "    return a + b\n"
                    "```\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-files.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "implement lesson blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": ["src/lesson_blocks/math_block.py"],
            "evidence_requirements": ["Proposed repo file artifacts"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_planning_fails_with_placeholder_issue_links(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "GitHub Issue Planning Artifacts\n"
                    "URL: https://github.com/[ORG]/[REPO]/issues/101 (Placeholder)\n"
                    "Project Board: https://github.com/[ORG]/[REPO]/projects/1 (Placeholder)\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-planning-links.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "create github issues",
            "role_hint": "coder",
            "step_kind": "planning",
            "deliverables": ["Issue #101", "Issue #102"],
            "evidence_requirements": ["URLs of the three created GitHub issues", "Milestone and project board links"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "placeholder" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_planning_succeeds_with_issue_definitions_without_links(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "## Issue Definitions\n"
                    "- Title: Triangle lesson block\n"
                    "- Labels: enhancement, geometry\n"
                    "- Acceptance Criteria: renders and validates inputs\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-planning-no-links.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "create issue definitions",
            "role_hint": "researcher",
            "step_kind": "planning",
            "deliverables": ["Issue definitions (markdown or JSON)"],
            "evidence_requirements": [
                "Proposed issue, milestone, or board definitions",
                "Only include live non-placeholder links if they actually exist",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_specification_issue_definitions_do_not_require_live_issue_links(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "## Issue Definitions\n"
                    "- Title: Geometry lesson block tracking issue\n"
                    "- Description: implement geometry block work\n"
                    "- Labels: enhancement, geometry\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-spec-issue-defs.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "define requirements and create tracking issue",
            "role_hint": "researcher",
            "step_kind": "specification",
            "deliverables": ["Issue definitions (markdown or JSON)"],
            "evidence_requirements": [
                "URL of the created GitHub issue",
                "Attached specification document (Markdown) in the issue",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_specification_fails_without_committed_file_evidence(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "# Task Analysis\n"
                    "Produce design_spec.md and open a PR with review comments.\n"
                    "This document outlines architecture, APIs, and data models.\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-spec-artifact.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "write spec",
            "role_hint": "researcher",
            "step_kind": "specification",
            "deliverables": ["design_spec.md"],
            "evidence_requirements": ["design_spec.md file committed to the repo", "Design review comments captured in the PR"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "repo-backed evidence" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_repo_change_fails_with_placeholder_commit_and_pr_evidence(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: src/lessons/math_geometry/types.ts\n"
                    "```typescript\n"
                    "export interface GeometryProblem { id: string }\n"
                    "```\n"
                    "Evidence Placeholders (User to fill):\n"
                    "Commit Hash: $(git rev-parse HEAD)\n"
                    "PR URL: https://repo.globeiq/pulls/<number>\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-placeholders.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "implement lesson blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": ["src/lessons/math_geometry/types.ts", "Pull request #<number>"],
            "evidence_requirements": ["Commit SHA that includes all code changes", "Diff showing modified/added files"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "placeholders" in updated.error.message.lower() or "placeholder" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_repo_change_succeeds_when_commit_and_pr_evidence_are_optional(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: src/lessons/math_geometry/__init__.py\n"
                    "```python\n"
                    "__all__ = ['geometry_lesson', 'algebra_lesson']\n"
                    "```\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-optional-pr.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "implement lesson blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": ["src/lessons/math_geometry/__init__.py"],
            "evidence_requirements": [
                "Proposed repo file artifacts or patches for changed files",
                "Only include non-placeholder commit or pull request evidence if it actually exists",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_repo_change_succeeds_when_pr_url_deliverable_is_optional(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: src/LessonBlocks/Geometry/PolygonBlock.cs\n"
                    "```csharp\n"
                    "public class PolygonBlock {}\n"
                    "```\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-optional-pr-url.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "implement geometry lesson blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": [
                "src/LessonBlocks/Geometry/PolygonBlock.cs",
                "Pull request URL",
            ],
            "evidence_requirements": [
                "Proposed repo file artifacts or patches for changed files",
                "Only include non-placeholder commit or pull request evidence if it actually exists",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_docs_repo_change_allows_internal_hyperlink_language(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: docs/lessons/math_lesson.md\n"
                    "```markdown\n"
                    "# Math Lesson\n"
                    "\n"
                    "See [API Reference](../api/math_geom_api.md).\n"
                    "```\n"
                    "Deliverable: README.md\n"
                    "```markdown\n"
                    "## Lesson Blocks\n"
                    "- [Math lesson](docs/lessons/math_lesson.md)\n"
                    "```\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-doc-links.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "write docs",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": [
                "docs/lessons/math_lesson.md",
                "README.md (updated section)",
            ],
            "evidence_requirements": [
                "Rendered markdown files in docs/lessons/ and docs/api/",
                "README section added with hyperlinks.",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None


@pytest.mark.anyio
async def test_chat_assign_test_execution_fails_when_reports_are_mocked(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "QA Report\n"
                    "The content of the reports is reproduced below (mocked but representative).\n"
                    "All 54 test cases passed.\n"
                    "Coverage report: coverage/report.html\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-mocked.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "run tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["tests/lesson_blocks/math_geometry.test.ts", "coverage/report.html"],
            "evidence_requirements": ["Executed test command output", "Coverage report artifact"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "mocked" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_test_execution_runs_in_repo_workspace_without_scheduler(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M lesson_blocks/geometry.py"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "lesson_blocks/geometry.py", "content": "def calculate_rectangle_area(a, b):\n    return a * b\n"},
            {"path": "tests/test_geometry.py", "content": "def test_stub():\n    assert True\n"},
        ],
    )
    monkeypatch.setattr(
        projects_module,
        "_write_assignment_files",
        lambda *, root, candidates, overwrite: [{"path": item["path"], "status": "created"} for item in candidates],
    )
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "collected 1 items\n1 passed in 0.02s\nTOTAL 90%\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-execution.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage report artifact"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None
    assert updated.result.get("executed_commands")
    assert updated.result.get("exit_code") == 0


@pytest.mark.anyio
async def test_chat_assign_python_test_execution_writes_requested_text_report(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M src/lessons/math_lesson.py"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/lessons/math_lesson.py", "content": "def add(a, b):\n    return a + b\n"},
            {"path": "tests/lessons/test_math_lesson.py", "content": "def test_stub():\n    assert True\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        written = []
        for item in candidates:
            path = root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["content"], encoding="utf-8")
            written.append({"path": item["path"], "status": "created"})
        return written

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "collected 1 items\n1 passed in 0.02s\nTOTAL 90%\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-python-text-report.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage_report.txt"],
            "evidence_requirements": [
                "Executed test command output",
                "Coverage report artifact",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None
    artifacts = updated.result.get("artifacts") or []
    report = next(item for item in artifacts if item.get("path") == "coverage_report.txt")
    assert "TOTAL 90%" in str(report.get("content") or "")


@pytest.mark.anyio
async def test_chat_assign_test_execution_fails_when_generated_tests_do_not_match_repo_runtime(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (repo_root / "GlobeIQ.Tests.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M GlobeIQ.Tests.csproj"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/lessons/math_lesson.py", "content": "def add(a, b):\n    return a + b\n"},
            {"path": "tests/lessons/test_math_lesson.py", "content": "def test_stub():\n    assert True\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-runtime-mismatch.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage_report.txt"],
            "evidence_requirements": [
                "Executed test command output",
                "Coverage report artifact",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "do not introduce a new runtime for this repo" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_tester_step_uses_internal_execution_even_when_step_kind_is_mislabeled(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for mislabeled tester execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M lesson_blocks/geometry.py"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "lesson_blocks/geometry.py", "content": "def calculate_rectangle_area(a, b):\n    return a * b\n"},
            {"path": "tests/test_geometry.py", "content": "def test_stub():\n    assert True\n"},
        ],
    )
    monkeypatch.setattr(
        projects_module,
        "_write_assignment_files",
        lambda *, root, candidates, overwrite: [{"path": item["path"], "status": "created"} for item in candidates],
    )
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "1 passed in 0.01s\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-mislabeled-tester.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "test new lesson blocks and conduct code review",
            "role_hint": "tester",
            "step_kind": "planning",
            "deliverables": ["tests/test_geometry.py", "TestResults.xml"],
            "evidence_requirements": ["Executed test command output", "Pass/fail or coverage evidence from the test run"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None
    executed = updated.result.get("executed_commands") or []
    assert executed
    assert executed[-1]["command"][:3] == ["py", "-m", "pytest"]
    assert updated.result.get("failure_type") == "implementation_issue"
    assert "missing report files" in " ".join(updated.result.get("evidence") or []).lower()


@pytest.mark.anyio
async def test_bot_trigger_final_qc_payload_from_tester_is_not_misclassified_as_test_execution(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            if task.bot_id == "pm-final-qc":
                return {"outcome": "fail", "failure_type": "test_failure", "findings": ["awaiting real execution"], "evidence": [], "artifacts": [], "handoff_notes": "needs real tests", "commit_message": ""}
            return {"ok": True}

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    tests_project = repo_root / "GlobeIQ.Server.Tests" / "GlobeIQ.Server.Tests.csproj"
    tests_project.parent.mkdir(parents=True, exist_ok=True)
    tests_project.write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": ["?? GlobeIQ.Server.Tests/BlockCatalogSeederTests.cs"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "GlobeIQ.Server/Services/BlockCatalogSeeder.cs", "content": "public class BlockCatalogSeeder {}\n"},
            {"path": "GlobeIQ.Server.Tests/BlockCatalogSeederTests.cs", "content": "public class BlockCatalogSeederTests {}\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        assert args[:2] == ["dotnet", "test"]
        return {
            "ok": False,
            "command": args,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": "command not found: dotnet",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    bot_registry = BotRegistry(db_path=str(tmp_path / "final-qc-trigger-bots.db"))
    await bot_registry.register(
        Bot(
            id="pm-tester",
            name="PM Tester",
            role="tester",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "tester-env-blocker-final-qc",
                        "event": "task_completed",
                        "target_bot_id": "pm-final-qc",
                        "condition": "has_result",
                        "result_field": "failure_type",
                        "result_equals": "environment_blocker",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="pm-final-qc", name="PM Final QC", role="final-qc", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "final-qc-trigger-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "Run tests for generated files",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["GlobeIQ.Server.Tests/BlockCatalogSeederTests.cs", "artifacts/test_results.json"],
            "evidence_requirements": ["Executed test command output"],
        },
        metadata=TaskMetadata(source="bot_trigger", project_id="proj-1", orchestration_id="orch-1"),
    )

    for _ in range(40):
        tasks = await tm.list_tasks()
        if len(tasks) >= 2 and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    final_qc = next(task for task in tasks if task.id != root.id and task.bot_id == "pm-final-qc")
    assert final_qc.status == "completed"
    assert final_qc.error is None
    assert final_qc.payload["role_hint"] == "final-qc"
    assert final_qc.payload["step_kind"] == "review"
    assert final_qc.result["failure_type"] == "test_failure"


@pytest.mark.anyio
async def test_chat_assign_repo_change_for_test_file_generation_does_not_use_internal_execution(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: tests/lesson_blocks/math_geometry_tests.cs\n"
                    "```csharp\n"
                    "using Xunit;\n"
                    "\n"
                    "public class MathGeometryTests\n"
                    "{\n"
                    "    [Fact]\n"
                    "    public void Placeholder()\n"
                    "    {\n"
                    "        Assert.True(true);\n"
                    "    }\n"
                    "}\n"
                    "```\n"
                )
            }

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )
    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M src/ui/components/MathGeometryBlock.razor"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/ui/components/MathGeometryBlock.razor", "content": "<div>existing block</div>\n"},
        ],
    )
    monkeypatch.setattr(
        projects_module,
        "_write_assignment_files",
        lambda *, root, candidates, overwrite: [{"path": item["path"], "status": "created"} for item in candidates],
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-create-test-files.db"))
    task = await tm.create_task(
        bot_id="pm-coder",
        payload={
            "instruction": "create test files for math geometry blocks",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": ["tests/lesson_blocks/math_geometry_tests.cs"],
            "evidence_requirements": ["Proposed repo file artifacts"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None
    assert "tests/lesson_blocks/math_geometry_tests.cs" in str(updated.result)


@pytest.mark.anyio
async def test_chat_assign_repo_change_fails_early_when_generated_files_mismatch_repo_runtime(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            return {
                "output": (
                    "Deliverable: src/lessons/math_lesson.py\n"
                    "```python\n"
                    "class MathLessonBlock:\n"
                    "    pass\n"
                    "```\n"
                )
            }

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (repo_root / "GlobeIQ.Tests.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )
    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-repo-runtime-mismatch.db"))
    task = await tm.create_task(
        bot_id="pm-coder",
        payload={
            "instruction": "implement geometry lesson block code",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "deliverables": ["src/lessons/math_lesson.py"],
            "evidence_requirements": ["Proposed repo file artifacts"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "introduce unsupported runtime" in updated.error.message.lower()
    assert "python" in updated.error.message.lower()
    assert "dotnet" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_test_execution_detects_and_runs_generated_dotnet_tests(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (repo_root / "GlobeIQ.Tests.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M src/lessons/MathLesson.cs"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/lessons/MathLesson.cs", "content": "namespace GlobeIQ.Lessons { public class MathLesson {} }\n"},
            {"path": "tests/MathLessonTests.cs", "content": "public class MathLessonTests {}\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        if args[:2] == ["dotnet", "test"]:
            coverage_file = repo_root / ".nexusai_test_results" / "run" / "coverage.cobertura.xml"
            coverage_file.parent.mkdir(parents=True, exist_ok=True)
            coverage_file.write_text("<coverage />\n", encoding="utf-8")
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "Passed!  Total tests: 1\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-execution-dotnet.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage/report.xml"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result is not None
    executed = updated.result.get("executed_commands") or []
    assert executed
    assert executed[-1]["command"][:2] == ["dotnet", "test"]
    artifacts = updated.result.get("artifacts") or []
    assert any(str(item.get("path") or "") == "coverage/report.xml" for item in artifacts)


@pytest.mark.anyio
async def test_chat_assign_test_execution_reports_missing_dotnet_toolchain(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": ["?? tests/GeometryLessonServiceTests.cs"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/Services/GeometryLessonService.cs", "content": "public class GeometryLessonService {}\n"},
            {"path": "tests/GeometryLessonServiceTests.cs", "content": "public class GeometryLessonServiceTests {}\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        assert args[:2] == ["dotnet", "test"]
        return {
            "ok": False,
            "command": args,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": "command not found: dotnet",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-execution-missing-dotnet.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage report"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.error is None
    assert updated.result is not None
    assert updated.result.get("missing_tools") == ["dotnet"]
    assert updated.result.get("outcome") == "fail"
    assert updated.result.get("failure_type") == "environment_blocker"
    artifacts = updated.result.get("artifacts") or []
    assert any(str(item.get("path") or "") == "test_logs/assignment_test_execution.log" for item in artifacts)


@pytest.mark.anyio
async def test_chat_assign_test_execution_runs_mixed_node_and_dotnet_tests(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M src/components/lessons/GeometryBlock.tsx"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "src/components/lessons/GeometryBlock.tsx", "content": "export default function GeometryBlock() { return null; }\n"},
            {"path": "src/components/lessons/__tests__/GeometryBlock.test.tsx", "content": "test('ok', () => expect(true).toBe(true));\n"},
            {"path": "Controllers/GeometryLessonController.cs", "content": "public class GeometryLessonController {}\n"},
            {"path": "Tests/GeometryLessonControllerTests.cs", "content": "public class GeometryLessonControllerTests {}\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        if args[:2] == ["npm", "test"]:
            return {
                "ok": True,
                "command": args,
                "exit_code": 0,
                "stdout": "PASS src/components/lessons/__tests__/GeometryBlock.test.tsx\n",
                "stderr": "",
                "resource_usage": {},
            }
        if args[:2] == ["dotnet", "test"]:
            return {
                "ok": True,
                "command": args,
                "exit_code": 0,
                "stdout": "Passed!  Total tests: 1\n",
                "stderr": "",
                "resource_usage": {},
            }
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-execution-mixed.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage reports"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    executed = updated.result.get("executed_commands") or []
    labels = [str(item.get("label") or "") for item in executed]
    assert "test_execution_node" in labels
    assert "test_execution_dotnet" in labels


@pytest.mark.anyio
async def test_bot_trigger_tester_step_uses_internal_execution_and_sees_triggered_coder_outputs(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            if task.bot_id == "pm-tester":
                raise AssertionError("scheduler should not be used for trigger-spawned internal test execution")
            return {"status": "complete", "artifacts": []}

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    tests_project = repo_root / "GlobeIQ.Server.Tests" / "GlobeIQ.Server.Tests.csproj"
    tests_project.parent.mkdir(parents=True, exist_ok=True)
    tests_project.write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M GlobeIQ.Server.Tests/GeometryLessonServiceTests.cs"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)

    seen_task_ids: List[str] = []

    def _assignment_file_candidates(tasks):
        seen_task_ids.extend(str(task.id) for task in tasks)
        assert any(
            task.bot_id == "pm-coder"
            and task.metadata
            and task.metadata.source == "bot_trigger"
            for task in tasks
        )
        return [
            {"path": "GlobeIQ.Server/Services/GeometryLessonService.cs", "content": "public class GeometryLessonService {}\n"},
            {"path": "GlobeIQ.Server.Tests/GeometryLessonServiceTests.cs", "content": "public class GeometryLessonServiceTests {}\n"},
        ]

    monkeypatch.setattr(projects_module, "_assignment_file_candidates", _assignment_file_candidates)

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        assert args[:2] == ["dotnet", "test"]
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "Passed!  Total tests: 1\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "bot-trigger-test-execution.db"))
    coder_task = await tm.create_task(
        bot_id="pm-coder",
        payload={"instruction": "implement the workstream", "role_hint": "coder", "step_kind": "repo_change"},
        metadata=TaskMetadata(
            source="bot_trigger",
            project_id="proj-1",
            orchestration_id="orch-1",
            parent_task_id="task-engineer",
        ),
    )

    for _ in range(40):
        updated_coder = await tm.get_task(coder_task.id)
        if updated_coder.status == "completed":
            break
        await asyncio.sleep(0.05)

    tester_task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage report"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="bot_trigger",
            project_id="proj-1",
            orchestration_id="orch-1",
            parent_task_id=coder_task.id,
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(tester_task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.error is None
    assert coder_task.id in seen_task_ids
    executed = updated.result.get("executed_commands") or []
    assert executed
    assert executed[-1]["command"][:2] == ["dotnet", "test"]
    assert updated.result.get("failure_type") == "pass"


@pytest.mark.anyio
async def test_bot_trigger_tester_execution_is_scoped_to_same_fanout_branch(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            if task.bot_id == "pm-tester":
                raise AssertionError("scheduler should not be used for trigger-spawned internal test execution")
            return {"status": "complete", "artifacts": []}

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    tests_project = repo_root / "GlobeIQ.Server.Tests" / "GlobeIQ.Server.Tests.csproj"
    tests_project.parent.mkdir(parents=True, exist_ok=True)
    tests_project.write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": [" M GlobeIQ.Server.Tests/GeometryLessonServiceTests.cs"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)

    seen_task_ids: List[str] = []

    def _assignment_file_candidates(tasks):
        seen_task_ids.extend(str(task.id) for task in tasks)
        return [
            {"path": "GlobeIQ.Server/Services/GeometryLessonService.cs", "content": "public class GeometryLessonService {}\n"},
            {"path": "GlobeIQ.Server.Tests/GeometryLessonServiceTests.cs", "content": "public class GeometryLessonServiceTests {}\n"},
        ]

    monkeypatch.setattr(projects_module, "_assignment_file_candidates", _assignment_file_candidates)

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)
    monkeypatch.setattr(projects_module, "_bootstrap_command_specs", lambda root, languages: [])

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        assert args[:2] == ["dotnet", "test"]
        return {
            "ok": True,
            "command": args,
            "exit_code": 0,
            "stdout": "Passed!  Total tests: 1\n",
            "stderr": "",
            "resource_usage": {},
        }

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "bot-trigger-test-scope.db"))
    coder_branch_one = await tm.create_task(
        bot_id="pm-coder",
        payload={
            "instruction": "implement branch one",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "fanout_id": "fanout:test",
            "fanout_branch_key": "branch-one",
        },
        metadata=TaskMetadata(
            source="bot_trigger",
            project_id="proj-1",
            orchestration_id="orch-1",
            parent_task_id="task-engineer",
        ),
    )
    coder_branch_two = await tm.create_task(
        bot_id="pm-coder",
        payload={
            "instruction": "implement branch two",
            "role_hint": "coder",
            "step_kind": "repo_change",
            "fanout_id": "fanout:test",
            "fanout_branch_key": "branch-two",
        },
        metadata=TaskMetadata(
            source="bot_trigger",
            project_id="proj-1",
            orchestration_id="orch-1",
            parent_task_id="task-engineer",
        ),
    )

    for _ in range(40):
        branch_one = await tm.get_task(coder_branch_one.id)
        branch_two = await tm.get_task(coder_branch_two.id)
        if branch_one.status == "completed" and branch_two.status == "completed":
            break
        await asyncio.sleep(0.05)

    tester_task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage report"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
            "workstream": {
                "deliverables": [
                    "GlobeIQ.Server/Services/GeometryLessonService.cs",
                    "GlobeIQ.Server.Tests/GeometryLessonServiceTests.cs",
                ]
            },
            "fanout_id": "fanout:test",
            "fanout_branch_key": "branch-one",
        },
        metadata=TaskMetadata(
            source="bot_trigger",
            project_id="proj-1",
            orchestration_id="orch-1",
            parent_task_id=coder_branch_one.id,
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(tester_task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert coder_branch_one.id in seen_task_ids
    assert coder_branch_two.id not in seen_task_ids


def test_assignment_execution_language_inherits_repo_runtime_over_generated_python_tests(tmp_path):
    from control_plane.task_manager.task_manager import _assignment_execution_language, _assignment_execution_languages

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "GlobeIQ.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (root / "GlobeIQ.Tests.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")

    languages = _assignment_execution_languages(
        applied_paths=["src/lessons/geometry.py", "tests/test_geometry.py"],
        test_files=["tests/test_geometry.py"],
        root=root,
    )
    language = _assignment_execution_language(
        applied_paths=["src/lessons/geometry.py", "tests/test_geometry.py"],
        test_files=["tests/test_geometry.py"],
        root=root,
    )

    assert languages == []
    assert language == "dotnet"


def test_assignment_execution_languages_support_mixed_node_and_dotnet(tmp_path):
    from control_plane.task_manager.task_manager import _assignment_execution_languages

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)

    languages = _assignment_execution_languages(
        applied_paths=[
            "src/components/lessons/GeometryBlock.tsx",
            "src/components/lessons/__tests__/GeometryBlock.test.tsx",
            "Controllers/GeometryLessonController.cs",
            "Tests/GeometryLessonControllerTests.cs",
        ],
        test_files=[
            "src/components/lessons/__tests__/GeometryBlock.test.tsx",
            "Tests/GeometryLessonControllerTests.cs",
        ],
        root=root,
    )

    assert languages == ["node", "dotnet"]


def test_assignment_execution_languages_support_go_rust_and_cpp(tmp_path):
    from control_plane.task_manager.task_manager import _assignment_execution_languages

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)

    languages = _assignment_execution_languages(
        applied_paths=[
            "cmd/service/main.go",
            "tests/example_test.go",
            "crates/core/src/lib.rs",
            "tests/integration_tests.rs",
            "native/tests/geometry_test.cpp",
        ],
        test_files=[
            "tests/example_test.go",
            "tests/integration_tests.rs",
            "native/tests/geometry_test.cpp",
        ],
        root=root,
    )

    assert languages == ["go", "rust", "cpp"]


def test_docs_only_tester_payload_does_not_use_internal_assignment_execution():
    from control_plane.task_manager.task_manager import _looks_like_assignment_test_execution_payload

    payload = {
        "title": "Validate documentation updates",
        "instruction": "Validate the implemented workstream with repo-appropriate checks.",
        "role_hint": "tester",
        "step_kind": "test_execution",
        "deliverables": ["Test run log artifact", "Coverage or test results artifact"],
        "evidence_requirements": ["Executed test command output"],
        "workstream": {
            "title": "Documentation updates",
            "deliverables": ["docs/ui_changes.md", "RELEASE_NOTES.md", "docs/qa_checklist.md"],
        },
    }

    assert _looks_like_assignment_test_execution_payload(payload) is False


def test_assignment_validation_rejects_non_doc_repo_artifacts_for_docs_only_requests():
    from control_plane.task_manager.task_manager import _assignment_validation_error
    from shared.models import Task, TaskMetadata

    task = Task(
        id="task-docs-only",
        bot_id="pm-coder",
        payload={
            "title": "Build lesson-block documentation",
            "instruction": (
                "Build documentation for the lesson blocks in docs/blocks. "
                "I am expecting only .md documents and no other code edited."
            ),
            "step_kind": "repo_change",
            "deliverables": ["docs/blocks/lesson-blocks.md"],
        },
        metadata=TaskMetadata(source="chat_assign"),
        created_at="2026-03-19T00:00:00+00:00",
        updated_at="2026-03-19T00:00:00+00:00",
    )

    result = {
        "artifacts": [
            {"path": "docs/blocks/lesson-blocks.md", "content": "# Lesson Blocks"},
            {"path": "GlobeIQ.Server/Controllers/UserLessonBlocksController.cs", "content": "// code"},
        ]
    }

    error = _assignment_validation_error(task, result)

    assert "documentation-only markdown outputs" in error
    assert "GlobeIQ.Server/Controllers/UserLessonBlocksController.cs" in error


def test_assignment_validation_allows_markdown_repo_artifacts_for_docs_only_requests():
    from control_plane.task_manager.task_manager import _assignment_validation_error
    from shared.models import Task, TaskMetadata

    task = Task(
        id="task-docs-only",
        bot_id="pm-coder",
        payload={
            "title": "Build lesson-block documentation",
            "instruction": (
                "Build documentation for the lesson blocks in docs/blocks. "
                "I am expecting only .md documents and no other code edited."
            ),
            "step_kind": "repo_change",
            "deliverables": ["docs/blocks/lesson-blocks.md"],
        },
        metadata=TaskMetadata(source="chat_assign"),
        created_at="2026-03-19T00:00:00+00:00",
        updated_at="2026-03-19T00:00:00+00:00",
    )

    result = {
        "artifacts": [
            {"path": "docs/blocks/lesson-blocks.md", "content": "# Lesson Blocks"},
            {"path": "docs/blocks/graphing.md", "content": "# Graphing"},
        ]
    }

    assert _assignment_validation_error(task, result) == ""


@pytest.mark.anyio
async def test_chat_assign_test_execution_runs_generated_go_tests(tmp_path, monkeypatch):
    import asyncio

    from control_plane.api import projects as projects_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Project, TaskMetadata

    class StubProjectRegistry:
        async def get(self, project_id):
            return Project(
                id=project_id,
                name="Proj",
                settings_overrides={
                    "repo_workspace": {
                        "enabled": True,
                        "managed_path_mode": False,
                        "root_path": str(tmp_path / "repo"),
                        "allow_command_execution": True,
                    }
                },
            )

    class StubScheduler:
        def __init__(self):
            self.project_registry = StubProjectRegistry()

        async def schedule(self, task):
            raise AssertionError("scheduler should not be used for internal test execution")

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    monkeypatch.setattr(projects_module, "_extract_project_repo_workspace", lambda project: project.settings_overrides["repo_workspace"])
    monkeypatch.setattr(projects_module, "_resolve_repo_workspace_root", lambda project_id, cfg, require_enabled=True: repo_root)

    async def _snapshot(*, root, cfg):
        return {"is_repo": True, "branch": "main", "clean": False, "porcelain": ["?? tests/example_test.go"]}

    monkeypatch.setattr(projects_module, "_repo_status_snapshot", _snapshot)
    monkeypatch.setattr(
        projects_module,
        "_assignment_file_candidates",
        lambda tasks: [
            {"path": "internal/geometry/area.go", "content": "package geometry\n"},
            {"path": "tests/example_test.go", "content": "package tests\n"},
        ],
    )

    def _write_assignment_files(*, root, candidates, overwrite):
        applied = []
        for item in candidates:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            applied.append({"path": item["path"], "status": "created"})
        return applied

    monkeypatch.setattr(projects_module, "_write_assignment_files", _write_assignment_files)

    async def _run_repo_command(args, *, cwd, timeout_seconds=None, env_overrides=None):
        if args[:3] == ["go", "mod", "download"]:
            return {
                "ok": True,
                "command": args,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "resource_usage": {},
            }
        if args[:3] == ["go", "test", "./..."]:
            coverage_file = repo_root / "coverage" / "report.out"
            coverage_file.parent.mkdir(parents=True, exist_ok=True)
            coverage_file.write_text("mode: set\n", encoding="utf-8")
            return {
                "ok": True,
                "command": args,
                "exit_code": 0,
                "stdout": "ok\texample.com/demo/tests\t0.123s\tcoverage: 81.0% of statements\n",
                "stderr": "",
                "resource_usage": {},
            }
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(projects_module, "_run_repo_command", _run_repo_command)

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-test-execution-go.db"))
    task = await tm.create_task(
        bot_id="pm-tester",
        payload={
            "instruction": "run generated tests",
            "role_hint": "tester",
            "step_kind": "test_execution",
            "deliverables": ["coverage/report.out"],
            "evidence_requirements": [
                "Executed test command output",
                "Pass/fail or coverage evidence from the test run",
            ],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    executed = updated.result.get("executed_commands") or []
    labels = [str(item.get("label") or "") for item in executed]
    assert "go_mod_download" in labels
    assert "test_execution_go" in labels
    artifacts = updated.result.get("artifacts") or []
    assert any(str(item.get("path") or "") == "coverage/report.out" for item in artifacts)


@pytest.mark.anyio
async def test_chat_assign_release_fails_when_output_is_only_checklist_guidance(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Final Merge & Release Step\n"
                    "These findings should be verified before the PR is merged.\n"
                    "Use this as a checklist when completing step 6.\n"
                    "Tag URL: https://github.com/globeiq/globeiq/releases/tag/v1.4.0\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-release-guidance.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={
            "instruction": "merge and release",
            "role_hint": "security-reviewer",
            "step_kind": "release",
            "deliverables": ["Merged PR #<number>", "Git tag vX.Y.Z", "RELEASE_NOTES.md entry"],
            "evidence_requirements": ["Merged pull request URL", "Merge commit SHA", "Release tag URL"],
        },
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "commit sha evidence" in updated.error.message.lower() or "checklist" in updated.error.message.lower()


@pytest.mark.anyio
async def test_chat_assign_reviewer_guidance_output_fails_without_evidence(tmp_path, monkeypatch):
    import asyncio

    from control_plane.task_manager import task_manager as task_manager_module
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {
                "output": (
                    "Security & Quality Review\n"
                    "Suggested review commands:\n"
                    "npm audit --production\n"
                    "Please proceed with the execution steps and report back any failures.\n"
                )
            }

    monkeypatch.setattr(
        task_manager_module,
        "_settings_int",
        lambda name, default: 0 if name == "max_task_retries" else default,
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "chat-assign-review-guidance.db"))
    task = await tm.create_task(
        bot_id="bot1",
        payload={"instruction": "review and merge", "role_hint": "security-reviewer"},
        metadata=TaskMetadata(
            source="chat_assign",
            project_id="proj-1",
            orchestration_id="orch-1",
        ),
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "failed"
    assert updated.error is not None
    assert "review evidence" in updated.error.message.lower()


@pytest.mark.anyio
async def test_payload_transform_supports_coalesce_paths_for_retry_loops(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot

    class StubScheduler:
        async def schedule(self, task):
            return {"ignored": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "coalesce-bots.db"))
    await bot_registry.register(
        Bot(
            id="outline-bot",
            name="Outline",
            role="assistant",
            backends=[],
            routing_rules={
                "output_contract": {
                    "enabled": True,
                    "mode": "payload_transform",
                    "format": "json_object",
                    "required_fields": ["course_brief", "generation_settings"],
                    "template": {
                        "course_brief": "{{coalesce:payload.source_result.course_brief,payload.source_payload.source_result.course_brief,payload.source_payload.source_payload.source_result.course_brief}}",
                        "generation_settings": "{{coalesce:payload.source_result.generation_settings,payload.source_payload.source_result.generation_settings,payload.source_payload.source_payload.source_result.generation_settings}}",
                    },
                }
            },
        )
    )

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "coalesce.db"), bot_registry=bot_registry)
    task = await tm.create_task(
        bot_id="outline-bot",
        payload={
            "source_result": {
                "qc_status": "fail",
            },
            "source_payload": {
                "source_result": {
                    "course_brief": {"topic": "AP World History"},
                    "generation_settings": {"generate_documentation": True},
                }
            },
        },
    )

    for _ in range(40):
        updated = await tm.get_task(task.id)
        if updated.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)

    assert updated.status == "completed"
    assert updated.result["course_brief"]["topic"] == "AP World History"
    assert updated.result["generation_settings"]["generate_documentation"] is True


@pytest.mark.anyio
async def test_task_manager_migrates_legacy_task_table_without_metadata(tmp_path):
    import asyncio
    import sqlite3

    from control_plane.task_manager.task_manager import TaskManager

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            bot_id TEXT,
            payload TEXT,
            status TEXT,
            result TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_dependencies (
            task_id TEXT NOT NULL,
            depends_on_task_id TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on_task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE bot_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE,
            bot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE bot_run_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "ok"}

    tm = TaskManager(StubScheduler(), db_path=str(db_path))
    task = await tm.create_task(
        bot_id="legacy-bot",
        payload={"instruction": "start"},
        metadata=None,
    )

    for _ in range(30):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(task.id)
    assert updated.status == "completed"


@pytest.mark.anyio
async def test_task_manager_ignores_legacy_dashboard_tasks_table_shape(tmp_path):
    import asyncio
    import sqlite3

    from control_plane.task_manager.task_manager import TaskManager

    db_path = tmp_path / "shared.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            result TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    class StubScheduler:
        async def schedule(self, task):
            return {"output": "ok"}

    tm = TaskManager(StubScheduler(), db_path=str(db_path))
    task = await tm.create_task(bot_id="course-intake", payload={"instruction": "start"})

    for _ in range(30):
        updated = await tm.get_task(task.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(task.id)


@pytest.mark.anyio
async def test_trigger_skipped_for_orchestrated_tasks(tmp_path):
    """For orchestrated tasks: forward triggers should be skipped, backward triggers should fire."""
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            return {"done": True}

    bot_registry = BotRegistry(db_path=str(tmp_path / "orchestrated-trigger-skip.db"))
    # bot-a has BOTH forward trigger (to bot-b) and backward trigger (to bot-c)
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    # Forward trigger - should be skipped for orchestrated tasks
                    {
                        "id": "forward-handoff",
                        "event": "task_completed",
                        "target_bot_id": "bot-b",
                        "condition": "has_result",
                    },
                    # Backward trigger - should fire for orchestrated tasks on failure
                    {
                        "id": "backward-error",
                        "event": "task_failed",
                        "target_bot_id": "bot-c",
                        "condition": "has_error",
                    },
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="bot-b", name="Bot B", role="assistant", backends=[]))
    await bot_registry.register(Bot(id="bot-c", name="Bot C", role="assistant", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "orchestrated-trigger-skip-tasks.db"), bot_registry=bot_registry)
    
    # Create a task WITH orchestration_id (simulating orchestrated task)
    root = await tm.create_task(
        bot_id="bot-a",
        payload={"instruction": "start"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="test-orchestration-123",
        ),
    )

    # Wait for task to complete
    for _ in range(30):
        updated = await tm.get_task(root.id)
        if updated.status == "completed":
            break
        await asyncio.sleep(0.1)

    updated = await tm.get_task(root.id)
    assert updated.status == "completed"

    await asyncio.sleep(0.3)  # Give time for any trigger dispatch attempt

    tasks = await tm.list_tasks()
    # Should only have 1 task - the forward trigger should be skipped (orchestrator manages forward progression)
    assert len(tasks) == 1, f"Expected 1 task (forward trigger skipped), got {len(tasks)}"
    assert tasks[0].bot_id == "bot-a"
    assert tasks[0].metadata.orchestration_id == "test-orchestration-123"


async def test_backward_trigger_fires_for_orchestrated_failed_tasks(tmp_path):
    """Backward triggers (failure routing) should fire for orchestrated tasks that fail."""
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata, TaskError

    class FailingScheduler:
        async def schedule(self, task):
            # Raise an exception to actually fail the task
            raise RuntimeError("Something went wrong")

    bot_registry = BotRegistry(db_path=str(tmp_path / "orchestrated-backward-trigger.db"))
    # bot-a has backward trigger for failure routing
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="assistant",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "backward-error",
                        "event": "task_failed",
                        "target_bot_id": "bot-c",
                        "condition": "has_error",
                    },
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="bot-c", name="Bot C", role="assistant", backends=[]))

    tm = TaskManager(FailingScheduler(), db_path=str(tmp_path / "orchestrated-backward-trigger-tasks.db"), bot_registry=bot_registry)
    
    # Create a task WITH orchestration_id (simulating orchestrated task)
    root = await tm.create_task(
        bot_id="bot-a",
        payload={"instruction": "start"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="test-orchestration-456",
        ),
    )

    # Wait for task to fail and trigger to fire
    for _ in range(30):
        tasks = await tm.list_tasks()
        if len(tasks) >= 2:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    # Should have 2 tasks: original (failed) + triggered bot-c (backward routing)
    assert len(tasks) == 2, f"Expected 2 tasks (backward trigger should fire), got {len(tasks)}"
    bot_ids = {t.bot_id for t in tasks}
    assert "bot-a" in bot_ids
    assert "bot-c" in bot_ids, "Backward trigger should have created bot-c task"
    
    # Original task should be failed
    bot_a_task = next(t for t in tasks if t.bot_id == "bot-a")
    assert bot_a_task.status == "failed", f"bot-a task should be failed, got {bot_a_task.status}"
    assert bot_a_task.metadata.orchestration_id == "test-orchestration-456"


@pytest.mark.anyio
async def test_plan_managed_fanout_forward_trigger_is_allowed(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class FanoutScheduler:
        async def schedule(self, task):
            if task.bot_id == "bot-a":
                return {
                    "implementation_workstreams": [
                        {
                            "title": "Implement branch one",
                            "instruction": "Change the first file set.",
                            "acceptance_criteria": ["Branch one is complete."],
                            "deliverables": ["src/branch_one.py"],
                            "quality_gates": ["Branch one matches plan."],
                            "evidence_requirements": ["Changed-file artifact for branch one."],
                        },
                        {
                            "title": "Implement branch two",
                            "instruction": "Change the second file set.",
                            "acceptance_criteria": ["Branch two is complete."],
                            "deliverables": ["src/branch_two.py"],
                            "quality_gates": ["Branch two matches plan."],
                            "evidence_requirements": ["Changed-file artifact for branch two."],
                        },
                    ]
                }
            return {"status": "ok"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "orchestrated-fanout-forward.db"))
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="engineer",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "forward-fanout",
                        "event": "task_completed",
                        "target_bot_id": "bot-b",
                        "condition": "has_result",
                        "fan_out_field": "source_result.implementation_workstreams",
                        "fan_out_alias": "workstream",
                        "fan_out_index_alias": "workstream_index",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="bot-b", name="Bot B", role="coder", backends=[]))

    tm = TaskManager(FanoutScheduler(), db_path=str(tmp_path / "orchestrated-fanout-forward-tasks.db"), bot_registry=bot_registry)

    root = await tm.create_task(
        bot_id="bot-a",
        payload={"instruction": "plan the branches"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="test-orchestration-fanout",
            step_id="step_2",
        ),
    )

    for _ in range(40):
        tasks = await tm.list_tasks(orchestration_id="test-orchestration-fanout")
        if len(tasks) == 3 and all(task.status == "completed" for task in tasks):
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks(orchestration_id="test-orchestration-fanout")
    assert len(tasks) == 3

    root_task = next(task for task in tasks if task.id == root.id)
    child_tasks = [task for task in tasks if task.id != root.id]

    assert root_task.metadata.orchestration_id == "test-orchestration-fanout"
    assert {task.bot_id for task in child_tasks} == {"bot-b"}
    assert sorted(str(task.payload.get("title") or "") for task in child_tasks) == [
        "Implement branch one",
        "Implement branch two",
    ]
    assert sorted(str(task.payload.get("instruction") or "") for task in child_tasks) == [
        "Change the first file set.",
        "Change the second file set.",
    ]
    assert all(task.metadata and task.metadata.source == "bot_trigger" for task in child_tasks)


def test_assignment_test_source_files_recognize_dotnet_test_projects() -> None:
    from control_plane.task_manager.task_manager import _assignment_test_source_files

    detected = _assignment_test_source_files(
        [
            "GlobeIQ.Server.Tests/Geometry/GeometryLessonServiceTests.cs",
            "GlobeIQ.WebApp.Tests/Pages/GeometryLessonTests.cs",
            "CoverageReport.xml",
        ]
    )

    assert detected == [
        "GlobeIQ.Server.Tests/Geometry/GeometryLessonServiceTests.cs",
        "GlobeIQ.WebApp.Tests/Pages/GeometryLessonTests.cs",
    ]


@pytest.mark.anyio
async def test_backward_reroute_can_progress_forward_again_for_trigger_spawned_tasks(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class MixedScheduler:
        async def schedule(self, task):
            if task.bot_id == "bot-a":
                raise RuntimeError("tester failed")
            return {"status": "ok"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "reroute-forward-again.db"))
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="tester",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "tester-back-coder",
                        "event": "task_failed",
                        "target_bot_id": "bot-c",
                        "condition": "has_error",
                    },
                ]
            },
        )
    )
    await bot_registry.register(
        Bot(
            id="bot-c",
            name="Bot C",
            role="coder",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "coder-forward-review",
                        "event": "task_completed",
                        "target_bot_id": "bot-d",
                        "condition": "has_result",
                    },
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="bot-d", name="Bot D", role="reviewer", backends=[]))

    tm = TaskManager(MixedScheduler(), db_path=str(tmp_path / "reroute-forward-again-tasks.db"), bot_registry=bot_registry)

    await tm.create_task(
        bot_id="bot-a",
        payload={"instruction": "start"},
        metadata=TaskMetadata(
            source="chat_assign",
            orchestration_id="test-orchestration-reroute",
        ),
    )

    for _ in range(40):
        tasks = await tm.list_tasks()
        if len(tasks) >= 3:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks()
    assert len(tasks) == 3, f"Expected 3 tasks (failed root, remediation, resumed forward step), got {len(tasks)}"
    bot_ids = [task.bot_id for task in tasks]
    assert bot_ids.count("bot-a") == 1
    assert bot_ids.count("bot-c") == 1
    assert bot_ids.count("bot-d") == 1

    remediation = next(task for task in tasks if task.bot_id == "bot-c")
    resumed = next(task for task in tasks if task.bot_id == "bot-d")
    assert remediation.metadata.source == "bot_trigger"
    assert remediation.metadata.orchestration_id == "test-orchestration-reroute"
    assert resumed.metadata.source == "bot_trigger"
    assert resumed.metadata.parent_task_id == remediation.id


@pytest.mark.anyio
async def test_default_failure_trigger_payload_preserves_remediation_context(tmp_path):
    import asyncio

    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import Bot, TaskMetadata

    class StubScheduler:
        async def schedule(self, task):
            if task.bot_id == "bot-a":
                return {
                    "outcome": "fail",
                    "failure_type": "implementation_issue",
                    "findings": ["Null reference in controller path"],
                    "evidence": ["dotnet test failed in IssuesControllerTests"],
                    "handoff_notes": "Fix only the scoped backend workstream",
                }
            return {"status": "ok"}

    bot_registry = BotRegistry(db_path=str(tmp_path / "failure-context-bots.db"))
    await bot_registry.register(
        Bot(
            id="bot-a",
            name="Bot A",
            role="tester",
            backends=[],
            workflow={
                "triggers": [
                    {
                        "id": "tester-back-coder",
                        "event": "task_completed",
                        "target_bot_id": "bot-c",
                        "condition": "has_result",
                        "result_field": "failure_type",
                        "result_equals": "implementation_issue",
                    }
                ]
            },
        )
    )
    await bot_registry.register(Bot(id="bot-c", name="Bot C", role="coder", backends=[]))

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "failure-context-tasks.db"), bot_registry=bot_registry)
    root = await tm.create_task(
        bot_id="bot-a",
        payload={
            "title": "Validate backend API workstream",
            "instruction": "Run backend controller and service tests for the scoped workstream.",
            "acceptance_criteria": ["All API tests pass for the issues workstream."],
            "deliverables": ["GlobeIQ.Server/Controllers/IssuesController.cs", "GlobeIQ.Server.Tests/IssuesControllerTests.cs"],
            "quality_gates": ["No failing backend API tests remain."],
            "evidence_requirements": ["Executed dotnet test output"],
            "workstream": {
                "title": "Backend API & Service Layer",
                "instruction": "Implement the issues API and tests only.",
                "deliverables": ["GlobeIQ.Server/Controllers/IssuesController.cs"],
            },
            "workstream_index": 1,
            "fanout_count": 3,
            "fanout_id": "fanout:test",
            "fanout_branch_key": "backend-api",
            "source_payload": {
                "title": "Backend API & Service Layer",
                "instruction": "Implement the issues API and tests only.",
                "acceptance_criteria": ["The issues API compiles and tests pass."],
                "deliverables": ["GlobeIQ.Server/Controllers/IssuesController.cs", "GlobeIQ.Server.Tests/IssuesControllerTests.cs"],
                "quality_gates": ["No unresolved backend API defects remain."],
            },
        },
        metadata=TaskMetadata(
            source="bot_trigger",
            orchestration_id="orch-remediate",
            project_id="proj-1",
        ),
    )

    for _ in range(40):
        tasks = await tm.list_tasks(orchestration_id="orch-remediate")
        if len(tasks) >= 2:
            break
        await asyncio.sleep(0.1)

    tasks = await tm.list_tasks(orchestration_id="orch-remediate")
    remediation = next(task for task in tasks if task.id != root.id and task.bot_id == "bot-c")
    assert remediation.payload["title"] == "Backend API & Service Layer"
    assert remediation.payload["instruction"] == "Implement the issues API and tests only."
    assert remediation.payload["fanout_branch_key"] == "backend-api"
    assert remediation.payload["upstream_failure_type"] == "implementation_issue"
    assert remediation.payload["upstream_findings"] == ["Null reference in controller path"]
    assert remediation.payload["upstream_handoff_notes"] == "Fix only the scoped backend workstream"
