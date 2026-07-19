from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
router = APIRouter(prefix="/v1/orchestration", tags=["orchestration"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateBindingRequest(BaseModel):
    template_id: str = Field(..., description="ID of the OrchestrationTemplate to bind")
    owner_id: str = Field(..., description="Owner / user / team that owns this binding")
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    role_map: Optional[Dict[str, str]] = Field(default=None)
    default_stage_configs: Optional[Dict[str, Any]] = Field(default=None)
    default_connection_requirements: Optional[List[Dict[str, Any]]] = Field(default=None)
    default_context_requirements: Optional[List[Dict[str, Any]]] = Field(default=None)
    overrides: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Deprecated alias for default_stage_configs retained for existing callers",
    )
    metadata: Optional[Dict[str, Any]] = Field(default=None)


class CompileRunContractRequest(BaseModel):
    binding_id: str = Field(..., description="PipelineBinding to compile into a RunContract")
    overrides: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-run overrides applied on top of the binding",
    )
    assignment_text: str = Field(default="", max_length=20000)
    operator_brief: str = Field(default="", max_length=20000)


class CancelOrchestrationRequest(BaseModel):
    reason: Optional[str] = Field(default=None)
    operator_id: Optional[str] = Field(default=None)


# ---------------------------------------------------------------------------
# Helper: safely obtain template_store from app.state
# ---------------------------------------------------------------------------


def _template_store(request: Request) -> Any:
    store = getattr(request.app.state, "orchestration_template_store", None)
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="orchestration template store is unavailable",
        )
    return store


# ---------------------------------------------------------------------------
# Template routes
# ---------------------------------------------------------------------------


@router.get("/templates")
async def list_templates(request: Request) -> Dict[str, Any]:
    """List all available orchestration templates."""
    store = _template_store(request)
    try:
        templates = await store.list_templates()
    except AttributeError:
        raise HTTPException(status_code=501, detail="template listing not available")
    return {"templates": templates, "count": len(templates)}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, request: Request) -> Dict[str, Any]:
    """Get a specific orchestration template by ID."""
    store = _template_store(request)
    safe_id = str(template_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="template_id required")
    try:
        template = await store.get_template(safe_id)
    except AttributeError:
        raise HTTPException(status_code=501, detail="template fetch not available")
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return {"template": template}


# ---------------------------------------------------------------------------
# Binding routes
# ---------------------------------------------------------------------------


@router.post("/bindings")
async def create_binding(request: Request, body: CreateBindingRequest) -> Dict[str, Any]:
    """Create a PipelineBinding linking a private bot config to a public template."""
    store = _template_store(request)
    template_id = str(body.template_id or "").strip()
    owner_id = str(body.owner_id or "").strip()
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id required")
    if not owner_id:
        raise HTTPException(status_code=400, detail="owner_id required")
    template = await store.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    stage_configs = body.default_stage_configs
    if stage_configs is None:
        stage_configs = body.overrides or {}
    default_name = f"{str(template.get('name') or template_id).strip()} binding"
    binding = await store.create_binding(
        template_id=template_id,
        owner_id=owner_id,
        name=str(body.name or "").strip() or default_name,
        description=str(body.description or "").strip(),
        role_map=body.role_map or {},
        default_stage_configs=stage_configs,
        default_connection_requirements=body.default_connection_requirements or [],
        default_context_requirements=body.default_context_requirements or [],
        metadata=body.metadata or {},
    )
    return {"binding": binding}


@router.get("/bindings/{binding_id}")
async def get_binding(binding_id: str, request: Request) -> Dict[str, Any]:
    """Get a PipelineBinding by ID."""
    store = _template_store(request)
    safe_id = str(binding_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="binding_id required")
    try:
        binding = await store.get_binding(safe_id)
    except AttributeError:
        raise HTTPException(status_code=501, detail="binding fetch not available")
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return {"binding": binding}


# ---------------------------------------------------------------------------
# Run contract compile (preview before launching)
# ---------------------------------------------------------------------------


@router.post("/compile")
async def compile_run_contract(request: Request, body: CompileRunContractRequest) -> Dict[str, Any]:
    """
    Compile a RunContract from a binding + optional overrides.
    This is a dry-run preview — it does not start any orchestration.
    """
    store = _template_store(request)
    binding_id = str(body.binding_id or "").strip()
    if not binding_id:
        raise HTTPException(status_code=400, detail="binding_id required")
    binding = await store.get_binding(binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    template_id = str(binding.get("template_id") or "").strip()
    template = await store.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=409, detail="binding references a missing template")
    contract = store.compile_run_contract(
        template=template,
        binding=binding,
        overrides=body.overrides or {},
        assignment_text=str(body.assignment_text or "").strip(),
        operator_brief=str(body.operator_brief or "").strip(),
    )
    return {"contract": contract}


# ---------------------------------------------------------------------------
# Run cancellation
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/cancel")
async def cancel_orchestration_run(
    run_id: str,
    request: Request,
    body: CancelOrchestrationRequest,
) -> Dict[str, Any]:
    """Explicitly cancel a running orchestration."""
    run_store = getattr(request.app.state, "orchestration_run_store", None)
    if run_store is None:
        raise HTTPException(status_code=503, detail="orchestration run store not available")
    safe_id = str(run_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="run_id required")
    run = await run_store.get_run(safe_id)
    if run is None:
        raise HTTPException(status_code=404, detail="orchestration run not found")
    reason = str(body.reason or "operator_cancelled").strip() or "operator_cancelled"
    orchestration_id = str(run.get("orchestration_id") or "").strip()
    task_result: Dict[str, Any] | None = None
    if orchestration_id:
        task_result = await request.app.state.task_manager.cancel_orchestration(
            orchestration_id,
            reason=reason,
        )
    result = await run_store.cancel_orchestration(
        safe_id,
        reason=reason,
        actor=str(body.operator_id or "").strip() or "operator",
    )
    await record_audit_event(
        request,
        action="orchestration.cancel",
        resource=f"orchestration_run:{safe_id}",
        details={
            "orchestration_id": orchestration_id or None,
            "reason": reason,
            "cancelled_task_count": (task_result or {}).get("cancelled_task_count", 0),
        },
    )
    return {
        "run_id": safe_id,
        "cancelled": True,
        "orchestration_id": orchestration_id or None,
        "task_cancellation": task_result,
        "run": result,
    }
