# Platform AI

The Platform AI module is an in-platform autonomous AI copilot and pipeline tuner. It provides a session-based interface where an operator can ask the AI to monitor, diagnose, and iteratively improve a running PM workflow — adjusting bot prompts, evaluating output quality, and relaunching orchestrations in a bounded feedback loop.

> **⚠️ Status: Active Development / Testing**
> Platform AI is currently under active testing and **not yet functioning reliably in production**. The autonomous tuner loop has gone through multiple fix iterations and still has known failure modes (see below). Do not depend on it for production pipeline repair.

---

## Architecture Overview

```
Operator (dashboard or API)
  │
  ▼
PlatformAISessionStore (session_store.py)
  │   SQLite-backed: sessions, messages, events, test suites, test runs
  │
  ▼
PlatformAISessionRuntime (runtime.py)
  │   Async session loop (1.5–4s heartbeat)
  │   ├── Progress snapshot ← TaskManager + OrchestrationRunStore
  │   ├── Quality evaluation (test suite assertions)
  │   ├── Bot refinement ← BotRegistry
  │   ├── New iteration launch ← AssignmentService
  │   └── Convergence / failure detection
  │
  ▼
control_plane/api/platform_ai.py
  │   REST endpoints (FastAPI router at /v1/platform-ai/)
  │
  ▼
dashboard/routes/platform_ai.py
    Web UI (Flask, /platform-ai/)
```

---

## Modes

| Mode | Description |
|------|-------------|
| `pipeline_tuner` | Autonomous mode: monitors a pipeline execution, evaluates quality, refines the entry bot, and relaunches until convergence or max iterations |
| `bot_designer` | Interactive mode: operator asks questions about bot configs, Platform AI suggests improvements |
| `copilot` | General assistant mode: answers questions about platform state |

The `pipeline_tuner` mode is the primary focus and the subject of current testing.

---

## Files

### `runtime.py` (1,551 lines)

Core autonomous runtime. One `PlatformAISessionRuntime` is instantiated per control plane process (shared across all sessions).

**Key methods:**

| Method | Purpose |
|--------|---------|
| `ensure_session_loop(session_id)` | Spawns an async background task for the session if not already running |
| `stop_session_loop(session_id)` | Cancels the background task |
| `post_message(session_id, ...)` | Adds an operator message and wakes the loop |
| `start_deploy_run(session_id, ...)` | Triggers a blue/green deploy within the session |
| `_session_loop(session_id)` | Core 1.5–4s heartbeat: snapshot → evaluate → refine → launch |
| `_build_progress_snapshot(session)` | Collects live task/graph state from task_manager |
| `_run_autonomous_pipeline_tuner(session_id)` | Orchestrates one full tuner iteration |
| `_apply_bot_refinement(session_id, ...)` | Injects failure analysis into bot's system prompt |
| `_refine_suite_definition(session_id, ...)` | Creates updated test suite for next iteration |
| `_launch_autonomous_orchestration(session_id, ...)` | Creates new orchestration run for next iteration |
| `_finalize_autonomous_session_if_terminal(session_id)` | Detects convergence or hard failure |

**Quality evaluation:**

The runtime evaluates pipeline health with structured assertions:

| Assertion kind | What it checks |
|---------------|----------------|
| `no_failed_tasks` | Zero tasks in failed state |
| `min_completed_ratio` | `completed / total >= value` |
| `node_coverage_ratio` | Fraction of graph nodes that executed |
| `min_avg_quality` | Average quality score across target tasks |
| `required_keywords` | Keywords present in task outputs |
| `required_fields` | Structured output fields present in results |

Each test has a `pass_threshold` (default 0.8) and `weight`. The suite score is a weighted average.

**Bot refinement:**

The runtime inserts autotuning directives into the target bot's `system_prompt` between markers:
```
[[NEXUS_PLATFORM_AI_AUTOTUNE_START]]
...failure analysis and corrective directives...
[[NEXUS_PLATFORM_AI_AUTOTUNE_END]]
```
Previous directives are replaced on each iteration.

**Convergence conditions:**

| Condition | Result |
|-----------|--------|
| `eval_score >= target_score` (default 0.9) | `completed` |
| Identical eval signature as previous iteration (stalled) | `stalled` → stop |
| Max iterations reached (default 6) | `max_iterations_reached` |
| Launch failed repeatedly | `launch_failed` |
| Terminal run has unresolved failed tasks | `failed` |

### `session_store.py` (722 lines)

SQLite-backed persistence for all Platform AI state.

**Tables:**

| Table | Purpose |
|-------|---------|
| `platform_ai_sessions` | One row per session: mode, status, IDs, metadata |
| `platform_ai_events` | Immutable action trace (event_type, payload JSON) |
| `platform_ai_messages` | Conversation history (role, content) |
| `platform_ai_test_suites` | Quality test definitions |
| `platform_ai_test_runs` | Test execution results with scores |

**Key behavior:** On startup, any sessions with `status=auto_managed` are automatically paused to prevent state leaks across restarts. This uses a fragile LIKE pattern on JSON strings (known issue — see below).

---

## API Endpoints

All routes are mounted at `/v1/platform-ai/` by the control plane.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sessions` | Create a new Platform AI session |
| `GET` | `/sessions` | List sessions (filter by assignment, orchestration, mode, archived) |
| `GET` | `/sessions/{id}` | Get session detail |
| `GET` | `/sessions/{id}/export` | Export full session bundle (messages, events, test runs) |
| `PATCH` | `/sessions/{id}` | Update session (goal, metadata, archive) |
| `GET` | `/sessions/{id}/events` | List session action trace events |
| `GET` | `/sessions/{id}/messages` | List conversation messages |
| `POST` | `/sessions/{id}/messages` | Post an operator message (drives autonomous loop) |
| `POST` | `/sessions/{id}/control` | Execute control actions (start_deploy, splice_rerun, rerun_node, pause, resume, archive) |
| `POST` | `/sessions/{id}/test-suites/design` | Design a quality suite for this session |
| `GET` | `/sessions/{id}/test-suites` | List suites for this session |
| `GET` | `/test-suites` | List all test suites |
| `GET` | `/test-suites/{suite_id}` | Get a test suite |
| `POST` | `/test-suites/{suite_id}/run` | Execute a test suite against an orchestration |
| `GET` | `/test-suites/{suite_id}/runs` | List runs for a suite |
| `GET` | `/test-runs/{run_id}` | Get a test run result |
| `GET` | `/pipelines` | List pipelines visible to Platform AI |
| `GET` | `/pipelines/{bot_id}/test-suites` | List suites for a pipeline |
| `POST` | `/pipelines/{bot_id}/test-suites/design` | Design a pipeline-scoped quality suite |
| `POST` | `/pipelines/{bot_id}/test-suites/run` | Run a quality suite against a pipeline |

---

## Dashboard UI

Routes at `/platform-ai/` (Flask blueprint):

- **`/platform-ai`** — Lists all sessions, pipelines, bots, and projects
- **`/platform-ai/sessions/<id>`** — Session detail: messages, events, test suite results, progress timeline
- **`/platform-ai/sessions/<id>/context-files`** — Upload context documents for the session

Context files are stored under `data/platform_ai/session_uploads/<session_id>/`. No file size limits are enforced (known issue).

---

## Operator Directive Actions (Chat-Driven)

Platform AI now supports structured JSON directives embedded in operator chat messages (use fenced `json` blocks). These directives are applied inside the runtime loop and recorded in session action traces.

Supported directive actions:

- `upsert_bot` / `upsert_bots` — create or update bot configs in `BotRegistry`
- `configure_pipeline_entry` — generate a linear pipeline graph/triggers on an entry bot
- `set_pipeline_target` — update `pipeline_bot_id` / `pipeline_name` and autonomous goal context
- `launch_pipeline` — start a new orchestration run for a pipeline entry bot
- `repo_edit` / `code_edit` / `external_repo_edit` — start privileged repo-edit runner jobs
- `deploy` — trigger the deployment runner

Example:

```json
{
  "platform_ai_action": "upsert_bot",
  "bot": {
    "id": "pm-coder-v2",
    "name": "PM Coder v2",
    "role": "assistant",
    "enabled": true,
    "backends": [{"type":"cloud_api","provider":"openai","model":"gpt-4o-mini"}]
  }
}
```

---

## Privileged Repo Edit Runner

Control actions `code_edit`, `hotfix`, and `external_repo_edit` now execute real asynchronous runner jobs instead of returning stub acceptance responses.

Environment variables:

- `NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD` (required for `code_edit`/`hotfix`)
- `NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_RUN_CMD` (required for `external_repo_edit`)
- `NEXUS_PLATFORM_AI_REPO_EDIT_CWD` (optional working directory)
- `NEXUS_PLATFORM_AI_REPO_EDIT_TIMEOUT_SECONDS` (runner hard timeout; default `1800`)
- `NEXUS_PLATFORM_AI_REPO_EDIT_AUTO_DEPLOY=1` (optional; auto-start deploy after successful internal repo edit)
- `NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED=1` + `NEXUS_PLATFORM_AI_OWNER_ALLOWLIST` are required; runtime now enforces these checks even when actions are triggered from chat directives.

The runner executes in a separate subprocess, streams logs into `action_trace` events, posts completion status back into the session chat, and releases control to the main Platform AI loop when done.

---

## Wiring

Platform AI is initialized in `control_plane/main.py` during the lifespan context:

```python
app.state.platform_ai_session_store = PlatformAISessionStore()
app.state.platform_ai_runtime = PlatformAISessionRuntime(
    session_store=...,
    task_manager=...,
    bot_registry=...,
    assignment_service=...,
    run_store=...,
)
```

The runtime holds references to the shared task manager, bot registry, assignment service, and run store.

---

## Known Issues / Current Limitations

> These are known at time of writing. Platform AI is actively being fixed.

| # | Severity | Issue | Location |
|---|----------|-------|----------|
| 1 | 🟡 Low | Session-loop duplication is guarded in-process with an async lock; cross-process duplicate loops are still possible if multiple control-plane workers share the same DB without distributed locking | `runtime.py` |
| 2 | 🔴 High | Stalled detection incomplete: if refinement changes don't alter eval signature, loop terminates prematurely | `runtime.py` ~lines 381-394 |
| 3 | 🟡 Low | Pipeline session claiming is serialized per process; cross-process duplicate claims remain possible without a database-level unique constraint | `api/platform_ai.py` |
| 4 | 🟠 Medium | `control` actions are partially implemented; repo-edit/deploy now execute runners, but autonomous code planning still depends on operator directives or custom runner scripts | `api/platform_ai.py` + `runtime.py` |
| 5 | 🟠 Medium | No wait/backoff between tuner iterations: launches immediately after refinement | `runtime.py` `_run_autonomous_pipeline_tuner` |
| 6 | 🟠 Medium | Session metadata JSON grows unbounded each iteration without cleanup | `runtime.py` `_run_autonomous_pipeline_tuner` |
| 7 | 🟡 Low | `_deploy_loop` imports `DeployManager` at runtime; unavailable manager is now surfaced via action trace + session metadata, but still depends on dashboard module presence in control-plane runtime | `runtime.py` |
| 8 | 🟡 Low | Auto-migration on startup uses fragile LIKE patterns on JSON strings | `session_store.py` ~lines 143-158 |
| 9 | 🟡 Low | Test suite and run records grow without cleanup (no TTL or pruning) | `session_store.py` |
| 10 | 🟡 Low | File uploads: no size limit, no cleanup, path traversal via symlinks possible | `dashboard/routes/platform_ai.py` |

---

## Refactor Notes

- The entire tuner is a single-threaded async loop with no persistence of loop state; a crash loses all iteration context.
- Bot refinement should use a separate staging field (e.g., `system_prompt_draft`) rather than directly mutating `system_prompt`, so rollback is possible.
- Quality assertions should be configurable per-pipeline, not just per-session.
- The session store should prune old test runs (e.g., keep last 20 per suite).
- The control plane should only hold one `PlatformAISessionRuntime` and route calls by session_id, not spawn per-session objects.
