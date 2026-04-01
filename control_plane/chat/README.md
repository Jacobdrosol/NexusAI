# Chat

The `chat` module manages persistent conversations, message delivery, semantic memory search, PM assignment orchestration, and workspace file tools. It is composed of three main files.

| File | Class / exports | Purpose |
|---|---|---|
| `chat_manager.py` | `ChatManager` | Conversation and message CRUD, semantic memory indexing and search |
| `pm_orchestrator.py` | `PMOrchestrator` | `@assign` entry point, PM workflow planning, completion tracking |
| `workspace_tools.py` | module-level functions | Workspace file reading, snippet search, path utilities |

---

## ChatManager (`chat_manager.py`)

### SQLite Schema

All tables live in the same SQLite database as the task tables (configured via `DATABASE_URL`).

#### `conversations`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `title` | TEXT | Display name |
| `scope` | TEXT | `global`, `project`, or `bridged` |
| `project_id` | TEXT | Set for project-scoped conversations |
| `bridge_project_ids` | TEXT | JSON list; used for `bridged` scope |
| `bot_id` | TEXT | Default bot for new messages |
| `model` | TEXT | Default model override |
| `provider` | TEXT | Default provider override |
| `tool_access_enabled` | INTEGER | Master switch for workspace tools (0/1) |
| `tool_access_filesystem` | INTEGER | Allows reading workspace files (0/1) |
| `tool_access_repo_search` | INTEGER | Allows snippet search (0/1) |
| `archived_at` | TEXT | ISO-8601 UTC; NULL if not archived |
| `created_at` | TEXT | ISO-8601 UTC |
| `updated_at` | TEXT | ISO-8601 UTC |

#### `messages`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `conversation_id` | TEXT | FK to `conversations.id` |
| `role` | TEXT | `system`, `user`, `assistant`, `tool` |
| `content` | TEXT | Message body |
| `bot_id` | TEXT | Bot that produced this message (assistant only) |
| `model` | TEXT | Model used (optional) |
| `provider` | TEXT | Provider used (optional) |
| `metadata` | TEXT | JSON dict; modes: `assign_request`, `assign_pending`, `pm_run_report`, `assign_summary`, `assign_error` |
| `created_at` | TEXT | ISO-8601 UTC |

#### `chat_message_memory`

Chunked embedding index for in-conversation semantic search.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `{message_id}:{chunk_index}` |
| `message_id` | TEXT | FK to `messages.id` |
| `conversation_id` | TEXT | Denormalised |
| `role` | TEXT | Denormalised |
| `chunk_index` | INTEGER | 0-based chunk number |
| `content` | TEXT | Chunk text (≤800 chars with 120-char overlap) |
| `embedding` | TEXT | JSON float array (64-dimensional) |
| `created_at` | TEXT | ISO-8601 UTC |

### ChatManager Methods

| Method | Signature | Description |
|---|---|---|
| `create_conversation` | `(title, scope, project_id, ...)` | Create and persist a new conversation |
| `get_conversation` | `(conversation_id) → ChatConversation` | Raises `ConversationNotFoundError` if not found |
| `list_conversations` | `(project_id, scope, archived, limit) → List[ChatConversation]` | Filtered listing; `archived=False` by default |
| `delete_conversation` | `(conversation_id)` | Requires `archived_at` to be set first |
| `archive_conversation` | `(conversation_id) → ChatConversation` | Sets `archived_at`; idempotent |
| `restore_conversation` | `(conversation_id) → ChatConversation` | Clears `archived_at`; idempotent |
| `update_conversation_tool_access` | `(id, *, tool_access_enabled, tool_access_filesystem, tool_access_repo_search)` | Updates the three tool-access flags atomically |
| `add_message` | `(conversation_id, role, content, bot_id, model, provider, metadata) → ChatMessage` | Persists message and triggers async memory indexing |
| `list_messages` | `(conversation_id, limit) → List[ChatMessage]` | Ordered by `created_at ASC`; limit capped at 2000 |
| `list_message_slice` | `(conversation_id, *, limit, newest) → List[ChatMessage]` | Returns oldest-first or newest-first slice |
| `update_message` | `(message_id, *, content, metadata, model, provider) → ChatMessage` | Partial update; re-indexes memory if content changes |
| `count_messages` | `(conversation_id) → int` | Total message count including assignment metadata messages |
| `count_indexable_messages` | `(conversation_id) → int` | Count of user/assistant messages excluding assignment metadata modes |
| `search_message_memory` | `(conversation_id, query, *, limit, roles) → List[Dict]` | Cosine similarity search over `chat_message_memory` |
| `get_messages_by_ids` | `(conversation_id, message_ids) → List[ChatMessage]` | Batch fetch by ID |

### Message Modes (Metadata)

Messages produced by the assignment flow carry a `metadata.mode` value:

| Mode | Role | Description |
|---|---|---|
| `assign_request` | user | The original `@assign` text |
| `assign_pending` | assistant | Posted immediately after orchestration starts; updated on completion |
| `pm_run_report` | assistant | Final completion report (posted by `persist_summary_message`) |
| `assign_summary` | assistant | Short summary variant |
| `assign_error` | assistant | Error message if orchestration fails to start |

These modes are excluded from `count_indexable_messages` and memory indexing.

### Conversation Scopes

| Scope | Access |
|---|---|
| `global` | Accessible to all projects |
| `project` | Scoped to a single project (`project_id`) |
| `bridged` | Scoped to a project and its bridge projects (`bridge_project_ids`) |

### Memory Indexing

Messages with `role=user` or `role=assistant` (excluding assignment metadata modes) are chunked at 800 chars with 120-char overlap and indexed.

**Important**: The embedding is a 64-dimensional SHA-256 hash-based projection — not a neural embedding. Retrieval quality is approximate (bag-of-words character hashing) and will miss semantic synonyms. See Known Issues.

Retrieval scoring (`search_message_memory`):
```
weighted_score = cosine_similarity(query_vec, chunk_vec)
               + role_bonus      (0.18 for user, 0.02 for assistant)
               + recency_bonus   (max 0.08, linear within conversation timestamp span)
```

Results are de-duplicated to the best chunk per message, then sorted by `weighted_score DESC`.

---

## PMOrchestrator (`pm_orchestrator.py`)

### PM Workflow Stage Order

The standard PM pack has a fixed stage order defined in `_DEFAULT_PM_STAGE_ORDER`:

```
pm-orchestrator                    # root entry point
  └─ pm-research-analyst (×3)      # parallel: repo, data, online lanes
       └─ pm-engineer              # planning + implementation_workstreams fan-out
            └─ pm-coder (1+)       # parallel workstreams (fan-out from pm-engineer)
                 ├─ pm-tester
                 ├─ pm-security-reviewer
                 ├─ pm-database-engineer
                 ├─ pm-ui-tester
                 └─ pm-final-qc    # terminal stage
```

The order is read from the PM bot's `workflow.reference_graph.nodes` at runtime. If that is empty or absent, `_DEFAULT_PM_STAGE_ORDER` is used as the fallback.

### `orchestrate_assignment`

Entry point called by the API when a user sends `@assign` or similar:

```python
await pm_orchestrator.orchestrate_assignment(
    conversation_id: str,
    instruction: str,
    requested_pm_bot_id: str,        # required; raises BotNotFoundError if invalid
    context_items: List[str],        # repo profile, vault items, etc.
    conversation_brief: str,
    conversation_transcript: str,
    conversation_message_count: int,
    assignment_memory_hits: List[Dict],
    project_id: Optional[str],
) -> Dict[str, Any]
```

Returns: `{orchestration_id, pm_bot_id, instruction, plan, tasks, allowed_bot_ids, workflow_graph_id, pipeline_name}`

#### Flow

1. `_select_pm_bot(bots, requested_pm_bot_id)` — validates PM bot exists, is enabled, and is configured as a project manager.
2. `_extract_assignment_scope(instruction, ...)` — builds the `assignment_scope` dict.
3. Optionally creates an orchestration temp workspace via `orchestration_workspace_store`.
4. `_has_standard_pm_pack(bots)` → if true, uses `_deterministic_pm_pack_plan()`; otherwise falls back through LLM planning → `_heuristic_plan()`.
5. `_sanitize_plan_for_operator_scope(plan, instruction)` — strips/rewrites steps that require operator actions (CI/CD, PR merge, deploys).
6. `_expand_test_execution_steps(plan)` — splits test_execution steps into create+execute sub-steps.
7. Creates a root task via `task_manager.create_task(bot_id=pm_bot.id, ...)`.
8. Returns metadata immediately — the DAG unfolds via trigger rules as stages complete.

### `_bootstrap_assignment_via_pm_workflow`

Alternative entry point for bots that have an explicit `BotWorkflow` with triggers. Skips plan-building and creates the root task directly, letting the trigger graph drive the rest of the execution.

### `wait_for_completion`

```python
await pm_orchestrator.wait_for_completion(
    orchestration_id: str,
    assignment: Dict[str, Any],
    *,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]
```

Polls `task_manager.list_tasks(orchestration_id=...)` until all tasks are terminal. Returns a detailed completion dict:

| Key | Type | Description |
|---|---|---|
| `summary_text` | str | Human-readable multi-line status |
| `completed` / `failed` | int | Task counts |
| `all_terminal` | bool | True when no task is queued/blocked/running |
| `workflow_complete` | bool | True when terminal stage reached AND deliverables complete |
| `deliverables_complete` | bool | Whether all expected repo deliverables were produced |
| `missing_stages` | List[str] | Stage IDs in expected order but not run in latest cycle |
| `skipped_stages` | List[str] | Stages that completed with outcome `skip` |
| `intentionally_excluded_stages` | List[str] | Stages correctly excluded by assignment scope |
| `workflow_policy_codes` | List[str] | Machine-readable reason codes for incomplete/skipped stages |

### `persist_summary_message`

Posts the `pm_run_report` message to the conversation and updates the original `assign_pending` message metadata with `run_status`, `ingest_allowed`, `missing_stages`, `skipped_stages`, and `workflow_policy_codes`.

### Scope Lock (`_extract_scope_lock`)

Extracts structured constraints from the instruction:
- `domains`: content domain signals (math, geometry, programming, science)
- `allowed_artifacts`: glob patterns restricting which file paths the run may produce
- `forbidden_keywords`: path substrings that are explicitly forbidden

For docs-only runs the lock is tightened to `allowed_artifacts: ["*.md"]`.

### Docs-Only Mode

`_instruction_requests_docs_only_outputs(instruction)` requires BOTH:
1. A docs signal: `documentation`, `markdown`, `.md`, `docs/`
2. A docs-only signal: `only .md`, `docs only`, `no code edited`, etc.

When both are present, `assignment_scope.docs_only = true` is set, which gates the policy checks throughout the system.

### Stage Exclusion Logic

`_assignment_stage_exclusion_scope(instruction, docs_only)` infers which stages to intentionally skip:
- `pm-database-engineer`: excluded when instruction explicitly says no DB changes
- `pm-ui-tester`: excluded when no UI/frontend signal in instruction
- For docs-only requests: both database and UI stages are excluded by default

These exclusions appear in `intentionally_excluded_stages` on the completion summary and do NOT contribute to a `workflow_incomplete` policy code.

### Bot Selection for Steps

`_pick_target_bot(bots, role_hint, pm_bot_id)` uses a three-priority matching scheme:

1. **Exact role match** — `bot.role` matches one of the canonical roles for this `role_hint`
2. **Pattern match** — bot ID / name / role matches a regex pattern for the `role_hint`
3. **Fallback** — highest-priority non-PM bot from the enabled list

Database engineer bots are excluded from non-DBA roles, and media planner bots are excluded from generic implementation steps.

`_preferred_bot_id_for_role` maps canonical `role_hint` values to their standard bot IDs:

| `role_hint` | Preferred bot ID |
|---|---|
| `researcher`, `assistant` | `pm-research-analyst` |
| `planner`, `engineer` | `pm-engineer` |
| `coder` | `pm-coder` |
| `tester`, `qa` | `pm-tester` |
| `reviewer`, `security`, `security-reviewer` | `pm-security-reviewer` |
| `dba`, `database`, `dba-sql` | `pm-database-engineer` |
| `ui`, `ui-tester` | `pm-ui-tester` |
| `final-qc`, `final_qc` | `pm-final-qc` |

### Plan Building

Three plan-building strategies are tried in order:

1. **`_deterministic_pm_pack_plan`** — used when all 8 standard PM bots are present and enabled. Produces a fixed plan: 3 parallel research steps → 1 engineering step. Downstream coder/tester/security steps are spawned via trigger rules, not plan steps.
2. **LLM plan** (`_build_plan` via `PM_SYSTEM_PROMPT`) — the PM bot itself is asked to produce a plan JSON. Validated: if the first step is an implementation bot, falls back to heuristic. If it starts with `pm-engineer` but `pm-research-analyst` is available, also falls back.
3. **`_heuristic_plan`** — rule-based plan built from available bots and instruction signals (has_tester, has_reviewer, needs_database, needs_ui, has_final_qc).

### `_ASSIGNMENT_TRANSCRIPT_REDUCTION_CHARS` and Research Lane Constants

Research lanes for the standard deterministic plan:

| Lane | Step ID | Focus |
|---|---|---|
| `repo` | `step_1_code` | Repository implementation patterns and code constraints |
| `data` | `step_1_data` | Requirements, prior decisions, data context |
| `online` | `step_1_online` | External docs and standards (only when needed) |

Workstream split markers detected for documentation fan-out: `"part "`, `"chunk "`, `"batch "`, `"shard "`, `"1/"`, `"2/"`, etc.

---

## The `@assign` Command Flow

```
User sends "@assign <instruction>"
    │
    ▼
API layer (api/chat.py)
    ├─ Parses @assign command
    ├─ Loads conversation, project, bot list
    ├─ Builds context_items (repo profile, vault hits, memory hits)
    └─ Calls PMOrchestrator.orchestrate_assignment(...)
          │
          ▼
    PMOrchestrator
          ├─ Validates PM bot
          ├─ Extracts assignment_scope (scope_lock, docs_only, exclusions)
          ├─ Builds plan (_deterministic / LLM / _heuristic)
          ├─ Sanitizes plan (removes operator-scope steps)
          ├─ Expands test steps
          └─ Creates root task via TaskManager.create_task(...)
                │
                ▼
          TaskManager
                ├─ Persists task (status: queued)
                ├─ Dispatcher picks it up → Scheduler.schedule(task)
                ├─ pm-orchestrator bot runs → returns plan JSON
                └─ Trigger rules fire → pm-research-analyst tasks created
                        │
                        ▼ (after all 3 research analysts complete)
                   pm-engineer task created
                        │
                        ▼ (fan-out from implementation_workstreams)
                   pm-coder tasks × N (parallel)
                        │
                        ▼
                   pm-tester / pm-security-reviewer / pm-database-engineer
                        │
                        ▼
                   pm-ui-tester (if UI scope)
                        │
                        ▼
                   pm-final-qc (terminal stage)
                        │
                        ▼
          PMOrchestrator.wait_for_completion() detects terminal
          PMOrchestrator.persist_summary_message() → pm_run_report posted
```

---

## Workspace Tools (`workspace_tools.py`)

These functions are called from the API layer when `tool_access_enabled` is true and workspace tools have been granted to the conversation.

### `read_workspace_file_snippet`
```python
def read_workspace_file_snippet(
    root: Path,
    path_hint: str,
    *,
    max_file_bytes: int = 200_000,
    max_chars: int = 4_000,
) -> dict[str, Any] | None
```
Reads a single file within the workspace root. Returns `{"path": relative_path, "snippet": content}` or `None` if the file is binary, too large, or outside the root. Content is truncated at `max_chars` with a `...[TRUNCATED]` suffix.

`_safe_resolve_under_root` prevents path traversal by resolving the candidate path and asserting it stays within `root`.

### `search_workspace_snippets`
```python
def search_workspace_snippets(
    root: Path,
    query: str,
    *,
    limit: int = 4,
    max_files: int = 400,
    max_file_bytes: int = 200_000,
    max_chars_per_snippet: int = 300,
) -> list[dict[str, Any]]
```
Searches workspace files for query terms. Returns up to `limit` results sorted by relevance score:

```
score = (path_hits × 4) + min(content_hits, 8) + _path_priority(rel_path)
```

`_path_priority` boosts code files and source directories; penalises migrations, temp files, and `.designer.cs` / EF migration files.

Directories in `_IGNORE_DIR_NAMES` are skipped (`.git`, `node_modules`, `__pycache__`, etc.). Walking is aborted after `max_files` files scanned.

`_best_matching_snippet` returns the first line containing a query term, falling back to the first line.

### `build_focus_query`
```python
def build_focus_query(query: str, *, max_terms: int = 12) -> str
```
Extracts meaningful search terms from a natural-language query. Tokens are scored by: frequency × 8 + min(len, 12)/6 + hint boost (4 for known important tokens). Stop terms (`the`, `and`, `with`, etc.) are filtered.

### `_query_terms`
```python
def _query_terms(query: str, *, max_terms: int = 8) -> list[str]
```
Tokenises, deduplicates, and ranks query terms. Splits on `[._/\-]` separators; skips tokens shorter than 3 chars or in `_STOP_TERMS`.

### Path Priority Heuristics

| Signal | Priority delta |
|---|---|
| Code file suffix (`.py`, `.ts`, etc.) | +6 |
| `src/`, `backend/`, `api/`, `services/`, etc. | +4 |
| `/src/`, `/backend/`, etc. (in path) | +2 |
| Doc file suffix (`.md`, `.rst`) | −2 |
| `/tests/` or `tests/` prefix | −1 |
| `migrations/` | −12 |
| EF migration file pattern (`\d{10,}_*.cs`) | −5 |
| `.designer.cs` suffix | −6 |
| `docs/timeline/` or `temp_issue_files/` | −5 to −10 |

### Binary / Large File Filtering

`_is_probably_text_file(path, max_file_bytes)` skips:
- Files over `max_file_bytes` (default 200 KB)
- Files with binary suffixes: `.pyc`, `.png`, `.jpg`, `.gif`, `.mp4`, `.zip`, `.db`, `.sqlite`, etc.

---

## Known Issues / Refactor Notes

1. **SHA-256 hash embedding gives poor semantic search quality.** `ChatManager._embed` computes a 64-dimensional random projection using the SHA-256 hash of character 3-grams. It is essentially a bag-of-characters approximation. Semantic synonyms and paraphrases are not matched. A real embedding model (e.g., sentence-transformers) would dramatically improve retrieval quality.

2. **`_embed` is duplicated** — a nearly identical implementation also exists in `VaultManager`. Any fix or replacement must be applied in both places.

3. **`pm-orchestrator` stage listed in `_DEFAULT_PM_STAGE_ORDER`** is the root PM bot itself. When the reference_graph is absent and the fallback is used, the orchestrator bot ID appears in `observed_bot_ids`, making it look like a missed stage if the assignment uses a custom PM bot ID. This produces spurious `missing_stages` entries.

4. **LLM plan query is expensive and can silently fall back** — `_build_plan` sends a full plan-generation request to the PM bot before creating any tasks. If the LLM plan is rejected (e.g., starts with an implementation bot), the entire LLM call is wasted and the heuristic plan is used. There is no telemetry for how often this fallback occurs.

5. **`_sanitize_plan_for_operator_scope` silently drops `release` steps** by converting them to `review` steps. If an operator explicitly requests a `release` step (e.g., "and then open a PR"), this sanitisation discards it unless `_instruction_explicitly_requests_operator_actions` returns true. The detection for explicit operator intent uses simple keyword matching and has false negatives for paraphrased release instructions.

6. **`wait_for_completion` cycle detection is heuristic.** The "latest PM cycle" is determined by scanning for anchor tasks (tasks whose `bot_id` matches the expected entry point and that are the most recent in updated_at order). In complex re-plan scenarios this may incorrectly identify an old cycle as the latest.

7. **Workspace tools access control is checked at three levels** (bot policy, project policy, conversation flags) but the three-level check is implemented in the API layer, not in `workspace_tools.py`. The tool functions themselves do not enforce access control — they trust callers to gate access.

8. **`search_workspace_snippets` has a hard cap of `max_files = 400`** but no timeout. On slow filesystems or very large repos, the walk can block the event loop for several seconds if called from an async context without `asyncio.to_thread`.
