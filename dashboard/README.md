# Dashboard

The dashboard is a **Flask** web application that provides the NexusAI operator interface. All data operations proxy through `cp_client.py` to the Control Plane REST API.

---

## Architecture

```
Browser → Flask (port 5000) → cp_client.py → Control Plane API (port 8000)
                            ↘ dashboard/db.py (SQLAlchemy, local SQLite)
                                └── User accounts, Connections, BotConnections
```

The dashboard has its own SQLite tables (User, Connection, BotConnection) managed via SQLAlchemy, separate from the control plane's aiosqlite tables. Both point to the same `data/nexusai.db` file.

---

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask factory `create_app()`, blueprint registration, CSRF, LoginManager |
| `auth.py` | Login/logout handlers, session management |
| `bot_launch.py` | Helpers for launching bots from the dashboard |
| `connections_service.py` | Connection schema injection, OpenAPI parsing |
| `cp_client.py` | HTTP client wrapping all Control Plane API calls |
| `db.py` | SQLAlchemy `get_db()`, `init_db()` |
| `deploy_manager.py` | Blue/green nginx config swap logic |
| `models.py` | SQLAlchemy models: `User`, `Connection`, `BotConnection` |
| `onboarding.py` | First-run setup flow |
| `project_data.py` | Project data vault file management |
| `project_data_ingest.py` | Background ingest tasks for project data |
| `settings.py` | Settings page blueprint |

---

## Auth (`auth.py`)

- Uses `Flask-Login` with `User` model loaded from SQLite.
- Session lifetime: 60 minutes (configurable via `session_timeout_minutes` setting).
- CSRF protection via `Flask-WTF` on all POST forms.
- SSE events route is CSRF-exempt (GET-only).
- `api_login_post` and `api_logout_post` are CSRF-exempt for programmatic API access.
- Registration is gated by `allow_user_registration` setting (default: `false`).

---

## CP Client (`cp_client.py`)

Central HTTP client for the dashboard. Wraps all Control Plane API calls with:
- Auth header injection (`X-Nexus-API-Key`)
- Error handling and status-code mapping
- JSON serialisation/deserialisation

All dashboard routes use `cp_client` rather than calling the CP API directly.

---

## Deploy Manager (`deploy_manager.py`)

Supports blue/green dashboard deploys via nginx upstream swap:
- Reads current active slot from nginx config in `data/nginx/`
- Writes new config pointing to the inactive slot
- Calls `nginx -s reload` to apply
- Used by the Settings → Deploy page in the dashboard

See [docs/DEPLOY_BLUEGREEN.md](../docs/DEPLOY_BLUEGREEN.md) for operator instructions.

---

## Connections Service (`connections_service.py`)

Manages bot-scoped HTTP/OpenAPI and database connections:
- Parses OpenAPI specs attached to connections
- Builds schema context strings injected into bot prompts
- Optionally fetches live JSON from HTTP connections before inference
- OpenAPI action discovery for in-dashboard connection test runner

---

## Database Models (`models.py`)

| Model | Table | Description |
|-------|-------|-------------|
| `User` | `users` | Dashboard user: username, password hash, role |
| `Connection` | `connections` | External connection: name, kind, base_url, auth config, schema |
| `BotConnection` | `bot_connections` | M:M: bot_ref → connection_id |

---

## Routes Overview

See [`routes/README.md`](routes/README.md) for the full route table.

| Blueprint | Prefix | Description |
|-----------|--------|-------------|
| `auth` | `/auth` | Login, logout |
| `onboarding` | `/onboarding` | First-run setup |
| `workers` | `/workers` | Worker list, detail, graphs |
| `bots` | `/bots` | Bot list, detail, editor, test runs |
| `tasks` | `/tasks` | Task board, detail, artifacts |
| `projects` | `/projects` | Project list, detail, GitHub, workspace |
| `pipelines` | `/pipelines` | Pipeline run tracking |
| `chat` | `/chat` | Conversation UI, @assign, SSE streaming |
| `connections` | `/connections` | Bot connection management |
| `vault` | `/vault` | Vault item list, preview, search |
| `users` | `/users` | User management |
| `events` | `/events` | SSE event stream |
| `settings` | `/settings` | Platform settings, deploy |

---

## Known Issues

- Dashboard SQLAlchemy models and aiosqlite models both write to the same SQLite file with no coordination layer — potential write conflicts under load.
- `deploy_manager.py` calls `nginx -s reload` as a subprocess — requires nginx to be running in the same container.
- `cp_client.py` does not implement retries — transient CP failures surface as dashboard errors.
