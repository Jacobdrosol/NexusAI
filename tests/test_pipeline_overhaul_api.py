import pytest


def _pm_bot_payload(bot_id: str) -> dict:
    return {
        "id": bot_id,
        "name": "PM Bot",
        "role": "project_manager",
        "enabled": True,
        "backends": [],
        "assignment_capabilities": {"is_project_manager": True},
        "workflow": {
            "triggers": [
                {
                    "id": "disabled-noop",
                    "event": "task_completed",
                    "target_bot_id": bot_id,
                    "enabled": False,
                    "condition": "always",
                }
            ],
            "reference_graph": {
                "graph_id": f"{bot_id}-graph",
                "entry_bot_id": bot_id,
                "current_bot_id": bot_id,
                "nodes": [{"bot_id": bot_id, "title": "PM Intake", "stage_kind": "planning"}],
                "edges": [{"source_bot_id": bot_id, "target_bot_id": bot_id, "route_kind": "forward"}],
            },
        },
    }


def _pipeline_entry_bot_payload(bot_id: str) -> dict:
    payload = _pm_bot_payload(bot_id)
    payload["name"] = "Pipeline Entry Bot"
    payload["role"] = "assistant"
    payload["assignment_capabilities"] = {
        "is_project_manager": False,
        "is_pipeline_entry": True,
    }
    routing_rules = payload.get("routing_rules") if isinstance(payload.get("routing_rules"), dict) else {}
    routing_rules["launch_profile"] = {
        "enabled": True,
        "label": "Pipeline Entry",
        "is_pipeline": True,
        "pipeline_name": "Pipeline Entry",
        "payload": {"instruction": "Execute pipeline entry"},
    }
    payload["routing_rules"] = routing_rules
    return payload


@pytest.mark.anyio
async def test_assignment_preview_create_and_lineage(cp_client):
    create_conversation = await cp_client.post("/v1/chat/conversations", json={"title": "Assignment Wizard"})
    conversation_id = create_conversation.json()["id"]
    pm_bot_id = "pm-assign-v2"
    created_bot = await cp_client.post("/v1/bots", json=_pm_bot_payload(pm_bot_id))
    assert created_bot.status_code == 200

    preview_resp = await cp_client.post(
        "/v1/assignments/preview",
        json={
            "conversation_id": conversation_id,
            "instruction": "Implement feature x with deterministic checks.",
            "pm_bot_id": pm_bot_id,
            "node_overrides": {
                pm_bot_id: {
                    "skip": False,
                    "instructions": "Use strict contract output",
                    "execution_mode": "code_runner",
                }
            },
        },
    )
    assert preview_resp.status_code == 200
    preview = preview_resp.json()
    assert preview["run_id"]
    assert preview["assignment_id"]
    assert isinstance(preview.get("graph", {}).get("nodes"), list)

    create_resp = await cp_client.post(
        "/v1/assignments",
        json={
            "conversation_id": conversation_id,
            "instruction": "Implement feature x with deterministic checks.",
            "pm_bot_id": pm_bot_id,
            "run_id": preview["run_id"],
            "node_overrides": {
                pm_bot_id: {
                    "skip": False,
                    "instructions": "Use strict contract output",
                    "execution_mode": "code_runner",
                }
            },
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assignment = created.get("assignment") or {}
    assignment_id = str(assignment.get("assignment_id") or "")
    assert assignment_id
    assert assignment.get("orchestration_id")

    graph_resp = await cp_client.get(f"/v1/assignments/{assignment_id}/graph")
    assert graph_resp.status_code == 200
    lineage_resp = await cp_client.get(f"/v1/assignments/{assignment_id}/lineage")
    assert lineage_resp.status_code == 200
    lineage = lineage_resp.json()
    assert isinstance(lineage.get("lineage"), list)
    assert len(lineage["lineage"]) >= 1


@pytest.mark.anyio
async def test_platform_ai_session_control_flow(cp_client):
    create_resp = await cp_client.post(
        "/v1/platform-ai/sessions",
        json={"mode": "assignment_follower", "operator_id": "owner@example.com", "privileged": False},
    )
    assert create_resp.status_code == 200
    session = create_resp.json()
    session_id = session["id"]

    pause_resp = await cp_client.post(
        f"/v1/platform-ai/sessions/{session_id}/control",
        json={"action": "pause", "operator_id": "owner@example.com"},
    )
    assert pause_resp.status_code == 200
    assert pause_resp.json()["result"]["status"] == "paused"

    resume_resp = await cp_client.post(
        f"/v1/platform-ai/sessions/{session_id}/control",
        json={"action": "resume", "operator_id": "owner@example.com"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["result"]["status"] == "active"

    events_resp = await cp_client.get(f"/v1/platform-ai/sessions/{session_id}/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json().get("events") or []) >= 2


@pytest.mark.anyio
async def test_platform_ai_quality_suite_design_and_rerun(cp_client):
    create_conversation = await cp_client.post("/v1/chat/conversations", json={"title": "Quality Suite"})
    conversation_id = create_conversation.json()["id"]
    pm_bot_id = "pm-quality-suite"
    created_bot = await cp_client.post("/v1/bots", json=_pm_bot_payload(pm_bot_id))
    assert created_bot.status_code == 200

    create_assignment_resp = await cp_client.post(
        "/v1/assignments",
        json={
            "conversation_id": conversation_id,
            "instruction": "Implement docs-only pipeline output.",
            "pm_bot_id": pm_bot_id,
        },
    )
    assert create_assignment_resp.status_code == 200
    assignment = (create_assignment_resp.json().get("assignment") or {})
    assignment_id = str(assignment.get("assignment_id") or "")
    assert assignment_id

    session_resp = await cp_client.post(
        "/v1/platform-ai/sessions",
        json={
            "mode": "pipeline_tuner",
            "assignment_id": assignment_id,
            "operator_id": "owner@example.com",
            "privileged": False,
        },
    )
    assert session_resp.status_code == 200
    session_id = session_resp.json()["id"]

    design_resp = await cp_client.post(
        f"/v1/platform-ai/sessions/{session_id}/test-suites/design",
        json={
            "name": "PM Quality Gate Suite",
            "assignment_id": assignment_id,
            "quality_expectations": [
                {
                    "name": "Require quality evidence",
                    "required_keywords": ["quality", "acceptance"],
                    "required_fields": ["summary"],
                    "min_score": 0.2,
                }
            ],
        },
    )
    assert design_resp.status_code == 200
    suite = design_resp.json().get("suite") or {}
    suite_id = str(suite.get("id") or "")
    assert suite_id
    assert isinstance((suite.get("suite") or {}).get("tests"), list)
    assert len((suite.get("suite") or {}).get("tests") or []) >= 1

    run_resp = await cp_client.post(
        f"/v1/platform-ai/test-suites/{suite_id}/run",
        json={"assignment_id": assignment_id, "operator_id": "owner@example.com"},
    )
    assert run_resp.status_code == 200
    test_run = run_resp.json().get("test_run") or {}
    assert str(test_run.get("id") or "")
    assert str(test_run.get("status") or "") in {"passed", "failed"}
    result = test_run.get("result") if isinstance(test_run.get("result"), dict) else {}
    assert isinstance(result.get("tests"), list)

    list_runs_resp = await cp_client.get(f"/v1/platform-ai/test-suites/{suite_id}/runs")
    assert list_runs_resp.status_code == 200
    runs = list_runs_resp.json().get("runs") or []
    assert isinstance(runs, list)
    assert len(runs) >= 1

    sessions_resp = await cp_client.get("/v1/platform-ai/sessions?mode=pipeline_tuner&limit=25")
    assert sessions_resp.status_code == 200
    sessions = sessions_resp.json().get("sessions") or []
    assert isinstance(sessions, list)
    assert any(str(item.get("id") or "") == session_id for item in sessions)

    suites_global_resp = await cp_client.get(f"/v1/platform-ai/test-suites?assignment_id={assignment_id}")
    assert suites_global_resp.status_code == 200
    suites_global = suites_global_resp.json().get("suites") or []
    assert isinstance(suites_global, list)
    assert any(str(item.get("id") or "") == suite_id for item in suites_global)


@pytest.mark.anyio
async def test_platform_ai_pipeline_entry_suite_catalog_and_run(cp_client):
    bot_id = "pipeline-entry-test"
    created_bot = await cp_client.post("/v1/bots", json=_pipeline_entry_bot_payload(bot_id))
    assert created_bot.status_code == 200

    pipelines_resp = await cp_client.get("/v1/platform-ai/pipelines")
    assert pipelines_resp.status_code == 200
    pipelines = pipelines_resp.json().get("pipelines") or []
    assert any(str(item.get("pipeline_bot_id") or "") == bot_id for item in pipelines)

    design_resp = await cp_client.post(
        f"/v1/platform-ai/pipelines/{bot_id}/test-suites/design",
        json={"name": "Pipeline Entry Suite", "set_default": True},
    )
    assert design_resp.status_code == 200
    suite = design_resp.json().get("suite") or {}
    suite_id = str(suite.get("id") or "")
    assert suite_id
    metadata = suite.get("metadata") if isinstance(suite.get("metadata"), dict) else {}
    assert int(metadata.get("suite_version") or 0) >= 1

    list_suites_resp = await cp_client.get(f"/v1/platform-ai/pipelines/{bot_id}/test-suites")
    assert list_suites_resp.status_code == 200
    listed = list_suites_resp.json().get("suites") or []
    assert any(str(item.get("id") or "") == suite_id for item in listed)

    run_resp = await cp_client.post(
        f"/v1/platform-ai/pipelines/{bot_id}/test-suites/run",
        json={"suite_id": suite_id, "wait_for_terminal": False},
    )
    assert run_resp.status_code == 200
    test_run = run_resp.json().get("test_run") or {}
    assert str(test_run.get("id") or "")
    assert str(test_run.get("status") or "") in {"passed", "failed"}


@pytest.mark.anyio
async def test_agent_scheduler_create_and_manual_trigger(cp_client):
    bot_id = "scheduled-bot"
    bot_resp = await cp_client.post(
        "/v1/bots",
        json={
            "id": bot_id,
            "name": "Scheduled Bot",
            "role": "worker",
            "enabled": True,
            "backends": [],
        },
    )
    assert bot_resp.status_code == 200

    create_schedule_resp = await cp_client.post(
        "/v1/schedules",
        json={
            "name": "Issue Sync",
            "cron_expression": "*/15 * * * *",
            "timezone": "UTC",
            "prompt": "Process queued issues",
            "target_bot_id": bot_id,
            "status": "active",
        },
    )
    assert create_schedule_resp.status_code == 200
    schedule_id = create_schedule_resp.json()["schedule"]["id"]

    trigger_resp = await cp_client.post(f"/v1/schedules/{schedule_id}/trigger")
    assert trigger_resp.status_code == 200
    run = trigger_resp.json()["run"]
    assert run["id"]
    assert run["schedule_id"] == schedule_id

    runs_resp = await cp_client.get(f"/v1/schedules/{schedule_id}/runs")
    assert runs_resp.status_code == 200
    assert isinstance(runs_resp.json().get("runs"), list)
