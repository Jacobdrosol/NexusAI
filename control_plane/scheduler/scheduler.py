import logging
import os
import time
import json
from typing import Any, AsyncGenerator

import httpx

from shared.exceptions import BackendError, BotNotFoundError, NoViableBackendError
from shared.models import BackendConfig, Task, Worker

logger = logging.getLogger(__name__)


def _backend_failure_message(task_id: str, last_error: Exception) -> str:
    detail = str(last_error or "").strip()
    if detail:
        return f"All backends failed for task {task_id}: {detail}"
    return f"All backends failed for task {task_id}"


def _ollama_options(params: dict[str, Any]) -> dict[str, Any]:
    options = dict(params or {})
    max_tokens = options.pop("max_tokens", None)
    if max_tokens is not None and "num_predict" not in options:
        options["num_predict"] = max_tokens
    return options


def _worker_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=None, write=120.0, pool=30.0)


def _payload_to_messages(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, list):
        normalized: list[dict[str, str]] = []
        for item in payload:
            if isinstance(item, dict):
                role = str(item.get("role") or "user")
                content = item.get("content")
                if isinstance(content, str):
                    normalized.append({"role": role, "content": content})
                else:
                    normalized.append({"role": role, "content": json.dumps(content if content is not None else "", ensure_ascii=False)})
            else:
                normalized.append({"role": "user", "content": str(item)})
        return normalized
    if isinstance(payload, dict):
        return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    return [{"role": "user", "content": str(payload)}]


def _inject_system_prompt(system_prompt: str | None, payload: Any) -> Any:
    prompt = str(system_prompt or "").strip()
    if not prompt:
        return payload

    messages = _payload_to_messages(payload)
    if messages and str(messages[0].get("role") or "").lower() == "system":
        existing = str(messages[0].get("content") or "").strip()
        if existing == prompt:
            return messages
    return [{"role": "system", "content": prompt}, *messages]


def _lookup_payload_path(payload: Any, path: str) -> Any:
    current: Any = payload
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _transform_template_value(template: Any, payload: Any) -> Any:
    if isinstance(template, dict):
        return {str(key): _transform_template_value(value, payload) for key, value in template.items()}
    if isinstance(template, list):
        return [_transform_template_value(item, payload) for item in template]
    if not isinstance(template, str):
        return template

    raw = template.strip()
    if raw.startswith("{{") and raw.endswith("}}"):
        expr = raw[2:-2].strip()
        mode = "value"
        path = expr
        if expr.startswith("json:"):
            mode = "json"
            path = expr[5:].strip()
        if path.startswith("payload."):
            path = path[8:].strip()
        value = _lookup_payload_path(payload, path)
        if mode == "json":
            if value in (None, ""):
                return None
            if isinstance(value, (dict, list)):
                return value
            return json.loads(str(value))
        return value
    return template


class Scheduler:
    def __init__(
        self,
        bot_registry: Any,
        worker_registry: Any,
        key_vault: Any = None,
        model_registry: Any = None,
        project_registry: Any = None,
    ) -> None:
        self.bot_registry = bot_registry
        self.worker_registry = worker_registry
        self.key_vault = key_vault
        self.model_registry = model_registry
        self.project_registry = project_registry
        self._inflight_by_worker: dict[str, int] = {}
        self._latency_ema_ms: dict[str, float] = {}
        self._latency_alpha = float(os.environ.get("NEXUSAI_WORKER_LATENCY_EMA_ALPHA", "0.30"))
        self._default_latency_ms = float(os.environ.get("NEXUSAI_WORKER_DEFAULT_LATENCY_MS", "800"))

    async def schedule(self, task: Task) -> Any:
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except BotNotFoundError:
            raise

        if not bot.enabled:
            raise NoViableBackendError(f"Bot {task.bot_id} is disabled")

        last_error: Exception = NoViableBackendError("No backends configured")
        transformed_payload = self._apply_input_transform(bot, task.payload)
        prepared_payload = _inject_system_prompt(getattr(bot, "system_prompt", None), transformed_payload)

        for backend in bot.backends:
            try:
                result = await self._dispatch_backend(backend, prepared_payload, task=task)
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

        raise NoViableBackendError(_backend_failure_message(task.id, last_error)) from last_error

    async def stream(self, task: Task) -> AsyncGenerator[dict[str, Any], None]:
        try:
            bot = await self.bot_registry.get(task.bot_id)
        except BotNotFoundError:
            raise

        if not bot.enabled:
            raise NoViableBackendError(f"Bot {task.bot_id} is disabled")

        last_error: Exception = NoViableBackendError("No backends configured")
        transformed_payload = self._apply_input_transform(bot, task.payload)
        prepared_payload = _inject_system_prompt(getattr(bot, "system_prompt", None), transformed_payload)

        for backend in bot.backends:
            try:
                yield {
                    "event": "backend_selected",
                    "provider": backend.provider,
                    "model": backend.model,
                    "worker_id": backend.worker_id,
                }
                async for event in self._dispatch_backend_stream(backend, prepared_payload, task=task):
                    yield event
                return
            except Exception as e:
                logger.warning(
                    "Backend %s/%s failed for stream task %s: %s",
                    backend.provider,
                    backend.model,
                    task.id,
                    e,
                )
                last_error = e
                continue

        raise NoViableBackendError(_backend_failure_message(task.id, last_error)) from last_error

    async def _dispatch_backend(self, backend: BackendConfig, payload: Any, task: Task | None = None) -> Any:
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm"):
            worker = await self._resolve_worker_for_llm_backend(backend)
            if worker.status != "online":
                raise BackendError(
                    f"Worker {worker.id} is not online (status={worker.status})"
                )
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        elif backend.type == "cloud_api":
            if backend.provider == "openai":
                return await self._call_openai(backend, safe_payload)
            elif backend.provider == "ollama_cloud":
                return await self._call_ollama_cloud(backend, safe_payload)
            elif backend.provider == "claude":
                return await self._call_claude(backend, safe_payload)
            elif backend.provider == "gemini":
                return await self._call_gemini(backend, safe_payload)
            else:
                raise BackendError(f"Unknown cloud_api provider: {backend.provider}")
        elif backend.type == "cli":
            if not backend.worker_id:
                raise BackendError("worker_id is required for cli backends")
            try:
                worker = await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e
            return await self._dispatch_to_worker(worker, backend, safe_payload)
        else:
            raise BackendError(f"Unsupported backend type: {backend.type}")

    def _apply_input_transform(self, bot: Any, payload: Any) -> Any:
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return payload
        config = routing_rules.get("input_transform")
        if not isinstance(config, dict) or not bool(config.get("enabled", False)):
            return payload
        template = config.get("template")
        if template is None:
            return payload
        return _transform_template_value(template, payload)

    async def _dispatch_backend_stream(
        self, backend: BackendConfig, payload: Any, task: Task | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        await self._validate_model_if_catalog_present(backend)
        safe_payload = await self._apply_cloud_context_policy(backend, payload, task=task)
        if backend.type in ("local_llm", "remote_llm", "cli"):
            worker = await self._resolve_worker_for_llm_backend(backend) if backend.type != "cli" else await self.worker_registry.get(backend.worker_id)  # type: ignore[arg-type]
            if worker.status != "online":
                raise BackendError(
                    f"Worker {worker.id} is not online (status={worker.status})"
                )
            yield {
                "event": "dispatch_started",
                "worker_id": worker.id,
                "host": worker.host,
                "port": worker.port,
                "provider": backend.provider,
                "model": backend.model,
            }
            async for event in self._dispatch_to_worker_stream(worker, backend, safe_payload):
                yield event
            return
        if backend.type == "cloud_api":
            result = await self._dispatch_backend(backend, payload, task=task)
            yield {"event": "final", **result}
            return
        raise BackendError(f"Unsupported backend type: {backend.type}")

    async def _apply_cloud_context_policy(
        self,
        backend: BackendConfig,
        payload: Any,
        task: Task | None = None,
    ) -> Any:
        # Applies only to cloud backends; local/remote worker execution keeps full payload.
        if backend.type != "cloud_api":
            return payload
        if not isinstance(payload, list):
            return payload

        policy = await self._resolve_cloud_context_policy(backend=backend, task=task)

        has_context = any(
            isinstance(m, dict)
            and str(m.get("role", "")).lower() == "system"
            and str(m.get("content", "")).startswith("Context:\n")
            for m in payload
        )
        if not has_context:
            return payload

        if policy == "allow":
            return payload
        if policy == "block":
            raise BackendError(
                "Cloud context policy blocks sending context payloads to cloud providers"
            )

        # redact policy
        redacted = []
        for m in payload:
            if (
                isinstance(m, dict)
                and str(m.get("role", "")).lower() == "system"
                and str(m.get("content", "")).startswith("Context:\n")
            ):
                redacted.append(
                    {
                        **m,
                        "content": "Context:\n[REDACTED_BY_POLICY]",
                    }
                )
            else:
                redacted.append(m)
        return redacted

    async def _resolve_cloud_context_policy(self, backend: BackendConfig, task: Task | None = None) -> str:
        default_policy = os.environ.get("NEXUSAI_CLOUD_CONTEXT_POLICY", "allow").strip().lower()
        if default_policy not in {"allow", "redact", "block"}:
            default_policy = "allow"
        if backend.type != "cloud_api":
            return default_policy

        provider = str(backend.provider or "").strip().lower()
        if not provider:
            return default_policy
        if not task or not task.metadata or not getattr(task.metadata, "project_id", None):
            return default_policy
        if self.project_registry is None:
            return default_policy

        project_id = str(task.metadata.project_id or "").strip()
        if not project_id:
            return default_policy

        try:
            project = await self.project_registry.get(project_id)
        except Exception:
            return default_policy

        settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
        cfg = settings.get("cloud_context_policy") if isinstance(settings.get("cloud_context_policy"), dict) else {}
        provider_policies = cfg.get("provider_policies") if isinstance(cfg.get("provider_policies"), dict) else {}
        bot_overrides = cfg.get("bot_overrides") if isinstance(cfg.get("bot_overrides"), dict) else {}

        baseline = str(provider_policies.get(provider, default_policy)).strip().lower()
        if baseline not in {"allow", "redact", "block"}:
            baseline = default_policy
        if baseline == "block":
            return "block"

        bot_id = str(task.bot_id or "").strip()
        bot_cfg = bot_overrides.get(bot_id) if isinstance(bot_overrides.get(bot_id), dict) else {}
        override = str(bot_cfg.get(provider, "")).strip().lower()
        if override not in {"allow", "redact", "block"}:
            override = ""

        if baseline == "redact":
            if override == "block":
                return "block"
            return "redact"

        # baseline allow
        if override:
            return override
        return "allow"

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
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                response = await client.post(url, json=body)
                response.raise_for_status()
                return response.json()
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                prev = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * prev)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

    async def _dispatch_to_worker_stream(
        self, worker: Worker, backend: BackendConfig, payload: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        url = f"http://{worker.host}:{worker.port}/infer/stream"
        params_dict = backend.params.model_dump(exclude_none=True) if backend.params else {}
        body = {
            "model": backend.model,
            "provider": backend.provider,
            "messages": payload if isinstance(payload, list) else [{"role": "user", "content": str(payload)}],
            "params": params_dict,
        }
        if backend.gpu_id:
            body["gpu_id"] = backend.gpu_id
        self._inflight_by_worker[worker.id] = int(self._inflight_by_worker.get(worker.id, 0)) + 1
        started = time.perf_counter()
        saw_token = False
        logger.info(
            "Dispatching stream task to worker=%s provider=%s model=%s url=%s",
            worker.id,
            backend.provider,
            backend.model,
            url,
        )
        async with httpx.AsyncClient(timeout=_worker_timeout()) as client:
            try:
                async with client.stream("POST", url, json=body) as response:
                    response.raise_for_status()
                    buffer = ""
                    event_type = "message"
                    async for chunk in response.aiter_text():
                        if not chunk:
                            continue
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            if not block.strip():
                                continue
                            event_type = "message"
                            data_text = ""
                            for line in block.splitlines():
                                if line.startswith("event:"):
                                    event_type = line[6:].strip()
                                elif line.startswith("data:"):
                                    data_text += line[5:].strip()
                            if not data_text:
                                continue
                            payload_obj = json.loads(data_text)
                            if isinstance(payload_obj, dict):
                                payload_obj.setdefault("event", event_type)
                                if event_type == "token" and not saw_token:
                                    saw_token = True
                                    logger.info(
                                        "First stream token received worker=%s provider=%s model=%s",
                                        worker.id,
                                        backend.provider,
                                        backend.model,
                                    )
                                yield payload_obj
            finally:
                logger.info(
                    "Stream task finished worker=%s provider=%s model=%s elapsed_ms=%.1f saw_token=%s",
                    worker.id,
                    backend.provider,
                    backend.model,
                    (time.perf_counter() - started) * 1000.0,
                    saw_token,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                prev = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
                alpha = min(max(self._latency_alpha, 0.01), 1.0)
                self._latency_ema_ms[worker.id] = (alpha * elapsed_ms) + ((1.0 - alpha) * prev)
                self._inflight_by_worker[worker.id] = max(
                    0, int(self._inflight_by_worker.get(worker.id, 1)) - 1
                )

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

    async def _call_ollama_cloud(self, backend: BackendConfig, payload: Any) -> Any:
        api_key_ref = backend.api_key_ref or "OLLAMA_API_KEY"
        api_key = await self._resolve_api_key(api_key_ref, "OLLAMA_API_KEY")
        if not api_key:
            raise BackendError(
                f"API key not found. Set the environment variable '{api_key_ref}' "
                f"with your Ollama API key before starting the service."
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
            "stream": False,
            "options": _ollama_options(params_dict),
        }
        base_url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api").rstrip("/")
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{base_url}/chat",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                detail = ""
                try:
                    payload_data = response.json()
                    if isinstance(payload_data, dict):
                        detail = str(
                            payload_data.get("error")
                            or payload_data.get("detail")
                            or payload_data.get("message")
                            or ""
                        ).strip()
                except Exception:
                    detail = (response.text or "").strip()
                status = response.status_code
                if detail:
                    raise BackendError(f"Ollama Cloud request failed ({status}): {detail}") from e
                raise BackendError(f"Ollama Cloud request failed ({status})") from e
            data = response.json()
            output = data.get("message", {}).get("content", "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            }
            return {"output": output, "usage": usage}

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
            f"{backend.model}:generateContent"
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

    async def _resolve_worker_for_llm_backend(self, backend: BackendConfig) -> Worker:
        if backend.worker_id:
            try:
                return await self.worker_registry.get(backend.worker_id)
            except Exception as e:
                raise BackendError(f"Worker not found: {backend.worker_id}") from e

        workers = await self.worker_registry.list()
        candidates = [
            w
            for w in workers
            if w.enabled and w.status == "online" and self._worker_supports_backend(w, backend)
        ]
        if not candidates:
            raise BackendError(
                f"No online worker supports provider={backend.provider} model={backend.model}"
            )
        return min(candidates, key=self._score_worker)

    def _worker_supports_backend(self, worker: Worker, backend: BackendConfig) -> bool:
        backend_provider = str(backend.provider or "").strip().lower()
        backend_model = str(backend.model or "").strip()
        for cap in worker.capabilities:
            if str(cap.type).lower() != "llm":
                continue
            if str(cap.provider).lower() != backend_provider:
                continue
            if backend_model in (cap.models or []):
                return True
        return False

    def _score_worker(self, worker: Worker) -> float:
        metrics = worker.metrics
        queue_depth = int(getattr(metrics, "queue_depth", 0) or 0)
        load = float(getattr(metrics, "load", 0.0) or 0.0)
        gpu_util = getattr(metrics, "gpu_utilization", None) or []
        gpu_avg = (sum(gpu_util) / len(gpu_util)) if gpu_util else 0.0
        inflight = int(self._inflight_by_worker.get(worker.id, 0))
        latency_ms = float(self._latency_ema_ms.get(worker.id, self._default_latency_ms))
        return (
            (queue_depth * 5.0)
            + (inflight * 4.0)
            + (load / 20.0)
            + (gpu_avg / 25.0)
            + (latency_ms / 500.0)
        )

    def get_worker_runtime_metrics(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for worker_id in set(self._inflight_by_worker.keys()) | set(self._latency_ema_ms.keys()):
            out[worker_id] = {
                "inflight": float(self._inflight_by_worker.get(worker_id, 0)),
                "latency_ema_ms": float(self._latency_ema_ms.get(worker_id, self._default_latency_ms)),
            }
        return out
