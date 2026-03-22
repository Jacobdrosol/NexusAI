import json
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import BackendConfig, Bot, Capability, Task, Worker, WorkerMetrics


def test_backend_failure_message_includes_attempts():
    from control_plane.scheduler.scheduler import _backend_failure_message

    message = _backend_failure_message(
        "task-err",
        RuntimeError("timed out"),
        ["ollama_cloud/qwen3.5:397b-cloud: timed out"],
    )

    assert "All backends failed for task task-err: timed out." in message
    assert "Attempts: ollama_cloud/qwen3.5:397b-cloud: timed out." in message


def test_cloud_timeout_reads_env(monkeypatch):
    from control_plane.scheduler.scheduler import _cloud_timeout

    monkeypatch.setenv("NEXUSAI_CLOUD_API_TIMEOUT_SECONDS", "1800")

    assert _cloud_timeout() == 1800.0


def test_cloud_timeout_prefers_settings_manager(monkeypatch):
    from control_plane.scheduler import scheduler as scheduler_module

    class _FakeSettings:
        def get(self, key, default=None):
            assert key == "cloud_backend_timeout_seconds"
            return 2400

    monkeypatch.delenv("NEXUSAI_CLOUD_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(
        scheduler_module.SettingsManager,
        "instance",
        staticmethod(lambda: _FakeSettings()),
    )

    assert scheduler_module._cloud_timeout() == 2400.0


@pytest.mark.anyio
async def test_scheduler_unpinned_backend_prefers_lower_weight_worker():
    from control_plane.scheduler.scheduler import Scheduler

    worker_a = Worker(
        id="w-a",
        name="Worker A",
        host="a.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=6, load=95.0, gpu_utilization=[90.0]),
    )
    worker_b = Worker(
        id="w-b",
        name="Worker B",
        host="b.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0, load=15.0, gpu_utilization=[10.0]),
    )
    worker_registry = AsyncMock()
    worker_registry.list.return_value = [worker_a, worker_b]
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3")

    selected = await scheduler._resolve_worker_for_llm_backend(backend)
    assert selected.id == "w-b"


@pytest.mark.anyio
async def test_scheduler_dispatch_tracks_latency_and_inflight():
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="w-lat",
        name="Worker Lat",
        host="lat.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0),
    )
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id="w-lat")
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"output": "ok"}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())
    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        out = await scheduler._dispatch_to_worker(worker, backend, payload)

    assert out["output"] == "ok"
    runtime = scheduler.get_worker_runtime_metrics()
    assert "w-lat" in runtime
    assert runtime["w-lat"]["inflight"] == 0.0
    assert runtime["w-lat"]["latency_ema_ms"] > 0.0


@pytest.mark.anyio
async def test_scheduler_injects_bot_system_prompt_into_payload():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-1",
        bot_id="course-outline",
        payload={"instruction": "build outline"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert isinstance(result["payload"], list)
    assert result["payload"][0] == {"role": "system", "content": "Return only strict JSON."}
    assert result["payload"][1]["role"] == "user"
    assert '"instruction": "build outline"' in result["payload"][1]["content"]


@pytest.mark.anyio
async def test_scheduler_injects_retry_guidance_into_payload():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.models import TaskError, TaskMetadata

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="doc-writer",
        name="Doc Writer",
        role="writer",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-retry-1",
        bot_id="doc-writer",
        payload={"instruction": "fix docs"},
        status="queued",
        created_at="now",
        updated_at="now",
        metadata=TaskMetadata(retry_attempt=1, source="auto_retry"),
        error=TaskError(
            message=(
                "Documentation output contains broken internal markdown links in generated artifacts: "
                "docs/blocks/implementation-guide.md -> ../project-context-research.md."
            )
        ),
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert isinstance(result["payload"], list)
    system_prompt = result["payload"][0]["content"]
    assert "Retry guidance:" in system_prompt
    assert "Previous attempt failed with this error:" in system_prompt
    assert "broken internal markdown links" in system_prompt
    assert "resolve internal markdown links relative to the generated file path" in system_prompt


@pytest.mark.anyio
async def test_scheduler_injects_attached_connection_schema_into_model_prompt(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-schema",
                                "kind": "http",
                                "description": "Lesson block schema",
                                "config_json": json.dumps({"base_url": "https://example.test"}),
                                "schema_text": json.dumps(
                                    {
                                        "lesson_blocks": [
                                            {
                                                "variant": "paragraph",
                                                "html": "<p>Example paragraph</p>",
                                                "options": {"textAlign": "left"},
                                            },
                                            {
                                                "code": "console.log('Hello, World!');",
                                                "language": "javascript",
                                                "theme": "dark",
                                                "showLineNumbers": True,
                                            },
                                        ]
                                    }
                                ),
                                "enabled": True,
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-lesson-writer",
        name="Course Lesson Writer",
        role="writer",
        system_prompt="Write lesson blocks as strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-ctx-1",
        bot_id="course-lesson-writer",
        payload={"instruction": "Write lesson 1"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Attached connection schemas:" in system_message
    assert "platform-schema" in system_message
    assert '"variant": "paragraph"' in system_message
    assert '"showLineNumbers": true' in system_message


@pytest.mark.anyio
async def test_scheduler_fetches_dynamic_connection_context_from_payload_items(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-blocks-api",
                                "kind": "http",
                                "description": "Remote block schema API",
                                "config_json": json.dumps({"base_url": "https://example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "name": "X-GLOBEIQ-BLOCKS-KEY", "api_key": "enc:ignored"}),
                                "schema_text": "openapi: 3.1.0",
                                "enabled": True,
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    def fake_http_connection_test(*, config, auth, schema_text, payload):
        assert auth["name"] == "X-GLOBEIQ-BLOCKS-KEY"
        block_type = str(payload.get("path") or "").split("/")[-1]
        return {
            "ok": True,
            "status": 200,
            "method": "GET",
            "url": f"https://example.test{payload.get('path')}",
            "body_preview": json.dumps(
                {
                    "blockType": block_type,
                    "schema": {"required": ["html"], "properties": {"html": {"type": "string"}}},
                    "example": {"variant": block_type, "html": "<p>Example</p>"},
                }
            ),
        }

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("dashboard.connections_service.resolve_auth_payload", lambda payload: {"type": "api_key", "name": "X-GLOBEIQ-BLOCKS-KEY", "api_key": "live-key"})
    monkeypatch.setattr("dashboard.connections_service.test_http_connection", fake_http_connection_test)

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-lesson-writer",
        name="Course Lesson Writer",
        role="writer",
        system_prompt="Write lesson blocks as strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "connection_context": {
                "enabled": True,
                "fetch_connection_name": "platform-blocks-api",
                "for_each_field": "generation_settings.allowed_lesson_blocks",
                "fetch_actions": [
                    {
                        "method": "GET",
                        "path": "/api/blocks/{{item}}",
                        "query_params": {"includeExample": "true"},
                    }
                ],
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-fetch-ctx-1",
        bot_id="course-lesson-writer",
        payload={"generation_settings": {"allowed_lesson_blocks": ["paragraph", "code"]}},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Dynamic connection fetch results:" in system_message
    assert "Fetch: /api/blocks/paragraph [paragraph]" in system_message
    assert '"blockType": "paragraph"' in system_message
    assert '"blockType": "code"' in system_message


@pytest.mark.anyio
async def test_scheduler_does_not_duplicate_existing_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-2",
        bot_id="course-outline",
        payload=[
            {"role": "system", "content": "Return only strict JSON."},
            {"role": "user", "content": "build outline"},
        ],
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"] == [
        {"role": "system", "content": "Return only strict JSON."},
        {"role": "user", "content": "build outline"},
    ]


@pytest.mark.anyio
async def test_scheduler_keeps_custom_backend_payload_unwrapped_when_connection_context_exists(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "description": "Importer connection",
                                "config_json": json.dumps({"base_url": "https://example.test"}),
                                "schema_text": "openapi: 3.1.0",
                                "enabled": True,
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt="Do not wrap payloads.",
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-custom-ctx",
        bot_id="course-importer",
        payload={"connection_actions": [{"operation_id": "createCourse"}]},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"] == {"connection_actions": [{"operation_id": "createCourse"}]}


@pytest.mark.anyio
async def test_scheduler_applies_bot_input_transform_before_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "instruction": "{{payload.instruction}}",
                    "course_brief": "{{payload.source_result.course_brief}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-3",
        bot_id="course-outline",
        payload={
            "instruction": "Build outline",
            "source_result": {
                "course_brief": {"topic": "AP World History", "subject": "History"}
            },
            "source_payload": {"noisy": True},
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"][0] == {"role": "system", "content": "Return only strict JSON."}
    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "instruction": "Build outline",
        "course_brief": {"topic": "AP World History", "subject": "History"},
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_coalesce_paths():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "course_brief": "{{coalesce:payload.source_result.course_brief,payload.source_payload.source_result.course_brief}}",
                    "generation_settings": "{{coalesce:payload.source_result.generation_settings,payload.source_payload.source_result.generation_settings}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-4",
        bot_id="course-outline",
        payload={
            "source_payload": {
                "source_result": {
                    "course_brief": {"topic": "AP World History"},
                    "generation_settings": {"generate_documentation": True},
                }
            }
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "course_brief": {"topic": "AP World History"},
        "generation_settings": {"generate_documentation": True},
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_can_render_nested_templates():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "connection_actions": "{{render:payload.generation_settings.platform_import_actions}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-render",
        bot_id="course-importer",
        payload={
            "import_package": {
                "course_package": {
                    "course_shell": {
                        "title": "World History Survey",
                    }
                }
            },
            "generation_settings": {
                "platform_import_actions": [
                    {
                        "operation_id": "createCourse",
                        "body_json": {
                            "title": "{{payload.import_package.course_package.course_shell.title}}",
                        },
                    }
                ]
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"] == {
        "connection_actions": [
            {
                "operation_id": "createCourse",
                "body_json": {"title": "World History Survey"},
            }
        ]
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_literal_fallbacks_and_list_index_paths():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "create_badge": "{{coalesce:payload.source_payload.generation_settings.badge_settings.enabled,true}}",
                    "course_title": "{{coalesce:payload.source_result.course_package.course_shell.title,payload.source_result.course_package.units.0.approved_unit_package.unit_package.title,'Generated Course'}}",
                    "first_unit_title": "{{payload.source_result.course_package.units.0.approved_unit_package.unit_package.title}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-literals",
        bot_id="course-importer",
        payload={
            "source_payload": {"generation_settings": None},
            "source_result": {
                "course_package": {
                    "course_shell": {"title": None},
                    "units": [
                        {
                            "approved_unit_package": {
                                "unit_package": {
                                    "title": "The Global Tapestry (c. 1200-1450)",
                                }
                            }
                        }
                    ],
                }
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"]["create_badge"] is True
    assert result["payload"]["course_title"] == "The Global Tapestry (c. 1200-1450)"
    assert result["payload"]["first_unit_title"] == "The Global Tapestry (c. 1200-1450)"


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_camelize_for_nested_payloads():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "coursePackage": "{{json:camelize:payload.source_result.approved_package.course_package}}",
                    "badgeSpec": "{{json:camelize:payload.source_result.approved_package.badge_spec}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-camelize",
        bot_id="course-importer",
        payload={
            "source_result": {
                "approved_package": {
                    "course_package": {
                        "course_shell": {"title": "World History Survey"},
                        "units": [
                            {
                                "unit_number": 1,
                                "unit_question_bank": {"question_count": 20},
                                "lessons": [{"lesson_number": 1, "title": "Lesson 1"}],
                            }
                        ],
                    },
                    "badge_spec": {"image_prompt": "Create a crest"},
                }
            }
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"]["coursePackage"]["courseShell"]["title"] == "World History Survey"
    assert result["payload"]["coursePackage"]["units"][0]["unitNumber"] == 1
    assert result["payload"]["coursePackage"]["units"][0]["unitQuestionBank"]["questionCount"] == 20
    assert result["payload"]["coursePackage"]["units"][0]["lessons"][0]["lessonNumber"] == 1
    assert result["payload"]["badgeSpec"]["imagePrompt"] == "Create a crest"


@pytest.mark.anyio
async def test_scheduler_custom_http_connection_backend_executes_actions(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "config_json": json.dumps({"base_url": "https://api.example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "api_key": "enc:ignored"}),
                                "schema_text": json.dumps(
                                    {
                                        "openapi": "3.1.0",
                                        "paths": {
                                            "/courses": {
                                                "post": {
                                                    "operationId": "createCourse",
                                                }
                                            }
                                        },
                                    }
                                ),
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("dashboard.connections_service.resolve_auth_payload", lambda payload: {"type": "api_key", "api_key": "live-key"})
    monkeypatch.setattr(
        "dashboard.connections_service.test_http_connection",
        lambda **kwargs: {
            "ok": True,
            "status": 201,
            "method": "POST",
            "url": "https://api.example.test/courses",
            "body_preview": "{\"id\": 42}",
        },
    )

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-http",
        bot_id="course-importer",
        payload={
            "connection": {"name": "platform-api"},
            "connection_actions": [
                {
                    "operation_id": "createCourse",
                    "body_json": {"title": "World History Survey"},
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    result = await scheduler.schedule(task)

    assert result["import_status"] == "success"
    assert result["connection_name"] == "platform-api"
    assert result["completed_actions"] == ["createCourse"]
    assert result["failed_actions"] == []
    assert result["action_results"][0]["status"] == 201


@pytest.mark.anyio
async def test_scheduler_custom_http_connection_404_import_includes_endpoint_hint(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "config_json": json.dumps({"base_url": "https://api.example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "api_key": "enc:ignored"}),
                                "schema_text": json.dumps(
                                    {
                                        "openapi": "3.1.0",
                                        "paths": {
                                            "/api/agent/import/course-package": {
                                                "post": {
                                                    "operationId": "importCoursePackage",
                                                }
                                            }
                                        },
                                    }
                                ),
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("dashboard.connections_service.resolve_auth_payload", lambda payload: {"type": "api_key", "api_key": "live-key"})
    monkeypatch.setattr(
        "dashboard.connections_service.test_http_connection",
        lambda **kwargs: {
            "ok": False,
            "status": 404,
            "method": "POST",
            "url": "https://api.example.test/api/agent/import/course-package",
            "body_preview": "{\"title\":\"Not Found\",\"status\":404}",
        },
    )

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-http-404",
        bot_id="course-importer",
        payload={
            "connection": {"name": "platform-api"},
            "connection_actions": [
                {
                    "operation_id": "importCoursePackage",
                    "body_json": {"coursePackage": {"courseShell": {"title": "World History Survey"}}},
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    result = await scheduler.schedule(task)

    assert result["import_status"] == "failed"
    assert result["failed_actions"] == ["importCoursePackage"]
    assert "Endpoint /api/agent/import/course-package is not available on the target server." in result["errors"][0]


@pytest.mark.anyio
async def test_scheduler_appends_output_contract_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Build the course outline.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "output_contract": {
                "enabled": True,
                "mode": "model_output",
                "format": "json_object",
                "required_fields": ["course_shell", "course_structure"],
                "non_empty_fields": ["course_structure.units"],
                "fallback_mode": "disabled",
                "description": "Return a structured outline only.",
                "example_output": {
                    "course_shell": {"title": "Example"},
                    "course_structure": {"units": []},
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-5",
        bot_id="course-outline",
        payload={"instruction": "Build outline"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Build the course outline." in system_message
    assert "Output contract:" in system_message
    assert "Required top-level fields: course_shell, course_structure." in system_message
    assert "Fields that must be populated: course_structure.units." in system_message
    assert "Missing or empty required fields will fail the run." in system_message
    assert "\"course_shell\"" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_docs_only_assignment_scope_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-engineer",
        name="PM Engineer",
        role="engineer",
        system_prompt="Plan the implementation workstreams.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-docs-only",
        bot_id="pm-engineer",
        payload={
            "instruction": "Combine the joined research outputs into a plan.",
            "assignment_request": "Build documentation only in docs/blocks for the mathematics blocks.",
            "assignment_scope": {
                "docs_only": True,
                "conversation_brief": (
                    "Prior user intent 1: Focus on algebra, trigonometry, statistics, calculus, and multivariable calculus.\n"
                    "Prior user intent 2: Build as much as possible in house and do not rely on the Desmos API."
                ),
                "conversation_transcript": (
                    "user: Help me plan the mathematics blocks from algebra through multivariable calculus.\n"
                    "assistant: Here is a roadmap.\n"
                    "user: Build as much as possible in house and do not rely on the Desmos API."
                ),
                "conversation_message_count": 3,
                "conversation_transcript_strategy": "full",
                "requested_output_paths": ["docs/blocks"],
                "prefer_in_house": True,
                "avoid_external_apis": True,
                "prefer_client_side_execution": True,
                "minimize_server_load": True,
                "minimize_bandwidth": True,
                "requested_outcome_style": "roadmap",
                "focus_topics": ["algebra", "trigonometry", "statistics"],
                "requested_artifact_hints": ["roadmap", "guide"],
                "constraint_hints": ["Prefer in-house or locally owned solutions."],
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Plan the implementation workstreams." in system_message
    assert "Assignment scope:" in system_message
    assert "documentation-only run" in system_message.lower()
    assert "docs/blocks" in system_message
    assert "Do not interpret documentation-only as an empty plan." in system_message
    assert "implementation_workstreams" in system_message
    assert "Conversation brief from earlier user messages" in system_message
    assert "Conversation transcript (3 prior message(s), full):" in system_message
    assert "Requested artifact shapes: roadmap, guide" in system_message
    assert "Do not rely on external product APIs" in system_message
    assert "Interpreted scope constraints:" in system_message
    assert "Requested output shape: a roadmap" in system_message
    assert "only cross-link to markdown docs that actually exist" in system_message
    assert "Every downstream stage must validate its output against the original assignment scope" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_docs_only_upstream_artifact_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-tester",
        name="PM Tester",
        role="tester",
        system_prompt="Validate the workstream deterministically.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-docs-only-tester",
        bot_id="pm-tester",
        payload={
            "instruction": "Validate the documentation workstream.",
            "role_hint": "tester",
            "assignment_request": "Build documentation only in docs/blocks for the mathematics blocks.",
            "assignment_scope": {
                "docs_only": True,
                "requested_output_paths": ["docs/blocks"],
            },
            "upstream_artifacts": [
                {
                    "path": "docs/blocks/arithmetic.md",
                    "content": "# Arithmetic",
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "upstream_artifacts" in system_message
    assert "live repo snapshot does not yet contain the proposed markdown files" in system_message
    assert "always return the repo-change contract JSON wrapper" in system_message
    assert "explicitly verify internal markdown links" in system_message
    assert "Do not invent sibling folders, placeholder doc names, or guessed markdown paths" in system_message
    assert "prefer the strongest upstream tester evidence over later skip/not_applicable review signals" in system_message
