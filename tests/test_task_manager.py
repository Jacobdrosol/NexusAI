"""Unit tests for TaskManager."""
import json
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
async def test_manual_retry_creates_new_task_with_override(tmp_path):
    from control_plane.task_manager.task_manager import TaskManager
    from shared.models import TaskError

    class StubScheduler:
        async def schedule(self, task):
            return {"ok": True}

    tm = TaskManager(StubScheduler(), db_path=str(tmp_path / "manual-retry.db"))
    original = await tm.create_task(
        bot_id="bot1",
        payload={"q": "hello"},
        metadata=None,
    )
    await tm.update_status(original.id, "failed", error=TaskError(message="failed"))

    retried = await tm.retry_task(original.id, payload_override={"q": "fixed"})

    assert retried.id != original.id
    assert retried.bot_id == original.bot_id
    assert retried.payload == {"q": "fixed"}
    assert retried.metadata is not None
    assert retried.metadata.retry_attempt == 1
    assert retried.metadata.original_task_id == original.id
    assert retried.metadata.retry_of_task_id == original.id


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
    assert "Execution Report" in labels
    assert "Usage Report" in labels
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
    assert len(packager.payload["lesson_bundles"]) == 2
    assert packager.payload["lesson_bundles"][0]["source_result"]["approved_lesson"]["lesson_number"] == 1
    assert packager.payload["lesson_bundles"][1]["source_result"]["approved_lesson"]["lesson_number"] == 2


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

    artifacts = await tm.list_bot_run_artifacts("usage-bot")
    usage_artifact = next(a for a in artifacts if a.label == "Usage Report")
    assert '"prompt_tokens": 11' in str(usage_artifact.content)
    execution_artifact = next(a for a in artifacts if a.label == "Execution Report")
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
async def test_output_contract_fails_when_required_fields_are_missing(tmp_path):
    import asyncio

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
    assert updated.status == "completed"
