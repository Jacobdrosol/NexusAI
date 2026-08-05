from __future__ import annotations

import asyncio
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.api.chat import (
    _build_assignment_context_snapshot,
    _assignment_context_message_metadata,
)


router = APIRouter(prefix="/v1/assignments", tags=["assignments"])

_ASSIGNMENT_CONTEXT_ITEM_MAX_CHARS = 12000
_ASSIGNMENT_CONTEXT_ITEM_MAX_COUNT = 50
_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_CHARS = 200
_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_COUNT = 200
AssignmentContextItem = Annotated[str, Field(max_length=_ASSIGNMENT_CONTEXT_ITEM_MAX_CHARS)]
AssignmentContextItemId = Annotated[str, Field(min_length=1, max_length=_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_CHARS)]


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
    context_items: List[AssignmentContextItem] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_MAX_COUNT)
    context_item_ids: List[AssignmentContextItemId] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_COUNT)


class CreateAssignmentRequest(BaseModel):
    conversation_id: str
    pm_bot_id: str
    instruction: str
    run_id: Optional[str] = None
    node_overrides: Dict[str, NodeOverride] = Field(default_factory=dict)
    context_items: List[AssignmentContextItem] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_MAX_COUNT)
    context_item_ids: List[AssignmentContextItemId] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_COUNT)


class SpliceAssignmentRequest(BaseModel):
    from_node_id: str
    node_overrides: Dict[str, NodeOverride] = Field(default_factory=dict)
    context_items: List[AssignmentContextItem] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_MAX_COUNT)
    context_item_ids: List[AssignmentContextItemId] = Field(default_factory=list, max_length=_ASSIGNMENT_CONTEXT_ITEM_ID_MAX_COUNT)


class RerunNodeRequest(BaseModel):
    payload: Optional[Any] = None


def _dump_overrides(raw: Dict[str, NodeOverride]) -> Dict[str, Dict[str, Any]]:
    return {str(key): value.model_dump() for key, value in (raw or {}).items()}


async def _resolve_assignment_context_items(
    request: Request,
    *,
    context_items: List[str],
    context_item_ids: List[str],
) -> List[str]:
    resolved: List[str] = []
    vault_manager = getattr(request.app.state, "vault_manager", None)
    if context_item_ids and vault_manager is not None:
        item_ids = [str(item_id).strip() for item_id in context_item_ids if str(item_id).strip()]
        for item_id in item_ids[:20]:
            try:
                item = await vault_manager.get_item(item_id)
                text = (item.content or "").strip()
                if not text:
                    continue
                resolved.append(f"[vault:{item.id}] {item.title}\n{text[:4000]}")
            except Exception:
                continue
    resolved.extend(str(item).strip() for item in context_items if str(item).strip())
    return resolved[:_ASSIGNMENT_CONTEXT_ITEM_MAX_COUNT]


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
        context_items = await _resolve_assignment_context_items(
            request,
            context_items=list(body.context_items or []),
            context_item_ids=list(body.context_item_ids or []),
        )
        return await service.preview(
            conversation_id=body.conversation_id,
            pm_bot_id=body.pm_bot_id,
            instruction=body.instruction,
            node_overrides=_dump_overrides(body.node_overrides),
            context_items=context_items,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("")
async def create_assignment(request: Request, body: CreateAssignmentRequest) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    chat_manager = request.app.state.chat_manager
    pm_orchestrator = request.app.state.pm_orchestrator
    try:
        context_items = await _resolve_assignment_context_items(
            request,
            context_items=list(body.context_items or []),
            context_item_ids=list(body.context_item_ids or []),
        )
        queued = await service.create_assignment(
            conversation_id=body.conversation_id,
            instruction=body.instruction,
            pm_bot_id=body.pm_bot_id,
            run_id=body.run_id,
            node_overrides=_dump_overrides(body.node_overrides),
            context_items=context_items,
        )
        # Build context snapshot so the "PM context: X messages" label shows in the UI
        try:
            context_snapshot = await _build_assignment_context_snapshot(
                chat_manager,
                conversation_id=body.conversation_id,
                assign_instruction=body.instruction,
                current_assign_message_id=None,
            )
            context_meta = _assignment_context_message_metadata(context_snapshot)
        except Exception:
            context_meta = {}
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
                **context_meta,
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
                **context_meta,
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


@router.get("/by-orchestration/{orchestration_id}/graph")
async def assignment_graph_by_orchestration(orchestration_id: str, request: Request) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    try:
        return await service.get_graph(orchestration_id=orchestration_id)
    except Exception as exc:
        detail = str(exc)
        status_code = 404 if "run not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail)


@router.post("/{assignment_id}/splice")
async def splice_assignment(assignment_id: str, request: Request, body: SpliceAssignmentRequest) -> Dict[str, Any]:
    service = request.app.state.assignment_service
    chat_manager = request.app.state.chat_manager
    pm_orchestrator = request.app.state.pm_orchestrator
    run = await _resolve_run(request, assignment_id)
    try:
        context_items = await _resolve_assignment_context_items(
            request,
            context_items=list(body.context_items or []),
            context_item_ids=list(body.context_item_ids or []),
        )
        result = await service.splice_and_rerun(
            run_id=str(run.get("id") or ""),
            from_node_id=body.from_node_id,
            override_patch=_dump_overrides(body.node_overrides),
            context_items=context_items,
        )
        assignment = result.get("assignment") if isinstance(result.get("assignment"), dict) else {}
        conversation_id = str(run.get("conversation_id") or "").strip()
        pm_bot_id = str(assignment.get("pm_bot_id") or run.get("pm_bot_id") or "").strip()
        if conversation_id and assignment:
            user_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=f"@splice {body.from_node_id}",
                metadata={
                    "mode": "assign_request",
                    "request_type": "splice",
                    "requested_pm_bot_id": pm_bot_id,
                    "assigned_pm_bot_id": pm_bot_id,
                    "orchestration_id": assignment.get("orchestration_id"),
                    "assignment_id": assignment.get("assignment_id"),
                    "run_id": assignment.get("run_id"),
                    "spliced_from_node_id": body.from_node_id,
                    "lineage_parent_run_id": str(run.get("id") or ""),
                    "node_overrides": _dump_overrides(body.node_overrides),
                },
            )
            assistant_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=(
                    f"Assignment splice queued from node '{body.from_node_id}'.\n"
                    f"Assigned Bot: {pm_bot_id}\n"
                    f"Orchestration ID: {assignment.get('orchestration_id')}\n"
                    f"Assignment ID: {assignment.get('assignment_id')}\n"
                    "A full assignment summary will be posted when the workflow finishes."
                ),
                bot_id=pm_bot_id or None,
                metadata={
                    "mode": "assign_pending",
                    "request_type": "splice",
                    "orchestration_id": assignment.get("orchestration_id"),
                    "assignment_id": assignment.get("assignment_id"),
                    "run_id": assignment.get("run_id"),
                    "task_count": len(assignment.get("tasks") or []),
                    "assigned_pm_bot_id": pm_bot_id,
                    "spliced_from_node_id": body.from_node_id,
                    "lineage_parent_run_id": str(run.get("id") or ""),
                },
            )

            async def _persist_summary() -> None:
                assignment_for_summary = {
                    "orchestration_id": assignment.get("orchestration_id"),
                    "pm_bot_id": pm_bot_id,
                    "tasks": assignment.get("tasks") or [],
                }
                try:
                    completion = await pm_orchestrator.wait_for_completion(assignment_for_summary)
                    await pm_orchestrator.persist_summary_message(
                        conversation_id=conversation_id,
                        assignment=assignment_for_summary,
                        completion=completion,
                    )
                except Exception as exc:
                    await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"Assignment summary failed for orchestration {assignment.get('orchestration_id')}: {exc}",
                        bot_id=pm_bot_id or None,
                        metadata={
                            "mode": "assign_error",
                            "request_type": "splice",
                            "orchestration_id": assignment.get("orchestration_id"),
                            "assignment_id": assignment.get("assignment_id"),
                        },
                    )

            asyncio.create_task(_persist_summary())
            result["user_message"] = user_message
            result["assistant_message"] = assistant_message
        return result
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
