"""Optional authentication for inference requests sent to a worker node."""
from __future__ import annotations

import hmac
import os
import re
from typing import Any

from fastapi import HTTPException, Request


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_worker_request_token(request: Request, worker_config: dict[str, Any]) -> None:
    """Require the configured control-plane token when this node opts in."""
    token_env = str(worker_config.get("request_token_env") or "").strip()
    if not token_env:
        return
    if not _ENV_NAME_RE.fullmatch(token_env):
        raise HTTPException(status_code=503, detail="Worker request token configuration is invalid")
    expected = os.environ.get(token_env, "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Worker request token is not configured")
    provided = request.headers.get("X-Nexus-Worker-Token", "").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Worker request token is invalid")
