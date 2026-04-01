# Scheduler

The `Scheduler` translates a `Task` into an inference request and dispatches it to the appropriate backend. It handles backend selection, system prompt injection, context reduction, message format normalisation, API key resolution, worker load balancing, and cloud context policy.

Source: `control_plane/scheduler/scheduler.py` (~2378 lines).

---

## Role in the System

```
TaskManager._run_task(task)
        │
        ▼
  Scheduler.schedule(task)
        │
        ├─ bot_registry.get(task.bot_id)          # Load bot config
        ├─ _apply_input_transform(bot, payload)    # Optional payload rewrite
        ├─ iterate bot.backends (in order):
        │       ├─ _backend_with_retry_params()    # Escalate params on retry
        │       ├─ _prepare_payload_for_backend()  # Inject system prompt, reduce context
        │       └─ _dispatch_backend()             # Route to worker or cloud API
        └─ return result  (or raise NoViableBackendError)
```

---

## Backend Selection

Backends are stored in `bot.backends` (a list). They are evaluated in order; the first viable backend wins. On failure the scheduler logs a warning and tries the next backend. If all fail, `NoViableBackendError` is raised with a combined failure message.

| `backend.type` | Routing |
|---|---|
| `local_llm` | HTTP POST to `http://{worker.host}:{worker.port}/infer`; requires worker with matching provider/model and `status == "online"` |
| `remote_llm` | Same as `local_llm` — points to a remote worker agent |
| `cloud_api` | Direct HTTPS call to provider API (OpenAI, Ollama Cloud, Claude, Gemini) |
| `cli` | Dispatches to a named worker; `backend.worker_id` is required |
| `custom` | Currently supports `provider: http_connection` only — executes HTTP connection actions |

### Worker Selection (local_llm / remote_llm)

When `backend.worker_id` is set, that specific worker is used (or `BackendError` if not found / no capacity).

When no `worker_id` is set, all online workers with matching capability (`type: llm`, `provider`, `model`) are candidates. The lowest-scoring worker is chosen via `_score_worker`:

```
score = (queue_depth × 5.0)
      + (inflight × 4.0)
      + (load / 20.0)
      + (gpu_avg / 25.0)
      + (latency_ema_ms / 500.0)
```

Inflight counts and latency EMA (`alpha = 0.30`, configurable via `NEXUSAI_WORKER_LATENCY_EMA_ALPHA`) are maintained in-memory per worker.

**Capacity limit**: `local_llm` workers are capped at 1 concurrent task. All other backend types are uncapped.

---

## Timeout Configuration

| Timeout | Value | Source |
|---|---|---|
| Worker connect | 10 s | `_worker_timeout()` — hardcoded |
| Worker read | None (unlimited) | `_worker_timeout()` — hardcoded |
| Worker write | 120 s | `_worker_timeout()` — hardcoded |
| Worker pool | 30 s | `_worker_timeout()` — hardcoded |
| Cloud API | 900 s | `_cloud_timeout()` — env `NEXUSAI_CLOUD_API_TIMEOUT_SECONDS` or setting `cloud_backend_timeout_seconds` |

---

## Context Reduction Constants

When a payload is large and looks like a join/research context, `_reduce_payload_for_context_limits` trims it before dispatch.

| Constant | Value | Purpose |
|---|---|---|
| `_PAYLOAD_CONTEXT_REDUCTION_TARGET_CHARS` | 48,000 | Trigger threshold: reduce only if serialised payload exceeds this |
| `_ASSIGNMENT_TRANSCRIPT_REDUCTION_CHARS` | 6,000 | Max chars for `assignment_scope.conversation_transcript` |
| `_ARTIFACT_CONTENT_REDUCTION_CHARS` | 1,800 | Max chars per artifact `content` field |
| `_LONG_STRING_REDUCTION_CHARS` | 1,200 | Max chars for other long string fields in artifacts |
| `_JOIN_RESULT_LIST_MAX_ITEMS` | 12 | Max items retained in `join_results` and similar branch lists |

Reduction is applied only if the payload looks like a join context (`_looks_like_join_context_payload`): `join_count > 1`, or multi-item `research_payloads` / `join_results` / `upstream_artifacts`.

### Transcript Reduction

`_reduce_assignment_transcript_for_context` keeps a head (4 lines) and tail (4 lines) of the transcript. Middle lines are scored by:
- User/assistant prefix boosts
- Keyword matches against the assignment scope (`focus_topics`, `constraint_hints`)
- Priority terms (action verbs, scope words)

The highest-scoring middle lines fill the remaining character budget, and an ellipsis line records the number omitted.

### Artifact Reduction

`_reduce_artifact_entry_for_context` truncates `content` to `_ARTIFACT_CONTENT_REDUCTION_CHARS` using a 65%/35% head/tail split with an omission notice. All other string fields are capped at 400 chars.

---

## Payload Normalisation and Message Building

`_payload_to_messages(payload)` converts any payload shape to a chat message list:

| Input type | Output |
|---|---|
| `list[{role, content}]` | Passed through; multipart content items normalised |
| `dict` | `[{role: "user", content: json.dumps(payload)}]` |
| `str` | `[{role: "user", content: str}]` |

### Provider-Specific Normalisation

Before dispatch, messages are normalised to each provider's expected format:

| Provider | Function | Notable behaviour |
|---|---|---|
| OpenAI | `_messages_for_openai` | Single-text content flattened; image_url parts preserved |
| Ollama | `_messages_for_ollama` | Multi-part text joined with `\n\n`; base64 images extracted |
| Claude | `_claude_payload_messages` | System messages extracted into top-level `system` param; role `user` or `assistant` only |
| Gemini | `_gemini_contents` | Roles mapped to `user` / `model`; inline image data as `inline_data` |

---

## System Prompt Injection

`_prepare_system_prompt(bot, bot_id, payload, task)` assembles the full system prompt:

```
base (bot.system_prompt)
  + _contract_prompt_suffix(bot)              # output contract format guidance
  + _repo_output_policy_prompt_suffix(bot, payload)  # repo output allow/deny policy
  + _assignment_scope_prompt_suffix(payload)  # scope lock, docs-only, constraint hints
  + _pm_database_contract_prompt_suffix(bot_id, payload)  # SQL migration contract (pm-database-engineer only)
  + _connection_context_prompt_suffix(bot_id, bot, payload)  # attached connection schemas + live fetch
  + _retry_prompt_suffix(task)                # retry guidance including error message
```

`_inject_system_prompt(prompt, payload)` prepends the system message to the message list. If an identical system message already exists, it is a no-op. If a shorter subset system message exists, it is replaced.

### Assignment Scope Suffix

`_assignment_scope_prompt_suffix(payload)` walks the `source_payload` chain (up to depth 8) to find `assignment_scope`. It injects:
- Scope constraints (forbidden patterns, required patterns, `avoid_external_apis`)
- Docs-only instructions when `docs_only: true`
- `ui_test_mode: build_only` guidance for UI tester without interactive access
- Transcript excerpt when `conversation_transcript` is present

### Database Contract Suffix

`_pm_database_contract_prompt_suffix` fires only when `bot_id == "pm-database-engineer"` (or equivalent). It mandates exactly one canonical `.sql` migration artifact and forbids destructive SQL.

---

## API Key Resolution

`_resolve_api_key(api_key_ref, default_env_var)`:

1. If `key_vault` is set and `api_key_ref` is non-empty → `key_vault.get_secret(api_key_ref)`
2. Fallback: `os.environ.get(api_key_ref)`
3. Final fallback: `os.environ.get(default_env_var)`

Default env var names per provider:

| Provider | `default_env_var` | Typical `api_key_ref` |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `OPENAI_API_KEY` |
| Ollama Cloud | `OLLAMA_API_KEY` | `OLLAMA_API_KEY` |
| Claude | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY` | `GEMINI_API_KEY` |

`BackendConfig.api_key_ref` overrides the default env var name. This allows multiple API keys (e.g., two OpenAI accounts) to be stored in the vault under different names.

---

## Retry Param Escalation (`_backend_with_retry_params`)

On retry (when `task.metadata.retry_attempt > 0`), backend params are escalated:

```python
max_tokens += task_retry_max_tokens_increment (default 2048) × retry_attempt
num_ctx     += task_retry_num_width_increment  (default 2048) × retry_attempt
num_width   += task_retry_num_width_increment  (default 2048) × retry_attempt
```

Baseline values used when the param is not set: `max_tokens = 1024`, `num_ctx = 8192`.

---

## Ollama-Specific Options (`_ollama_options`)

- Converts `max_tokens` → `num_predict`
- If `num_predict` is absent, defaults to `-1` (unlimited) — avoids Ollama's built-in 128-token cap
- Default can be overridden via the `default_ollama_num_predict` setting

---

## Cloud Context Policy

For `cloud_api` backends, `_apply_cloud_context_policy` can restrict payloads that contain a `Context:\n...` system message:

| Policy | Behaviour |
|---|---|
| `allow` | Payload sent unchanged (default) |
| `redact` | `Context:\n` system message replaced with `[REDACTED_BY_POLICY]` |
| `block` | Raises `BackendError`; next backend in list is tried |

Policy is resolved via `_resolve_cloud_context_policy`:
1. `NEXUSAI_CLOUD_CONTEXT_POLICY` env var (global default)
2. `project.settings_overrides.cloud_context_policy.provider_policies.{provider}` (per-provider override)
3. `...bot_overrides.{bot_id}.{provider}` (per-bot override)

---

## Connection Context Injection

When a bot has attached connections and `routing_rules.connection_context` is configured:

1. `_load_attached_connection_rows(bot_id)` queries `dashboard.db` for `BotConnection → Connection` rows (runtime import from `dashboard` package)
2. `_static_connection_context_prompt` renders connection name, kind, base URL, available OpenAPI actions, and schema into the system prompt
3. `_dynamic_connection_fetch_prompt` optionally fetches live data from the HTTP connection and injects the response JSON

---

## Custom Backend: `http_connection`

When `backend.type == "custom"` and `backend.provider == "http_connection"`, the scheduler executes one or more HTTP actions against a bot-attached connection:
- Actions are listed under `payload.connection_actions` or `payload.connection_action`
- Method must be GET/HEAD/OPTIONS unless `payload.continue_on_error = true` is set
- Results include `import_status`, `completed_actions`, `failed_actions`, `action_results`

---

## `Scheduler.stream(task)`

Streaming variant of `schedule`. Yields SSE-style events:
```
{"event": "backend_selected", "provider": ..., "model": ..., "worker_id": ...}
{"event": "dispatch_started", "worker_id": ..., "host": ..., "port": ..., ...}
{"event": "token", ...}          # from worker SSE stream
{"event": "final", ...}          # last event from cloud_api (non-streaming cloud calls)
```

Cloud API backends do not support native streaming — the `_dispatch_backend` call is awaited and wrapped in a single `final` event.

---

## Worker Runtime Metrics

`get_worker_runtime_metrics()` returns:
```python
{
    "worker_id": {
        "inflight": float,          # current in-flight request count
        "latency_ema_ms": float,    # exponential moving average latency
    }
}
```

---

## Known Issues / Refactor Notes

1. **`dashboard` package runtime import** — `_load_attached_connection_rows`, `_dynamic_connection_fetch_prompt`, and `_run_http_connection_backend_sync` all import from `dashboard.db` and `dashboard.models` at runtime. This creates a cross-package dependency from `control_plane` to `dashboard`. If the dashboard package is absent, these silently fall back to empty results; errors are swallowed.

2. **Ollama Cloud base URL** — `_call_ollama_cloud` defaults to `https://ollama.com/api`. This is almost certainly wrong for production Ollama Cloud deployments. The correct URL should come from configuration, not a hardcoded constant.

3. **Streaming and non-streaming asymmetry** — `stream()` yields a `backend_selected` event before the worker call, but the non-streaming `schedule()` has no equivalent event. Callers cannot distinguish a successfully selected backend from a request that never reached the backend.

4. **`_transform_template_value` and `_lookup_payload_path` are defined in both `task_manager.py` and `scheduler.py`** (via `scheduler.py` importing from a shared helper). The two copies have diverged slightly in edge-case handling, particularly around empty-string vs. null handling in `coalesce`.

5. **Cloud context policy block raises `BackendError`**, which causes the scheduler to try the next backend in the list rather than propagating the policy rejection immediately. This means a policy `block` on one backend may silently allow the request through on the next backend if it has a different provider.

6. **`_compact_text_with_edges` head/tail split uses a 65%/35% ratio** hardcoded. For long artifacts, the tail (35%) may cut off important ending content such as JSON closing braces, leaving unparseable fragments.
