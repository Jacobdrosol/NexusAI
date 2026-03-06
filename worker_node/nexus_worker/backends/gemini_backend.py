from typing import Any

import httpx


async def infer(model: str, messages: list[dict], params: dict, api_key: str) -> dict[str, Any]:
    parts = [{"text": msg.get("content", "")} for msg in messages]
    body: dict[str, Any] = {
        "contents": [{"parts": parts}],
    }
    if params:
        body["generationConfig"] = params
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"x-goog-api-key": api_key},
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        output = data["candidates"][0]["content"]["parts"][0]["text"]
        return {"output": output, "usage": data.get("usageMetadata", {})}
