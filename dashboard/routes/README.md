# Dashboard Routes

All routes require `@login_required` (Flask-Login) unless explicitly noted. CSRF protection (`Flask-WTF`) is active on all non-exempt endpoints. The overview (`GET /`) and health (`GET /health`) routes are part of the inline `main` blueprint registered in `app.py`.

---

## Patterns and Middleware

- **Auth guard:** Every blueprint endpoint has `@login_required`. Unauthenticated requests redirect to `/login`.
- **Admin guard:** Endpoints that modify users, settings, deploy, or tool installs additionally call `_require_admin()`, which aborts with HTTP 403 if `current_user.role != "admin"`.
- **Control plane fallback:** Most API endpoints try the control plane first via `get_cp_client()`; on failure they fall back to the local SQLite database (workers, bots, tasks).
- **CSRF exemptions:** `events_bp` (GET-only SSE) is fully exempt. `POST /api/auth/login` and `POST /api/auth/logout` are CSRF-exempt for programmatic access. All `/api/*` JSON endpoints posted by the SPA use the CSRF token via `X-CSRFToken` header.
- **`_cp_error_response(cp, ...)`:** Shared helper used across blueprints — extracts the last CP error and returns an appropriate `4xx`/`502` JSON response.

---

## main (inline in `app.py`)

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/` | Overview/dashboard page (stats, setup checklist, worker health, recent activity) | Yes |
| GET | `/health` | Health probe — returns `{"status": "ok"}` | No |

---

## `auth.py`

Blueprint: `auth` (no prefix).

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/login` | Show login form; redirects to onboarding if no users exist | No |
| POST | `/login` | Process login form (CSRF protected) | No |
| POST | `/api/auth/login` | JSON login — returns `{id, email, role}` (CSRF-exempt) | No |
| GET | `/api/auth/session` | Returns current session user info or `{authenticated: false}` | No |
| POST | `/api/auth/logout` | JSON logout (CSRF-exempt) | No |
| GET | `/logout` | HTML logout and redirect to `/login` | Yes |

---

## `onboarding.py`

Blueprint: `onboarding`, prefix `/onboarding`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/onboarding/` | Redirect to step 1 | No |
| GET | `/onboarding/step1` | Welcome screen | No |
| POST | `/onboarding/step1` | Advance wizard to step 2 | No |
| GET | `/onboarding/step2` | Create admin account form | No |
| POST | `/onboarding/step2` | Create admin account (bcrypt-hashed) | No |
| GET | `/onboarding/step3` | LLM backend selection | No |
| POST | `/onboarding/step3` | Save LLM backend choice | No |
| GET | `/onboarding/step4` | Register first worker (optional) | No |
| POST | `/onboarding/step4` | Register or skip worker | No |
| GET | `/onboarding/step5` | Completion screen | No |
| POST | `/onboarding/step5` | Finish wizard, redirect to login | No |

All onboarding routes redirect to `/login` if an admin user already exists.

---

## `routes/workers.py`

Blueprint: `workers`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/workers` | Workers list page (CP or local fallback) | Yes |
| GET | `/workers/<worker_id>` | Worker detail page with live metrics | Yes |
| GET | `/api/workers` | List workers as JSON (local SQLite only) | Yes |
| POST | `/api/workers` | Create a new worker | Yes |
| GET | `/api/workers/<worker_id>` | Get single worker (CP then local fallback) | Yes |
| PUT | `/api/workers/<worker_id>` | Update worker (CP then local fallback) | Yes |
| DELETE | `/api/workers/<worker_id>` | Delete worker (CP then local fallback) | Yes |
| POST | `/api/workers/<worker_id>/ping` | Heartbeat worker via CP | Yes |
| GET | `/api/workers/<worker_id>/live` | Live worker + running tasks snapshot for polling | Yes |
| POST | `/api/workers/<worker_id>/models/pull` | Pull a model on the worker node directly (HTTP → worker port) | Yes |

---

## `routes/bots.py`

Blueprint: `bots`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/bots` | Bots list page | Yes |
| GET | `/bots/<bot_id>` | Bot detail page (tasks, runs, artifacts, models, API keys) | Yes |
| GET | `/api/bots` | List all bots as JSON | Yes |
| POST | `/api/bots` | Create a new bot (CP first, then local fallback) | Yes |
| GET | `/api/bots/<bot_id>` | Get single bot (CP first, then local fallback) | Yes |
| GET | `/api/bots/<bot_id>/export` | Download bot as `<id>.bot.json` bundle (includes connections) | Yes |
| POST | `/api/bots/import` | Import a bot bundle; supports `overwrite` flag | Yes |
| PUT | `/api/bots/<bot_id>` | Update bot (CP first, then local fallback) | Yes |
| DELETE | `/api/bots/<bot_id>` | Delete bot (CP first, then local fallback) | Yes |
| POST | `/api/bots/<bot_id>/test-run` | Submit a one-off test task to CP with source=`bot_test` | Yes |
| POST | `/api/bots/<bot_id>/launch` | Launch bot via its saved launch profile | Yes |
| GET | `/api/bots/<bot_id>/artifacts/<artifact_id>` | Get a single artifact JSON | Yes |
| GET | `/api/bots/<bot_id>/artifacts/<artifact_id>/download` | Download artifact as attachment | Yes |

---

## `routes/chat.py`

Blueprint: `chat`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/chat` | Chat workspace page (conversation list, messages, vault context) | Yes |
| POST | `/api/chat/conversations` | Create a new conversation | Yes |
| DELETE | `/api/chat/conversations/<conversation_id>` | Delete a conversation | Yes |
| POST | `/api/chat/conversations/<conversation_id>/archive` | Archive a conversation | Yes |
| POST | `/api/chat/conversations/<conversation_id>/restore` | Restore an archived conversation | Yes |
| PUT | `/api/chat/conversations/<conversation_id>/tool-access` | Update tool access settings for a conversation | Yes |
| POST | `/api/chat/messages` | Send a chat message (non-streaming) | Yes |
| POST | `/api/chat/stream` | Send a chat message and stream the reply as SSE (proxies CP `/v1/chat/conversations/<id>/stream`) | Yes |
| GET | `/api/chat/conversations/<conversation_id>/messages` | List messages for a conversation | Yes |
| POST | `/api/chat/assignments/apply` | Apply orchestration assignment files to a project repo workspace | Yes |
| POST | `/api/chat/assignments/review` | Review orchestration assignment files diff | Yes |
| POST | `/api/chat/ingest` | Ingest a full conversation into the vault | Yes |
| POST | `/api/chat/message-to-vault` | Ingest a single message into the vault | Yes |
| POST | `/api/chat/orchestrations/<orchestration_id>/mark-failed` | Mark a PM orchestration run as failed | Yes |
| GET | `/api/chat/orchestrations/<orchestration_id>/graph` | Build DAG graph of all tasks in an orchestration | Yes |
| GET | `/api/chat/orchestrations/<orchestration_id>/recap` | Full text recap of all task results in an orchestration | Yes |

---

## `routes/connections.py`

Blueprint: `connections`. Manages bot-scoped HTTP and database connections stored in local SQLite.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/bots/<bot_id>/connections` | Bot connections management page | Yes |
| GET | `/api/bots/<bot_id>/connections` | List all connections attached to a bot | Yes |
| POST | `/api/bots/<bot_id>/connections` | Create a new connection and attach it to the bot | Yes |
| PUT | `/api/connections/<connection_id>` | Update a connection (any kind) | Yes |
| DELETE | `/api/connections/<connection_id>` | Delete a connection (also removes bot_connections links) | Yes |
| POST | `/api/bots/<bot_id>/connections/<connection_id>/attach` | Attach an existing connection to a bot (idempotent) | Yes |
| DELETE | `/api/bots/<bot_id>/connections/<connection_id>/attach` | Detach a connection from a bot | Yes |
| POST | `/api/connections/parse-openapi` | Parse an OpenAPI schema text and return discovered actions | Yes |
| GET | `/api/connections/<connection_id>/actions` | List OpenAPI actions from a connection's stored schema | Yes |
| POST | `/api/connections/<connection_id>/test` | Test a connection (HTTP or database) | Yes |

---

## `routes/events.py`

Blueprint: `events`. **CSRF-exempt** (GET-only stream).

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/events` | SSE stream; emits a JSON snapshot of worker/bot/task counts every 5 seconds | Yes |

The stream format is `data: <json>\n\n`. Clients disconnect to stop receiving. The snapshot covers local SQLite `Worker`, `Bot`, and `Task` tables (not CP data).

---

## `routes/pipelines.py`

Blueprint: `pipelines`. Groups multi-task CP orchestrations into pipeline views.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/pipelines` | Pipeline list page (grouped by `orchestration_id`, CP tasks) | Yes |
| GET | `/pipelines/<orchestration_id>` | Pipeline detail page (tasks, artifacts, status summary, DAG) | Yes |
| GET | `/api/pipelines` | List pipeline groups as JSON | Yes |
| GET | `/api/pipelines/<orchestration_id>` | Get single pipeline detail as JSON | Yes |

A "pipeline" is inferred from CP tasks that share a `metadata.orchestration_id` and have `metadata.source == "saved_launch_pipeline"` or a non-empty `metadata.pipeline_name`.

---

## `routes/projects.py`

Blueprint: `projects`.

### HTML pages

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/projects` | Project list page | Yes |
| GET | `/projects/<project_id>` | Project detail (bots, tasks, GitHub, workspace, connections, data) | Yes |

### Project CRUD

| Method | Path | Description | Auth Required |
|-|-|-|-|
| POST | `/api/projects` | Create a project | Yes |
| POST | `/api/projects/<project_id>/bridges` | Add a project bridge (cross-project context) | Yes |
| DELETE | `/api/projects/<project_id>/bridges/<target_project_id>` | Remove a project bridge | Yes |

### GitHub integration

| Method | Path | Description | Auth Required |
|-|-|-|-|
| POST | `/api/projects/<project_id>/github/pat` | Connect a GitHub PAT to the project | Yes |
| GET | `/api/projects/<project_id>/github/status` | Get GitHub PAT and webhook status | Yes |
| DELETE | `/api/projects/<project_id>/github/pat` | Disconnect GitHub PAT | Yes |
| POST | `/api/projects/<project_id>/github/webhook/secret` | Set webhook secret | Yes |
| DELETE | `/api/projects/<project_id>/github/webhook/secret` | Remove webhook secret | Yes |
| GET | `/api/projects/<project_id>/github/webhook/events` | List recent webhook events | Yes |
| POST | `/api/projects/<project_id>/github/context/sync` | Trigger repo context sync to vault | Yes |
| GET | `/api/projects/<project_id>/github/context/sync` | Get repo context sync status | Yes |
| POST | `/api/projects/<project_id>/github/pr-review/config` | Configure PR review bot | Yes |

### Cloud context & chat tool access

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/projects/<project_id>/git/status` | Local git working tree status | Yes |
| GET | `/api/projects/<project_id>/cloud-context-policy` | Get cloud context policy | Yes |
| PUT | `/api/projects/<project_id>/cloud-context-policy` | Update cloud context policy | Yes |
| GET | `/api/projects/<project_id>/chat-tool-access` | Get chat tool access config | Yes |
| PUT | `/api/projects/<project_id>/chat-tool-access` | Update chat tool access config | Yes |

### Repo workspace

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/projects/<project_id>/repo/workspace` | Get repo workspace config | Yes |
| PUT | `/api/projects/<project_id>/repo/workspace` | Update repo workspace config | Yes |
| GET | `/api/projects/<project_id>/repo/workspace/status` | Get repo workspace status (git status, dirty files) | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/discard-untracked` | Discard untracked files in workspace | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/clone` | Clone a repo into the workspace | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/pull` | Pull latest changes | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/commit` | Commit staged changes | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/push` | Push to remote | Yes |
| POST | `/api/projects/<project_id>/repo/workspace/run` | Run a command in the workspace | Yes |
| GET | `/api/projects/<project_id>/repo/workspace/runs` | List command run history | Yes |
| GET | `/api/projects/<project_id>/repo/workspace/runs/summary` | Summarise run history | Yes |

### Project data (local filesystem)

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/projects/<project_id>/data/files` | List project data files and tree | Yes |
| POST | `/api/projects/<project_id>/data/folders` | Create a project data folder | Yes |
| POST | `/api/projects/<project_id>/data/upload` | Upload files to project data directory | Yes |
| DELETE | `/api/projects/<project_id>/data/path` | Delete a single file/folder (query param `path`) | Yes |
| POST | `/api/projects/<project_id>/data/delete` | Bulk-delete files/folders | Yes |
| POST | `/api/projects/<project_id>/data/ingest` | Start background vault ingest for all project data files | Yes |
| GET | `/api/projects/<project_id>/data/ingest` | Get latest ingest job status | Yes |

### Project database connections

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/projects/<project_id>/connections` | List database connections for the project | Yes |
| POST | `/api/projects/<project_id>/connections` | Create a database connection for the project | Yes |
| DELETE | `/api/projects/<project_id>/connections/<connection_id>` | Delete a project database connection | Yes |
| POST | `/api/projects/<project_id>/connections/<connection_id>/test` | Run a query against the connection | Yes |
| POST | `/api/projects/<project_id>/connections/<connection_id>/schema-ingest` | Inspect DB schema and upsert into vault | Yes |

---

## `routes/tasks.py`

Blueprint: `tasks`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/tasks` | Tasks board (running, queued, recent completed/failed; CP then local fallback) | Yes |
| GET | `/api/tasks` | List tasks; supports query params `status`, `bot_id`, `orchestration_id`, `limit`, `include_content` | Yes |
| GET | `/api/tasks/<task_id>` | Get single task; supports `section` (payload/result/error) and `include_content` | Yes |
| GET | `/api/tasks/<task_id>/download` | Download task section as JSON file | Yes |
| POST | `/api/tasks/<task_id>/retry` | Retry a task (optional new payload in body) | Yes |
| POST | `/api/tasks/<task_id>/cancel` | Cancel a task | Yes |

---

## `routes/users.py`

Blueprint: `users`. All endpoints require **admin** role.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/users` | User management page | Admin only |
| GET | `/api/users` | List all users (no password hashes) | Admin only |
| POST | `/api/users` | Create a new user (email, password, role) | Admin only |
| PUT | `/api/users/<user_id>` | Update user (admin: role/is_active; self: password change) | Yes (admin or self) |
| DELETE | `/api/users/<user_id>` | Delete a user (cannot delete self) | Admin only |

---

## `routes/vault.py`

Blueprint: `vault`.

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/vault` | Vault browser page (filterable by namespace) | Yes |
| POST | `/api/vault/ingest` | Ingest a text item into the vault (JSON body) | Yes |
| POST | `/api/vault/search` | Semantic search (query, namespace, project_id, limit) | Yes |
| POST | `/api/vault/upload` | Upload file, URL, or paste into vault (multipart form; `source_mode=file\|url\|paste`) | Yes |
| GET | `/api/vault/items/<item_id>/detail` | Get item with chunk list and content preview | Yes |
| GET | `/api/vault/namespaces` | List all vault namespaces | Yes |
| POST | `/api/vault/bulk-delete` | Delete multiple vault items by ID list | Yes |

---

## `settings.py`

Blueprint: `settings`. All endpoints require **admin** role.

### Pages & export/import

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/settings` | Settings management page | Admin only |
| GET | `/api/settings/export/yaml` | Download all settings as `nexusai_settings.yaml` (secrets masked) | Admin only |
| GET | `/api/settings/export/json` | Download all settings as `nexusai_settings.json` (secrets masked) | Admin only |
| POST | `/api/settings/import` | Import settings from uploaded YAML or JSON file | Admin only |

### Settings CRUD

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/settings` | List all settings (secrets masked) | Admin only |
| POST | `/api/settings` | Bulk-update settings `{key: value, ...}` | Admin only |
| GET | `/api/settings/<key>` | Get a single setting (masked) | Admin only |
| PUT | `/api/settings/<key>` | Update a single setting value | Admin only |

### API keys and model catalog

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/settings/keys` | List CP API keys | Admin only |
| POST | `/api/settings/keys` | Create or update a CP API key | Admin only |
| DELETE | `/api/settings/keys/<name>` | Delete a CP API key | Admin only |
| GET | `/api/settings/models` | List CP model catalog | Admin only |
| POST | `/api/settings/models` | Add a model to the catalog | Admin only |
| DELETE | `/api/settings/models/<model_id>` | Remove a model from the catalog | Admin only |
| GET | `/api/settings/projects` | List CP projects | Admin only |

### Deployment

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/settings/deploy/status` | Get deploy state (pass `?fetch=1` to refresh git remote) | Admin only |
| POST | `/api/settings/deploy/check` | Force-refresh deploy status | Admin only |
| POST | `/api/settings/deploy/run` | Trigger a blue/green deploy | Admin only |
| POST | `/api/settings/deploy/log/clear` | Clear the deploy log | Admin only |

### Tool catalog

| Method | Path | Description | Auth Required |
|-|-|-|-|
| GET | `/api/settings/tools` | List all tools with enabled status, install support, and runtime status | Admin only |
| PUT | `/api/settings/tools` | Bulk-update enabled tools `{"enabled_tools": [...]}` | Admin only |
| PUT | `/api/settings/tools/<tool_id>` | Toggle a single tool on/off | Admin only |
| POST | `/api/settings/tools/preset/<preset_id>` | Apply a tool preset | Admin only |
| POST | `/api/settings/tools/test` | Run availability checks for enabled (or all) tools | Admin only |
| POST | `/api/settings/tools/install/<tool_id>` | Queue a curated tool install (async; returns immediately) | Admin only |
| GET | `/api/settings/tools/install/<tool_id>/status` | Poll the latest install job status for a tool | Admin only |

---

## Known Issues

- **`GET /api/workers`** queries only local SQLite (not the control plane), while the workers page prefers CP data. The API and page can return different worker lists.
- **`GET /events` SSE** snapshots only local SQLite counters (workers, bots, tasks). If the dashboard is running in CP-primary mode, these counts may lag behind CP reality.
- **`POST /api/bots/import`** must appear before `/api/bots/<bot_id>` in route registration to avoid Flask matching `import` as a bot ID. This is handled correctly in the current code but would break if route order changed.
- **`DELETE /api/projects/<project_id>/data/path`** uses a query parameter for the path rather than a path segment, which is non-standard for a DELETE route.
- **Duplicate project connection endpoints:** Project database connections are managed both in `routes/projects.py` (full CRUD with project guard) and in `routes/connections.py` (bot-scoped connections). The two systems use the same `Connection` table but different association tables (`ProjectConnection` vs `BotConnection`).
- **`POST /api/settings/tools/install/<tool_id>`** returns HTTP 202 in all cases — both when an install is freshly started and when one is already running. Callers must poll `/status` to distinguish the two.

