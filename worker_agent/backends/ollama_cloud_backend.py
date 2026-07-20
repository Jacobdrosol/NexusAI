from typing import Any, Dict, List

import httpx


async def infer(
    model: str,
    messages: List[Dict],
    params: Dict,
    api_key: str,
    base_url: str = "https://ollama.com/api",
) -> Dict[str, Any]:
    if not api_key:
        raise ValueError("OLLAMA_API_KEY is not configured")

    request_params = dict(params or {})
    response_format = request_params.pop("response_format", None)
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        # Prevent short bounded calls from spending their output budget on hidden
        # reasoning before a final answer can be returned.
        "think": request_params.pop("think", False),
        "options": request_params,
    }
    if response_format == "json":
        body["format"] = "json"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        data = response.json()

    output = data.get("message", {}).get("content", "")
    usage = {
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "completion_tokens": data.get("eval_count", 0),
    }
    finish_reason = str(data.get("done_reason") or data.get("finish_reason") or "").strip()
    result = {"output": output, "usage": usage}
    if finish_reason:
        result["finish_reason"] = finish_reason
    return result
