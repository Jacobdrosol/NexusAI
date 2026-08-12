from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from control_plane.connection_action_approvals import ConnectionActionApprovalStore
from shared.models import BackendConfig, Bot, Task


_SCHEMA = json.dumps(
    {
        "openapi": "3.0.0",
        "paths": {
            "/api/agent/courses/{courseId}": {
                "patch": {"operationId": "updateCourse"},
            },
            "/api/agent/approvals": {
                "post": {"operationId": "createApproval"},
            },
        },
    }
)


def _course_update_payload() -> dict:
    return {
        "connection": {"name": "acme Agent API"},
        "connection_action": {
            "operation_id": "updateCourse",
            "method": "PATCH",
            "path": "/api/agent/courses/{courseId}",
            "path_params": {"courseId": 78},
            "body_json": {
                "summary": "A bounded draft update.",
                "changeSummary": "Applied after independent author and QC review.",
            },
            "agent_approval": {
                "action": {
                    "operation_id": "createApproval",
                    "method": "POST",
                    "path": "/api/agent/approvals",
                    "body_json": {
                        "scope": "Course.Update",
                        "targetType": "Course",
                        "targetId": "78",
                        "approvedBy": "Jacob Drosol",
                        "expiresInMinutes": 5,
                        "maxUses": 1,
                        "source": "nexusai",
                    },
                },
                "response_token_field": "token",
                "inject_header": "X-acme-AGENT-APPROVAL",
            },
        },
    }


class _ConnectionResolver:
    def find_bot_connection(self, bot_id, *, requested_name=None, requested_id=None):
        assert bot_id == "course-metadata-applier"
        assert requested_name == "acme Agent API"
        return {
            "id": 90,
            "name": "acme Agent API",
            "kind": "http",
            "config": {"base_url": "https://acme.test"},
            "auth": {"type": "none"},
            "schema_text": _SCHEMA,
        }


@pytest.mark.anyio
async def test_connection_action_approval_is_payload_bound_and_single_use(tmp_path):
    store = ConnectionActionApprovalStore(db_path=str(tmp_path / "approvals.db"))
    payload = _course_update_payload()
    approval = await store.create(
        bot_id="course-metadata-applier",
        action_key="acme-agent-api.updatecourse",
        payload=payload,
        expires_in_seconds=60,
    )

    approved_payload = {**payload, "owner_approval_id": approval["id"]}
    assert await store.consume(
        approval_id=approval["id"],
        bot_id="course-metadata-applier",
        action_key="acme-agent-api.updatecourse",
        payload=approved_payload,
    )
    assert not await store.consume(
        approval_id=approval["id"],
        bot_id="course-metadata-applier",
        action_key="acme-agent-api.updatecourse",
        payload=approved_payload,
    )


@pytest.mark.anyio
async def test_scheduler_requires_owner_approval_for_one_connection_mutation(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    backend = BackendConfig(type="custom", provider="http_connection", model="attached-http")
    bot = Bot(
        id="course-metadata-applier",
        name="Course Metadata Applier",
        role="course-metadata-applier",
        execution_policy={
            "connection_action_allowlist": ["acme-agent-api.updatecourse"],
            "connection_action_owner_approval_required": ["acme-agent-api.updatecourse"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-course-78",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    store = ConnectionActionApprovalStore(db_path=str(tmp_path / "approvals.db"))
    payload = _course_update_payload()
    approval = await store.create(
        bot_id=bot.id,
        action_key="acme-agent-api.updatecourse",
        payload=payload,
        expires_in_seconds=60,
    )
    payload["owner_approval_id"] = approval["id"]

    monkeypatch.setattr(
        "shared.connection_runtime.test_http_connection",
        lambda **kwargs: {"ok": True, "status": 200, "body_preview": "{}"},
    )
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(
        bot_registry=bot_registry,
        worker_registry=AsyncMock(),
        connection_resolver=_ConnectionResolver(),
        connection_action_approval_store=store,
    )

    result = await scheduler._dispatch_backend(backend, payload, task=task)
    assert result["import_status"] == "success"
    assert result["completed_actions"] == ["updateCourse"]
    with pytest.raises(BackendError, match="valid, unused owner approval"):
        await scheduler._dispatch_backend(backend, payload, task=task)


def test_bot_rejects_unallowlisted_connection_action_approval():
    from shared.bot_policy import validate_bot_configuration

    bot = Bot(
        id="unsafe-connection-policy",
        name="Unsafe Connection Policy",
        role="test",
        execution_policy={
            "connection_action_allowlist": ["acme-agent-api.updatecourse"],
            "connection_action_owner_approval_required": ["acme-agent-api.updateLesson"],
        },
        backends=[],
    )

    assert validate_bot_configuration(bot) == [
        "Bot 'unsafe-connection-policy' requires owner approval for connection actions not present "
        "in its allowlist: acme-agent-api.updateLesson"
    ]
