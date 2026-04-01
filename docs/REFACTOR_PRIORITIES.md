# Refactor Priorities

> Based on direct code observations. Issues are listed with file references and severity ratings (P1 = blocking, P2 = high, P3 = medium, P4 = low).

---

## P1 — Blocking / Correctness Issues

### 1. Duplicate helper functions across modules
**Files**: `control_plane/task_manager/task_manager.py` and `control_plane/scheduler/scheduler.py`

Both files independently define:
- `_transform_template_value(template, payload)` — with different internal helpers
- `_lookup_payload_path(payload, path)` — identical logic
- `_split_transform_expr_list(expr)` — identical logic
- `_parse_transform_literal(expr)` — identical logic
- `_camelize_key` / `_camelize_json_keys` — only in scheduler

The scheduler version has a `notes` parameter on `_resolve_transform_value` not present in task_manager. The two diverge silently. **Fix**: extract to `shared/template_helpers.py`.

### 2. Scheduler imports dashboard package at runtime
**File**: `control_plane/scheduler/scheduler.py` (lines ~623–646)

```python
from dashboard.db import get_db
from dashboard.models import BotConnection, Connection
```

This creates a hard runtime dependency from `control_plane` on `dashboard`. If the dashboard package is not installed, the scheduler silently returns `[]` for connection rows. **Fix**: move connection resolution to a shared service or an injectable interface.

### 3. Naive hash-based embeddings for vault and chat memory
**Files**: `control_plane/vault/vault_manager.py`, `control_plane/chat/chat_manager.py`

Both use a 64-dimensional SHA-256 hash embedding (`_embed`) for similarity search. This is a bag-of-words approximation with no semantic understanding. Cosine similarity over these vectors is nearly meaningless for semantic retrieval. The `embedding_status` field on `VaultItem` suggests real embeddings were planned.

**Fix**: wire up the `embedding_model` worker backend to generate actual embeddings, store them (possibly as a BLOB or via a proper vector store), and replace the hash-based `_embed`.

### 4. Fan-out produces 0 tasks on empty workstreams — silent stall
**File**: `control_plane/task_manager/task_manager.py` (trigger processing)

If `pm-engineer` returns an empty `implementation_workstreams` array, the fan-out trigger spawns zero coder tasks. The join gate for `pm-database-engineer` then waits indefinitely. No timeout or alarm is triggered.

**Fix**: validate that fan-out produces at least 1 task; if 0, fail the trigger explicitly with a structured error.

---

## P2 — High Priority

### 5. Rate limit store is per-process (not shared across workers)
**File**: `control_plane/security/guards.py`

`request.app.state.rate_limit_store` is an in-memory dict. Under uvicorn multi-worker mode or Gunicorn, each worker has its own store. The effective rate limit is multiplied by worker count.

**Fix**: replace with Redis-backed rate limiting, or enforce single-worker mode in deployment docs.

### 6. SQLite write contention under concurrent task dispatch
**File**: `control_plane/task_manager/task_manager.py`, `control_plane/scheduler/scheduler.py`

All task creates, updates, artifact writes go through `aiosqlite` on one SQLite file. The file-level write lock serializes all writes. Under PM workflow fan-out (3 parallel research tasks), all three tasks try to write simultaneously.

**Fix**: batch writes where possible; evaluate migration to PostgreSQL for production.

### 7. `CONTROL_PLANE_API_TOKEN` auth bypasses `/v1/bots/{id}/trigger`
**File**: `control_plane/main.py` (lines 183–184)

The auth middleware explicitly exempts `POST /v1/bots/<id>/trigger`. This means unauthenticated callers can trigger any bot without a token.

**Fix**: document this as intentional or add an opt-in flag; if it's unintentional, remove the exemption.

### 8. KeyVault uses insecure default key in dev
**File**: `control_plane/keys/key_vault.py` (line 62)

```python
or "nexusai-dev-insecure-default-key"
```

If neither `NEXUS_MASTER_KEY` nor `NEXUSAI_SECRET_KEY` is set, all API keys are encrypted with a known plaintext. This is fine for development but should emit a loud warning at startup.

**Fix**: add a startup warning when the fallback key is used; fail hard if `NEXUS_MASTER_KEY` is not set in production mode.

### 9. `_extract_scope_lock` hardcoded phrase catalog
**File**: `control_plane/chat/pm_orchestrator.py` (lines 470–517)

Scope lock extraction only matches `math`, `geometry`, `programming` domains and two narrow conditions. For any instruction outside these domains, the scope lock is effectively empty (`domains: ["general"]`), making it a no-op.

**Fix**: replace keyword extraction with a structured prompt to the PM bot itself, or at minimum expand the domain catalog to cover common project types.

### 10. No orchestration-level cancellation API
**File**: `control_plane/api/tasks.py`

There is no endpoint to cancel all tasks sharing an `orchestration_id`. Operators can only cancel individual tasks.

**Fix**: add `DELETE /v1/tasks?orchestration_id=<id>` or `POST /v1/orchestrations/{id}/cancel`.

---

## P3 — Medium Priority

### 11. `_is_output_contract_error_message` is fragile substring match
**File**: `control_plane/task_manager/task_manager.py` (line 445)

Detects output contract errors by checking if `"output contract"` is in the error message string. Any error message containing that substring triggers contract-specific retry logic.

**Fix**: use a typed exception or structured error code instead of string matching.

### 12. `_looks_like_truncated_result` only checks finish_reason
**File**: `control_plane/task_manager/task_manager.py` (lines 497–514)

The function checks `finish_reason in {length, max_tokens, ...}` and a small set of output endings. The token-count-based truncation check was intentionally removed but the comment is misleading. Truncation detection is incomplete.

### 13. Worker heartbeat stores only `queue_depth` + `gpu_utilization`
**File**: `worker_agent/main.py` (lines 78–86)

`gpu_utilization` is a list of percentages derived from `memory_used / memory_total`. Actual GPU compute utilization (as reported by `nvidia-smi`) is not captured. The metric is actually memory utilization, mislabelled.

**Fix**: rename to `gpu_memory_utilization` or fetch actual GPU utilization from `gpu_monitor.py`.

### 14. `ChatManager._embed` is identical to `VaultManager._embed`
**Files**: `control_plane/chat/chat_manager.py`, `control_plane/vault/vault_manager.py`

Exact duplicate of a 10-line function. **Fix**: move to `shared/embedding_helpers.py`.

### 15. `BotRegistry.seed_from_configs` persists all bots after locking
**File**: `control_plane/registry/bot_registry.py` (lines 137–164)

The method acquires the lock, builds all bots in memory, releases the lock, then persists each bot one-by-one without the lock. If a persist fails, the in-memory state is ahead of the DB.

**Fix**: persist inside the lock, or wrap all persists in a transaction.

---

## P4 — Low Priority / Cleanup

### 16. `_DEFAULTS` in `settings_manager.py` mixes concerns
**File**: `shared/settings_manager.py`

Settings include auth secrets (`session_secret_key`), LLM hosts, DB params, PM orchestration tuning, etc. in a flat list. No grouping by service boundary.

### 17. `architecture-and-plan.md` is stale
**File**: `architecture-and-plan.md`

Contains planning notes that diverge from the actual implemented code. Should be archived or updated.

### 18. Temp directories `tmp_pm_probe*` left in repo root
**Files**: `tmp_pm_probe/`, `tmp_pm_probe_finalqc/`, `tmp_pm_probe_finalqc2/`, `tmp_pm_probe_match/`

These appear to be debugging artifacts from PM probe runs. They should be cleaned up and added to `.gitignore`.

### 19. `check_exact.py` and `check_lines.py` in repo root
**Files**: `check_exact.py`, `check_lines.py`

Ad-hoc debugging scripts committed to the repo root. Should be removed or moved to `scripts/`.

---

## Invariants That Need Enforcing

1. **PM workflow stage order**: The 8-stage topology (researcher×3 → engineer → coder×N → tester×N → security×N → database → ui → final-qc) must be enforced by the orchestrator, not just recommended. Currently it's only a prompt instruction.

2. **Fan-out must produce ≥ 1 task**: Any trigger with `fan_out_field` must validate the source list is non-empty before spawning tasks.

3. **Output contract field presence**: The task manager already checks `required_output_fields`, but the check happens after the bot completes. Pre-flight validation of the contract schema itself (e.g., required fields are strings) is missing.

4. **`NEXUS_MASTER_KEY` in production**: The key vault should refuse to start with the insecure default key if `ENV=production` or similar signal is set.

5. **Bot `id` uniqueness across seeding**: `seed_from_configs` silently skips bots with duplicate IDs (unless `force=True`). A startup warning should be emitted for each skipped bot.

---

## What Needs Rewriting vs. Cleaning Up

| Component | Action | Rationale |
|---|---|---|
| Hash-based embeddings | **Rewrite** | Semantic quality is too low; requires real model integration |
| `_extract_scope_lock` | **Rewrite** | Keyword heuristics don't generalize; use structured LLM call |
| Template helper duplication | **Extract to shared** | Two diverging copies are a maintenance hazard |
| Rate limiter | **Replace** | In-memory per-process; use Redis or a shared store |
| SQLite for task queue | **Evaluate migration** | PostgreSQL with row-level locking would eliminate contention |
| `scheduler.py` → dashboard import | **Extract interface** | Break circular dependency |
| `tmp_pm_probe*` dirs | **Delete** | Debugging artifacts |
| `check_exact.py`, `check_lines.py` | **Delete or move** | Not production code |
