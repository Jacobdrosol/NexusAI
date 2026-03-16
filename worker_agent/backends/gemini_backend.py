from typing import Any, Dict, List

import httpx


async def infer(
    model: str,
    messages: List[Dict],
    params: Dict,
    api_key: str,
) -> Dict[str, Any]:
    parts = [{"text": msg.get("content", "")} for msg in messages]
    body: Dict[str, Any] = {
        "contents": [{"parts": parts}],
    }
    if params:
        body["generationConfig"] = params
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"x-goog-api-key": api_key},
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        output = data["candidates"][0]["content"]["parts"][0]["text"]
        finish_reason = ""
        try:
            finish_reason = str((data.get("candidates") or [{}])[0].get("finishReason") or "").strip()
        except Exception:
            finish_reason = ""
        result = {"output": output, "usage": data.get("usageMetadata", {})}
        if finish_reason:
            result["finish_reason"] = finish_reason
        return result
