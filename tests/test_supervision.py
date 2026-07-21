import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import bcrypt
import pytest

from control_plane.supervision_store import SupervisionStore
from control_plane.schedule_payload_sources import (
    SUPERVISION_PORTFOLIO_SOURCE,
    materialize_system_schedule_payload,
    validate_system_payload_source,
)
from control_plane.schedule_safety import require_schedule_autonomy_safety
from control_plane.task_manager.task_manager import TaskManager
from shared.models import BackendConfig, Bot


class _RecordingScheduler:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def schedule(self, task):
        self.calls += 1
        return self.result


def _specialist_bot() -> Bot:
    return Bot(
        id="specialist",
        name="Specialist",
        role="writer",
        enabled=True,
        backends=[BackendConfig(type="cloud_api", provider="test", model="test-model")],
    )


def _manager_bot(*, schedule_id: str) -> Bot:
    return Bot(
        id="operations-manager",
        name="Operations Manager",
        role="manager",
        enabled=True,
        backends=[BackendConfig(type="cloud_api", provider="test", model="test-model")],
        routing_rules={
            "worker_profile": {
                "can_edit": False,
                "task_scope": "read-only-manager-review",
            },
            "output_contract": {
                "format": "json_object",
                "required_fields": [
                    "executive_summary",
                    "overall_status",
                    "portfolio",
                    "action_proposals",
                ],
            },
            "supervision_manager": {
                "enabled": True,
                "portfolio": {
                    "project_id": "project-a",
                    "bot_ids": ["specialist"],
                    "schedule_ids": [schedule_id],
                },
                "action_policy": {
                    "allow_actions": [
                        "pause_schedule",
                        "hold_bot",
                        "configuration_review",
                    ]
                },
            },
        },
    )


async def _wait_for_terminal(task_manager: TaskManager, task_id: str):
    for _ in range(100):
        task = await task_manager.get_task(task_id)
        if task.status in {"completed", "failed", "cancelled", "retried"}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not reach a terminal status")


@pytest.mark.anyio
async def test_manager_reports_create_portfolio_bound_actions_and_operator_can_apply_them(cp_app, cp_client, tmp_path):
    specialist = _specialist_bot()
    await cp_app.state.bot_registry.register(specialist)
    schedule = await cp_app.state.agent_schedule_engine.create_schedule(
        {
            "name": "Specialist cadence",
            "cron_expression": "0 * * * *",
            "timezone": "UTC",
            "prompt": "Perform the bounded specialist review.",
            "status": "active",
            "target_bot_id": specialist.id,
            "metadata": {"mutation_safe": True},
        }
    )
    manager = _manager_bot(schedule_id=schedule["id"])
    await cp_app.state.bot_registry.register(manager)

    manager_result = {
        "executive_summary": "The portfolio needs operator attention before the next run.",
        "overall_status": "attention",
        "accomplishments": ["Readiness signals reviewed."],
        "risks": ["The specialist needs a temporary hold."],
        "decisions_needed": ["Confirm the planned configuration revision."],
        "portfolio": [
            {"target_type": "bot", "target_id": "specialist", "status": "attention", "summary": "Needs review."}
        ],
        "action_proposals": [
            {
                "action_type": "pause_schedule",
                "target_id": schedule["id"],
                "rationale": "Prevent a new run while the review is open.",
                "evidence": ["recent quality review requires intervention"],
            },
            {
                "action_type": "hold_bot",
                "target_id": "specialist",
                "rationale": "Stop direct dispatch until the operator resolves the review.",
            },
            {
                "action_type": "configuration_review",
                "target_id": "specialist",
                "rationale": "Route the proposed configuration change through Platform AI preflight.",
            },
            {
                "action_type": "hold_bot",
                "target_id": "not-in-the-portfolio",
                "rationale": "This must be ignored.",
            },
            {
                "proposal": "configuration_review",
                "target": "specialist",
                "rationale": "The common model alias must still be portfolio-bound.",
            },
            {
                "proposal": "hold_bot",
                "target": "free-text target",
                "rationale": "The common model alias must reject free-text targets.",
            },
        ],
    }
    scheduler = _RecordingScheduler(manager_result)
    task_manager = TaskManager(
        scheduler,
        db_path=str(tmp_path / "manager-tasks.db"),
        bot_registry=cp_app.state.bot_registry,
        supervision_store=cp_app.state.supervision_store,
    )
    try:
        task = await task_manager.create_task(manager.id, {"instruction": "Prepare the executive report."})
        completed = await _wait_for_terminal(task_manager, task.id)
        assert completed.status == "completed"
        assert scheduler.calls == 1
    finally:
        await task_manager.close()

    reports = await cp_app.state.supervision_store.list_reports()
    assert len(reports) == 1
    assert reports[0]["overall_status"] == "attention"
    actions = await cp_app.state.supervision_store.list_actions(status="pending")
    assert {action["action_type"] for action in actions} == {
        "pause_schedule",
        "hold_bot",
        "configuration_review",
    }
    assert all(action["target_id"] != "not-in-the-portfolio" for action in actions)
    assert all(action["target_id"] != "free-text target" for action in actions)
    assert sum(action["action_type"] == "configuration_review" for action in actions) == 2

    by_type = {action["action_type"]: action for action in actions}
    pause_response = await cp_client.post(
        f"/v1/supervision/actions/{by_type['pause_schedule']['id']}/approve",
        json={"decision_note": "Pause while the review is resolved."},
    )
    assert pause_response.status_code == 200
    assert pause_response.json()["action"]["status"] == "applied"
    assert pause_response.json()["applied"]["schedule"]["status"] == "paused"

    hold_response = await cp_client.post(
        f"/v1/supervision/actions/{by_type['hold_bot']['id']}/approve",
        json={"decision_note": "Hold approved."},
    )
    assert hold_response.status_code == 200
    assert hold_response.json()["applied"]["hold"]["bot_id"] == specialist.id

    blocked_trigger = await cp_client.post(f"/v1/schedules/{schedule['id']}/trigger")
    assert blocked_trigger.status_code == 409
    assert blocked_trigger.json()["detail"]["reason_code"] == "schedule_target_supervision_blocked"

    config_response = await cp_client.post(
        f"/v1/supervision/actions/{by_type['configuration_review']['id']}/approve",
        json={"decision_note": "Create a governed preflight proposal next."},
    )
    assert config_response.status_code == 200
    assert config_response.json()["action"]["status"] == "approved"
    assert config_response.json()["applied"]["kind"] == "configuration_review"

    overview = await cp_client.get("/v1/supervision/overview")
    assert overview.status_code == 200
    assert overview.json()["active_holds"][0]["bot_id"] == specialist.id

    release_response = await cp_client.post(
        f"/v1/supervision/holds/{specialist.id}/release",
        json={"decision_note": "Issue resolved."},
    )
    assert release_response.status_code == 200
    assert release_response.json()["released"] is True


@pytest.mark.anyio
async def test_active_supervision_hold_stops_task_before_any_backend_dispatch(tmp_path):
    from control_plane.registry.bot_registry import BotRegistry

    store = SupervisionStore(db_path=str(tmp_path / "supervision.db"))
    registry = BotRegistry(db_path=str(tmp_path / "bots.db"))
    specialist = _specialist_bot()
    await registry.register(specialist)
    await store.hold_bot(
        specialist.id,
        reason="Pending operator investigation.",
        created_by="test",
    )
    scheduler = _RecordingScheduler({"unexpected": True})
    manager = TaskManager(
        scheduler,
        db_path=str(tmp_path / "tasks.db"),
        bot_registry=registry,
        supervision_store=store,
    )
    try:
        task = await manager.create_task(specialist.id, {"instruction": "This must not dispatch."})
        terminal = await _wait_for_terminal(manager, task.id)
        assert terminal.status == "failed"
        assert terminal.error is not None
        assert terminal.error.code == "supervision_blocked"
        assert scheduler.calls == 0
    finally:
        await manager.close()


@pytest.mark.anyio
async def test_manager_schedule_payload_contains_only_declared_portfolio_metadata(tmp_path):
    manager = _manager_bot(schedule_id="specialist-hourly")
    specialist = _specialist_bot()
    store = SupervisionStore(db_path=str(tmp_path / "supervision.db"))

    class _BotRegistry:
        async def get(self, bot_id):
            assert bot_id == manager.id
            return manager

        async def list(self):
            return [manager, specialist]

    class _TaskManager:
        async def list_tasks(self, limit):
            assert limit == 200
            return [
                SimpleNamespace(
                    bot_id=specialist.id,
                    status="completed",
                    updated_at="2026-07-19T12:00:00+00:00",
                    payload={
                        "instruction": "private task text must not appear",
                        "artifact": {
                            "course_id": 197,
                            "lesson_id": 605009044,
                            "unit_number": 1,
                            "lesson_number": 3,
                            "artifact_type": "FullDraftReview",
                            "lesson_title": "private lesson title must not appear",
                        },
                    },
                    result={
                        "status": "passed",
                        "findings": "private generated prose must not appear",
                    },
                )
            ]

    class _ScheduleEngine:
        async def list_schedules(self, limit):
            assert limit == 500
            return [
                {
                    "id": "specialist-hourly",
                    "name": "Specialist hourly",
                    "status": "active",
                    "last_run_status": "completed",
                    "last_run_at": "2026-07-19T12:00:00+00:00",
                }
            ]

    schedule = {
        "target_bot_id": manager.id,
        "metadata": {
            "system_payload_source": {
                "type": SUPERVISION_PORTFOLIO_SOURCE,
                "target_field": "portfolio_snapshot",
            }
        },
    }
    validate_system_payload_source(schedule, manager)
    payload = await materialize_system_schedule_payload(
        schedule,
        worker_registry=object(),
        worker_probe_store=object(),
        bot_registry=_BotRegistry(),
        task_manager=_TaskManager(),
        schedule_engine=_ScheduleEngine(),
        supervision_store=store,
    )
    snapshot = json.loads(payload["portfolio_snapshot"])
    assert snapshot["source"] == SUPERVISION_PORTFOLIO_SOURCE
    assert snapshot["bots"][0]["bot_id"] == specialist.id
    assert snapshot["bots"][0]["latest_task"]["result_status"] == "passed"
    assert snapshot["bots"][0]["latest_task"]["workflow_scope"] == {
        "course_id": 197,
        "lesson_id": 605009044,
        "unit_number": 1,
        "lesson_number": 3,
        "artifact_type": "FullDraftReview",
    }
    assert snapshot["schedules"][0]["schedule_id"] == "specialist-hourly"
    assert "private task text" not in payload["portfolio_snapshot"]
    assert "private lesson title" not in payload["portfolio_snapshot"]
    assert "private generated prose" not in payload["portfolio_snapshot"]


@pytest.mark.anyio
async def test_manager_schedule_accepts_a_system_materialized_required_payload_field():
    manager = Bot(
        id="portfolio-manager",
        name="Portfolio Manager",
        role="manager",
        project_id="project-a",
        enabled=True,
        backends=[],
        routing_rules={
            "worker_profile": {
                "can_edit": False,
                "task_scope": "read-only-manager-review",
            },
            "input_contract": {
                "enabled": True,
                "format": "json_object",
                "required_fields": ["instruction", "portfolio_snapshot"],
                "non_empty_fields": ["instruction", "portfolio_snapshot"],
            },
            "supervision_manager": {
                "enabled": True,
                "portfolio": {"project_id": "project-a", "bot_ids": ["specialist"]},
                "action_policy": {"allow_actions": ["hold_bot"]},
            },
        },
    )

    class _BotRegistry:
        async def get(self, bot_id):
            assert bot_id == manager.id
            return manager

    await require_schedule_autonomy_safety(
        {
            "target_bot_id": manager.id,
            "project_id": "project-a",
            "prompt": "Produce a read-only portfolio report.",
            "metadata": {
                "mutation_safe": True,
                "system_payload_source": {
                    "type": SUPERVISION_PORTFOLIO_SOURCE,
                    "target_field": "portfolio_snapshot",
                },
            },
        },
        bot_registry=_BotRegistry(),
        only_when_active=False,
    )


def test_supervision_dashboard_renders_executive_decisions(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    db = get_db()
    try:
        if db.query(User).filter_by(email="admin@test.com").first() is None:
            db.add(
                User(
                    email="admin@test.com",
                    password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                    role="admin",
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()
    login = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": password},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}

    class _FakeCP:
        def get_supervision_overview(self):
            return {
                "fleet": {
                    "workers": {"enabled": 3, "online": 3},
                    "bots": {"enabled": 4, "enabled_with_runtime_attention": 0},
                    "schedules": {"active": 2, "failed_active_last_run_count": 0},
                },
                "latest_reports": [
                    {
                        "manager_bot_id": "operations-manager",
                        "overall_status": "attention",
                        "created_at": "2026-07-19T12:00:00+00:00",
                        "report": {
                            "executive_summary": "One decision requires approval.",
                            "decisions_needed": ["Pause the failed specialist schedule."],
                        },
                    }
                ],
                "pending_actions": [
                    {
                        "id": "action-1",
                        "manager_bot_id": "operations-manager",
                        "action_type": "pause_schedule",
                        "target_id": "specialist-hourly",
                        "rationale": "Prevent another failed run.",
                    }
                ],
                "active_holds": [],
            }

    with patch("dashboard.routes.supervision.get_cp_client", return_value=_FakeCP()):
        response = dashboard_client.get("/supervision")
    assert response.status_code == 200
    assert b"Pending Decisions" in response.data
    assert b"One decision requires approval." in response.data
    assert b"specialist-hourly" in response.data
