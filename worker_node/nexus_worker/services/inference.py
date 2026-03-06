import os
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException

from nexus_worker.backends import (
    claude_backend,
    cli_backend,
    gemini_backend,
    ollama_backend,
    openai_backend,
)


def _cloud_context_policy(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    policy = os.environ.get("NEXUS_WORKER_CLOUD_CONTEXT_POLICY", "redact").strip().lower()
    if policy not in {"allow", "redact", "block"}:
        policy = "redact"

    has_context = any(
        str(m.get("role", "")).lower() == "system"
        and str(m.get("content", "")).startswith("Context:\n")
        for m in messages
        if isinstance(m, dict)
    )
    if not has_context:
        return messages, False
    if policy == "allow":
        return messages, False
    if policy == "block":
        raise HTTPException(status_code=403, detail="Cloud context transfer blocked by policy")

    redacted = []
    for m in messages:
        if (
            isinstance(m, dict)
            and str(m.get("role", "")).lower() == "system"
            and str(m.get("content", "")).startswith("Context:\n")
        ):
            redacted.append({**m, "content": "Context:\n[REDACTED_BY_POLICY]"})
        else:
            redacted.append(m)
    return redacted, True


async def run_inference(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    params: Dict[str, Any],
    worker_config: Dict[str, Any],
    command: str = "",
) -> Dict[str, Any]:
    if provider == "ollama":
        return await ollama_backend.infer(
            model=model,
            messages=messages,
            params=params,
            host=str(worker_config.get("ollama_host") or "http://localhost:11434"),
        )

    if provider in {"openai", "claude", "gemini"}:
        safe_messages, redacted = _cloud_context_policy(messages)
        if provider == "openai":
            out = await openai_backend.infer(
                model=model,
                messages=safe_messages,
                params=params,
                api_key=os.environ.get("OPENAI_API_KEY", ""),
            )
        elif provider == "claude":
            out = await claude_backend.infer(
                model=model,
                messages=safe_messages,
                params=params,
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            )
        else:
            out = await gemini_backend.infer(
                model=model,
                messages=safe_messages,
                params=params,
                api_key=os.environ.get("GEMINI_API_KEY", ""),
            )
        if redacted:
            out["policy_context_redacted"] = True
        return out

    if provider == "cli":
        return await cli_backend.infer(command=command or model, params=params)

    raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
