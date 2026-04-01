# NexusAI Dashboard

The **dashboard** is a Flask web application that provides the operator-facing UI and REST API for the NexusAI system. It communicates with a separate **control plane** service for all AI/task orchestration, and maintains its own local SQLite database for users, workers, bots, connections, and settings.

---

## Tech Stack

| Layer | Library / Tool |
|---|---|
| Web framework | Flask 3.x |
| Authentication | Flask-Login + Flask-WTF (CSRF) + bcrypt |
| ORM | SQLAlchemy (declarative, `sessionmaker`) |
| Database | SQLite (via `DATABASE_URL`; any SQLAlchemy URL accepted) |
| HTTP client | `requests` (synchronous) |
| Secrets encryption | `cryptography` Fernet (AES-128-CBC) |
| OpenAPI parsing | `yaml` + `json` |
| Settings store | `shared.settings_manager.SettingsManager` singleton |
| SSE streaming | Flask `Response` + `stream_with_context` |

---

## Module Reference

### `app.py` — Application Factory

`create_app()` builds and returns the Flask app:

1. **Config** — `SECRET_KEY`, `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE=Lax`, `WTF_CSRF_ENABLED=True`, `PERMANENT_SESSION_LIFETIME=60 min`.
2. **Extensions** — `CSRFProtect`, `LoginManager` (login view → `auth.login_get`).
3. **Database** — calls `init_db()` on startup (creates all SQLAlchemy tables).
4. **Settings singleton** — initialises `SettingsManager.instance(db_path=…)` pointing at `data/nexusai.db`.
5. **Blueprints registered** — `auth`, `onboarding`, `workers`, `bots`, `tasks`, `projects`, `pipelines`, `chat`, `connections`, `vault`, `users`, `events`, `settings`.
6. **CSRF exemptions** — `events_bp` (GET-only SSE), `api_login_post`, `api_logout_post`.
7. **Main blueprint** — inline `Blueprint("main")` handles `GET /` (overview page) and `GET /health`.
8. **Middleware**
   - `before_request`: records request start time in `g._request_start_ts`.
   - `after_request`: logs a `WARNING` for requests slower than `DASHBOARD_SLOW_REQUEST_SECONDS` (default 1.5 s).
   - `before_request` (session inactivity): reads `session_timeout_minutes` from `SettingsManager` (default 60); logs out idle users and redirects to login.

The module-level `app = create_app()` is used by WSGI servers; `__main__` runs on `0.0.0.0:DASHBOARD_PORT` (default 5000).

---

### `auth.py` — Authentication

Blueprint: `auth` (no URL prefix).

**How it works:**
- Login form uses `flask_wtf.FlaskForm` with email + password fields and CSRF protection.
- `_authenticate_user()` queries the `users` table (case-insensitive email), checks `is_active`, then verifies the password with `bcrypt.checkpw`.
- On success, `login_user(user, remember=False)` is called and `session["last_activity_ts"]` is set (used by the inactivity timeout middleware).
- Open-redirect protection: the `?next=` parameter is validated against `urlparse`; only safe relative paths are accepted.
- A parallel JSON API (`POST /api/auth/login`, `GET /api/auth/session`, `POST /api/auth/logout`) supports SPA or programmatic clients; both `/api/auth/login` and `/api/auth/logout` are CSRF-exempt.

**Routes:**

| Method | Path | Description |
|---|---|---|
| GET | `/login` | Show login form (redirects to onboarding if no users exist) |
| POST | `/login` | Process login form |
| POST | `/api/auth/login` | JSON login (CSRF-exempt) |
| GET | `/api/auth/session` | Returns current session user info |
| POST | `/api/auth/logout` | JSON logout (CSRF-exempt) |
| GET | `/logout` | HTML logout + redirect |

---

### `bot_launch.py` — Launch Profile Helpers

Pure utility module (no Flask routes). Used by `routes/bots.py`, `routes/tasks.py`, and the overview page.

Key functions:

| Function | Purpose |
|---|---|
| `normalize_launch_profile(bot)` | Extracts and normalises a bot's `launch_profile` dict from `routing_rules` or top-level fields. Returns `None` if no valid profile exists. |
| `normalize_launch_payload(bot, payload)` | Applies `input_transform` or `output_contract` template substitution (`{{payload.field}}` syntax) to a launch payload dict. |
| `launchable_bots(bots, *, surface)` | Filters a list of bot dicts to only those with an enabled launch profile visible on the requested surface (`"overview"` or `"tasks"`). Returns sorted list. |

Template expressions use `{{payload.some.nested.field}}` and `json:payload.field_json` for JSON-parsing stored values.

---

### `connections_service.py` — External Connection Helpers

Provides auth secret handling, OpenAPI schema parsing, and connectivity tests for `http` and `database` connections. No Flask routes — consumed by `routes/connections.py` and `routes/bots.py`.

**Secret handling:**
- Secret fields: `api_key`, `bearer_token`, `password`.
- `normalize_auth_payload()` — encrypts incoming secrets using Fernet (key derived from SHA-256 of `NEXUSAI_SECRET_KEY`), prefixing stored values with `enc:`.
- `resolve_auth_payload()` — decrypts stored values.
- `mask_auth_payload()` — replaces secret values with `[REDACTED]` for display.

**OpenAPI parsing:**
- `parse_openapi_actions(schema_text)` — parses JSON or YAML OpenAPI schema and returns a list of `{operation_id, method, path}` dicts for all HTTP operations.

**HTTP connection test (`test_http_connection`):**
- Uses `urllib.request` (no extra library).
- Supports auth types: `api_key` (header or query), `bearer` (Authorization header), `basic` (Base64).
- Supports custom headers, query params, JSON body, SSL verification toggle.

**Database connection test (`test_database_connection`):**
- Uses SQLAlchemy `create_engine` + `text()`.
- Enforces `readonly` guard: only `SELECT`/`WITH` queries allowed when `readonly=True`.

**Database schema inspection (`inspect_database_schema`):**
- Returns a full schema snapshot (tables, columns, PKs, FKs, indexes, views) across all non-system schemas.
- Used by project connections to generate vault documents.

**DSN normalisation (`normalize_database_dsn`):**
- Converts `postgres://` → `postgresql+psycopg2://`.
- Parses key=value connection strings (ODBC/Npgsql style) into SQLAlchemy URLs.

---

### `cp_client.py` — Control Plane Client

`CPClient` is a thin synchronous HTTP client that wraps every control plane REST call.

**Config (from environment):**

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_URL` | `http://control_plane:8000` | Base URL |
| `CP_TIMEOUT` | `2` | Default request timeout (seconds) |
| `CP_CHAT_TIMEOUT` | `900` | Timeout for chat/message endpoints |
| `CP_INGEST_TIMEOUT` | `1800` | Timeout for sync/clone/run endpoints |
| `CONTROL_PLANE_API_TOKEN` | `""` | Token sent as `X-Nexus-API-Key` header |

**Error handling:** All methods return `None` on failure (HTTP error or connection error); the last error is stored in `_last_error` and readable via `last_error()`. `unavailable_reason()` returns a human-readable string describing 401/403/404/timeout failures.

**Method groups:**

| Group | Methods |
|---|---|
| Health | `health()` |
| Workers | `list_workers`, `get_worker`, `register_worker`, `update_worker`, `heartbeat_worker`, `delete_worker` |
| Bots | `list_bots`, `get_bot`, `create_bot`, `update_bot`, `delete_bot`, `list_bot_runs`, `list_bot_artifacts`, `get_bot_artifact` |
| Tasks | `list_tasks`, `get_task`, `retry_task`, `cancel_task`, `create_task`, `create_task_full` |
| Projects | `list_projects`, `create_project`, `get_project`, `delete_project` |
| Project bridges | `add_project_bridge`, `remove_project_bridge` |
| Project GitHub | `connect_project_github_pat`, `get_project_github_status`, `disconnect_project_github_pat`, `set_project_github_webhook_secret`, `delete_project_github_webhook_secret`, `list_project_github_webhook_events`, `sync_project_github_context`, `get_project_github_context_sync_status`, `configure_project_github_pr_review` |
| Project cloud policy | `get_project_cloud_context_policy`, `update_project_cloud_context_policy` |
| Project chat tool | `get_project_chat_tool_access`, `update_project_chat_tool_access` |
| Project repo workspace | `get_project_repo_workspace`, `update_project_repo_workspace`, `get_project_repo_workspace_status`, `discard_project_repo_workspace_untracked`, `clone_project_repo_workspace`, `pull_project_repo_workspace`, `commit_project_repo_workspace`, `push_project_repo_workspace`, `run_project_repo_workspace_command`, `apply_project_assignment_to_repo_workspace`, `review_project_assignment_files`, `list_project_repo_workspace_runs`, `summarize_project_repo_workspace_runs`, `list_project_orchestration_workspaces` |
| Models | `list_models`, `create_model`, `delete_model` |
| API keys | `list_keys`, `upsert_key`, `delete_key` |
| Chat | `list_conversations`, `create_conversation`, `delete_conversation`, `archive_conversation`, `restore_conversation`, `list_messages`, `post_message`, `mark_pm_run_failed`, `update_conversation_tool_access` |
| Vault | `list_vault_items`, `ingest_vault_item`, `upsert_vault_item`, `search_vault`, `get_vault_item`, `list_vault_chunks`, `delete_vault_item`, `list_vault_namespaces` |
| Diagnostics | `probe_paths` |

A module-level singleton is returned by `get_cp_client()` (lazily initialised).

---

### `db.py` — Database Session Management

- Reads `DATABASE_URL` (defaults to `sqlite:///data/nexusai.db`).
- Creates a `sessionmaker` bound to a SQLAlchemy engine (`check_same_thread=False` for SQLite).
- `init_db()` — thread-safe one-time `Base.metadata.create_all()`; tolerates SQLite race conditions on first startup.
- `get_db()` — returns a new `Session`; **caller must close it**.

---

### `models.py` — ORM Models

All tables are managed by SQLAlchemy declarative `Base`.

| Table | Model | Key columns |
|---|---|---|
| `users` | `User` | `id` (PK), `email` (unique), `password_hash`, `role` (`admin`/`user`), `is_active`, `created_at` |
| `workers` | `Worker` | `id`, `name`, `host`, `port` (default 8001), `status`, `capabilities` (JSON text), `metrics` (JSON text), `enabled` |
| `bots` | `Bot` | `id`, `name`, `role`, `priority`, `enabled`, `backends` (JSON text), `routing_rules` (JSON text) |
| `connections` | `Connection` | `id`, `name`, `kind` (`http`/`database`), `description`, `config_json`, `auth_json`, `schema_text`, `enabled`, `created_at`, `updated_at` |
| `bot_connections` | `BotConnection` | `id`, `bot_ref` (string, not FK), `connection_id`, `created_at` |
| `project_connections` | `ProjectConnection` | `id`, `project_ref` (string, not FK), `connection_id`, `created_at` |
| `tasks` | `Task` | `id`, `bot_id`, `payload` (JSON text), `metadata_json`, `status`, `result`, `error`, `created_at`, `updated_at` |
| `settings` | `Setting` | `key` (PK), `value`, `updated_at` |

`User` implements the Flask-Login `UserMixin` interface directly (no mixin import — `is_authenticated`, `is_anonymous`, `get_id()` are defined manually).

`models.py` also contains onboarding helper functions (`admin_exists`, `create_user`, `create_worker`, `set_setting`, `get_setting`) that use lazy imports of `dashboard.db` to avoid circular imports.

---

### `onboarding.py` — First-Run Wizard

Blueprint: `onboarding`, prefix `/onboarding`.

A 5-step wizard gated behind "no admin user exists". Each GET step checks `admin_exists()` and redirects to `/login` if setup is already complete. Steps are tracked via `session["wizard"]`.

| Step | Path | What happens |
|---|---|---|
| 1 | `/onboarding/step1` | Welcome screen |
| 2 | `/onboarding/step2` | Create admin account (email + bcrypt-hashed password, min 8 chars) |
| 3 | `/onboarding/step3` | Select LLM backend (`ollama`, `openai`, `claude`, `gemini`) — stored via `set_setting("llm_backend", …)` |
| 4 | `/onboarding/step4` | Optionally register first worker (name, host, port) — skippable |
| 5 | `/onboarding/step5` | Completion screen; clears `session["wizard"]` and redirects to login |

Steps enforce a forward-only flow: a GET to step N redirects to step N-1 if the wizard hasn't advanced that far.

---

### `project_data.py` — Project Data Filesystem

Manages a per-project local filesystem layout at `NEXUSAI_PROJECT_DATA_ROOT/<project_id>/` (defaults to `data/project_data/<project_id>/`).

Default subdirectories created on first access: `docs`, `inbox`, `exports`, `notes`.

Key functions:

| Function | Purpose |
|---|---|
| `project_data_base_dir()` | Returns configured or default base path |
| `ensure_project_data_layout(project_id)` | Creates project root + default subdirs if absent |
| `resolve_project_data_path(project_id, relative_path)` | Resolves and validates a relative path (path-escape guard) |
| `create_project_data_folder(project_id, parent_path, folder_name)` | Creates a new subdirectory; uses `secure_filename` |
| `save_project_data_upload(project_id, target_path, storage)` | Saves an uploaded `FileStorage` object with deduplication (e.g. `(1) file.txt`) |
| `delete_project_data_path(project_id, relative_path)` | Deletes a file or directory (`shutil.rmtree` for dirs) |
| `delete_project_data_paths(project_id, paths)` | Bulk delete; sorted deepest-first to avoid double-delete errors |
| `build_project_data_tree(project_id)` | Returns a nested tree dict (max depth 6, max 500 entries) |
| `list_project_data_files(project_id)` | Flat list of all files/dirs with size and mtime |

---

### `project_data_ingest.py` — Vault Ingest Job Runner

Runs a background thread that walks a project's data directory and upserts every text file into the control plane vault.

- In-memory job store `_JOBS` (dict keyed by `job_id`), protected by a threading lock.
- `start_project_data_ingest(project_id, namespace, max_bytes)` — returns immediately if an identical job is already `queued`/`running`; otherwise creates a job record and starts a daemon thread.
- The thread skips binary file extensions and files exceeding `effective_max_bytes` (default 5 MB; hard cap 25 MB from env vars `NEXUSAI_PROJECT_DATA_INGEST_DEFAULT_MAX_BYTES` / `NEXUSAI_PROJECT_DATA_INGEST_HARD_MAX_BYTES`).
- Each file is upserted via `cp_client.upsert_vault_item()` with `source_type="file"`.
- Job status fields: `queued → running → completed / completed_with_errors / failed`.
- `latest_job_for_project(project_id)` — returns the most recently updated job for a project.

---

### `settings.py` — Settings Blueprint

Blueprint: `settings` (no URL prefix). All endpoints require `admin` role.

Wraps `shared.settings_manager.SettingsManager` (singleton, backed by the same SQLite database as the ORM). Settings are grouped into categories: `general`, `auth`, `llm`, `logging`, `advanced`.

**Deploy management** — integrates `DeployManager` (see below) for blue/green deploys.

**Tool catalog** — exposes endpoints for listing, enabling/disabling, testing, and installing tools from `shared.tool_catalog`. Platform-specific install plans are generated for Windows (winget) and Linux (apt/dnf/yum/rustup/dotnet-install) at runtime.

Key routes: `GET /settings`, `GET/POST /api/settings`, `GET/PUT /api/settings/<key>`, import/export YAML+JSON, API keys, model catalog, deploy management, tool catalog management.

---

### `deploy_manager.py` — Blue/Green Deployment Manager

`DeployManager` is a singleton that manages dashboard-triggered blue/green deploys.

**Gate checks** (all must pass before a deploy starts):
1. `NEXUSAI_DEPLOY_ENABLE=1`
2. `NEXUSAI_DEPLOY_RUN_CMD` is set (the shell command to execute)
3. `NEXUSAI_DEPLOY_STRATEGY=bluegreen`

**State** is persisted to `data/deploy_status.json` so it survives restarts. Fields: `state` (idle/running/succeeded/failed), `run_id`, `deployed_commit`, `started_at`, `finished_at`, `last_error`, `last_run_by`, `log_tail` (last 200 lines).

**`status(refresh_remote)`** — returns full state including local/remote git commits (`git rev-parse HEAD` and `git rev-parse origin/main`), active color (from `data/active_color.txt`), next color, `commits_differ`, `deploy_allowed`, and `deploy_blocked_reason`.

**`start(requested_by)`** — validates gate, starts a daemon thread running the deploy command via `subprocess.Popen(shell=True)`. Output is streamed line-by-line into `log_tail`.

---

## Environment Variables

| Variable | Default | Required | Purpose |
|---|---|---|---|
| `NEXUSAI_SECRET_KEY` | `dev-secret-change-in-production` | **Yes (production)** | Flask session secret; also used for Fernet connection secret encryption |
| `DATABASE_URL` | `sqlite:///data/nexusai.db` | No | SQLAlchemy URL for the dashboard's own database |
| `CONTROL_PLANE_URL` | `http://control_plane:8000` | **Yes** | Base URL of the control plane service |
| `CONTROL_PLANE_API_TOKEN` | `""` | **Yes** | API token sent to control plane as `X-Nexus-API-Key` |
| `CP_TIMEOUT` | `2` | No | Default CP request timeout (seconds) |
| `CP_CHAT_TIMEOUT` | `900` | No | Timeout for chat/message requests |
| `CP_INGEST_TIMEOUT` | `1800` | No | Timeout for sync/clone/workspace run requests |
| `DASHBOARD_PORT` | `5000` | No | Port when running via `__main__` |
| `DASHBOARD_SLOW_REQUEST_SECONDS` | `1.5` | No | Log threshold for slow requests |
| `NEXUSAI_PROJECT_DATA_ROOT` | `data/project_data` | No | Root directory for per-project uploaded files |
| `NEXUSAI_PROJECT_DATA_INGEST_DEFAULT_MAX_BYTES` | `5000000` | No | Default per-file size limit for vault ingest |
| `NEXUSAI_PROJECT_DATA_INGEST_HARD_MAX_BYTES` | `25000000` | No | Hard per-file size ceiling for vault ingest |
| `NEXUSAI_DEPLOY_ENABLE` | `""` | No | Set to `1` to enable deploy API |
| `NEXUSAI_DEPLOY_RUN_CMD` | `""` | No | Shell command executed for blue/green deploy |
| `NEXUSAI_DEPLOY_STRATEGY` | `""` | No | Must be `bluegreen` to allow deploys |
| `NEXUSAI_COMPOSE_PROJECT_NAME` | `nexusai` | No | Docker Compose project name (used by tool install and deploy) |
| `NEXUSAI_CLOUD_CONTEXT_POLICY` | `""` | **Yes (production)** | Should be `block` or `redact`; checked on overview setup checklist |
| `NEXUS_WORKER_CLOUD_CONTEXT_POLICY` | `""` | **Yes (production)** | Same, for worker containers |
| `NEXUSAI_REPO_RUNTIME_TOOLCHAINS` | `""` | No | Comma-separated toolchains baked into control plane image |

---

## How to Run

```bash
# Install dependencies
pip install -e .

# Set required environment variables
export NEXUSAI_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export CONTROL_PLANE_URL="http://localhost:8000"
export CONTROL_PLANE_API_TOKEN="your-token"

# Run (development)
python -m dashboard.app
# or
flask --app dashboard.app run --port 5000

# Run (production — gunicorn example)
gunicorn "dashboard.app:app" --bind 0.0.0.0:5000 --workers 2
```

The database (`data/nexusai.db`) is created automatically on first start. Navigate to `http://localhost:5000` — if no admin exists, you are redirected to the onboarding wizard.

---

## Known Issues / Refactor Notes

- **`bot_id` type mismatch:** Control plane uses string bot IDs (slugs); local SQLite `bots` table uses integer PKs. Several fallback code paths check `str(bot_id).isdigit()` to decide whether to look up the local DB. This dual-identity is confusing and error-prone.
- **No foreign keys:** `bot_connections.bot_ref` and `project_connections.project_ref` are plain strings, not DB foreign keys. Referential integrity is enforced manually in application code.
- **`Task` table is a local mirror:** The `tasks` table in SQLite is used only as a fallback when the control plane is unavailable. It can fall out of sync with CP state and is not reliably updated.
- **Global `_client` singleton in `cp_client.py`:** `get_cp_client()` uses a module-level singleton. Changing `CONTROL_PLANE_URL` at runtime has no effect; the client must be restarted.
- **`session["wizard"]` state:** Onboarding wizard state is stored in the Flask cookie session. If a user opens two tabs or lets the session expire mid-wizard, they lose progress.
- **`deploy_manager.py` reads from `data/active_color.txt`:** This file must be written by the deploy script itself; there is no provision to create or validate it from within the dashboard.
- **`settings.py` is very large (1192 lines):** The tool install/check logic (Windows vs Linux platform detection, winget, apt, rustup, dotnet-install) would benefit from extraction into a dedicated `tool_installer.py` module.
- **Inactivity timeout ignores `session_timeout_minutes` type safety:** `int(timeout_raw)` can raise `ValueError` if the setting is corrupt; the `except Exception` silently falls back to 60 minutes.
- **`connections_service.py` uses `urllib.request` for HTTP tests** while the rest of the codebase uses `requests`. Inconsistent.
- **`cp_client.py` does not implement retries** — transient CP failures surface as dashboard errors immediately.
