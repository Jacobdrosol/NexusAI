# Scheduler

The scheduler selects a backend for a task, builds the message list, injects system prompts and output contracts, reduces context to fit model limits, and dispatches the inference request to a worker or cloud API.

---

## Role

The scheduler sits between the task manager and the actual inference backends. It:
1. Resolves the bot's backends in priority order (first enabled backend with an online worker)
2. Builds the full message list from the task payload
3. Injects system prompt, output contract hints, scope suffixes
4. Reduces oversized payloads/transcripts to fit token budgets
5. Dispatches via HTTP to worker agents or cloud APIs directly
6. Normalises token usage from different providers into a uniform dict

---

## Backend Selection

Backends are evaluated in the order listed in `bot.backends`. The first viable backend is chosen:
- **`local_llm`**: requires a worker with matching `worker_id` and `status == "online"`
- **`remote_llm`**: same as local_llm (points to a separate remote worker agent)
- **`cloud_api`**: calls cloud provider directly (OpenAI, Claude, Gemini); resolves `api_key_ref` from `KeyVault`
- **`cli`**: runs a local CLI command
- **`custom`**: custom handler

If all backends fail, `NoViableBackendError` is raised.

---

## Timeouts

| Timeout | Value | Config |
|---------|-------|--------|
| Worker connect | 10s | hardcoded |
| Worker read | None (unlimited) | hardcoded |
| Worker write | 120s | hardcoded |
| Worker pool | 30s | hardcoded |
| Cloud API | 900s | `NEXUSAI_CLOUD_API_TIMEOUT_SECONDS` env or `cloud_backend_timeout_seconds` setting |

---

## Context Reduction

To prevent oversized payloads from exceeding model context windows, several reduction strategies are applied:

| Constant | Value | Purpose |
|---|---|---|
| `_PAYLOAD_CONTEXT_REDUCTION_TARGET_CHARS` | 48,000 | Max chars for full payload before reduction |
| `_ASSIGNMENT_TRANSCRIPT_REDUCTION_CHARS` | 6,000 | Max chars for conversation transcript |
| `_ARTIFACT_CONTENT_REDUCTION_CHARS` | 1,800 | Max chars per artifact content field |
| `_LONG_STRING_REDUCTION_CHARS` | 1,200 | Max chars for other long string fields |
| `_JOIN_RESULT_LIST_MAX_ITEMS` | 12 | Max items in join branch result lists |

**Transcript reduction** (`_reduce_assignment_transcript_for_context`):
- Keeps head (4 lines) and tail (4 lines) of the transcript
- Scores middle lines by relevance (User/assistant prefix, keywords matching scope, priority terms)
- Inserts an ellipsis line showing how many lines were omitted

**Artifact reduction** (`_reduce_artifact_entry_for_context`):
- Truncates `content` to 1,800 chars using head (1,100) + tail (400) with an omission notice
- Truncates all other string fields to 400 chars

**Result summarisation** (`_summarize_result_dict_for_context`):
- Retains only preferred keys: `status`, `outcome`, `failure_type`, `summary`, `findings`, `evidence`, `implementation_plan`, `implementation_workstreams`, `artifacts`, `handoff_notes`, etc.

---

## Payload → Messages Normalisation

`_payload_to_messages(payload)` converts any payload shape to a chat message list:

| Input type | Output |
|---|---|
| `list[{role, content}]` | Normalised message list |
| `dict` | `[{role: "user", content: json.dumps(payload)}]` |
| `str` | `[{role: "user", content: str}]` |

---

## System Prompt Injection

`_inject_system_prompt(prompt, payload)`:
- If the payload already has a system message with identical content → no-op
- If the payload has a system message that is a substring of the new prompt → replaces it
- Otherwise prepends `{role: "system", content: prompt}` to the message list

Three prompt suffixes are appended to the system prompt:
1. **`_assignment_scope_prompt_suffix`**: scope lock, docs-only instructions, constraint hints, excluded stages, ui_test_mode, transcript
2. **`_pm_database_contract_prompt_suffix`**: SQL constraints for `pm-database-engineer` only
3. **`_contract_prompt_suffix`**: output contract format instructions (JSON shape, required fields, example)

---

## Ollama-Specific Handling

`_ollama_options(params)`:
- Converts `max_tokens` → `num_predict`
- If `num_predict` is not set, defaults to `-1` (unlimited) to avoid Ollama's built-in 128-token cap
- Override via `default_ollama_num_predict` setting

---

## Token Usage Normalisation

Provider field names vary; the scheduler normalises to:
- `prompt_tokens` — from OpenAI `prompt_tokens`, Claude `input_tokens`, Gemini `promptTokenCount`, Ollama `prompt_eval_count`
- `completion_tokens` — from OpenAI `completion_tokens`, Claude `output_tokens`, Gemini `candidatesTokenCount`, Ollama `eval_count`
- `total_tokens` — computed if absent

---

## Retry Param Escalation

On retry (when `task.metadata.retry_attempt > 0`), `_backend_with_retry_params()` escalates:
- `max_tokens` += `task_retry_max_tokens_increment` (default 2048) × `retry_attempt`
- `num_ctx` or `num_width` += `task_retry_num_width_increment` (default 2048) × `retry_attempt`

Fallback values if the param is not in the backend config: `max_tokens=1024`, `num_ctx=8192`.

---

## Connection Context Injection

When a bot has attached connections (via `routing_rules.connection_context`), the scheduler:
1. Queries `dashboard.db` for `BotConnection` → `Connection` rows (runtime import — known issue)
2. Fetches connection schemas and optionally live JSON from the connection endpoint
3. Injects the schema as authoring context into the payload

---

## Known Issues

- Scheduler imports `dashboard.db` and `dashboard.models` at runtime. This creates a cross-package dependency from `control_plane` to `dashboard`. If the dashboard package is absent, it silently falls back to an empty connection list.
- `_transform_template_value` and `_lookup_payload_path` are duplicated from `task_manager.py` with slightly different internal helpers.
- The `_compact_text_with_edges` head/tail split uses a 65/35 ratio, which may cut important context from the middle of large payloads.
