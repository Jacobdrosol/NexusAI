# NexusAI Worker Agent

The Worker Agent is a standalone **FastAPI** service that handles AI inference on behalf of the NexusAI control plane. Each worker registers itself with the control plane on startup, continuously sends heartbeats, and exposes HTTP endpoints for capability discovery, health checks, and inference.

---

## Architecture Overview

```
Control Plane
     │  POST /v1/workers          (registration)
     │  POST /v1/workers/{id}/heartbeat  (every N seconds)
     ▼
Worker Agent (FastAPI)
  ├── GET  /capabilities
  ├── GET  /health
  ├── POST /infer
  └── GET  /metrics          (Prometheus text format)
         │
         ├── ollama_backend  ──► local Ollama server
         ├── openai_backend  ──► api.openai.com
         ├── claude_backend  ──► api.anthropic.com
         ├── gemini_backend  ──► generativelanguage.googleapis.com
         └── cli_backend     ──► subprocess shell command
```

---

## main.py

### Application Factory

`create_app()` builds the FastAPI application:

1. Calls `install_observability(app)` to attach Prometheus middleware and the `/metrics` endpoint.
2. Registers the three API routers: `health`, `capabilities`, `infer`.
3. Attaches a generic 500 exception handler that returns `{"error": "<ExceptionType>", "detail": "<message>"}`.

### Startup / Lifespan (`lifespan`)

The `lifespan` async context manager runs on every application start:

1. **Logging** — configures `INFO`-level structured logging (`asctime name level message`).
2. **Config loading** — loads the worker YAML config via `ConfigLoader.load_yaml(WORKER_CONFIG_PATH)`. On failure, falls back to a minimal default config (`id="unknown-worker"`, `host="localhost"`, `port=8080`).
3. **Registration** — `POST {CONTROL_PLANE_URL}/v1/workers` with the full worker config JSON and the `X-Nexus-API-Key` header. Failures are logged as warnings but do not abort startup.
4. **Heartbeat loop** — starts `_send_heartbeats()` as a background `asyncio.Task`.
5. **Shutdown** — cancels the heartbeat task and awaits `CancelledError`.

### Heartbeat Loop (`_send_heartbeats`)

Runs every `HEARTBEAT_INTERVAL` seconds. Each tick:

1. Calls `get_gpu_info()` and computes per-GPU memory utilisation as `(memory_used / memory_total) * 100`.
2. Reads `app.state.inference_inflight` for the current in-flight request count.
3. `POST {CONTROL_PLANE_URL}/v1/workers/{worker_id}/heartbeat` with `{"metrics": {"queue_depth": N, "gpu_utilization": [...]}}`.
   - `gpu_utilization` key is omitted when no GPUs are detected.
4. Heartbeat errors are swallowed (warning log only) so a transient control-plane outage does not crash the worker.

### Direct Execution

When run as `python -m worker_agent.main` (or `python worker_agent/main.py`), the file calls `uvicorn.run()` using the `host` and `port` from the worker config (defaulting to `0.0.0.0:8080`).

---

## GPU Monitoring (`gpu_monitor.py`)

`get_gpu_info()` returns a list of dicts describing each NVIDIA GPU:

```python
[
    {
        "id": "GPU-0",
        "name": "NVIDIA GeForce RTX 4090",
        "memory_total": 25769803776,   # bytes
        "memory_used":   4294967296,   # bytes
    },
    ...
]
```

- Requires the optional `pynvml` package (NVIDIA Management Library Python bindings).
- If `pynvml` is not installed, `get_gpu_info()` returns `[]` silently.
- Any runtime NVML error (e.g., no NVIDIA driver) also returns `[]` after logging a warning.
- GPU utilisation percentage is calculated by the caller: `memory_used / memory_total * 100`.

---

## API Endpoints

### `GET /capabilities`

Returns the worker's declared capabilities as loaded from its YAML config.

**Response:**
```json
{
  "worker_id": "my-worker",
  "capabilities": [...]
}
```

Source of truth is `app.state.worker_config["capabilities"]` set during lifespan.

---

### `GET /health`

Simple liveness probe.

**Response:**
```json
{
  "status": "ok",
  "worker_id": "my-worker"
}
```

Always returns HTTP 200 while the process is running.

---

### `POST /infer`

Dispatches an inference request to the appropriate backend.

**Request body (`InferRequest`):**

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | `str` | ✓ | Model identifier (e.g., `"llama3"`, `"gpt-4o"`) |
| `provider` | `str` | ✓ | Backend selector: `"ollama"`, `"openai"`, `"claude"`, `"gemini"`, `"cli"` |
| `messages` | `List[Dict]` | ✓ | Chat messages in `[{"role": "...", "content": "..."}]` format |
| `params` | `Dict` | — | Provider-specific generation parameters (e.g., `temperature`, `max_tokens`) |
| `gpu_id` | `str` | — | Reserved; not currently used by any backend |
| `command` | `str` | — | Shell command for `cli` provider; falls back to `model` if absent |

**Response (all backends normalised to):**
```json
{
  "output": "<generated text>",
  "usage": { ... },
  "finish_reason": "stop"
}
```

`finish_reason` is omitted when the backend does not supply one.

**Error responses:**
- `400` — unsupported `provider` value.
- `500` — backend raised an exception; detail contains the error message.

**In-flight tracking:** `app.state.inference_inflight` is incremented before dispatch and decremented in a `finally` block, ensuring the counter stays accurate even on errors.

---

### `GET /metrics`

Prometheus text-format metrics endpoint (not included in OpenAPI schema).

Exposes:

| Metric | Type | Labels |
|---|---|---|
| `nexus_worker_agent_http_requests_total` | Counter | `method`, `path`, `status` |
| `nexus_worker_agent_http_errors_total` | Counter | `method`, `path`, `status` |
| `nexus_worker_agent_http_request_duration_seconds` | Histogram | `method`, `path` |
| `nexus_worker_agent_inference_inflight` | Gauge | — |

Histogram buckets: `0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5` seconds.

---

## Backend Selection

The `POST /infer` handler selects a backend based on `body.provider`:

| `provider` value | Backend module | Credential source |
|---|---|---|
| `"ollama"` | `ollama_backend` | `worker_config["ollama_host"]` (default `http://localhost:11434`) |
| `"openai"` | `openai_backend` | `OPENAI_API_KEY` env var |
| `"claude"` | `claude_backend` | `ANTHROPIC_API_KEY` env var |
| `"gemini"` | `gemini_backend` | `GEMINI_API_KEY` env var |
| `"cli"` | `cli_backend` | n/a (runs a shell command) |
| anything else | — | Returns HTTP 400 |

---

## Registration and Deregistration Flow

### Registration

```
startup
  └─► POST {CONTROL_PLANE_URL}/v1/workers
        body: full worker_config YAML as JSON
        header: X-Nexus-API-Key: <CONTROL_PLANE_API_TOKEN>
        timeout: 10 s
```

### Heartbeat

```
loop (every HEARTBEAT_INTERVAL seconds)
  └─► POST {CONTROL_PLANE_URL}/v1/workers/{worker_id}/heartbeat
        body: {"metrics": {"queue_depth": N, "gpu_utilization": [...]}}
        header: X-Nexus-API-Key: <CONTROL_PLANE_API_TOKEN>
        timeout: 5 s
```

### Deregistration

There is **no explicit deregistration call** on shutdown. The control plane is expected to detect worker unavailability via missed heartbeats.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WORKER_CONFIG_PATH` | `config/workers/local_worker.yaml` | Path to the worker YAML config file |
| `CONTROL_PLANE_URL` | `http://localhost:8000` | Base URL of the NexusAI control plane |
| `HEARTBEAT_INTERVAL` | `15` | Seconds between heartbeat POSTs |
| `CONTROL_PLANE_API_TOKEN` | *(empty)* | API token sent as `X-Nexus-API-Key`; omitted from headers when empty |
| `OPENAI_API_KEY` | *(empty)* | OpenAI API key for the `openai` provider |
| `ANTHROPIC_API_KEY` | *(empty)* | Anthropic API key for the `claude` provider |
| `GEMINI_API_KEY` | *(empty)* | Google Gemini API key for the `gemini` provider |

---

## Known Issues

- **No explicit deregistration** — the worker does not notify the control plane when it shuts down gracefully; the control plane must rely on heartbeat timeouts to detect worker loss.
- **GPU utilisation approximation** — GPU load is reported as VRAM utilisation (`memory_used / memory_total`), not compute utilisation. A model fully loaded into VRAM with no active inference will still appear 100% utilised.
- **`gpu_id` field is unused** — `InferRequest.gpu_id` is accepted by the API but not forwarded to any backend; GPU affinity cannot currently be pinned per request.
- **No streaming support** — all backends request complete responses (`stream: false` for Ollama; blocking HTTP calls for others). Long-running inference will block the HTTP connection until completion.
- **CLI backend ignores `messages`** — `cli_backend` only receives `command` and `params`; the `messages` list is silently dropped.
