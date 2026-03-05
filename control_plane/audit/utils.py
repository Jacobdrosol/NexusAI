from typing import Any, Optional

from fastapi import Request


def _actor_from_request(request: Request) -> Optional[str]:
    auth = (request.headers.get("Authorization", "") or "").strip()
    if auth:
        return auth[:64]
    api_key = (request.headers.get("X-Nexus-API-Key", "") or "").strip()
    if api_key:
        return "api_key"
    client = request.client.host if request.client and request.client.host else ""
    return client or None


async def record_audit_event(
    request: Request,
    action: str,
    resource: str,
    status: str = "ok",
    details: Optional[Any] = None,
) -> None:
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is None:
        return
    await audit_log.record(
        action=action,
        resource=resource,
        status=status,
        actor=_actor_from_request(request),
        details=details,
    )

