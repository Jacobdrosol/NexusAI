# NexusAI Test Suite

This directory contains the automated test suite for the NexusAI platform. Tests are written with [pytest](https://docs.pytest.org/) and use `pytest-asyncio` / `anyio` for async test cases.

---

## Running Tests

```bash
# Run all tests from the repo root
pytest tests/

# Run a specific test file
pytest tests/test_worker_agent_backends.py

# Run with verbose output
pytest tests/ -v

# Run only async tests
pytest tests/ -m anyio

# Run with coverage
pytest tests/ --cov=. --cov-report=term-missing
```

> Tests that use `@pytest.mark.anyio` require `anyio_backend` to return `"asyncio"` (configured in `conftest.py`).

---

## conftest.py — Fixtures and Test Setup

`conftest.py` provides shared fixtures used across the entire test suite.

### `anyio_backend` (function scope)
Sets the async backend to `"asyncio"` for all `@pytest.mark.anyio` tests.

### `cp_app` (async, function scope)
Creates a fully wired **control plane FastAPI application** backed by temporary SQLite databases (via `tmp_path`). This fixture:

- Instantiates all major control-plane components with isolated databases:
  - `WorkerRegistry` (in-memory)
  - `BotRegistry` (`bots.db`)
  - `ProjectRegistry` (`projects.db`)
  - `ModelRegistry` (`models.db`)
  - `KeyVault` (`keys.db`, master key `"test-master-key"`)
  - `ChatManager` (`chat.db`)
  - `VaultManager` (`vault.db`)
  - `MCPBroker`
  - `GitHubWebhookStore` (`github_webhooks.db`)
  - `AuditLog` (`audit.db`)
  - `Scheduler`
  - `TaskManager`
  - `PMOrchestrator`
  - `OrchestrationWorkspaceStore`
- Registers all control-plane API routers (`tasks`, `bots`, `workers`, `projects`, `keys`, `models_catalog`, `chat`, `vault`, `audit`).
- Installs observability middleware.
- Yields the `FastAPI` application for use with `httpx.AsyncClient(transport=ASGITransport(...))`.

---

## Test Organization

### Worker Agent

#### `test_worker_agent_backends.py`
Tests each inference backend module in isolation using mocked `httpx.AsyncClient` responses.

- `test_ollama_backend_infer` — verifies correct extraction of `message.content`, `prompt_eval_count` → `prompt_tokens`, `eval_count` → `completion_tokens` from Ollama's `/api/chat` response.
- `test_openai_backend_infer` — verifies extraction of `choices[0].message.content` and the `usage` object from OpenAI's chat completions response.
- Additional tests cover `claude_backend`, `gemini_backend`, and `cli_backend` (first 80 lines shown; full file contains tests for all five backends and the `/infer` FastAPI endpoint).

---

### Control Plane — Registries

#### `test_bot_registry.py`
Tests `BotRegistry` CRUD and validation.

- Register and retrieve a bot by ID.
- Raise `BotNotFoundError` for unknown IDs.
- Enable/disable bots.
- Remove a bot.
- Persist bots across registry reloads (SQLite-backed).
- Verify that `seed_from_configs` does not overwrite existing bots.
- Reject invalid bot reference graphs (e.g., a project-manager bot with no valid sub-bot references).

#### `test_worker_registry.py`
Tests `WorkerRegistry` CRUD and heartbeat behaviour.

- Register and retrieve a worker by ID.
- Raise `WorkerNotFoundError` for unknown IDs.
- List all registered workers.
- Update worker status.
- Remove a worker.
- Verify that `update_heartbeat` transitions a worker's status to `"online"`.

---

### Control Plane — Task Management

#### `test_task_manager.py`
Tests `TaskManager` task lifecycle and payload utilities.

- `_lookup_payload_path` — supports dot-notation paths including list index access (e.g., `"approved_units.0.source_payload.unit_blueprint.unit_number"`).
- Create a task and verify it enters `"queued"` status with the correct `bot_id`.
- Poll a task until it reaches `"completed"` status and verify the result payload.
- Deny-policy enforcement for bots that emit `repo_file` outputs.
- Additional tests for orchestration workspace integration, subtask expansion, and error propagation (file extends beyond the first 80 lines shown).

---

### Control Plane — Scheduling & Routing

#### `test_scheduler_routing.py`
Tests `Scheduler` worker selection and backend failure reporting.

- `_backend_failure_message` — verifies that failure messages include the task ID, root error, and a list of all attempted backends.
- `_cloud_timeout` — reads timeout from `NEXUSAI_CLOUD_API_TIMEOUT_SECONDS` env var.
- `_cloud_timeout` — prefers `SettingsManager` over env var when available.
- Worker load balancing — when two workers both support the same model, the scheduler selects the one with lower `queue_depth` and `gpu_utilization`.

---

### Control Plane — Chat & Orchestration

#### `test_pm_orchestrator.py`
Tests `PMOrchestrator` bot selection logic.

- `_pick_target_bot` avoids media-planner bots when a `"researcher"` role is requested.
- `_pick_target_bot` selects a researcher bot when one is explicitly available.
- `_pick_target_bot` avoids media-planner bots for `"planner"` role hints.
- `_pick_target_bot` prefers exact role match (`"coder"`) over pattern-matched bots (`"dba-sql"`).
- `_get_bot_by_id` returns the exact bot when the ID matches.

#### `test_chat_manager.py`
Tests `ChatManager` conversation and message management.

- Create a conversation and add a message; verify title and content.
- Raise `ConversationNotFoundError` when listing messages for a non-existent conversation.
- Update conversation tool access flags (`tool_access_enabled`, `tool_access_filesystem`, `tool_access_repo_search`).

---

### Control Plane — Secrets & Keys

#### `test_key_vault.py`
Tests `KeyVault` encrypted secret storage.

- Store and retrieve a secret by name; verify plaintext value and provider metadata.
- Delete a key and verify `APIKeyNotFoundError` is raised on subsequent access.
- Verify that decryption with a mismatched master key raises `ValueError`.

#### `test_vault_manager.py`
Tests `VaultManager` document ingestion and semantic search.

- Ingest text and retrieve by item ID; verify title and that at least one chunk was created.
- Search returns ranked results; the most relevant document ranks first.
- Accessing a missing item ID raises `VaultItemNotFoundError`.
- `upsert_text` with the same `source_ref` reuses the existing item (same `id`) and updates content in-place.

---

### Control Plane — Database Engineering

#### `test_database_engineer.py`
Tests `DatabaseEngineer`, `SchemaManager`, and `ConnectionRepository`.

- `SchemaManager.get_current_schema` returns a dict for an empty database.
- `table_exists` correctly identifies present and absent tables.
- `column_exists` correctly identifies present and absent columns within a table.
- `create_migration_plan` generates a migration plan for a new table definition.
- Additional tests cover `DatabaseEngineer` operations and `ConnectionRepository` CRUD (file extends beyond the first 80 lines shown).

---

### Utilities

#### `test_chunker.py`
Tests `chunk_text` (used by `VaultManager` for document chunking).

- Splits a 2500-character string into 3 chunks of size 1000 with overlap 100.
- Raises `ValueError` when `overlap >= chunk_size`.

---

## All Test Files

| File | What it tests |
|---|---|
| `conftest.py` | Shared fixtures (control plane app, async backend, tmp databases) |
| `test_assignment_apply_api.py` | Assignment/apply API endpoint behaviour |
| `test_bot_registry.py` | `BotRegistry` CRUD, persistence, validation |
| `test_chat_api.py` | Chat HTTP API endpoints |
| `test_chat_manager.py` | `ChatManager` conversation and message management |
| `test_chunker.py` | `chunk_text` splitting and overlap logic |
| `test_context_limits.py` | Context window limit enforcement |
| `test_control_plane_api.py` | Control plane HTTP API integration tests |
| `test_dashboard_auth_api.py` | Dashboard authentication API |
| `test_dashboard_connections.py` | Dashboard database connection management |
| `test_dashboard_cp_client.py` | Dashboard control plane client |
| `test_dashboard_db_init.py` | Dashboard database initialisation |
| `test_dashboard_deploy_api.py` | Dashboard deployment API |
| `test_dashboard_onboarding.py` | Dashboard onboarding flows |
| `test_dashboard_phase4_pages.py` | Dashboard phase 4 page rendering |
| `test_dashboard_smoke.py` | Dashboard smoke tests (basic page loads) |
| `test_database_engineer.py` | `DatabaseEngineer`, `SchemaManager`, `ConnectionRepository` |
| `test_dependency_engine.py` | Task dependency resolution engine |
| `test_key_vault.py` | `KeyVault` encrypted key storage and retrieval |
| `test_mcp_broker.py` | `MCPBroker` tool brokering |
| `test_model_registry.py` | `ModelRegistry` CRUD |
| `test_pm_orchestrator.py` | `PMOrchestrator` bot selection routing |
| `test_pm_workflow_routing.py` | PM workflow routing logic |
| `test_project_registry.py` | `ProjectRegistry` CRUD |
| `test_repo_workspace_bootstrap.py` | Repository workspace bootstrap |
| `test_scheduler_api_keys.py` | Scheduler API key injection |
| `test_scheduler_model_catalog.py` | Scheduler model catalog resolution |
| `test_scheduler_routing.py` | Scheduler worker selection and load balancing |
| `test_scope_preservation.py` | Task scope preservation across handoffs |
| `test_settings_manager.py` | `SettingsManager` key-value configuration |
| `test_shared_models.py` | Pydantic model validation for shared models |
| `test_sqlite_helpers.py` | SQLite utility functions |
| `test_task_manager.py` | `TaskManager` lifecycle, payload paths, policy enforcement |
| `test_task_result_files.py` | Task result file attachment handling |
| `test_vault_manager.py` | `VaultManager` ingestion, search, upsert |
| `test_worker_agent_backends.py` | Ollama, OpenAI, Claude, Gemini, CLI backends + `/infer` endpoint |
| `test_worker_registry.py` | `WorkerRegistry` CRUD, heartbeat, status |
| `test_workspace_tools.py` | Workspace tool operations |
