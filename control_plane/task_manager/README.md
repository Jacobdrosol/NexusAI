# Task Manager

The `TaskManager` is the central execution engine of NexusAI. It owns the lifecycle of every bot task: creation, scheduling, dependency resolution, retry logic, fan-out/join, result validation, policy enforcement, and artifact persistence. All state is backed by a SQLite database (default: `data/nexusai.db`).

Source: `control_plane/task_manager/task_manager.py` (~8600 lines).

---

## Task Lifecycle State Machine

```
create_task()
     │
     ▼
  [queued]  ─────────────────────────────────► [cancelled]
     │                                              ▲
     ▼ (has depends_on)                             │
  [blocked] → (waiting for dependency tasks)        │
     │                                              │
     ▼ (all deps complete)                          │
  [running]  (awaiting inference result)            │
     │                                              │
     ├──► [completed]  (result stored, triggers dispatched, artifacts recorded)
     │
     ├──► [failed]     (error stored; auto-retry evaluated)
     │         │
     │         └──► creates new task ──► [retried] (original frozen)
     │
     └──► [cancelled]  ─────────────────────────────┘
```

**Status meanings:**

| Status | Description |
|---|---|
| `queued` | Ready to run; no unresolved dependencies |
| `blocked` | Waiting for one or more dependency tasks to reach a terminal state |
| `running` | Dispatcher has picked it up; awaiting scheduler response |
| `completed` | Scheduler returned a result that passed all contract and policy checks |
| `failed` | Unrecoverable error or retry budget exhausted |
| `retried` | Superseded by a new retry task; original task is frozen |
| `cancelled` | Explicitly cancelled by operator or system |

Terminal statuses (no further transitions): `completed`, `failed`, `cancelled`, `retried`.

Constant: `TaskManager._TERMINAL_TASK_STATUSES = {"completed", "failed", "retried", "cancelled"}`

---

## SQLite Schema

Database path: `DATABASE_URL` env var (`sqlite:///...`) → `data/nexusai.db` fallback.

### `cp_tasks`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `bot_id` | TEXT | Bot assigned to this task |
| `payload` | TEXT | JSON-serialised task payload |
| `metadata` | TEXT | JSON-serialised `TaskMetadata` |
| `depends_on` | TEXT | JSON array of dependency task IDs (denormalised copy) |
| `status` | TEXT | queued / blocked / running / completed / failed / retried / cancelled |
| `result` | TEXT | JSON-serialised result from scheduler |
| `error` | TEXT | JSON-serialised `TaskError` |
| `created_at` | TEXT | ISO-8601 UTC timestamp |
| `updated_at` | TEXT | ISO-8601 UTC timestamp |

### `cp_task_dependencies`

Normalised dependency store. The `cp_tasks.depends_on` column is a denormalised copy; both are kept in sync.

| Column | Type | Notes |
|---|---|---|
| `task_id` | TEXT PK (composite) | Child task |
| `depends_on_task_id` | TEXT PK (composite) | Parent task |

### `cp_bot_runs`

One row per task; surfaces the task for bot-run dashboards.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Always equal to `task_id` (see Known Issues) |
| `task_id` | TEXT UNIQUE NOT NULL | Foreign key to `cp_tasks.id` |
| `bot_id` | TEXT NOT NULL | |
| `status` | TEXT NOT NULL | Mirrors `cp_tasks.status` |
| `payload` | TEXT | |
| `metadata` | TEXT | |
| `result` | TEXT | |
| `error` | TEXT | |
| `triggered_by_task_id` | TEXT | Parent task that spawned this run via a trigger rule |
| `trigger_rule_id` | TEXT | The trigger rule ID that created this task |
| `created_at` | TEXT NOT NULL | |
| `updated_at` | TEXT NOT NULL | |
| `started_at` | TEXT | Set when status transitions to `running` |
| `completed_at` | TEXT | Set when status enters a terminal state |

### `cp_bot_run_artifacts`

Structured artifacts extracted from each task's result.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `{task_id}:{kind}` or `{task_id}:artifact:{idx}` |
| `run_id` | TEXT NOT NULL | Always equal to `task_id` |
| `task_id` | TEXT NOT NULL | |
| `bot_id` | TEXT NOT NULL | |
| `kind` | TEXT NOT NULL | `payload`, `result`, `note`, `error`, `file` |
| `label` | TEXT NOT NULL | Human-readable name |
| `content` | TEXT | Artifact body (JSON or markdown) |
| `path` | TEXT | Repo-relative file path (for `file` kind artifacts) |
| `metadata` | TEXT | JSON dict |
| `created_at` | TEXT NOT NULL | |

Standard artifacts recorded for every task: `payload` (task payload JSON), `report` (run report markdown), `result` (if non-null), `execution-report`, `usage` (if usage data present), `error` (if failed). Additional `file` artifacts are extracted from the model's output via explicit `artifacts` array and fenced code block scanning (`extract_file_candidates`).

---

## `TaskManager` Class

```python
class TaskManager:
    def __init__(
        self,
        scheduler: Any,
        db_path: Optional[str] = None,
        bot_registry: Optional[Any] = None,
        orchestration_workspace_store: Optional[Any] = None,
    ) -> None
```

`NEXUSAI_TASK_MAX_CONCURRENCY` (default `4`) controls how many tasks run in parallel globally. `local_llm` backends are capped at 1 concurrent task per worker.

The instance maintains an in-memory `_tasks: Dict[str, Task]` cache loaded from SQLite at startup. All mutations go through the cache first and are persisted asynchronously.

### Public Methods

#### `create_task`
```python
async def create_task(
    self,
    bot_id: str,
    payload: Any,
    metadata: Optional[TaskMetadata] = None,
    depends_on: Optional[List[str]] = None,
) -> Task
```
Creates and persists a new task. Initial status is `blocked` if `depends_on` is non-empty, otherwise `queued`. Immediately calls `_schedule_ready_tasks()` for queued tasks. Raises `TaskNotFoundError` if any dependency ID is unknown. Validates the payload against the bot's `input_contract` if configured.

#### `get_task`
```python
async def get_task(self, task_id: str) -> Task
```
Returns the in-memory task snapshot. Raises `TaskNotFoundError` if not found.

#### `list_tasks`
```python
async def list_tasks(
    self,
    orchestration_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    bot_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Task]
```
Filters the in-memory task map. Results are sorted by `updated_at DESC, created_at DESC`. Tasks in `_trigger_dispatch_pending` (terminal but not yet trigger-dispatched) are presented as `running` to callers to avoid premature "done" signals.

#### `retry_task`
```python
async def retry_task(self, task_id: str, payload_override: Any = None) -> Task
```
Creates a new task cloned from the original with `retry_attempt` incremented. Marks the original `retried`. Preserves original `workflow_root_task_id` through `metadata.original_task_id`. Used for manual retries from the operator UI.

#### `cancel_task`
```python
async def cancel_task(self, task_id: str) -> Task
```
Transitions a task to `cancelled`. If a runner asyncio task is active, it is also cancelled via `asyncio.Task.cancel()`.

#### `update_status`
```python
async def update_status(
    self,
    task_id: str,
    status: str,
    result: Any = _STATUS_UPDATE_UNSET,
    error: Any = _STATUS_UPDATE_UNSET,
) -> None
```
Internal method used by runners to persist status transitions. On terminal status triggers: dependency unblocking for downstream tasks, trigger rule dispatch, and artifact recording.

#### `list_bot_runs`
```python
async def list_bot_runs(self, bot_id: str, limit: int = 50) -> List[BotRun]
```
Queries `cp_bot_runs` directly from SQLite (not in-memory cache), ordered by `created_at DESC`.

#### `list_bot_run_artifacts`
```python
async def list_bot_run_artifacts(
    self,
    bot_id: str,
    limit: int = 100,
    task_id: Optional[str] = None,
    include_content: bool = True,
) -> List[BotRunArtifact]
```
Queries `cp_bot_run_artifacts` from SQLite. Pass `include_content=False` to return metadata only.

#### `get_bot_run_artifact`
```python
async def get_bot_run_artifact(self, bot_id: str, artifact_id: str) -> BotRunArtifact
```
Returns a single artifact by its composite ID. Raises `TaskNotFoundError` if not found.

#### `close`
```python
async def close(self) -> None
```
Cancels all in-flight runner tasks, retry tasks, and the watchdog task. Waits up to 2 seconds for graceful shutdown before force-cancelling.

---

## Internal Exceptions

| Exception | Purpose |
|---|---|
| `_TaskExecutionFailure(message, result=None)` | Raised when execution fails with a partial result worth preserving |
| `_TaskPolicyViolation(message, code, details, result=None)` | Raised when a workflow policy is violated; `code` is a stable machine-readable identifier; always includes `reason_code` in `details` |

---

## Template Rendering

Templates use `{{...}}` placeholders resolved against the task payload. Used in `output_contract.defaults_template`, `output_contract.template`, `input_transform.template`, and bot routing rules.

The core function is `_transform_template_value(template, payload, notes)`.

| Syntax | Behaviour |
|---|---|
| `{{field}}` | Resolve `field` from the payload using dot-path |
| `{{json:field}}` | Resolve `field` and JSON-serialise the value |
| `{{render:field}}` | Resolve `field`, then recursively render any nested templates in the result |
| `{{coalesce:a,b,c}}` | Return the first non-empty value among `a`, `b`, `c` (each is a field path) |
| `{{camelize:field}}` | Resolve `field` and convert the string to camelCase |

Field paths support dot-notation (`foo.bar.baz`). The entire template may be a dict or list; every string value is rendered recursively. `_lookup_nested_path` resolves the paths. Transformation notes are accumulated in a `notes: list[str]` parameter and surfaced in `normalization_notes` on the result dict.

---

## Retry Logic

### Automatic Retries

After a task fails, the runner evaluates `_is_retryable_error_message(message)` against an allowlist:
- Connection errors: `connecttimeout`, `connection reset`, `temporarily unavailable`, `read timeout`
- HTTP errors: `http 500/502/503/504`, `bad gateway`, `service unavailable`, `rate limit`, `too many requests`
- Output contract failures: `no valid json`, `output contract missing required fields`, `truncated at token limit`, `model output likely truncated`
- Documentation-specific: `broken internal markdown links`, `documentation workstream emitted markdown files outside its assigned deliverables`

Auto-retry is only applied if `_prefers_truncation_retry(task)` is true: task source is `chat_assign` or `auto_retry`, or the task has an `orchestration_id`.

On each retry `_backend_with_retry_params(backend, task)` in the scheduler escalates model parameters:
- `max_tokens` += `task_retry_max_tokens_increment` (default 2048) × `retry_attempt`
- `num_ctx` / `num_width` += `task_retry_num_width_increment` (default 2048) × `retry_attempt`

Maximum retries: `task_max_retries` setting (default 10).

A `_retry_prompt_suffix` is injected into the system prompt on retries, surfacing the previous error message and targeted guidance (e.g., broken link corrections).

### Orphan Recovery on Startup

Tasks found in `running` state on startup are recovered during `_ensure_db()`:
- `attempt_count < max_retries` → re-queued
- `attempt_count >= max_retries` → marked `failed` with code `ORPHANED`

### Running Task Watchdog

A background coroutine `_running_task_watchdog` polls every `running_task_watchdog_poll_seconds` (default 30s):
- Initial stall window: `running_task_watchdog_initial_stall_seconds` (default 600s)
- After a live runner is confirmed, issues a `progress_grace_seconds` (default 300s) extension
- Stalled tasks are retried via `_retry_stuck_task`

---

## Fan-Out and Join Mechanics

### Fan-Out

When `pm-engineer` completes, its result's `implementation_workstreams` array drives a fan-out. Trigger rules on the bot's workflow configuration define what tasks to spawn on completion. Each spawned task receives:
- `source_payload`: the parent task's full payload (including the parent's result)
- `workstream`: the specific workstream sub-dict scoped to that branch
- `fanout_branch_key`: branch identifier

### Join

When multiple upstream branches (e.g., parallel `pm-coder` tasks) all complete, a join task collects their results:
- `join_count`: number of branches
- `join_results`: list of result dicts from each branch (capped at `_JOIN_RESULT_LIST_MAX_ITEMS = 12` for context reduction)
- `join_task_ids`: list of source task IDs

The `DependencyEngine` (`control_plane/scheduler/dependency_engine.py`) manages the join gate.

### Research Phase Parallelism

The standard PM pack spawns three parallel `pm-research-analyst` tasks (lanes: `repo`, `data`, `online`) with `depends_on: []`. `pm-engineer` depends on all three.

---

## Step Kind Classification

Tasks carry a `step_kind` field used for output policy enforcement:

| `step_kind` | Meaning |
|---|---|
| `specification` | Research / requirements gathering — no repo file outputs |
| `planning` | Architecture / engineering plan — no repo file outputs |
| `repo_change` | Code implementation — repo file outputs mandatory |
| `test_execution` | Run automated tests; real command output required; no repo ownership |
| `review` | Security / quality review — findings only, no repo ownership |
| `release` | Release / merge — sanitized to `review` by `_sanitize_plan_for_operator_scope` |

Step kinds are inferred from `role_hint`, `title`, `instruction`, and `deliverables` if not explicitly set (`_infer_step_kind`). An explicit split of `test_execution` steps into a `create_tests` sub-step (repo_change) and an `execute_tests` sub-step (test_execution) is performed by `_expand_test_execution_steps`.

---

## Output Contract Validation

If a bot's `routing_rules.output_contract` is enabled:
- `format: json_object` → result must be a JSON object
- `format: json_array` → result must be a JSON array
- `required_fields: [...]` → all listed fields must be present
- `non_empty_fields: [...]` → all listed fields must be non-null/empty
- `mode: payload_transform` → result is derived by rendering `template` against the task payload
- `defaults_template` + `fallback_mode` → provides fallback values when model output is missing or unparseable

Docs-only workstream results are synthesised via `_synthesize_docs_only_repo_change_contract_result` when JSON parsing fails, recovering markdown artifacts.

---

## Scope Lock Enforcement

`assignment_scope.scope_lock` (dict, optional) can restrict which artifacts a task may produce:
- `allowed_artifacts`: list of glob patterns; non-matching paths raise `_TaskPolicyViolation` with code `scope_violation_not_allowed`
- `forbidden_keywords`: list of path substrings; matching paths raise `_TaskPolicyViolation` with code `scope_violation_forbidden`

These checks run in `_record_artifacts_for_task` before any artifact is written.

---

## Docs-Only Policy Enforcement

When a task's `assignment_scope.docs_only` is true (or inferred from deliverables/instructions), additional policy checks fire:
- Non-writer roles (`tester`, `qa`, `reviewer`, `security-reviewer`, `researcher`, `analyst`) cannot own any repo file outputs — `_non_writer_step_repo_deliverables`
- Specification/planning steps cannot emit non-documentation repo files — `_is_documentation_like_repo_file`
- `pm-database-engineer` must return exactly one canonical `.sql` artifact — `_database_result_contract_failure`
- Destructive SQL (`DELETE`, `DROP`, `TRUNCATE`, `ALTER TABLE DROP COLUMN`) is blocked — `_contains_destructive_sql`
- Broken internal markdown links in delivered `.md` files are detected and reported — `_docs_only_broken_markdown_links_from_artifacts`
- Placeholder content (`... full content omitted for brevity ...`) in markdown artifacts is rejected — `_docs_only_placeholder_markdown_artifacts`

---

## Known Issues / Refactor Notes

1. **`cp_bot_runs.id` is always equal to `task_id`** (line 3433–3434 sets both to `task.id`). The separate `id` PK is redundant. The dashboard reads by `task_id`, not `id`, so this causes no bugs but wastes schema space.

2. **`_database_result_contains_destructive_sql` has a redundant inner check** (lines 1819–1822): the loop already calls `_contains_destructive_sql(content)` for all items; the `.sql`-suffixed re-check on lines 1820–1822 is always covered by the preceding iteration.

3. **`cp_bot_run_artifacts` ON CONFLICT** does not update `run_id`, `task_id`, `bot_id`, or `kind` columns. If these diverge (unlikely), old values persist silently.

4. **In-memory task map grows unboundedly** — tasks are only ever added to `self._tasks`, never evicted. Long-running deployments accumulate all historical task objects in memory.

5. **`_task_result_is_skip` is referenced in `pm_orchestrator.py`** but defined at module scope in `task_manager.py`. The import chain makes it accessible but the dependency is implicit and fragile.

6. **`_expand_test_execution_steps` silently converts docs-only `test_execution` steps to `review`** with no log message or metadata flag, making unexpected step-kind mismatches hard to diagnose.

7. **`_is_assignment_execution_artifact_file` classifies `.txt` files as execution artifacts**, which can produce false negatives for `.txt` deliverables that are actually plain-text documentation.

8. **The `_looks_like_assignment_test_execution_payload` function has three overlapping code paths** (role_hint check, then explicit step_kind check, then inferred step_kind check) that can yield contradictory results if `explicit_step_kind != _assignment_step_kind(payload)` for the same payload.
