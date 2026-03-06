import os
from typing import Any, Dict, List

import httpx


async def discover_local_models(worker_config: Dict[str, Any]) -> Dict[str, Any]:
    ollama_host = str(worker_config.get("ollama_host") or "http://localhost:11434").rstrip("/")
    discovered: List[Dict[str, Any]] = []
    errors: List[str] = []

    # Ollama discovery.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ollama_host}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("models", []):
                    name = str(m.get("name") or "")
                    if name:
                        discovered.append({"provider": "ollama", "name": name})
            else:
                errors.append(f"ollama tags status={resp.status_code}")
    except Exception as e:
        errors.append(f"ollama discovery failed: {e}")

    # Optional vLLM models via env CSV.
    vllm_csv = (os.environ.get("VLLM_MODELS", "") or "").strip()
    if vllm_csv:
        for name in [x.strip() for x in vllm_csv.split(",") if x.strip()]:
            discovered.append({"provider": "vllm", "name": name})

    # Deduplicate.
    seen = set()
    unique = []
    for m in discovered:
        k = (m["provider"], m["name"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(m)

    return {"models": unique, "errors": errors}

