# Registry — Control Plane

The `registry` package contains four in-memory registries that serve as the source of truth for the control plane's runtime state. Each registry uses an asyncio `Lock` for thread safety and persists its data to a shared SQLite database (default: `data/nexusai.db`).

---

## Overview

| Registry | SQLite Table | Persistent? | Notes |
|---|---|---|---|
| `BotRegistry` | `cp_bots` | Yes | Bots survive restarts |
| `ModelRegistry` | `models` | Yes | CatalogModel entries |
| `ProjectRegistry` | `projects` | Yes | Includes bridge links |
| `WorkerRegistry` | *(none)* | No | In-memory only |

---

## Shared Architecture

All persistent registries follow the same pattern:

1. **Lazy initialization** — The `_ensure_db()` method creates the SQLite table if it does not exist and loads all rows into the in-memory dict on first use. A second `asyncio.Lock` (`_init_lock`) guards against concurrent initialization races (double-checked locking pattern).
2. **In-memory cache** — Every registry holds a `Dict[str, <Model>]` that is the primary read path. Database reads happen only at startup.
3. **Write-through persistence** — Every mutation updates the in-memory dict first (under `_lock`), then calls an async helper (`_persist_*` / `_delete_*`) that performs an upsert (`INSERT ... ON CONFLICT ... DO UPDATE`) or `DELETE` against SQLite.
4. **DB path resolution** — The constructor accepts an optional `db_path`. If omitted it checks the `DATABASE_URL` environment variable for a `sqlite:///` URL, and falls back to `data/nexusai.db` relative to the repo root.

---

## BotRegistry (`bot_registry.py`)

Manages `Bot` model instances. Bots are JSON-serialised and stored in the `cp_bots` table.

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS cp_bots (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL        -- JSON-serialised Bot.model_dump()
)
```

### Bot Validation

Every write path (`register`, `update`, `seed_from_configs`) calls `validate_bot_configuration(bot)` from `shared.bot_policy`. This returns a list of error strings; if non-empty a `ValueError` is raised and the operation is aborted.

### Public Methods

| Method | Signature | Description |
|---|---|---|
| `register` | `(bot: Bot) -> None` | Validates and inserts a bot. Raises `ValueError` on policy failure. |
| `get` | `(bot_id: str) -> Bot` | Returns bot from cache or raises `BotNotFoundError`. |
| `list` | `() -> List[Bot]` | Returns all bots. |
| `update` | `(bot_id: str, bot: Bot) -> None` | Validates and replaces. Raises `BotNotFoundError` if absent. |
| `remove` | `(bot_id: str) -> None` | Deletes from cache and DB. Raises `BotNotFoundError` if absent. |
| `enable` | `(bot_id: str) -> None` | Sets `enabled=True` via `model_copy`; persists. |
| `disable` | `(bot_id: str) -> None` | Sets `enabled=False` via `model_copy`; persists. |
| `seed_from_configs` | `(configs: list, worker_ids: set[str], *, force: bool = False) -> None` | Bulk-loads bots from raw config dicts. Skips existing bots unless `force=True`. Warns (does not fail) if a bot references an unknown `worker_id`. |

### seed_from_configs Detail

`seed_from_configs` iterates the config list inside `_lock`, validates each entry, and either skips it (if `bot.id` already exists and `force=False`) or upserts it. After releasing the lock it persists every bot currently in `_bots` — not just the newly seeded ones.

---

## ModelRegistry (`model_registry.py`)

Manages `CatalogModel` instances in the `models` table.

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS models (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL        -- JSON-serialised CatalogModel.model_dump()
)
```

### Public Methods

| Method | Signature | Description |
|---|---|---|
| `register` | `(model: CatalogModel) -> None` | Inserts or replaces a model. No policy validation. |
| `get` | `(model_id: str) -> CatalogModel` | Returns model or raises `CatalogModelNotFoundError`. |
| `list` | `() -> List[CatalogModel]` | Returns all models regardless of enabled state. |
| `update` | `(model_id: str, model: CatalogModel) -> None` | Raises `CatalogModelNotFoundError` if absent. |
| `remove` | `(model_id: str) -> None` | Deletes from cache and DB. |
| `exists` | `(provider: str, name: str) -> bool` | Returns `True` only if an **enabled** model with matching `provider` and `name` exists. |
| `has_any` | `() -> bool` | Returns `True` if at least one model is registered (enabled or not). |

### enabled/disabled Filtering

`list()` returns **all** models. `exists()` filters by `model.enabled == True`. There is no dedicated `list_enabled()` helper — callers must filter `list()` manually if they need only enabled models.

---

## ProjectRegistry (`project_registry.py`)

Manages `Project` instances. Projects support an optional `bridge_project_ids` relationship that allows cross-project context sharing.

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS projects (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL        -- JSON-serialised Project.model_dump()
)
```

### Project Validation (`_validate_project`)

Called synchronously inside `register` and `update` before acquiring `_lock`:

- A project cannot list itself in `bridge_project_ids`.
- A project with `mode == "isolated"` cannot have any `bridge_project_ids`.

### Public Methods

| Method | Signature | Description |
|---|---|---|
| `register` | `(project: Project) -> None` | Validates then inserts. |
| `get` | `(project_id: str) -> Project` | Returns project or raises `ProjectNotFoundError`. |
| `list` | `() -> List[Project]` | Returns all projects. |
| `update` | `(project_id: str, project: Project) -> None` | Validates then replaces. |
| `remove` | `(project_id: str) -> None` | Deletes project and removes it from all other projects' `bridge_project_ids`. Persists every affected project. |
| `add_bridge` | `(source_project_id: str, target_project_id: str) -> None` | Creates a bidirectional bridge link. Both projects must have `mode == "bridged"`. |
| `remove_bridge` | `(source_project_id: str, target_project_id: str) -> None` | Removes the link from both sides. |

### How `bridge_project_ids` Works

- `bridge_project_ids` is a list of project IDs stored in each `Project` model.
- Bridges are **bidirectional**: `add_bridge(A, B)` adds `B` to `A.bridge_project_ids` and `A` to `B.bridge_project_ids`. Both models are persisted.
- `remove` cascades: when a project is deleted, every other project that referenced it in `bridge_project_ids` is updated in-memory and re-persisted.
- IDs in `bridge_project_ids` are kept sorted (`sorted(source_links)`) when a bridge is added.

---

## WorkerRegistry (`worker_registry.py`)

Manages `Worker` instances. **This registry is entirely in-memory — workers are not persisted to SQLite and are lost on process restart.**

### State

- `_workers: Dict[str, Worker]` — registered workers.
- `_last_heartbeat: Dict[str, datetime]` — UTC timestamp of the most recent heartbeat per worker.

### Worker Status

Workers have a `status` field with three valid values: `"online"`, `"offline"`, `"degraded"`. `update_heartbeat` always sets the status back to `"online"` regardless of its previous value.

### Public Methods

| Method | Signature | Description |
|---|---|---|
| `register` | `(worker: Worker) -> None` | Adds worker to cache; sets initial heartbeat to now. |
| `get` | `(worker_id: str) -> Worker` | Returns worker or raises `WorkerNotFoundError`. |
| `list` | `() -> List[Worker]` | Returns all workers. |
| `update` | `(worker_id: str, worker: Worker) -> None` | Replaces full worker record. |
| `update_status` | `(worker_id: str, status: Literal["online", "offline", "degraded"]) -> None` | Updates status field only. |
| `update_heartbeat` | `(worker_id: str) -> None` | Records current UTC time in `_last_heartbeat`; sets `status = "online"`. |
| `update_metrics` | `(worker_id: str, metrics: WorkerMetrics) -> None` | Updates the `metrics` field on the worker. |
| `remove` | `(worker_id: str) -> None` | Removes worker and its heartbeat entry. |
| `get_worker_ids` | `() -> List[str]` | Returns list of all registered worker IDs. |
| `get_last_heartbeat` | `(worker_id: str) -> Optional[datetime]` | Returns last heartbeat time or `None` if unknown. |
| `load_from_configs` | `(configs: list) -> None` | Synchronously seeds workers from raw config dicts. Sets initial heartbeat to now for each. |

---

## Known Issues / Refactor Notes

### BotRegistry

- **Over-broad persist in `seed_from_configs`**: after releasing `_lock`, the method persists **all** bots in `_bots`, not just the newly seeded ones. If the registry already held many bots, this causes unnecessary writes on every seed call.
- **Persist outside lock**: `_persist_bot` is called outside `_lock`. A concurrent `remove` could delete a bot from memory between the lock release and the persist, causing a ghost entry to be written back to SQLite.

### ModelRegistry

- **Uses raw `aiosqlite.connect()`** in `_ensure_db` and `_persist_model`/`_delete_model`, while `BotRegistry` uses the shared `open_sqlite()` helper. Any connection-level options applied by `open_sqlite` (e.g., WAL mode, busy timeout) are absent for `ModelRegistry`.
- **No policy validation**: `register` and `update` do not call any validation function, unlike `BotRegistry`. Malformed `CatalogModel` objects can be stored as long as Pydantic accepts them.

### ProjectRegistry

- **Uses raw `aiosqlite.connect()`** — same issue as `ModelRegistry`.
- **`remove` re-persists all projects**: when a project is deleted, every project that had it as a bridge is written back to SQLite. For large registries this is O(N) writes on every delete.
- **`_validate_project` is synchronous and called before `_lock`**: a TOCTOU window exists between validation and the subsequent lock acquisition.

### WorkerRegistry

- **No persistence**: workers are lost on restart. The control plane must re-register workers on reconnect.
- **`load_from_configs` is synchronous** (no `async`/`await`), unlike every other write method. It also does not acquire `_lock`, making it unsafe to call concurrently with other async operations.
- **`update_heartbeat` unconditionally sets `status = "online"`**: if a worker is explicitly marked `"degraded"` or `"offline"`, the next heartbeat will silently promote it back to online.
- **No heartbeat expiry**: there is no background task or method to mark workers offline when heartbeats stop. Callers must implement their own staleness check using `get_last_heartbeat`.
