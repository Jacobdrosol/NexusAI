from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/orchestration", tags=["orchestration"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateBindingRequest(BaseModel):
    template_id: str = Field(..., description="ID of the OrchestrationTemplate to bind")
    owner_id: str = Field(..., description="Owner / user / team that owns this binding")
    name: Optional[str] = Field(default=None)
    overrides: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-binding node overrides that extend/narrow the template without mutating it",
    )
    metadata: Optional[Dict[str, Any]] = Field(default=None)


class CompileRunContractRequest(BaseModel):
    binding_id: str = Field(..., description="PipelineBinding to compile into a RunContract")
    overrides: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-run overrides applied on top of the binding",
    )


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
            detail="OrchestrationTemplateStore not initialised (main.py needs updating — Pass 2 pending)",
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
    try:
        binding = await store.create_binding(
            template_id=template_id,
            owner_id=owner_id,
            name=str(body.name or "").strip() or None,
            overrides=body.overrides or {},
            metadata=body.metadata or {},
        )
    except AttributeError:
        raise HTTPException(status_code=501, detail="binding creation not available")
    if binding is None:
        raise HTTPException(status_code=400, detail="binding creation failed — check template_id")
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
    try:
        contract = await store.compile_run_contract(
            binding_id=binding_id,
            overrides=body.overrides or {},
        )
    except AttributeError:
        raise HTTPException(status_code=501, detail="run contract compile not available")
    if contract is None:
        raise HTTPException(status_code=404, detail="binding not found or template missing")
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
    try:
        result = await run_store.cancel_orchestration(
            safe_id,
            reason=str(body.reason or "operator_cancelled").strip() or "operator_cancelled",
            operator_id=str(body.operator_id or "").strip() or None,
        )
    except AttributeError:
        raise HTTPException(
            status_code=501,
            detail="cancel_orchestration not available (run_store not upgraded — Pass 2 pending)",
        )
    if result is None:
        raise HTTPException(status_code=404, detail="orchestration run not found")
    return {"run_id": safe_id, "cancelled": True, "run": result}
