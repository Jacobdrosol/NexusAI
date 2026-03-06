from typing import Any

import httpx


async def infer(model: str, messages: list[dict], params: dict, api_key: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    body.update(params)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        output = data["choices"][0]["message"]["content"]
        return {"output": output, "usage": data.get("usage", {})}
