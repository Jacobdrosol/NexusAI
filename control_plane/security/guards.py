import asyncio
import os
import time
from collections import deque
from typing import Deque, Dict, Tuple

from fastapi import HTTPException, Request


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _route_key(route_name: str) -> str:
    return route_name.upper().replace("-", "_")


def _resolve_max_body(route_name: str, default_bytes: int) -> int:
    route_key = _route_key(route_name)
    return _env_int(f"CP_MAX_BODY_BYTES_{route_key}", default_bytes)


def _resolve_rate_limit(route_name: str, default_limit: int, default_window_seconds: int) -> Tuple[int, int]:
    route_key = _route_key(route_name)
    limit = _env_int(f"CP_RATE_LIMIT_{route_key}_COUNT", default_limit)
    window = _env_int(f"CP_RATE_LIMIT_{route_key}_WINDOW_SECONDS", default_window_seconds)
    return max(1, limit), max(1, window)


async def enforce_body_size(request: Request, route_name: str, default_max_bytes: int) -> None:
    max_bytes = _resolve_max_body(route_name, default_max_bytes)
    if max_bytes <= 0:
        return

    cl_header = (request.headers.get("content-length") or "").strip()
    if cl_header:
        try:
            content_length = int(cl_header)
            if content_length > max_bytes:
                raise HTTPException(status_code=413, detail=f"request body too large (max {max_bytes} bytes)")
        except ValueError:
            pass

    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"request body too large (max {max_bytes} bytes)")


async def enforce_rate_limit(
    request: Request,
    route_name: str,
    default_limit: int,
    default_window_seconds: int,
) -> None:
    limit, window = _resolve_rate_limit(route_name, default_limit, default_window_seconds)
    now = time.time()
    client = request.client.host if request.client and request.client.host else "unknown"
    key = f"{route_name}:{client}"

    if not hasattr(request.app.state, "rate_limit_lock"):
        request.app.state.rate_limit_lock = asyncio.Lock()
    if not hasattr(request.app.state, "rate_limit_store"):
        request.app.state.rate_limit_store = {}

    lock = request.app.state.rate_limit_lock
    store: Dict[str, Deque[float]] = request.app.state.rate_limit_store

    async with lock:
        bucket = store.get(key)
        if bucket is None:
            bucket = deque()
            store[key] = bucket

        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded")

        bucket.append(now)

