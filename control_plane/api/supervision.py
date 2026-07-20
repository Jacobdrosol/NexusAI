"""Approval-gated manager actions and executive supervision reports."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from control_plane.schedule_payload_sources import fleet_health_summary
from shared.exceptions import BotNotFoundError

router = APIRouter(prefix="/v1/supervision", tags=["supervision"])


class ActionDecisionRequest(BaseModel):
    decision_note: str = Field(default="", max_length=2_000)


class HoldReleaseRequest(BaseModel):
    decision_note: str = Field(default="", max_length=2_000)


def _bounded_limit(value: int, *, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


@router.get("/overview")
async def supervision_overview(request: Request) -> Dict[str, Any]:
    """Return the evidence and decisions needed for an executive operations review."""
    store = request.app.state.supervision_store
    fleet = await fleet_health_summary(
        worker_registry=request.app.state.worker_registry,
        worker_probe_store=request.app.state.worker_probe_store,
        bot_registry=request.app.state.bot_registry,
        task_manager=request.app.state.task_manager,
        schedule_engine=request.app.state.agent_schedule_engine,
    )
    return {
        "fleet": fleet,
        "latest_reports": await store.list_reports(limit=20),
        "pending_actions": await store.list_actions(status="pending", limit=100),
        "active_holds": await store.list_holds(limit=100),
    }


@router.get("/reports")
async def list_supervision_reports(
    request: Request,
    manager_bot_id: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    reports = await request.app.state.supervision_store.list_reports(
        manager_bot_id=manager_bot_id,
        limit=_bounded_limit(limit),
    )
    return {"reports": reports}


@router.get("/reports/{report_id}")
async def get_supervision_report(report_id: str, request: Request) -> Dict[str, Any]:
    report = await request.app.state.supervision_store.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="supervision report not found")
    return {"report": report}


@router.get("/actions")
async def list_supervision_actions(
    request: Request,
    status: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    actions = await request.app.state.supervision_store.list_actions(
        status=status,
        limit=_bounded_limit(limit, default=100),
    )
    return {"actions": actions}


@router.post("/actions/{action_id}/approve")
async def approve_supervision_action(
    action_id: str,
    request: Request,
    body: ActionDecisionRequest,
) -> Dict[str, Any]:
    """Apply only predeclared, operator-approved manager actions.

    Configuration changes are intentionally not applied from an LLM proposal.  An
    approval records the decision and requires a normal Platform AI preflight for
    the eventual config mutation.
    """
    store = request.app.state.supervision_store
    action = await store.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="supervision action not found")
    if action["status"] != "pending":
        raise HTTPException(status_code=409, detail="supervision action is no longer pending")

    decision_status = "approved"
    applied: Dict[str, Any] = {"kind": "configuration_review"}
    if action["action_type"] == "pause_schedule":
        schedule = await request.app.state.agent_schedule_engine.get_schedule(action["target_id"])
        if schedule is None:
            raise HTTPException(status_code=409, detail="proposed schedule no longer exists")
        updated = await request.app.state.agent_schedule_engine.update_schedule(
            action["target_id"], {"status": "paused"}
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="proposed schedule could not be paused")
        decision_status = "applied"
        applied = {"kind": "schedule_paused", "schedule": updated}
    elif action["action_type"] == "hold_bot":
        try:
            await request.app.state.bot_registry.get(action["target_id"])
        except BotNotFoundError as exc:
            raise HTTPException(status_code=409, detail="proposed bot no longer exists") from exc
        hold = await store.hold_bot(
            action["target_id"],
            reason=action["rationale"],
            created_by="supervision_action_approval",
            report_id=action["report_id"],
            action_id=action["id"],
        )
        decision_status = "applied"
        applied = {"kind": "bot_held", "hold": hold}
    elif action["action_type"] != "configuration_review":
        raise HTTPException(status_code=409, detail="unsupported supervision action type")

    decided = await store.decide_action(
        action_id,
        status=decision_status,
        decided_by="api_operator",
        decision_note=body.decision_note,
    )
    if decided is None:
        raise HTTPException(status_code=409, detail="supervision action decision conflicted")
    await record_audit_event(
        request,
        action="supervision.actions.approve",
        resource=f"supervision_action:{action_id}",
        details={"action_type": action["action_type"], "target_id": action["target_id"]},
    )
    return {"action": decided, "applied": applied}


@router.post("/actions/{action_id}/reject")
async def reject_supervision_action(
    action_id: str,
    request: Request,
    body: ActionDecisionRequest,
) -> Dict[str, Any]:
    store = request.app.state.supervision_store
    decided = await store.decide_action(
        action_id,
        status="rejected",
        decided_by="api_operator",
        decision_note=body.decision_note,
    )
    if decided is None:
        raise HTTPException(status_code=409, detail="supervision action is not pending")
    await record_audit_event(
        request,
        action="supervision.actions.reject",
        resource=f"supervision_action:{action_id}",
    )
    return {"action": decided}


@router.post("/holds/{bot_id}/release")
async def release_supervision_hold(
    bot_id: str,
    request: Request,
    body: HoldReleaseRequest,
) -> Dict[str, Any]:
    store = request.app.state.supervision_store
    hold = await store.get_hold(bot_id)
    if hold is None:
        raise HTTPException(status_code=404, detail="active supervision hold not found")
    await store.release_hold(bot_id)
    await record_audit_event(
        request,
        action="supervision.holds.release",
        resource=f"bot:{bot_id}",
        details={"decision_note": body.decision_note[:2_000]},
    )
    return {"released": True, "hold": hold}
