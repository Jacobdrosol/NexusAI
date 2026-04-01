# Audit — `control_plane/audit/`

Asynchronous, SQLite-backed audit trail for every significant action taken
through the NexusAI control plane.

---

## Files

| File | Purpose |
|---|---|
| `audit_log.py` | `AuditLog` class — schema creation and read/write helpers |
| `utils.py` | FastAPI helpers — actor resolution and one-liner event recording |

---

## SQLite Schema

Table: **`audit_events`** (in `data/nexusai.db` by default)

| Column | Type | Description |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | UUID v4 |
| `actor` | `TEXT` | Who triggered the action (nullable) |
| `action` | `TEXT NOT NULL` | Short verb describing the operation |
| `resource` | `TEXT NOT NULL` | The object the action was performed on |
| `status` | `TEXT NOT NULL` | Outcome — typically `"ok"` or `"error"` |
| `details` | `TEXT` | JSON-serialised extra context (nullable) |
| `created_at` | `TEXT NOT NULL` | UTC ISO-8601 timestamp |

Records are ordered newest-first when queried. `details` is stored as a JSON
string and automatically round-tripped to/from a Python object by the
`AuditLog` methods.

---

## `audit_log.py` — `AuditLog` class

### Constructor

```python
AuditLog(db_path: Optional[str] = None)
```

Resolves the database path in priority order:

1. Explicit `db_path` argument
2. `DATABASE_URL` environment variable (must start with `sqlite:///`)
3. Hard-coded default: `<repo_root>/data/nexusai.db`

Uses two `asyncio.Lock` objects:
- `_init_lock` — prevents concurrent schema creation on first use
- `_lock` — serialises write operations

### Methods

#### `async _ensure_db() → None`

Lazily creates the `audit_events` table the first time any method is called.
Uses double-checked locking (checked before and inside the `_init_lock`) so
this is safe under high concurrency.

#### `async record(action, resource, status, actor, details) → Dict[str, Any]`

```python
async def record(
    action: str,
    resource: str,
    status: str = "ok",
    actor: Optional[str] = None,
    details: Optional[Any] = None,
) -> Dict[str, Any]
```

Inserts a new row into `audit_events` and returns a dict with all fields
(including the generated UUID and timestamp). `details` is serialised with
`json.dumps` before storage.

#### `async list_events(limit: int = 100) → List[Dict[str, Any]]`

Returns up to `limit` events ordered newest-first. The `limit` is clamped to
the range `[1, 1000]`. The stored JSON `details` string is automatically
parsed back to a Python object before returning.

---

## `utils.py` — FastAPI Helpers

### `_actor_from_request(request: Request) → Optional[str]`

Derives a human-readable actor identifier from an incoming HTTP request,
checked in priority order:

1. `Authorization` header — returns the first 64 characters of the value
2. `X-Nexus-API-Key` header — returns the fixed string `"api_key"`
3. Client IP address (`request.client.host`)
4. `None` if none of the above are present

The 64-character truncation on the `Authorization` header prevents runaway
storage for long bearer tokens while still being identifiable.

### `async record_audit_event(request, action, resource, status, details) → None`

```python
async def record_audit_event(
    request: Request,
    action: str,
    resource: str,
    status: str = "ok",
    details: Optional[Any] = None,
) -> None
```

Convenience wrapper for route handlers. Reads the `AuditLog` instance from
`request.app.state.audit_log` (silently no-ops if it is absent) and calls
`audit_log.record(...)` with the actor resolved by `_actor_from_request`.

Typical usage inside a FastAPI route:

```python
from control_plane.audit.utils import record_audit_event

@router.post("/bots/{bot_id}/tasks")
async def create_task(bot_id: str, request: Request, ...):
    ...
    await record_audit_event(request, action="task.create", resource=f"bot:{bot_id}")
```

---

## Audit Event Types

The audit system is generic — callers pass arbitrary `action` and `resource`
strings. The following patterns are used across the control plane:

| action | resource pattern | notes |
|---|---|---|
| `task.create` | `bot:<id>` | New task submitted |
| `task.cancel` | `task:<id>` | Task cancelled by user |
| `key.set` | `key:<name>` | API key created or updated |
| `key.delete` | `key:<name>` | API key deleted |
| `webhook.receive` | `project:<id>` | GitHub webhook received |
| `settings.update` | `setting:<key>` | Runtime setting changed |

(This list reflects usages found across the control plane. New endpoints may
add additional event types without updating this table.)

---

## Known Issues

- **No pagination cursor** — `list_events` uses `LIMIT` only; older events
  require direct SQL queries.
- **No index on `created_at`** — full table scan for every `list_events` call.
  At high event volumes this will degrade. A `CREATE INDEX` on `created_at`
  would help.
- **No event-type filtering** — callers cannot filter by `action`, `actor`, or
  `resource` through the public API.
- **In-process lock only** — `_lock` prevents concurrent writes within a single
  process but does not protect against concurrent writes from multiple processes
  sharing the same SQLite file.
- **Actor truncated at 64 chars** — `Authorization` header values longer than
  64 characters are silently truncated, which can make actor identification
  ambiguous.
