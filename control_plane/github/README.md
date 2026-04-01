# GitHub — `control_plane/github/`

Persistent storage layer for incoming GitHub webhook events. Handles
idempotent deduplication, schema migration, retention pruning, and per-project
event listing.

---

## Files

| File | Purpose |
|---|---|
| `webhook_store.py` | `GitHubWebhookStore` class — SQLite storage for GitHub webhook payloads |

---

## SQLite Schema

Table: **`github_webhook_events`** (in `data/nexusai.db` by default)

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | `TEXT PRIMARY KEY` | No | UUID v4 generated at ingest time |
| `project_id` | `TEXT NOT NULL` | No | NexusAI project that owns this webhook |
| `delivery_id` | `TEXT` | Yes | GitHub `X-GitHub-Delivery` header value |
| `event_type` | `TEXT NOT NULL` | No | GitHub event name (e.g. `push`, `pull_request`) |
| `action` | `TEXT` | Yes | Webhook action sub-type (e.g. `opened`, `closed`) |
| `repository_full_name` | `TEXT` | Yes | `owner/repo` from the webhook payload |
| `payload` | `TEXT NOT NULL` | No | Full JSON-serialised webhook payload |
| `created_at` | `TEXT NOT NULL` | No | UTC ISO-8601 ingest timestamp |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_github_webhook_events_project_created` | `(project_id, created_at DESC)` | Fast per-project listing newest-first |
| `idx_github_webhook_events_project_delivery` | `(project_id, delivery_id)` | Idempotency check by delivery ID |

---

## Schema Migration

`_ensure_schema` is called every time the database is initialised. It uses
`PRAGMA table_info` to inspect the live schema and applies `ALTER TABLE ADD COLUMN`
migrations for any missing columns. This supports upgrading existing databases
without data loss.

Migration history handled by `_ensure_schema`:

- `payload` column — added if missing; data is migrated from legacy
  `payload_json` column if it exists.
- `delivery_id`, `action`, `repository_full_name` — nullable columns added if
  absent.
- `project_id`, `event_type`, `created_at` — required columns added with
  `NOT NULL DEFAULT ''` if absent.

---

## `webhook_store.py` — `GitHubWebhookStore` class

### Constructor

```python
GitHubWebhookStore(db_path: Optional[str] = None)
```

Resolves `db_path` in priority order:

1. Explicit argument
2. `DATABASE_URL` env var (must start with `sqlite:///`)
3. Default `<repo_root>/data/nexusai.db`

### Methods

#### `async _ensure_db() → None`

Lazily creates the table, runs `_ensure_schema`, and creates indexes on first
use. Uses double-checked locking with `_init_lock`.

#### `async _ensure_schema(db: aiosqlite.Connection) → None`

Inspects live schema via `PRAGMA table_info` and applies additive migrations.
Called inside `_ensure_db` before the initial `db.commit()`.

#### `async record_event(project_id, event_type, payload, ...) → Dict[str, Any]`

```python
async def record_event(
    project_id: str,
    event_type: str,
    payload: Dict[str, Any],
    delivery_id: Optional[str] = None,
    action: Optional[str] = None,
    repository_full_name: Optional[str] = None,
) -> Dict[str, Any]
```

Inserts a new webhook event row. The `payload` dict is serialised with
`json.dumps` before storage. Returns a dict of the stored row (with `payload`
still as a dict, not the JSON string).

#### `async has_delivery_id(project_id, delivery_id) → bool`

```python
async def has_delivery_id(project_id: str, delivery_id: str) -> bool
```

Returns `True` if a row with this `(project_id, delivery_id)` pair already
exists — used for idempotency. Returns `False` if `delivery_id` is empty.

#### `async prune_older_than(cutoff_iso: str) → int`

```python
async def prune_older_than(cutoff_iso: str) -> int
```

Deletes all rows where `created_at < cutoff_iso`. Returns the number of rows
deleted. The caller is responsible for computing the cutoff timestamp.

Example:

```python
from datetime import datetime, timedelta, timezone
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
deleted = await store.prune_older_than(cutoff)
```

#### `async list_events(project_id, limit) → List[Dict[str, Any]]`

```python
async def list_events(project_id: str, limit: int = 50) -> List[Dict[str, Any]]
```

Returns up to `limit` events for the given project, ordered newest-first.
The stored `payload` JSON string is parsed back to a dict before returning.

---

## Webhook Events Processed

`GitHubWebhookStore` is event-type agnostic — it stores whatever `event_type`
string the control plane passes in. The following GitHub event types are
handled by the webhook ingestion route:

| Event Type | Common Actions | Typical Trigger |
|---|---|---|
| `push` | — | Commits pushed to a branch |
| `pull_request` | `opened`, `closed`, `synchronize`, `reopened` | PR lifecycle |
| `pull_request_review` | `submitted`, `dismissed` | PR review submitted |
| `issues` | `opened`, `closed`, `labeled` | Issue lifecycle |
| `issue_comment` | `created` | Comment on issue or PR |
| `create` | — | Branch or tag created |
| `delete` | — | Branch or tag deleted |
| `workflow_run` | `completed`, `requested` | GitHub Actions workflow |
| `release` | `published` | Release published |
| `ping` | — | Webhook registration confirmation |

(Exact event routing is defined in the control plane webhook route handler,
not in `webhook_store.py`.)

---

## How Webhooks Trigger Tasks or Bot Workflows

1. GitHub sends an HTTP POST to the control plane webhook endpoint (typically
   `/api/projects/{project_id}/github/webhook`).
2. The route handler validates the `X-Hub-Signature-256` HMAC signature using
   the project's configured webhook secret.
3. `has_delivery_id` is called with the `X-GitHub-Delivery` header to check
   for duplicate delivery — if already seen, a `200 OK` is returned immediately.
4. `record_event` stores the raw payload.
5. The route handler inspects `event_type` and `action`, then creates a NexusAI
   task or fires a bot workflow trigger based on the project's GitHub integration
   settings (e.g. auto-create a task for every opened PR).
6. The task is dispatched through the normal control-plane scheduling path.

---

## Known Issues

- **No limit parameter on `prune_older_than`** — the prune deletes all matching
  rows in one statement, which can lock the database for an extended period on
  large tables.
- **No server-side payload size limit** — very large webhook payloads (e.g.
  bulk push with many commits) are stored verbatim. Callers should enforce a
  body-size limit via `enforce_body_size` before calling `record_event`.
- **Single-process write lock** — `_lock` is an `asyncio.Lock`; multiple
  control-plane processes sharing the same SQLite file are not protected beyond
  SQLite file-level locking.
- **No index on `event_type`** — filtering by event type requires a full table
  scan if the `project_id` index is not selective enough.
- **`list_events` limit is not clamped** — unlike `AuditLog.list_events`, the
  `limit` parameter is not validated, so arbitrarily large values are accepted.
