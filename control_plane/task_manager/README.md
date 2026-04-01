# Task Manager

The `TaskManager` is the central execution engine of NexusAI. It owns the lifecycle of every bot task: creation, scheduling, dependency resolution, retry logic, fan-out/join, result validation, policy enforcement, and artifact persistence. All state is backed by a SQLite database (default: `data/nexusai.db`).

The module is `control_plane/task_manager/task_manager.py` (~8600 lines). Supporting helpers live alongside it.

---

## Task Lifecycle

```
create_task()
     │
     ▼
  [queued]  ──────────────────────────────────────► [cancelled]
     │
     ▼ (dependencies resolved by DependencyEngine)
  [blocked] → (wait for depends_on tasks to complete)
     │
     ▼
  [running]  (dispatching to scheduler / waiting for inference)
     │
     ├──► [completed]  (result stored, triggers fired)
     │
     ├──► [failed]     (error stored, retry logic evaluated)
     │
     └──► [retried]    (re-queued with incremented params)
```

**Transitions**:
- `queued → blocked` — if `depends_on` task IDs are not yet complete
- `queued / blocked → running` — when all dependencies resolved and scheduler accepts
- `running → completed` — result passes output contract check
- `running → failed` — scheduler error, or output contract violation after retry budget
- `failed → retried` — auto-retry via `_is_retryable_error_message()` + `_prefers_truncation_retry()`
- any → `cancelled` — manual cancel via API

---

## SQLite Schema

### `cp_tasks`
| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID |
| `bot_id` | TEXT | Target bot |
| `payload` | TEXT | JSON-serialized payload |
| `metadata` | TEXT | JSON-serialized `TaskMetadata` |
| `depends_on` | TEXT | JSON list of task IDs |
| `status` | TEXT | Task lifecycle status |
| `result` | TEXT | JSON result from bot |
| `error` | TEXT | JSON error (`TaskError`) |
| `created_at` | TEXT | ISO 8601 UTC |
| `updated_at` | TEXT | ISO 8601 UTC |

### `cp_task_dependencies`
| Column | Type | Description |
|--------|------|-------------|
| `task_id` | TEXT | Dependent task |
| `depends_on_task_id` | TEXT | Prerequisite task |

### `cp_bot_runs`
| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID |
| `task_id` | TEXT UNIQUE | One run per task |
| `bot_id` | TEXT | Bot that ran |
| `status` | TEXT | `queued/running/completed/failed` |
| `payload` | TEXT | JSON payload sent |
| `metadata` | TEXT | JSON metadata |
| `result` | TEXT | JSON result |
| `error` | TEXT | JSON error |
| `triggered_by_task_id` | TEXT | Parent task if trigger-spawned |
| `trigger_rule_id` | TEXT | Trigger ID that spawned this run |
| `created_at` | TEXT | ISO 8601 UTC |
| `updated_at` | TEXT | ISO 8601 UTC |
| `started_at` | TEXT | ISO 8601 UTC |
| `completed_at` | TEXT | ISO 8601 UTC |

### `cp_bot_run_artifacts`
| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID |
| `run_id` | TEXT | Parent run |
| `task_id` | TEXT | Parent task |
| `bot_id` | TEXT | Bot that produced artifact |
| `kind` | TEXT | `payload/result/error/file/note` |
| `label` | TEXT | Human-readable label |
| `content` | TEXT | Text content (or NULL if file) |
| `path` | TEXT | Relative file path (if kind=file) |
| `metadata` | TEXT | JSON metadata |
| `created_at` | TEXT | ISO 8601 UTC |

---

## Key Functions

### `create_task(bot_id, payload, metadata, depends_on)`
Creates and immediately dispatches a task. Returns the `Task` object.
- Validates bot exists (raises `BotNotFoundError`)
- Persists to `cp_tasks`
- If `depends_on` is provided, creates `cp_task_dependencies` rows
- Calls `scheduler.dispatch(task, bot)` asynchronously

### `get_task(task_id)` / `list_tasks(...)`
- `list_tasks` supports filtering by `orchestration_id`, `statuses`, `bot_id`, `limit`

### `retry_task(task_id, payload_override)`
- Creates a new task linked to the original via `metadata.original_task_id` / `retry_of_task_id`
- Marks the original task as `retried`
- Increments `metadata.retry_attempt` on the new task

### `update_task_result(task_id, result, status)`
- Validates output contract if bot has `required_output_fields`
- Fires `BotWorkflowTrigger` rules on the bot's workflow
- Stores `BotRunArtifact` rows for file candidates extracted from result

---

## Template Rendering (`{{...}}` Syntax)

Payload templates in `BotWorkflowTrigger.payload_template` are rendered using `_transform_template_value()`:

| Expression | Result |
|---|---|
| `{{field_name}}` | Value of `payload.field_name` |
| `{{payload.a.b.c}}` | Nested dot-path lookup |
| `{{json:field}}` | Parse field as JSON |
| `{{render:field}}` | Recursively render template stored at field |
| `{{coalesce:a, b, 'default'}}` | First non-empty of a, b, or literal `'default'` |
| `{{camelize:field}}` | camelCase keys of a JSON object at field |
| `{{item}}` / `{{item_json}}` | Loop variable in fan-out templates |
| `{{item_index}}` | Loop index in fan-out templates |
| `'literal string'` | Static string literal |
| `null` / `true` / `false` | JSON primitives |

---

## Output Contract Validation

If a bot's `routing_rules.output_contract` is enabled:
- `format: json_object` → result must be a JSON object
- `format: json_array` → result must be a JSON array
- `required_fields: [...]` → all fields must be present
- `non_empty_fields: [...]` → all fields must be non-null/empty

Contract violations are retryable (up to `task_retry_max_tokens_increment` x `retry_attempt` increments).

---

## Retry Logic

Auto-retry triggers when:
1. `_is_retryable_error_message(error.message)` is true (timeouts, rate limits, JSON parse errors, contract failures, gateway errors), AND
2. `_prefers_truncation_retry(task)` is true (`source == "chat_assign"` or `source == "auto_retry"` or has `orchestration_id`).

On each retry:
- `max_tokens` incremented by `task_retry_max_tokens_increment` (default: 2048) × `retry_attempt`
- `num_ctx` or `num_width` incremented by `task_retry_num_width_increment` (default: 2048) × `retry_attempt`

These increments are applied in `scheduler.py`'s `_backend_with_retry_params()`.

---

## Truncation Detection

`_looks_like_truncated_result(result)` returns True if:
- `finish_reason` is in `{length, max_tokens, max_output_tokens, token_limit, max_new_tokens}`
- Output text ends with `...`, ` ``` `, `` ` ``, `:`, `,`, `(`, `[`, `{`, `|`

---

## Known Issues

- No maximum retry depth enforcement — a task can retry indefinitely if the error remains retryable.
- `_transform_template_value` is duplicated between this module and `scheduler.py` with diverging behaviour.
- Artifact file extraction (`extract_file_candidates`) is heuristic — may match false positives.
