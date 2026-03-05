from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("/events")
async def list_audit_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
) -> List[Dict[str, Any]]:
    audit_log = request.app.state.audit_log
    return await audit_log.list_events(limit=limit)

