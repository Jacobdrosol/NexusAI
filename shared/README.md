# Shared Library

The `shared` package contains models, policy helpers, exceptions, configuration loading, tool catalog, and settings management used by all three services (control_plane, dashboard, worker_agent).

---

## Files

| File | Purpose |
|------|---------|
| `models.py` | All Pydantic v2 data models |
| `bot_policy.py` | Bot policy evaluation helpers |
| `exceptions.py` | Exception hierarchy |
| `config_loader.py` | YAML config loading and merging |
| `tool_catalog.py` | Tool definitions and preset groups |
| `settings_manager.py` | SQLite-backed runtime settings singleton |
| `chat_attachments.py` | Chat attachment size/type constants |
| `observability/metrics.py` | Prometheus metric definitions |

---

## models.py — Data Models

### Worker Models
- `Capability(type, provider, models, gpus)` — what a worker can do
- `WorkerMetrics(queue_depth, gpu_utilization)` — live metrics
- `Worker(id, name, host, port, capabilities, status, metrics, enabled)`

### Backend Models
- `BackendParams(temperature, max_tokens, top_p, num_ctx, num_width, num_gpu, main_gpu, num_thread, repeat_penalty)` — inference parameters
- `BackendConfig(type, worker_id, model, provider, api_key_ref, gpu_id, params)` — one backend in a bot's chain

### Workflow / Bot Models
- `BotWorkflowTrigger` — defines routing between bots (see PM_WORKFLOW.md)
- `AssignmentCapabilities(is_project_manager)` — PM flag
- `DBActionPolicy` — fine-grained DB permission set
- `BotExecutionPolicy(repo_output_mode, can_apply_db_actions, db_action_policy, allow_run_result_ingest)`
- `BotContextAccess(receives, can_self_serve)` — advisory scheduler hints
- `WorkflowReferenceGraph` + `WorkflowReferenceGraphNode` + `WorkflowReferenceGraphEdge` — DAG visualisation metadata
- `BotWorkflow(triggers, notes, reference_graph, required_output_fields)`
- `Bot(id, name, role, system_prompt, priority, enabled, backends, routing_rules, workflow, context_access, assignment_capabilities, execution_policy)`

### Task Models
- `TaskMetadata` — full lineage tracking (user_id, project_id, orchestration_id, step_id, parent_task_id, trigger_rule_id, trigger_depth, retry_attempt, workflow IDs, pipeline metadata, run_class)
- `TaskError(message, code, details)`
- `Task(id, bot_id, payload, metadata, depends_on, status, result, error, created_at, updated_at)`
- `BotRun(id, task_id, bot_id, status, payload, metadata, result, error, triggered_by_task_id, trigger_rule_id, timestamps...)`
- `BotRunArtifact(id, run_id, task_id, bot_id, kind, label, content, path, metadata, created_at)`

### Project / Catalog / Chat / Vault Models
- `Project(id, name, description, mode, bridge_project_ids, bot_ids, settings_overrides, enabled)`
- `CatalogModel(id, name, provider, context_window, capabilities, cost fields, notes, enabled)`
- `ChatConversation(id, title, project_id, bridge_project_ids, scope, default_bot_id, default_model_id, tool_access_*, archived_at, timestamps)`
- `ChatMessage(id, conversation_id, role, content, bot_id, model, provider, metadata, created_at)`
- `VaultItem(id, source_type, source_ref, title, content, namespace, project_id, metadata, embedding_status, timestamps)`
- `VaultChunk(id, item_id, chunk_index, content, embedding, metadata, created_at)`

---

## bot_policy.py — Policy Helpers

| Function | Description |
|----------|-------------|
| `bot_execution_policy(bot)` | Returns `BotExecutionPolicy` (defaults if not set) |
| `bot_is_project_manager(bot)` | True if `assignment_capabilities.is_project_manager` |
| `bot_allows_repo_output(bot)` | True if `execution_policy.repo_output_mode == "allow"` |
| `bot_allows_run_result_ingest(bot)` | True if `execution_policy.allow_run_result_ingest` |
| `bot_can_apply_db_actions(bot)` | True if `execution_policy.can_apply_db_actions` |
| `bot_workflow_graph_id(bot)` | Returns `workflow.reference_graph.graph_id` or `bot.id` |
| `bot_has_explicit_workflow(bot)` | True if `workflow.triggers` is non-empty |
| `validate_reference_graph(bot)` | Checks graph_id, entry_bot_id, node/edge consistency. Returns list of error strings. |
| `validate_bot_configuration(bot)` | PM bots must have workflow triggers. Returns list of errors. |
| `derive_allowed_bot_ids(root_bot_id, bots)` | BFS through triggers to find all reachable bot IDs |
| `bot_map_by_id(bots)` | Dict keyed by bot.id |

---

## exceptions.py — Exception Hierarchy

```
NexusError (base)
├── ConfigError
├── WorkerNotFoundError
├── BotNotFoundError
├── TaskNotFoundError
├── ProjectNotFoundError
├── APIKeyNotFoundError
├── CatalogModelNotFoundError
├── ConversationNotFoundError
├── VaultItemNotFoundError
├── SchedulerError
├── BackendError
└── NoViableBackendError
```

All exceptions inherit from `NexusError`. API routes catch the specific subtypes and map them to HTTP status codes (404 for not-found errors, 400 for config errors, etc.).

---

## config_loader.py — YAML Config Loading

| Method | Description |
|--------|-------------|
| `ConfigLoader.load_yaml(path)` | Loads a single YAML file, returns dict |
| `ConfigLoader.merge_configs(base, override)` | Deep merge: override wins at leaf level |
| `ConfigLoader.load_config(config_path, override_path)` | Load main config + optional override, deep-merged |
| `ConfigLoader.load_all_from_dir(directory)` | Loads all `.yaml`/`.yml` files in a directory, sorted by filename |

---

## tool_catalog.py — Tool Definitions

`TOOL_CATALOG`: 30+ `ToolDefinition` entries covering:

| Category | Tools |
|----------|-------|
| `workspace` | `filesystem`, `repo_search` |
| `research` | `web_search`, `vault_search` |
| `execution` | `python`, `dotnet`, `node`, `rust`, `cpp`, `java`, `go`, `swift`, `kotlin`, `php` |
| `data` | `db_sql`, `db_mongo`, `db_redis` |
| `testing` | `pytest`, `jest`, `dotnet_test`, `cargo_test`, `gtest`, `junit` |
| `ui_testing` | `browser`, `desktop`, `mobile`, `game` |
| `devops` | `docker`, `git` |
| `iot` | `serial`, `cross_compile` |
| `ai` | `llm_inference`, `embedding_model` |

`TOOL_PRESETS`: named preset groups: `all`, `web`, `dotnet`, `data_science`, `mobile`, `desktop`, `game`, `iot`, `systems`, `enterprise`, `ai`.

Helper functions: `default_enabled_tools()`, `tools_for_preset(preset_name)`.

---

## settings_manager.py — Runtime Settings

`SettingsManager` is a thread-safe singleton backed by `nexus_settings` SQLite table.

```python
sm = SettingsManager.instance(db_path="data/nexusai.db")
value = sm.get("site_name", "NexusAI")
sm.set("cloud_backend_timeout_seconds", "600", changed_by="admin")
```

All settings are stored as strings with a `value_type` hint (`string`, `int`, `bool`, `secret`, `json`). Changes are audited in `nexus_settings_audit`.

Default settings include: site name, auth config, LLM host/model, worker heartbeat, task retry increments, cloud timeout, PM orchestration flags, and more.

---

## chat_attachments.py — Attachment Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `CHAT_ATTACHMENT_MAX_FILES` | (see code) | Max attachments per message |
| `CHAT_ATTACHMENT_MAX_TEXT_BYTES` | (see code) | Max bytes for text attachment |
| `CHAT_ATTACHMENT_MAX_TOTAL_BYTES` | (see code) | Max total attachment bytes |

Used by `api/chat.py` to validate incoming chat message attachments.
