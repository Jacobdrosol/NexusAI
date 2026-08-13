"""REST API endpoints for ticket sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from control_plane.tickets.pollers import poll_source

router = APIRouter(prefix="/v1/projects", tags=["ticket-sources"])

_VALID_SOURCE_TYPES = {"github_issues", "generic_http", "jira", "asana"}


# ---------------------------------------------------------------------------
#  Request models
# ---------------------------------------------------------------------------

class CreateTicketSourceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_type: str
    config: Optional[Dict[str, Any]] = None
    credential_value: Optional[str] = None
    credential_key_ref: Optional[str] = None
    enabled: bool = True


class UpdateTicketSourceBody(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    credential_value: Optional[str] = None
    credential_key_ref: Optional[str] = None
    enabled: Optional[bool] = None


class PollSourceBody(BaseModel):
    max_items: Optional[int] = None


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _get_store(request: Request):
    store = getattr(request.app.state, "ticket_source_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="ticket_source_store not initialized")
    return store


def _get_key_vault(request: Request):
    vault = getattr(request.app.state, "key_vault", None)
    if vault is None:
        raise HTTPException(status_code=500, detail="key_vault not initialized")
    return vault


def _validate_source_type(source_type: str) -> str:
    if source_type not in _VALID_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported source_type '{source_type}'. Valid types: {', '.join(sorted(_VALID_SOURCE_TYPES))}",
        )
    return source_type


def _validate_config(source_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    config = config or {}
    if source_type == "github_issues":
        if not config.get("repo_full_name"):
            raise HTTPException(status_code=400, detail="github_issues config requires 'repo_full_name'")
    elif source_type == "generic_http":
        if not config.get("url"):
            raise HTTPException(status_code=400, detail="generic_http config requires 'url'")
        board_field = str(config.get("board_field") or "").strip()
        column_field = str(config.get("column_field") or "").strip()
        card_field = str(config.get("card_field") or "").strip()
        if any([board_field, column_field, card_field]) and not all([board_field, column_field, card_field]):
            raise HTTPException(
                status_code=400,
                detail="generic_http board flattening requires board_field, column_field, and card_field together",
            )
    elif source_type == "jira":
        if not config.get("base_url"):
            raise HTTPException(status_code=400, detail="jira config requires 'base_url'")
    elif source_type == "asana":
        if not config.get("project_id"):
            raise HTTPException(status_code=400, detail="asana config requires 'project_id'")
    return config


async def _resolve_credential(
    request: Request,
    source_type: str,
    project_id: str,
    credential_value: Optional[str],
    credential_key_ref: Optional[str],
) -> Optional[str]:
    """Resolve the vault key name to store on the source.

    Returns the key vault reference name (never the secret value). If a raw
    credential_value is supplied it is written into the vault under a
    generated key name. If a credential_key_ref is supplied it is validated
    to exist in the vault and returned as-is so the raw secret never touches
    the source config.
    """
    vault = _get_key_vault(request)
    key_name = f"ticket_source:{source_type}::{project_id}"

    if credential_value:
        await vault.set_key(name=key_name, provider="ticket_source", value=credential_value)
        return key_name

    ref = (credential_key_ref or "").strip()
    if not ref:
        return None
    try:
        secret = await vault.get_secret(ref)
        if not secret:
            return None
    except Exception:
        return None
    # Return the reference, not the secret.
    return ref


async def _get_credential_value(request: Request, key_ref: Optional[str]) -> Optional[str]:
    if not key_ref:
        return None
    vault = _get_key_vault(request)
    try:
        return await vault.get_secret(key_ref)
    except Exception:
        return None


# ---------------------------------------------------------------------------
#  Endpoints
# ---------------------------------------------------------------------------

@router.get("/{project_id}/ticket-sources/overview")
async def ticket_sources_overview(request: Request, project_id: str, limit: int = 100) -> Dict[str, Any]:
    """Aggregate all ticket sources + their items for a project.

    Used by the Ticket Hub dashboard to render a single project-wide view
    of tickets and their lifecycle state in one request.
    """
    store = _get_store(request)
    sources = await store.list_sources(project_id=project_id)
    limit = max(1, min(int(limit or 100), 500))
    aggregated: List[Dict[str, Any]] = []
    for source in sources:
        source["item_count"] = await store.count_items(source["id"])
        items = await store.list_items(source["id"], limit=limit)
        aggregated.append({**source, "items": items})
    return {"project_id": project_id, "sources": aggregated}

@router.get("/{project_id}/ticket-sources")
async def list_ticket_sources(request: Request, project_id: str) -> Dict[str, Any]:
    store = _get_store(request)
    sources = await store.list_sources(project_id=project_id)
    for s in sources:
        s["item_count"] = await store.count_items(s["id"])
    return {"project_id": project_id, "sources": sources}


@router.post("/{project_id}/ticket-sources")
async def create_ticket_source(
    request: Request, project_id: str, body: CreateTicketSourceBody
) -> Dict[str, Any]:
    store = _get_store(request)
    _validate_source_type(body.source_type)
    config = _validate_config(body.source_type, body.config or {})

    key_ref = await _resolve_credential(
        request, body.source_type, project_id, body.credential_value, body.credential_key_ref
    )

    source = await store.create_source(
        project_id=project_id,
        name=body.name.strip(),
        source_type=body.source_type,
        config=config,
        credential_key_ref=key_ref,
        enabled=body.enabled,
    )
    await record_audit_event(
        request,
        action="ticket_sources.create",
        resource=f"project:{project_id}/ticket_source:{source['id']}",
        details={"name": body.name, "source_type": body.source_type},
    )
    return {"status": "ok", "source": source}


@router.get("/{project_id}/ticket-sources/{source_id}")
async def get_ticket_source(
    request: Request, project_id: str, source_id: str
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    source["item_count"] = await store.count_items(source_id)
    return {"source": source}


@router.patch("/{project_id}/ticket-sources/{source_id}")
async def update_ticket_source(
    request: Request, project_id: str, source_id: str, body: UpdateTicketSourceBody
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")

    update_kwargs: Dict[str, Any] = {}
    if body.name is not None:
        update_kwargs["name"] = body.name.strip()
    if body.config is not None:
        update_kwargs["config"] = _validate_config(source["source_type"], body.config)
    if body.enabled is not None:
        update_kwargs["enabled"] = body.enabled
    if body.credential_value is not None:
        key_ref = await _resolve_credential(
            request, source["source_type"], project_id, body.credential_value, body.credential_key_ref
        )
        update_kwargs["credential_key_ref"] = key_ref
    elif body.credential_key_ref is not None:
        update_kwargs["credential_key_ref"] = body.credential_key_ref

    updated = await store.update_source(source_id, **update_kwargs)
    await record_audit_event(
        request,
        action="ticket_sources.update",
        resource=f"project:{project_id}/ticket_source:{source_id}",
        details={k: v for k, v in update_kwargs.items() if k != "credential_key_ref"},
    )
    return {"status": "ok", "source": updated}


@router.delete("/{project_id}/ticket-sources/{source_id}")
async def delete_ticket_source(
    request: Request, project_id: str, source_id: str
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    await store.delete_source(source_id)
    await record_audit_event(
        request,
        action="ticket_sources.delete",
        resource=f"project:{project_id}/ticket_source:{source_id}",
        details={"name": source.get("name")},
    )
    return {"status": "ok", "deleted": source_id}


@router.post("/{project_id}/ticket-sources/{source_id}/poll")
async def poll_ticket_source(
    request: Request, project_id: str, source_id: str, body: Optional[PollSourceBody] = None
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    if not source["enabled"]:
        raise HTTPException(status_code=400, detail="ticket source is disabled")

    credential = await _get_credential_value(request, source.get("credential_key_ref"))

    config = dict(source.get("config") or {})
    if body and body.max_items:
        config["max_items"] = body.max_items

    try:
        items_raw = await poll_source(
            source["source_type"], config, credential=credential
        )
    except Exception as exc:
        await store.record_poll(source_id, status="error", error=str(exc))
        raise HTTPException(status_code=502, detail=f"poll failed: {exc}")

    new_count = 0
    updated_count = 0
    items_summary: List[Dict[str, Any]] = []
    for raw_item in items_raw:
        ext_id = raw_item["external_id"]
        existing = await store.get_item_by_external_id(source_id, ext_id)
        item = await store.upsert_item(
            source_id=source_id,
            external_id=ext_id,
            title=raw_item.get("title"),
            body=raw_item.get("body"),
            url=raw_item.get("url"),
            state=raw_item.get("state"),
            labels=raw_item.get("labels"),
            author=raw_item.get("author"),
            raw=raw_item.get("raw"),
        )
        if existing:
            updated_count += 1
        else:
            new_count += 1
        items_summary.append({
            "id": item["id"],
            "external_id": ext_id,
            "title": raw_item.get("title"),
            "url": raw_item.get("url"),
            "state": raw_item.get("state"),
            "is_new": existing is None,
            "task_id": item.get("task_id"),
        })

    await store.record_poll(source_id, status="ok", item_count=len(items_raw))
    await record_audit_event(
        request,
        action="ticket_sources.poll",
        resource=f"project:{project_id}/ticket_source:{source_id}",
        details={"items_fetched": len(items_raw), "new": new_count, "updated": updated_count},
    )
    return {
        "status": "ok",
        "source_id": source_id,
        "total_fetched": len(items_raw),
        "new_count": new_count,
        "updated_count": updated_count,
        "items": items_summary,
    }


@router.get("/{project_id}/ticket-sources/{source_id}/items")
async def list_ticket_source_items(
    request: Request,
    project_id: str,
    source_id: str,
    limit: int = 50,
    offset: int = 0,
    unlinked_only: bool = False,
    status: Optional[str] = None,
    manager_bot_id: Optional[str] = None,
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    items = await store.list_items(
        source_id,
        limit=limit,
        offset=offset,
        unlinked_only=unlinked_only,
        status=status,
        manager_bot_id=manager_bot_id,
    )
    return {"source_id": source_id, "items": items, "count": len(items)}


@router.get("/{project_id}/ticket-sources/{source_id}/items/{external_id}/link")
async def get_item_link(
    request: Request, project_id: str, source_id: str, external_id: str
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    item = await store.get_item_by_external_id(source_id, external_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"item": item}


@router.post("/{project_id}/ticket-sources/{source_id}/items/{external_id}/link")
async def link_item_to_task(
    request: Request, project_id: str, source_id: str, external_id: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    task_id = str((body or {}).get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id is required")
    ok = await store.link_item_to_task(source_id, external_id, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="item not found")
    return {"status": "ok", "linked": {"source_id": source_id, "external_id": external_id, "task_id": task_id}}


class UpdateItemBody(BaseModel):
    status: Optional[str] = None
    manager_bot_id: Optional[str] = None
    clear_manager: bool = False
    clear_task: bool = False


@router.patch("/{project_id}/ticket-sources/{source_id}/items/{external_id}")
async def update_ticket_item(
    request: Request, project_id: str, source_id: str, external_id: str,
    body: UpdateItemBody,
) -> Dict[str, Any]:
    """Update an item's lifecycle status and/or manager assignment."""
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    item = await store.get_item_by_external_id(source_id, external_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    updated = item
    if body.manager_bot_id is not None or body.clear_manager:
        manager_id = None if body.clear_manager else (body.manager_bot_id or "").strip() or None
        updated = await store.set_item_manager(source_id, external_id, manager_id)
    if body.status is not None:
        updated = await store.update_item_status(
            source_id, external_id, status=body.status, clear_task=body.clear_task
        )
    await record_audit_event(
        request,
        action="ticket_sources.items.update",
        resource=f"project:{project_id}/ticket_source:{source_id}/item:{external_id}",
        details={
            "status": body.status,
            "manager_bot_id": body.manager_bot_id,
            "clear_manager": body.clear_manager,
            "clear_task": body.clear_task,
        },
    )
    return {"status": "ok", "item": updated}


class DispatchItemBody(BaseModel):
    manager_bot_id: Optional[str] = None
    instruction: Optional[str] = None
    plan_approval_required: Optional[bool] = None


@router.post("/{project_id}/ticket-sources/{source_id}/items/{external_id}/dispatch")
async def dispatch_ticket_item(
    request: Request, project_id: str, source_id: str, external_id: str,
    body: Optional[DispatchItemBody] = None,
) -> Dict[str, Any]:
    """Manually dispatch an item to a manager bot.

    Creates a conversation + assignment (orchestration) for the item and
    links the item to the resulting orchestration. The plan approval gate
    holds execution until the operator approves the generated plan.
    """
    store = _get_store(request)
    source = await store.get_source(source_id)
    if source is None or source["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="ticket source not found")
    item = await store.get_item_by_external_id(source_id, external_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    manager_bot_id = str((body.manager_bot_id if body else None) or item.get("manager_bot_id") or "").strip()
    if not manager_bot_id:
        raise HTTPException(status_code=400, detail="manager_bot_id is required to dispatch")

    chat_manager = getattr(request.app.state, "chat_manager", None)
    assignment_service = getattr(request.app.state, "assignment_service", None)
    if chat_manager is None or assignment_service is None:
        raise HTTPException(status_code=500, detail="chat/assignment services not initialized")

    bot_registry = getattr(request.app.state, "bot_registry", None)
    if bot_registry is not None:
        try:
            await bot_registry.get(manager_bot_id)
        except Exception:
            raise HTTPException(status_code=400, detail=f"manager bot not found: {manager_bot_id}")

    title = str(item.get("title") or f"Ticket {external_id}")
    instruction = str((body.instruction if body else None) or "").strip()
    if not instruction:
        instruction = (
            f"Work this ticket from the {source.get('name') or source_id} board.\n\n"
            f"Title: {title}\n"
            f"External ID: {external_id}\n"
            f"URL: {item.get('url') or 'n/a'}\n"
            f"Description:\n{item.get('body') or item.get('raw') or 'No description provided.'}"
        )

    plan_approval_required = None
    if body and body.plan_approval_required is not None:
        plan_approval_required = bool(body.plan_approval_required)

    conversation = await chat_manager.create_conversation(
        title=f"Ticket {external_id}: {title[:80]}",
        project_id=project_id,
        scope="project",
        default_bot_id=manager_bot_id,
        owner_user_id=None,
    )

    try:
        assignment = await assignment_service.create_assignment(
            conversation_id=conversation.id,
            instruction=instruction,
            pm_bot_id=manager_bot_id,
            context_items=[],
            node_overrides={},
            task_source="ticket_dispatch",
        )
    except Exception as exc:
        await chat_manager.delete_conversation(conversation.id)
        raise HTTPException(status_code=500, detail=f"assignment failed: {exc}")

    orchestration_id = str(assignment.get("orchestration_id") or "").strip()
    task_id = str(assignment.get("task_id") or "").strip()

    # Link the item to the orchestration (and task if available).
    await store.update_item_status(
        source_id, external_id,
        status="assigned",
        task_id=task_id or None,
    )
    await store.set_item_manager(source_id, external_id, manager_bot_id)

    if plan_approval_required is not None:
        try:
            project_registry = getattr(request.app.state, "project_registry", None)
            if project_registry is not None:
                project = await project_registry.get(project_id)
                settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
                workflow_cfg = settings.get("workflow") if isinstance(settings.get("workflow"), dict) else {}
                merged = dict(workflow_cfg)
                merged["plan_approval_required"] = plan_approval_required
                from control_plane.api.projects import _merge_settings
                updated = project.model_copy(update={"settings_overrides": _merge_settings(project, {"workflow": merged})})
                await project_registry.update(project_id, updated)
        except Exception:
            pass

    await record_audit_event(
        request,
        action="ticket_sources.items.dispatch",
        resource=f"project:{project_id}/ticket_source:{source_id}/item:{external_id}",
        details={
            "manager_bot_id": manager_bot_id,
            "orchestration_id": orchestration_id,
            "task_id": task_id,
        },
    )
    return {
        "status": "ok",
        "dispatched": True,
        "item": {
            "source_id": source_id,
            "external_id": external_id,
            "title": title,
            "manager_bot_id": manager_bot_id,
        },
        "orchestration_id": orchestration_id or None,
        "task_id": task_id or None,
        "conversation_id": conversation.id,
    }