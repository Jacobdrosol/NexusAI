# Shared — `shared/`

Cross-cutting modules importable by `dashboard`, `control_plane`, and
`worker_agent`. Contains Pydantic models, exception types, configuration
loading, runtime settings, tool definitions, bot policy helpers, and chat
attachment constants.

---

## Module Overview

| Module | Summary |
|---|---|
| `models.py` | All Pydantic domain models |
| `exceptions.py` | Exception hierarchy rooted at `NexusError` |
| `bot_policy.py` | Pure functions for reading bot capabilities and validating configuration |
| `config_loader.py` | YAML loading and deep-merge utilities |
| `settings_manager.py` | SQLite-backed runtime settings singleton |
| `tool_catalog.py` | Tool definitions, presets, and helpers |
| `chat_attachments.py` | Constants governing chat file attachment limits |

---

## `models.py` — Pydantic Model Hierarchy

`shared/models.py` is the single source of truth for all data structures
exchanged between services. All models derive from Pydantic `BaseModel`.

### Core domain models

#### `Bot`
Represents a configured AI bot.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique bot identifier |
| `name` | `str` | Display name |
| `role` | `str` | Role hint (e.g. `"coder"`, `"researcher"`) |
| `system_prompt` | `str` | System prompt injected into every LLM call |
| `backend_id` | `str` | References a `BackendConfig` |
| `tool_ids` | `List[str]` | Tools available to this bot |
| `workflow` | `Optional[BotWorkflow]` | Workflow triggers and graph |
| `execution_policy` | `Optional[BotExecutionPolicy]` | Execution constraints |
| `assignment_capabilities` | `Optional[AssignmentCapabilities]` | PM-role flags |

#### `BackendConfig`
LLM backend definition.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique backend identifier |
| `provider` | `str` | Provider name (`"ollama"`, `"openai"`, `"anthropic"`, `"gemini"`, etc.) |
| `model` | `str` | Model name or tag |
| `host` | `Optional[str]` | Base URL for local/self-hosted backends |
| `api_key_ref` | `Optional[str]` | Name of the key in `KeyVault` |
| `max_tokens` | `Optional[int]` | Token limit per request |
| `num_ctx` | `Optional[int]` | Context window size (Ollama) |
| `num_width` | `Optional[int]` | Width parameter (Ollama) |
| `temperature` | `Optional[float]` | Sampling temperature |
| `timeout_seconds` | `Optional[int]` | Per-request timeout |

#### `Project`
A NexusAI project that groups bots, tasks, and settings.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique project identifier |
| `name` | `str` | Display name |
| `description` | `Optional[str]` | Free-text description |
| `bot_ids` | `List[str]` | Bots belonging to this project |
| `workspace_path` | `Optional[str]` | Filesystem path for project files |
| `github` | `Optional[GitHubIntegration]` | GitHub repo connection |

#### `Task`
A unit of work dispatched to a bot.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID |
| `project_id` | `str` | Owning project |
| `bot_id` | `str` | Target bot |
| `prompt` | `str` | User instruction |
| `status` | `TaskStatus` | Current lifecycle state |
| `result` | `Optional[str]` | Bot response |
| `error` | `Optional[str]` | Error message if failed |
| `metadata` | `Dict[str, Any]` | Arbitrary scheduling metadata |
| `created_at` | `str` | UTC ISO-8601 |
| `updated_at` | `str` | UTC ISO-8601 |
| `parent_task_id` | `Optional[str]` | Parent task for chained workflows |

#### `TaskStatus` (enum)
`pending` · `running` · `completed` · `failed` · `cancelled`

#### `Worker`
A registered worker agent.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID |
| `name` | `str` | Human-readable label |
| `host` | `str` | Worker HTTP host |
| `port` | `int` | Worker HTTP port |
| `status` | `WorkerStatus` | Lifecycle state |
| `last_heartbeat` | `Optional[str]` | UTC ISO-8601 of last ping |
| `capabilities` | `List[str]` | Tool IDs the worker supports |

#### `WorkerStatus` (enum)
`online` · `offline` · `busy`

### Workflow models

#### `BotWorkflow`

| Field | Type | Description |
|---|---|---|
| `triggers` | `List[WorkflowTrigger]` | Conditions that fire this bot |
| `reference_graph` | `Optional[ReferenceGraph]` | Topology of the multi-bot graph |

#### `WorkflowTrigger`

| Field | Type | Description |
|---|---|---|
| `target_bot_id` | `str` | Bot to invoke when trigger fires |
| `condition` | `Optional[str]` | Optional condition expression |
| `metadata` | `Dict[str, Any]` | Extra trigger parameters |

#### `ReferenceGraph`

| Field | Type | Description |
|---|---|---|
| `graph_id` | `str` | Unique graph identifier |
| `current_bot_id` | `str` | Must equal the owning bot's `id` |
| `entry_bot_id` | `str` | First bot executed in the graph |
| `nodes` | `List[GraphNode]` | All bots participating in the graph |
| `edges` | `List[GraphEdge]` | Directed connections between nodes |

#### `GraphNode`

| Field | Type | Description |
|---|---|---|
| `bot_id` | `str` | Bot represented by this node |

#### `GraphEdge`

| Field | Type | Description |
|---|---|---|
| `source_bot_id` | `str` | Origin node |
| `target_bot_id` | `str` | Destination node |

### Policy and capability models

#### `BotExecutionPolicy`

| Field | Type | Default | Description |
|---|---|---|---|
| `repo_output_mode` | `str` | `"deny"` | `"allow"` or `"deny"` repo write access |
| `allow_run_result_ingest` | `bool` | `False` | Allow bot to ingest task run results |
| `can_apply_db_actions` | `bool` | `False` | Allow bot to apply database actions |

#### `AssignmentCapabilities`

| Field | Type | Description |
|---|---|---|
| `is_project_manager` | `bool` | Whether this bot acts as a PM orchestrator |

### GitHub integration models

#### `GitHubIntegration`

| Field | Type | Description |
|---|---|---|
| `repo_full_name` | `str` | `owner/repo` |
| `webhook_secret_ref` | `Optional[str]` | Vault key name for HMAC secret |
| `installation_id` | `Optional[int]` | GitHub App installation ID |
| `access_token_ref` | `Optional[str]` | Vault key name for access token |

### Chat models

#### `ChatMessage`

| Field | Type | Description |
|---|---|---|
| `role` | `str` | `"user"` or `"assistant"` |
| `content` | `str` | Message text |
| `attachments` | `List[ChatAttachment]` | File attachments |
| `created_at` | `str` | UTC ISO-8601 |

#### `ChatAttachment`

| Field | Type | Description |
|---|---|---|
| `filename` | `str` | Original filename |
| `content_type` | `str` | MIME type |
| `content` | `str` | Base64-encoded or text content |

#### `Conversation`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID |
| `project_id` | `str` | Owning project |
| `bot_id` | `str` | Bot the conversation is with |
| `messages` | `List[ChatMessage]` | Ordered message history |
| `created_at` | `str` | UTC ISO-8601 |
| `updated_at` | `str` | UTC ISO-8601 |

---

## `exceptions.py` — Exception Hierarchy

```
Exception
└── NexusError                     Base exception for all NexusAI errors
    ├── ConfigError                Invalid or missing configuration
    ├── WorkerNotFoundError        Worker lookup failure
    ├── BotNotFoundError           Bot lookup failure
    ├── TaskNotFoundError          Task lookup failure
    ├── ProjectNotFoundError       Project lookup failure
    ├── APIKeyNotFoundError        Key not found in KeyVault
    ├── CatalogModelNotFoundError  Model not in tool catalog
    ├── ConversationNotFoundError  Chat conversation not found
    ├── VaultItemNotFoundError     Vault item not found
    ├── SchedulerError             Scheduling failure
    │   └── NoViableBackendError   No available backend for a task
    └── BackendError               LLM backend call failure
```

All exceptions carry a human-readable message as their first positional
argument. Catching `NexusError` covers all platform-specific errors.

---

## `bot_policy.py` — Bot Policy Functions

Pure functions that extract policy information from `Bot` objects. They do
not mutate state and have no I/O.

### `bot_execution_policy(bot: Bot) → BotExecutionPolicy`

Returns `bot.execution_policy` if set, otherwise a default
`BotExecutionPolicy()` instance (all restrictive defaults).

### `bot_is_project_manager(bot: Bot) → bool`

Returns `True` if the bot has `assignment_capabilities.is_project_manager == True`.

### `bot_allows_repo_output(bot: Bot) → bool`

Returns `True` if `bot_execution_policy(bot).repo_output_mode == "allow"`.

### `bot_allows_run_result_ingest(bot: Bot) → bool`

Returns `True` if the execution policy's `allow_run_result_ingest` flag is set.

### `bot_can_apply_db_actions(bot: Bot) → bool`

Returns `True` if the execution policy's `can_apply_db_actions` flag is set.

### `bot_workflow_graph_id(bot: Bot) → str`

Returns the `reference_graph.graph_id` if the bot has a workflow with a
non-empty graph ID; otherwise falls back to `str(bot.id)`.

### `bot_has_explicit_workflow(bot: Bot) → bool`

Returns `True` if the bot's workflow has at least one trigger defined.

### `validate_reference_graph(bot: Bot) → List[str]`

Validates the consistency of a bot's `reference_graph`. Returns a list of
error strings (empty means valid). Checks:

- `graph_id` is non-empty
- `current_bot_id` matches the bot's own `id`
- `entry_bot_id` is non-empty
- Both `current_bot_id` and `entry_bot_id` appear in `nodes`
- Every trigger's `target_bot_id` appears in `nodes`
- Every trigger edge `(source, target)` appears in `edges`

### `validate_bot_configuration(bot: Bot) → List[str]`

Runs `validate_reference_graph` and additionally checks that a PM bot
(`bot_is_project_manager`) has at least one explicit workflow trigger. Returns
a combined list of error strings.

### `derive_allowed_bot_ids(root_bot_id: str, bots: Sequence[Bot]) → List[str]`

BFS traversal starting from `root_bot_id`, following workflow trigger
`target_bot_id` edges. Returns the ordered list of all reachable bot IDs
(including the root). Used to scope which bots can be invoked within a
workflow execution.

### `bot_map_by_id(bots: Iterable[Bot]) → Dict[str, Bot]`

Convenience helper. Returns `{bot.id: bot}` for all bots with a non-empty ID.

---

## `config_loader.py` — YAML Configuration Loading

`ConfigLoader` is a stateless class with four static methods.

### `ConfigLoader.load_yaml(path: str) → dict`

Opens and parses a single YAML file with `yaml.safe_load`. Raises
`ConfigError` on `FileNotFoundError` or `yaml.YAMLError`. Returns `{}` for
empty files.

### `ConfigLoader.merge_configs(base: dict, override: dict) → dict`

Performs a **deep merge**: if a key exists in both `base` and `override` and
both values are dicts, they are merged recursively. Otherwise the `override`
value wins. Scalar and list values in `override` always replace `base`.

```python
base     = {"a": {"x": 1, "y": 2}, "b": 3}
override = {"a": {"y": 99, "z": 4}, "c": 5}
result   = {"a": {"x": 1, "y": 99, "z": 4}, "b": 3, "c": 5}
```

### `ConfigLoader.load_config(config_path, override_path=None) → dict`

Loads a base YAML file. If `override_path` is provided, loads that file too
and deep-merges it over the base. Returns the merged result.

### `ConfigLoader.load_all_from_dir(directory: str) → list[dict]`

Scans `directory` for files ending in `.yaml` or `.yml`, sorted
alphabetically. Loads each with `load_yaml` and returns a list of dicts.
Returns `[]` if the directory does not exist.

---

## `settings_manager.py` — Runtime Settings

Thread-safe SQLite-backed singleton for NexusAI application settings. Changes
persist across restarts and take effect within ~5 seconds in other processes
(cache TTL).

### SQLite Schema

**`nexus_settings`**

| Column | Type | Description |
|---|---|---|
| `key` | `TEXT PRIMARY KEY` | Setting key |
| `value` | `TEXT` | Raw string value |
| `value_type` | `TEXT` | `string`, `int`, `float`, `bool`, `json`, `secret` |
| `category` | `TEXT` | UI grouping category |
| `label` | `TEXT` | Human-readable display label |
| `description` | `TEXT` | Tooltip/help text |
| `updated_at` | `DATETIME` | Last change timestamp |
| `updated_by` | `TEXT` | Who last changed the setting |

**`nexus_settings_audit`**

| Column | Type | Description |
|---|---|---|
| `id` | `INTEGER AUTOINCREMENT` | Row ID |
| `key` | `TEXT` | Setting key that changed |
| `old_value` | `TEXT` | Previous value (secrets shown as `[REDACTED]`) |
| `new_value` | `TEXT` | New value (secrets shown as `[REDACTED]`) |
| `changed_by` | `TEXT` | Identity of changer |
| `changed_at` | `DATETIME` | Timestamp |

### `SettingsManager` API

| Method | Description |
|---|---|
| `SettingsManager.instance(db_path)` | Return/create the process-wide singleton |
| `get(key, default)` | Return the typed (coerced) value for a key |
| `get_all(mask_secrets)` | Return all settings as a dict; optionally redact secrets |
| `set(key, value, changed_by)` | Persist a new value and append audit record |
| `import_from_dict(d, changed_by)` | Bulk-import `{key: value}` mapping |
| `export_yaml()` | Serialize all settings to YAML string (secrets masked) |
| `export_json()` | Serialize all settings to JSON string (secrets masked) |
| `get_audit_log(limit)` | Return most-recent N audit entries newest-first |

### Module-level helper

#### `get_context_limits_for_model(model, settings) → tuple[int, int]`

Returns `(context_item_limit, context_source_limit)` for the given model name.
Models whose names contain any pattern from the `large_context_model_patterns`
setting (`gpt-oss`, `qwen3.5`, `claude-3`, `gpt-4`, `o1`, `o3` by default)
receive the "large context" limits; others receive the standard limits.

### Complete Settings Reference

#### General (`category="general"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `site_name` | `"NexusAI"` | string | Dashboard header display name |
| `site_tagline` | `""` | string | Dashboard subtitle |
| `control_plane_host` | `"localhost"` | string | Control plane hostname/IP |
| `control_plane_port` | `8000` | int | Control plane TCP port |

#### Auth (`category="auth"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `session_secret_key` | `""` | secret | Session signing secret |
| `session_timeout_minutes` | `60` | int | Idle session expiry |
| `allow_user_registration` | `false` | bool | Allow self-registration |

#### LLM / Workers (`category="llm"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `default_llm_host` | `"http://localhost:11434"` | string | Default LLM base URL |
| `default_llm_model` | `"llama3.2:latest"` | string | Default model name |
| `default_embedding_model` | `"nomic-embed-text"` | string | Default embedding model |
| `worker_heartbeat_interval` | `30` | int | Seconds between worker heartbeats |
| `cloud_backend_timeout_seconds` | `900` | int | Cloud API call timeout |

#### Logging (`category="logging"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `log_level` | `"INFO"` | string | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `log_to_file` | `true` | bool | Write logs to file |
| `log_file_path` | `"data/nexusai.log"` | string | Log file path |

#### Advanced (`category="advanced"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `max_task_retries` | `3` | int | Max retry attempts per task |
| `task_max_concurrency` | `4` | int | Max concurrent tasks platform-wide |
| `task_provider_concurrency_limits` | `{}` | json | Per-provider concurrency cap |
| `task_retry_delay` | `5.0` | float | Seconds between retries |
| `task_retry_max_tokens_increment` | `0` | int | Extra max_tokens per retry |
| `task_retry_num_width_increment` | `2048` | int | Extra num_width per retry |
| `running_task_watchdog_enabled` | `true` | bool | Enable stuck-task watchdog |
| `running_task_watchdog_poll_seconds` | `30` | float | Watchdog poll interval |
| `running_task_watchdog_initial_stall_seconds` | `600` | float | Stall window before first liveness check |
| `running_task_watchdog_progress_grace_seconds` | `300` | float | Grace period after liveness confirmed |
| `bot_trigger_max_depth` | `60` | int | Max chained trigger hops in workflow |
| `pm_assignment_trigger_max_depth` | `120` | int | Max hops in PM assignment run |
| `pm_assignment_research_fanout_limit` | `3` | int | Preferred max research branches |
| `pm_assignment_research_fanout_split_limit` | `6` | int | Absolute max research branches |
| `pm_assignment_workstream_fanout_limit` | `5` | int | Preferred max workstream branches |
| `pm_assignment_workstream_fanout_split_limit` | `6` | int | Absolute max workstream branches |
| `workflow_route_repeat_limit` | `3` | int | Max repeated dispatches per route |
| `external_trigger_default_auth_header` | `"X-Nexus-Trigger-Token"` | string | External trigger auth header name |
| `external_trigger_default_source` | `"external_trigger"` | string | Source label for external tasks |
| `external_trigger_max_body_bytes` | `1000000` | int | Max body size for external triggers |
| `external_trigger_rate_limit_count` | `120` | int | Max external trigger requests per window |
| `external_trigger_rate_limit_window_seconds` | `60` | int | Rate limit window (seconds) |

#### Context (`category="context"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `context_item_limit_default` | `30` | int | Default context item limit |
| `context_source_limit_default` | `12` | int | Default source label limit |
| `context_item_limit_large` | `100` | int | Large-context item limit |
| `context_source_limit_large` | `50` | int | Large-context source limit |
| `large_context_model_patterns` | `"gpt-oss,qwen3.5,claude-3,gpt-4,o1,o3"` | string | Patterns identifying large-context models |

#### Coding (`category="coding"`)

| Key | Default | Type | Description |
|---|---|---|---|
| `coding_enhancement_enabled` | `true` | bool | Enable coder-role prompt enhancements |
| `agent_session_ttl_minutes` | `60` | int | Agent session time-to-live |

### Known Issues — `settings_manager.py`

- **Synchronous SQLite** — uses `sqlite3` (not `aiosqlite`), so writes block
  the calling thread. In an async context this can stall the event loop.
- **Cache TTL is fixed at 5 seconds** — not configurable at runtime; other
  processes may see stale values for up to 5 seconds after a change.
- **No validation on `set`** — any key/value pair is accepted, including keys
  not in `_DEFAULTS`.
- **Secret masking in audit log only** — plaintext secrets are stored in
  `nexus_settings.value`; only the audit log redacts them.

---

## `tool_catalog.py` — Tool Catalog

Defines every tool the platform can offer bots. The `enabled_tools` setting
controls which tools are active. Missing entries default to the tool's
`default_enabled` flag.

### `ToolDefinition` dataclass

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique tool identifier |
| `name` | `str` | Human-readable name |
| `category` | `str` | Category key |
| `description` | `str` | What the tool does |
| `check_command` | `Optional[str]` | Shell command to verify availability |
| `default_enabled` | `bool` | Enabled on fresh install |
| `install_hint` | `Optional[str]` | How to install the tool |
| `presets` | `List[str]` | Preset group memberships |

### Full Tool Catalog

#### Workspace

| ID | Name | Default | Description |
|---|---|---|---|
| `filesystem` | Filesystem R/W | Yes | Read and write project workspace files |
| `repo_search` | Semantic Repo Search | Yes | Vector-index search across repo |

#### Research

| ID | Name | Default | Description |
|---|---|---|---|
| `web_search` | Web Search | Yes | Search the web |
| `vault_search` | Project Vault Search | Yes | Search project data vault |

#### Execution — Language Runtimes

| ID | Name | Default | Check Command |
|---|---|---|---|
| `code_exec_python` | Python Execution | Yes | `python --version` |
| `code_exec_dotnet` | .NET / C# Execution | No | `dotnet --version` |
| `code_exec_node` | Node.js / npm | No | `node --version` |
| `code_exec_rust` | Rust / Cargo | No | `cargo --version` |
| `code_exec_cpp` | C / C++ Build | No | `cmake --version` |
| `code_exec_java` | Java / Maven / Gradle | No | `java -version` |
| `code_exec_go` | Go Build & Test | No | `go version` |
| `code_exec_swift` | Swift / Xcode CLI | No | `swift --version` |
| `code_exec_kotlin` | Kotlin / Gradle | No | `kotlinc -version` |
| `code_exec_php` | PHP Execution | No | `php --version` |

#### Data & Databases

| ID | Name | Default | Check Command |
|---|---|---|---|
| `db_sql` | SQL Database Tools | Yes | — |
| `db_mongo` | MongoDB Tools | No | `mongosh --version` |
| `db_redis` | Redis Tools | No | `redis-cli --version` |

#### Testing Frameworks

| ID | Name | Default | Check Command |
|---|---|---|---|
| `test_runner_pytest` | pytest | Yes | `pytest --version` |
| `test_runner_jest` | Jest / Vitest | No | `npx jest --version` |
| `test_runner_dotnet_test` | .NET Test | No | `dotnet --version` |
| `test_runner_cargo_test` | Cargo Test | No | `cargo --version` |
| `test_runner_gtest` | Google Test (C++) | No | — |
| `test_runner_junit` | JUnit / Gradle Test | No | `java -version` |

#### UI & UX Testing

| ID | Name | Default | Check Command |
|---|---|---|---|
| `ui_browser` | Browser Automation (Playwright/Puppeteer) | No | `npx playwright --version` |
| `ui_desktop` | Desktop UI Testing (WinForms/WPF/Electron) | No | — |
| `ui_mobile` | Mobile Testing (Appium/XCUITest) | No | `appium --version` |
| `ui_game` | Game Engine Testing (Unreal/Unity) | No | — |

#### DevOps & Containers

| ID | Name | Default | Check Command |
|---|---|---|---|
| `container_docker` | Docker Build & Run | No | `docker --version` |
| `devops_git` | Git CLI | Yes | `git --version` |

#### IoT & Embedded

| ID | Name | Default | Description |
|---|---|---|---|
| `iot_serial` | IoT / Serial Communication | No | UART/serial device communication |
| `iot_cross_compile` | Cross-Compiler Toolchain | No | Cross-compile for ARM/embedded targets |

#### AI & LLM Inference

| ID | Name | Default | Description |
|---|---|---|---|
| `llm_inference` | LLM Inference (local / cloud) | Yes | Ollama, LM Studio, OpenAI, Claude, Gemini |
| `embedding_model` | Embedding Model | Yes | Text embeddings for semantic search |

### TOOL_PRESETS

Quick-select preset groups shown in the Settings → Tools UI:

| Preset ID | Label | Included Tool Highlights |
|---|---|---|
| `all` | All Tools | Every tool in the catalog |
| `web` | Web Development | Node.js, Python, browser testing, SQL, Docker |
| `dotnet` | .NET / C# | .NET SDK, xUnit, WinForms/WPF UI testing |
| `data_science` | Data Science | Python, pytest, SQL, MongoDB, embeddings |
| `mobile` | Mobile Development | Node.js, Swift, Kotlin, Appium |
| `desktop` | Desktop Apps | .NET, Swift, desktop UI testing |
| `game` | Game Development | C++, cmake, GoogleTest, Unreal/Unity testing |
| `iot` | IoT / Embedded | C/C++, Rust, serial communication, cross-compiler |
| `systems` | Systems Programming | C/C++, Rust, Go, cross-compilation |
| `enterprise` | Enterprise / JVM | Java, Kotlin, SQL, Redis, Docker |
| `ai` | AI / LLM | Python, LLM inference, embeddings, Docker |

### Helper Functions

| Function | Description |
|---|---|
| `default_enabled_tools() → list[str]` | IDs of all tools with `default_enabled=True` |
| `tools_for_preset(preset_id) → list[str]` | Tool IDs belonging to a preset |

### Module-level constants

| Name | Type | Description |
|---|---|---|
| `TOOL_CATALOG` | `List[ToolDefinition]` | Ordered list of all tools |
| `TOOL_CATALOG_BY_ID` | `dict[str, ToolDefinition]` | Lookup by tool ID |
| `TOOL_CATEGORIES` | `list[str]` | Ordered list of unique category keys |
| `CATEGORY_LABELS` | `dict[str, str]` | Human-readable category names |
| `TOOL_PRESETS` | `dict[str, dict]` | Preset definitions (label + description) |

### Known Issues — `tool_catalog.py`

- **No runtime availability check** — `check_command` is defined but the
  catalog itself does not run checks; availability verification must be
  implemented by the caller.
- **Presets are not validated** — `tools_for_preset` silently returns `[]` for
  unknown preset IDs.
- **`android` preset referenced but not defined** — `code_exec_java` and
  `code_exec_kotlin` list `"android"` as a preset tag, but no `"android"`
  entry exists in `TOOL_PRESETS`.

---

## `chat_attachments.py` — Attachment Constants

Three module-level constants governing chat file attachment behaviour:

| Constant | Value | Description |
|---|---|---|
| `CHAT_ATTACHMENT_MAX_FILES` | `15` | Maximum number of files per message |
| `CHAT_ATTACHMENT_MAX_TOTAL_BYTES` | `1 073 741 824` (1 GiB) | Maximum total size across all attachments |
| `CHAT_ATTACHMENT_MAX_TEXT_BYTES` | `120 000` | Maximum bytes for text-extracted content |

These constants are imported by the chat ingestion pipeline to validate
uploads before processing. No enforcement logic lives in this file.

### Known Issues — `chat_attachments.py`

- **`CHAT_ATTACHMENT_MAX_TOTAL_BYTES` is 1 GiB** — this is very generous for a
  chat interface and may allow memory exhaustion if all files are loaded into
  memory simultaneously.
- **Constants are not configurable** — they are hardcoded and cannot be
  adjusted via `SettingsManager` without a code change.
