# Control Plane API Reference

All routes are prefixed with `/v1/`. Auth: set `X-Nexus-API-Key: <token>` header when `CONTROL_PLANE_API_TOKEN` is configured. The `/health` endpoint and `POST /v1/bots/{id}/trigger` are always exempt.

---

## Tasks — `/v1/tasks`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/tasks` | Create and dispatch a task | `bot_id`, `payload`, `metadata`, `depends_on` | 404 bot not found, 400 invalid |
| `GET` | `/v1/tasks` | List tasks | `orchestration_id`, `status`, `bot_id`, `limit` (max 1000), `include_content` | — |
| `GET` | `/v1/tasks/{id}` | Get task by ID | — | 404 |
| `PUT` | `/v1/tasks/{id}` | Update task fields | `status`, `result`, `error` | 404, 400 |
| `DELETE` | `/v1/tasks/{id}` | Cancel/delete task | — | 404 |
| `POST` | `/v1/tasks/{id}/retry` | Retry a failed task | `payload` (optional override) | 404, 400 |
| `GET` | `/v1/tasks/{id}/artifacts` | List artifacts for task | — | 404 |
| `GET` | `/v1/tasks/{id}/artifacts/{artifact_id}` | Get single artifact | — | 404 |
| `GET` | `/v1/tasks/{id}/bot-run` | Get bot run for task | — | 404 |

---

## Bots — `/v1/bots`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/bots` | Register/upsert a bot | Full `Bot` model | 400 validation error (structured detail with `reason_code`) |
| `GET` | `/v1/bots` | List all bots | — | — |
| `GET` | `/v1/bots/{id}` | Get bot by ID | — | 404 |
| `PUT` | `/v1/bots/{id}` | Update bot | Full `Bot` model | 404, 400 |
| `DELETE` | `/v1/bots/{id}` | Remove bot | — | 404 |
| `POST` | `/v1/bots/{id}/enable` | Enable bot | — | 404 |
| `POST` | `/v1/bots/{id}/disable` | Disable bot | — | 404 |
| `POST` | `/v1/bots/{id}/trigger` | Trigger a bot run directly | `payload`, `metadata` | 404, 400 |
| `GET` | `/v1/bots/{id}/runs` | List bot run history | `limit` | 404 |
| `GET` | `/v1/bots/{id}/runs/{run_id}` | Get specific run | — | 404 |
| `GET` | `/v1/bots/{id}/runs/{run_id}/artifacts` | List run artifacts | — | 404 |

---

## Workers — `/v1/workers`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/workers` | Register worker (self-registration) | Full `Worker` model | 400 |
| `GET` | `/v1/workers` | List all workers | — | — |
| `GET` | `/v1/workers/{id}` | Get worker by ID | — | 404 |
| `PUT` | `/v1/workers/{id}` | Update worker | Full `Worker` model | 404 |
| `DELETE` | `/v1/workers/{id}` | Remove worker | — | 404 |
| `POST` | `/v1/workers/{id}/heartbeat` | Worker heartbeat + metrics | `metrics: {queue_depth, gpu_utilization}` | 404 |

---

## Projects — `/v1/projects`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/projects` | Create project | `Project` model | 400 |
| `GET` | `/v1/projects` | List projects | — | — |
| `GET` | `/v1/projects/{id}` | Get project | — | 404 |
| `PUT` | `/v1/projects/{id}` | Update project | `Project` model | 404, 400 |
| `DELETE` | `/v1/projects/{id}` | Delete project | — | 404 |
| `POST` | `/v1/projects/{id}/github/connect` | Connect GitHub PAT | `token`, `repo_full_name`, `validate` | 400, 404 |
| `DELETE` | `/v1/projects/{id}/github/disconnect` | Disconnect GitHub | — | 404 |
| `POST` | `/v1/projects/{id}/github/webhook-secret` | Set webhook HMAC secret | `secret` | 404 |
| `POST` | `/v1/projects/{id}/github/sync` | Sync repo context to vault | `branch`, `sync_mode` (full/update), `namespace` | 404, 400 |
| `POST` | `/v1/projects/{id}/github/webhook` | Receive GitHub webhook | Webhook payload, `X-Hub-Signature-256`, `X-GitHub-Delivery` | 400, 401 |
| `POST` | `/v1/projects/{id}/github/pr-review` | Configure PR review bot | `enabled`, `bot_id` | 404 |
| `GET` | `/v1/projects/{id}/repo-workspace` | Get workspace config | — | 404 |
| `POST` | `/v1/projects/{id}/repo-workspace` | Update workspace config | Workspace config fields | 404 |
| `POST` | `/v1/projects/{id}/repo-workspace/clone` | Clone repo | `branch` | 404, 400 |
| `POST` | `/v1/projects/{id}/repo-workspace/pull` | Pull latest | `branch` | 404, 400 |
| `POST` | `/v1/projects/{id}/repo-workspace/run` | Run guarded command | `command`, `use_temp_workspace` | 404, 400, 403 |
| `GET` | `/v1/projects/{id}/repo-workspace/status` | Git status | — | 404 |

---

## Chat — `/v1/chat`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/chat/conversations` | Create conversation | `title`, `scope`, `project_id`, `default_bot_id`, `tool_access_*` | 400 |
| `GET` | `/v1/chat/conversations` | List conversations | `project_id`, `scope`, `archived`, `limit` | — |
| `GET` | `/v1/chat/conversations/{id}` | Get conversation | — | 404 |
| `PUT` | `/v1/chat/conversations/{id}` | Update conversation | Conversation fields | 404 |
| `DELETE` | `/v1/chat/conversations/{id}` | Archive conversation | — | 404 |
| `POST` | `/v1/chat/conversations/{id}/messages` | Post message (streaming SSE) | `content`, `bot_id`, `context_items`, `include_project_context`, `use_workspace_tools`, `attachments`, `is_assign` | 404, 400 |
| `GET` | `/v1/chat/conversations/{id}/messages` | List messages | `limit` | 404 |
| `POST` | `/v1/chat/conversations/{id}/assign` | @assign orchestration | `instruction`, `pm_bot_id`, `context_items`, `conversation_brief` | 404, 400 |
| `PUT` | `/v1/chat/conversations/{id}/tool-access` | Update tool access flags | `enabled`, `filesystem`, `repo_search` | 404 |
| `GET` | `/v1/chat/conversations/{id}/memory` | Semantic search in conv memory | `query`, `limit` | 404 |

---

## Vault — `/v1/vault`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/vault/items` | Ingest new vault item | `title`, `content`, `namespace`, `project_id`, `source_type`, `source_ref`, `chunk_size`, `chunk_overlap` | 400; rate limited 30/min, max 2MB |
| `POST` | `/v1/vault/items/upsert` | Upsert by source_ref | Same as ingest | 400 |
| `GET` | `/v1/vault/items` | List vault items | `namespace`, `project_id`, `limit` | — |
| `GET` | `/v1/vault/items/{id}` | Get vault item | — | 404 |
| `DELETE` | `/v1/vault/items/{id}` | Delete vault item + chunks | — | 404 |
| `POST` | `/v1/vault/search` | Semantic search | `query`, `namespace`, `project_id`, `limit` | 400 |
| `GET` | `/v1/vault/items/{id}/chunks` | List chunks for item | — | 404 |

---

## Keys — `/v1/keys`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/keys` | Upsert API key (Fernet encrypted) | `name`, `provider`, `value` | 400 |
| `GET` | `/v1/keys` | List keys (name + provider only, no decryption) | — | — |
| `GET` | `/v1/keys/{name}` | Get key metadata (no plaintext value) | — | 404 |
| `DELETE` | `/v1/keys/{name}` | Delete key | — | 404 |

---

## Model Catalog — `/v1/models`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `POST` | `/v1/models` | Add/upsert model to catalog | `CatalogModel` | 400 |
| `GET` | `/v1/models` | List catalog models | `enabled_only` | — |
| `GET` | `/v1/models/{id}` | Get model by ID | — | 404 |
| `PUT` | `/v1/models/{id}` | Update model | `CatalogModel` | 404 |
| `DELETE` | `/v1/models/{id}` | Remove model | — | 404 |

---

## Audit — `/v1/audit`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `GET` | `/v1/audit/events` | List recent audit events | `limit` (max 1000, default 100) | — |

---

## Database — `/v1/database`

| Method | Path | Description | Key Params | Errors |
|--------|------|-------------|------------|--------|
| `GET` | `/v1/database/schema` | Get current internal DB schema | — | — |
| `POST` | `/v1/database/connections` | Add external DB connection | `name`, `kind`, `connection_string`, `config_json` | 400 |
| `GET` | `/v1/database/connections` | List connections | — | — |
| `GET` | `/v1/database/connections/{id}` | Get connection | — | 404 |
| `PUT` | `/v1/database/connections/{id}` | Update connection | — | 404 |
| `DELETE` | `/v1/database/connections/{id}` | Delete connection | — | 404 |
| `POST` | `/v1/database/connections/{id}/test` | Test connection | — | 404, 400 |

---

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status": "ok"}`. Auth-exempt. |

---

## Security Guards

High-risk endpoints enforce body size and per-IP rate limits via `guards.py`. Override via environment variables:

- `CP_MAX_BODY_BYTES_<ROUTE>` — max body size in bytes
- `CP_RATE_LIMIT_<ROUTE>_COUNT` — max requests per window
- `CP_RATE_LIMIT_<ROUTE>_WINDOW_SECONDS` — window size

Route names are uppercased and hyphen-replaced: e.g., `VAULT_INGEST` for `vault_ingest`.

Default limits: vault ingest = 30 req/min, 2MB body; chat message = configurable.
