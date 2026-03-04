import logging
import os
from typing import Any

import httpx

from shared.exceptions import BackendError, BotNotFoundError, NoViableBackendError
from shared.models import BackendConfig, Task, Worker

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        bot_registry: Any,
        worker_registry: Any,
        key_vault: Any = None,
        model_registry: Any = None,
    ) -> None:
        self.bot_registry = bot_registry
        self.worker_registry = worker_registry
        self.key_vault = key_vault
        self.model_registry = model_registry

    async def schedule(self, task: Task) -> Any:
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except BotNotFoundError:
            raise

        if not bot.enabled:
            raise NoViableBackendError(f"Bot {task.bot_id} is disabled")

        last_error: Exception = NoViableBackendError("No backends configured")

        for backend in bot.backends:
            try:
                result = await self._dispatch_backend(backend, task.payload)
                return result
            except Exception as e:
                logger.warning(
                    "Backend %s/%s failed for task %s: %s",
                    backend.provider,
                    backend.model,
                    task.id,
                    e,
                )
                last_error = e
                continue

        raise NoViableBackendError(
            f"All backends failed for task {task.id}"
        ) from last_error

    async def _dispatch_backend(self, backend: BackendConfig, payload: Any) -> Any:
        await self._validate_model_if_catalog_present(backend)
        if backend.type in ("local_llm", "remote_llm"):
            if not backend.worker_id:
                raise BackendError("worker_id is required for local_llm/remote_llm backends")
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            if worker.status != "online":
                raise BackendError(
                    f"Worker {backend.worker_id} is not online (status={worker.status})"
                )
            return await self._dispatch_to_worker(worker, backend, payload)
        elif backend.type == "cloud_api":
            if backend.provider == "openai":
                return await self._call_openai(backend, payload)
            elif backend.provider == "claude":
                return await self._call_claude(backend, payload)
            elif backend.provider == "gemini":
                return await self._call_gemini(backend, payload)
            else:
                raise BackendError(f"Unknown cloud_api provider: {backend.provider}")
        elif backend.type == "cli":
            if not backend.worker_id:
                raise BackendError("worker_id is required for cli backends")
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            return await self._dispatch_to_worker(worker, backend, payload)
        else:
            raise BackendError(f"Unsupported backend type: {backend.type}")

    async def _dispatch_to_worker(
        self, worker: Worker, backend: BackendConfig, payload: Any
    ) -> Any:
        url = f"http://{worker.host}:{worker.port}/infer"
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        body = {
            "model": backend.model,
            "provider": backend.provider,
            "messages": payload if isinstance(payload, list) else [{"role": "user", "content": str(payload)}],
            "params": params_dict,
        }
        if backend.gpu_id:
            body["gpu_id"] = backend.gpu_id
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            return response.json()

    async def _call_openai(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "OPENAI_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "OPENAI_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your OpenAI API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        body: dict = {
            "model": backend.model,
            "messages": messages,
        }
        body.update(params_dict)
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

    async def _call_claude(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "ANTHROPIC_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Anthropic API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        max_tokens = params_dict.pop("max_tokens", 1024)
        body: dict = {
            "model": backend.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        body.update(params_dict)
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            output = data["content"][0]["text"]
            return {"output": output, "usage": data.get("usage", {})}

    async def _call_gemini(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "GEMINI_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "GEMINI_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Gemini API key before starting the service."
            )
        messages = (
            payload
            if isinstance(payload, list)
            else [{"role": "user", "content": str(payload)}]
        )
        # Convert messages to Gemini format
        parts = []
        for msg in messages:
            parts.append({"text": msg.get("content", "")})
        body = {
            "contents": [{"parts": parts}],
        }
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        if params_dict:
            body["generationConfig"] = params_dict
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{backend.model}:generateContent?key={api_key}"
        )
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            data = response.json()
            output = data["candidates"][0]["content"]["parts"][0]["text"]
            return {"output": output, "usage": data.get("usageMetadata", {})}

    async def _resolve_api_key(self, api_key_ref: str, default_env_var: str) -> str:
        if self.key_vault and api_key_ref:
            try:
                return (await self.key_vault.get_secret(api_key_ref)).strip()
            except Exception:
                # Fall through to environment-variable lookup for backward compatibility.
                pass

        if api_key_ref:
            return os.environ.get(api_key_ref, "").strip()
        return os.environ.get(default_env_var, "").strip()

    async def _validate_model_if_catalog_present(self, backend: BackendConfig) -> None:
        if not self.model_registry:
            return
        try:
            has_models = await self.model_registry.has_any()
            if not has_models:
                return
            exists = await self.model_registry.exists(backend.provider, backend.model)
            if not exists:
                raise BackendError(
                    f"Model '{backend.model}' (provider '{backend.provider}') "
                    "is not present/enabled in the model catalog."
                )
        except BackendError:
            raise
        except Exception:
            # If model registry lookup fails unexpectedly, avoid blocking execution.
            return
