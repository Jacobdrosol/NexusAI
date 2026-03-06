from typing import Any

import httpx


async def infer(
    model: str,
    messages: list[dict],
    params: dict,
    host: str = "http://localhost:11434",
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": params,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(f"{host}/api/chat", json=body)
        response.raise_for_status()
        data = response.json()
        output = data.get("message", {}).get("content", "")
        usage = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        }
        return {"output": output, "usage": usage}
