# Worker Agent Backends

This directory contains the inference backend implementations for the NexusAI Worker Agent. Each backend is a thin async adapter that translates the worker agent's internal `(model, messages, params)` calling convention into the wire format expected by a specific AI provider.

---

## Abstract Base Class (`base.py`)

```python
class BaseBackend(ABC):
    @abstractmethod
    async def infer(self, model: str, messages: List[Dict], params: Dict) -> Dict[str, Any]:
        ...
```

`BaseBackend` defines the contract every backend must satisfy. The single abstract method `infer` receives:

| Argument | Type | Description |
|---|---|---|
| `model` | `str` | Provider-specific model name (e.g. `"llama3"`, `"gpt-4o"`) |
| `messages` | `List[Dict]` | Chat history in `[{"role": "...", "content": "..."}]` format |
| `params` | `Dict` | Provider-specific generation parameters |

> **Note:** The backend implementations in this directory are currently module-level functions rather than class instances, but they follow the same interface contract as `BaseBackend`.

### Normalised Response Format

Every backend returns a `dict` with the following keys:

| Key | Type | Always present | Description |
|---|---|---|---|
| `output` | `str` | ✓ | Generated text |
| `usage` | `dict` | ✓ | Token counts (structure varies by provider) |
| `finish_reason` | `str` | Only when non-empty | Why generation stopped |

---

## Backends

### Ollama (`ollama_backend.py`)

Runs inference against a **locally hosted** [Ollama](https://ollama.com) server.

**Configuration:**
- `host` — Ollama server URL. Read from `worker_config["ollama_host"]` in the infer router; defaults to `http://localhost:11434`.
- `params` — passed as Ollama's `options` object (e.g., `temperature`, `num_predict`).

**How inference works:**

1. Constructs an Ollama `/api/chat` request body:
   ```json
   {
     "model": "<model>",
     "messages": [...],
     "stream": false,
     "options": { ...params }
   }
   ```
2. POSTs to `{host}/api/chat` with a 120-second timeout.
3. Extracts `message.content` as `output`.
4. Maps Ollama's token fields to the normalised usage dict:
   - `prompt_eval_count` → `prompt_tokens`
   - `eval_count` → `completion_tokens`
5. Sets `finish_reason` from `done_reason` or `finish_reason` in the response (whichever is present).

**Special behaviour:**
- Streaming is explicitly disabled (`"stream": false`); the call blocks until the full response is returned.
- No API key required; Ollama is expected to run on localhost or a trusted network.

---

### OpenAI (`openai_backend.py`)

Calls the **OpenAI Chat Completions API**.

**Configuration:**
- `api_key` — read from the `OPENAI_API_KEY` environment variable.
- `params` — merged directly into the request body via `body.update(params)`, so any OpenAI parameter (`temperature`, `max_tokens`, `top_p`, etc.) can be passed through.

**How inference works:**

1. Constructs request body:
   ```json
   {
     "model": "<model>",
     "messages": [...],
     ...params
   }
   ```
2. POSTs to `https://api.openai.com/v1/chat/completions` with `Authorization: Bearer <api_key>` and a 120-second timeout.
3. Extracts `choices[0].message.content` as `output`.
4. Returns the `usage` object from the response as-is (OpenAI already uses `prompt_tokens` / `completion_tokens` / `total_tokens`).
5. Sets `finish_reason` from `choices[0].finish_reason`.

**Request/Response:**
```
POST https://api.openai.com/v1/chat/completions
Authorization: Bearer <OPENAI_API_KEY>

→ { output: "...", usage: { prompt_tokens, completion_tokens, total_tokens }, finish_reason: "stop" }
```

---

### Gemini (`gemini_backend.py`)

Calls the **Google Gemini** (`generativelanguage.googleapis.com`) API.

**Configuration:**
- `api_key` — read from the `GEMINI_API_KEY` environment variable; sent as the `x-goog-api-key` HTTP header.
- `params` — forwarded as `generationConfig` in the request body (e.g., `temperature`, `maxOutputTokens`).

**How inference works:**

1. Flattens all messages into a single Gemini `contents` structure:
   ```json
   {
     "contents": [
       { "parts": [{"text": "..."}, {"text": "..."}, ...] }
     ],
     "generationConfig": { ...params }
   }
   ```
   - Role information is **not** forwarded; all message `content` fields are concatenated as text parts.
   - `generationConfig` is omitted when `params` is empty.
2. POSTs to `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with a 120-second timeout.
3. Extracts `candidates[0].content.parts[0].text` as `output`.
4. Returns `usageMetadata` from the response as the `usage` dict (field names are Gemini-specific, e.g. `promptTokenCount`, `candidatesTokenCount`).
5. Sets `finish_reason` from `candidates[0].finishReason`.

**Special behaviour / Limitations:**
- Multi-turn role-based conversations lose their role structure; all message content is merged into a single turn.

---

### Claude (`claude_backend.py`)

Calls the **Anthropic Claude Messages API**.

**Configuration:**
- `api_key` — read from the `ANTHROPIC_API_KEY` environment variable.
- `params` — merged into the request body; `max_tokens` is extracted separately with a default of `1024`.

**How inference works:**

1. Extracts `max_tokens` from `params` (default `1024`); remaining params are merged into the body:
   ```json
   {
     "model": "<model>",
     "max_tokens": 1024,
     "messages": [...],
     ...other_params
   }
   ```
2. POSTs to `https://api.anthropic.com/v1/messages` with headers:
   - `x-api-key: <api_key>`
   - `anthropic-version: 2023-06-01`
   - `content-type: application/json`
   - Timeout: 120 seconds.
3. Extracts `content[0].text` as `output`.
4. Returns the `usage` object from the response as-is (Anthropic uses `input_tokens` / `output_tokens`).
5. Sets `finish_reason` from `stop_reason`.

**Special behaviour:**
- `max_tokens` is a **required** field in the Anthropic API. The backend always supplies it, defaulting to `1024` if not provided in `params`.
- The API version is hardcoded to `2023-06-01`.

---

### CLI (`cli_backend.py`)

Runs an arbitrary **shell command** as a subprocess and returns its output.

**Configuration:**
- `command` — the shell command string. Taken from `InferRequest.command` if set; falls back to `InferRequest.model`.
- `params` — accepted but not used.

**How inference works:**

1. Uses `asyncio.create_subprocess_shell` to spawn `command` with `stdout` and `stderr` piped.
2. Awaits `proc.communicate()` for the process to finish.
3. Returns:
   ```json
   {
     "output": "<stdout decoded as utf-8>",
     "stderr": "<stderr decoded as utf-8>",
     "returncode": 0,
     "usage": {}
   }
   ```
   Decoding uses `errors="replace"` so malformed bytes do not raise exceptions.

**Special behaviour / Limitations:**
- `messages` is silently ignored; only `command` matters.
- No timeout is enforced — a hung subprocess will block the worker indefinitely.
- `usage` is always an empty dict.
- This backend is primarily intended for scripted or tool-calling workflows, not conversational AI.

---

## Backend Selection Logic

Backend selection is performed entirely in `api/infer.py` based on `InferRequest.provider`:

```python
if body.provider == "ollama":   → ollama_backend.infer(...)
elif body.provider == "openai": → openai_backend.infer(...)
elif body.provider == "claude": → claude_backend.infer(...)
elif body.provider == "gemini": → gemini_backend.infer(...)
elif body.provider == "cli":    → cli_backend.infer(...)
else:                           → HTTP 400
```

The `provider` value is set by the **control plane scheduler** when it dispatches a task to a worker. The scheduler resolves which worker supports a given backend by inspecting each worker's `capabilities` list (as registered via `POST /v1/workers`). Workers with lower `queue_depth` and `gpu_utilization` are preferred for unpinned tasks.

---

## Token Normalisation Across Backends

Token field names differ across providers. The worker agent does **not** normalise them into a common schema — the raw `usage` object from each provider is passed through:

| Backend | `usage` keys |
|---|---|
| Ollama | `prompt_tokens`, `completion_tokens` (normalised by the backend) |
| OpenAI | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| Claude | `input_tokens`, `output_tokens` |
| Gemini | `promptTokenCount`, `candidatesTokenCount`, `totalTokenCount` (from `usageMetadata`) |
| CLI | `{}` (always empty) |

Callers that need to aggregate token counts across providers must handle these differences themselves.

---

## Known Issues

- **No streaming** — all backends use blocking HTTP calls and return the complete response. There is no Server-Sent Events or chunked-transfer streaming support.
- **Gemini role loss** — the Gemini backend flattens all messages into a single `contents` turn, discarding `role` metadata. This breaks multi-turn conversation context for models that rely on alternating `user`/`model` roles.
- **CLI has no timeout** — a subprocess that never terminates will block the worker indefinitely, consuming a slot in `inference_inflight`.
- **`gpu_id` is ignored** — the `InferRequest.gpu_id` field is not forwarded to any backend; per-request GPU pinning is not implemented.
- **Anthropic API version is hardcoded** — `claude_backend` always sends `anthropic-version: 2023-06-01`. Newer API features requiring a later version header are inaccessible without code changes.
- **OpenAI `params` collision** — because `params` is merged directly into the OpenAI request body with `body.update(params)`, a caller could accidentally overwrite `model` or `messages` by including those keys in `params`.
