from typing import Any, Dict, List

import httpx


async def infer(
    model: str,
    messages: List[Dict],
    params: Dict,
    api_key: str,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
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
