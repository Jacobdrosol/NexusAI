from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/v1/assignments", tags=["assignments"])


class NodeConnectionBinding(BaseModel):
    slot: str
    project_connection_id: str


class NodeOverride(BaseModel):
    skip: bool = False
    instructions: str = ""
    connection_bindings: List[NodeConnectionBinding] = Field(default_factory=list)
    execution_mode: str = ""
    policy_overrides: Dict[str, Any] = Field(default_factory=dict)


class PreviewAssignmentRequest(BaseModel):
    conversation_id: str
    pm_bot_id: str
    instruction: str
    node_overrides: Dict[str, NodeOverride] = Field(default_factory=dict)


class CreateAssignmentRequest(BaseModel):
    conversation_id: str
    pm_bot_id: str
    instruction: str
    run_id: Optional[str] = None
    node_overrides: Dict[str, NodeOverride] = Field(default_factory=dict)
    context_items: List[str] = Field(default_factory=list)


class SpliceAssignmentRequest(BaseModel):
    from_node_id: str
    node_overrides: Dict[str, NodeOverride] = Field(default_factory=dict)
    context_items: List[str] = Field(default_factory=list)


class RerunNodeRequest(BaseModel):
    payload: Optional[Any] = None


def _dump_overrides(raw: Dict[str, NodeOverride]) -> Dict[str, Dict[str, Any]]:
    return {str(key): value.model_dump() for key, value in (raw or {}).items()}


async def _resolve_run(request: Request, assignment_id: str) -> Dict[str, Any]:
    run_store = request.app.state.orchestration_run_store
    run = await run_store.get_run(assignment_id)
    if run is not None:
        return run
    run = await run_store.get_latest_run_for_assignment(assignment_id)
    if run is not None:
        return run
    raise HTTPException(status_code=404, detail=f"assignment/run not found: {assignment_id}")


@router.post("/preview")
async def preview_assignment(request: Request, body: PreviewAssignmentRequest) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    try:
        return await service.preview(
            conversation_id=body.conversation_id,
            pm_bot_id=body.pm_bot_id,
            instruction=body.instruction,
            node_overrides=_dump_overrides(body.node_overrides),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("")
async def create_assignment(request: Request, body: CreateAssignmentRequest) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    chat_manager = request.app.state.chat_manager
    pm_orchestrator = request.app.state.pm_orchestrator
    try:
        queued = await service.create_assignment(
            conversation_id=body.conversation_id,
            instruction=body.instruction,
            pm_bot_id=body.pm_bot_id,
            run_id=body.run_id,
            node_overrides=_dump_overrides(body.node_overrides),
            context_items=list(body.context_items or []),
        )
        user_message = await chat_manager.add_message(
            conversation_id=body.conversation_id,
            role="user",
            content=f"@assign {body.instruction}",
            metadata={
                "mode": "assign_request",
                "requested_pm_bot_id": body.pm_bot_id,
                "assigned_pm_bot_id": str(queued.get("pm_bot_id") or body.pm_bot_id or ""),
                "orchestration_id": queued.get("orchestration_id"),
                "assignment_id": queued.get("assignment_id"),
                "run_id": queued.get("run_id"),
                "node_overrides": _dump_overrides(body.node_overrides),
            },
        )
        assistant_message = await chat_manager.add_message(
            conversation_id=body.conversation_id,
            role="assistant",
            content=(
                f"Assignment queued ({len(queued.get('tasks') or [])} tasks).\n"
                f"Assigned Bot: {queued.get('pm_bot_id')}\n"
                f"Orchestration ID: {queued.get('orchestration_id')}\n"
                f"Assignment ID: {queued.get('assignment_id')}\n"
                "A full assignment summary will be posted when the workflow finishes."
            ),
            bot_id=str(queued.get("pm_bot_id") or ""),
            metadata={
                "mode": "assign_pending",
                "orchestration_id": queued.get("orchestration_id"),
                "assignment_id": queued.get("assignment_id"),
                "run_id": queued.get("run_id"),
                "task_count": len(queued.get("tasks") or []),
                "assigned_pm_bot_id": str(queued.get("pm_bot_id") or ""),
            },
        )

        async def _persist_summary() -> None:
            assignment_for_summary = {
                "orchestration_id": queued.get("orchestration_id"),
                "pm_bot_id": queued.get("pm_bot_id"),
                "tasks": queued.get("tasks") or [],
            }
            try:
                completion = await pm_orchestrator.wait_for_completion(assignment_for_summary)
                await pm_orchestrator.persist_summary_message(
                    conversation_id=body.conversation_id,
                    assignment=assignment_for_summary,
                    completion=completion,
                )
            except Exception as exc:
                await chat_manager.add_message(
                    conversation_id=body.conversation_id,
                    role="assistant",
                    content=f"Assignment summary failed for orchestration {queued.get('orchestration_id')}: {exc}",
                    bot_id=str(queued.get("pm_bot_id") or ""),
                    metadata={
                        "mode": "assign_error",
                        "orchestration_id": queued.get("orchestration_id"),
                        "assignment_id": queued.get("assignment_id"),
                    },
                )

        asyncio.create_task(_persist_summary())
        return {
            "mode": "assign",
            "assignment": queued,
            "user_message": user_message,
            "assistant_message": assistant_message,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{assignment_id}/graph")
async def assignment_graph(assignment_id: str, request: Request) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    run = await _resolve_run(request, assignment_id)
    try:
        return await service.get_graph(
            run_id=str(run.get("id") or ""),
            orchestration_id=str(run.get("orchestration_id") or "") or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{assignment_id}/splice")
async def splice_assignment(assignment_id: str, request: Request, body: SpliceAssignmentRequest) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    run = await _resolve_run(request, assignment_id)
    try:
        return await service.splice_and_rerun(
            run_id=str(run.get("id") or ""),
            from_node_id=body.from_node_id,
            override_patch=_dump_overrides(body.node_overrides),
            context_items=list(body.context_items or []),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{assignment_id}/nodes/{node_id}/rerun")
async def rerun_assignment_node(
    assignment_id: str,
    node_id: str,
    request: Request,
    body: RerunNodeRequest,
) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    run = await _resolve_run(request, assignment_id)
    orchestration_id = str(run.get("orchestration_id") or "").strip()
    if not orchestration_id:
        raise HTTPException(status_code=400, detail="assignment run has no bound orchestration_id yet")
    try:
        return await service.rerun_node(
            orchestration_id=orchestration_id,
            node_id=node_id,
            payload_override=body.payload,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{assignment_id}/lineage")
async def assignment_lineage(assignment_id: str, request: Request) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    run = await _resolve_run(request, assignment_id)
    try:
        return await service.list_lineage(str(run.get("id") or ""))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
