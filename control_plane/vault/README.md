# Vault — Control Plane

The `vault` package provides a document store with chunk-level semantic search. It is built on SQLite and uses a lightweight hash-based embedding approach. Three components make up the package:

| Component | File | Responsibility |
|---|---|---|
| `VaultManager` | `vault_manager.py` | CRUD, chunking, embedding, search |
| `chunk_text` | `chunker.py` | Splits text into overlapping character chunks |
| `MCPBroker` | `mcp_broker.py` | Context-pull adapter wrapping `VaultManager.search` |

---

## VaultManager (`vault_manager.py`)

### SQLite Schema

`VaultManager` manages two tables. Foreign key enforcement is enabled (`PRAGMA foreign_keys = ON`) via `open_sqlite(..., foreign_keys=True)`.

#### `vault_items`

```sql
CREATE TABLE IF NOT EXISTS vault_items (
    id               TEXT PRIMARY KEY,
    source_type      TEXT NOT NULL,
    source_ref       TEXT,                  -- optional external identifier (e.g. file path, URL)
    title            TEXT NOT NULL,
    content          TEXT NOT NULL,         -- full raw text
    namespace        TEXT NOT NULL,         -- logical grouping (default: "global")
    project_id       TEXT,                  -- optional project scope
    metadata         TEXT,                  -- JSON blob, nullable
    embedding_status TEXT NOT NULL,         -- always "completed" in current implementation
    created_at       TEXT NOT NULL,         -- ISO-8601 UTC
    updated_at       TEXT NOT NULL          -- ISO-8601 UTC
)
```

#### `vault_chunks`

```sql
CREATE TABLE IF NOT EXISTS vault_chunks (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    TEXT NOT NULL,             -- JSON array of 64 floats
    metadata     TEXT,                      -- JSON blob, nullable
    created_at   TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES vault_items(id) ON DELETE CASCADE
)
```

`ON DELETE CASCADE` means deleting a `vault_items` row automatically removes all associated `vault_chunks` rows.

### VaultItem and VaultChunk Models

Both are Pydantic models from `shared.models`:

- **`VaultItem`** — mirrors the `vault_items` schema. `embedding_status` is always `"completed"` when written by `VaultManager`.
- **`VaultChunk`** — mirrors the `vault_chunks` schema. `embedding` is a `List[float]` (64 dimensions).

### Initialization

`VaultManager.__init__` resolves the DB path the same way as other registries: explicit `db_path` argument -> `DATABASE_URL` env var (`sqlite:///...`) -> `data/nexusai.db`. Initialization is lazy: `_ensure_db()` creates both tables on first use and is guarded by a double-checked lock (`_init_lock`).

---

## CRUD Operations

### Create — `ingest_text`

```python
async def ingest_text(
    title: str,
    content: str,
    namespace: str = "global",
    project_id: Optional[str] = None,
    source_type: str = "text",
    source_ref: Optional[str] = None,
    metadata: Optional[Any] = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
) -> VaultItem
```

Always creates a **new** item with a fresh UUID. Does not check for duplicates. Use `upsert_text` if idempotent ingest is required.

### Create-or-Update — `upsert_text`

```python
async def upsert_text(
    title: str,
    content: str,
    namespace: str = "global",
    project_id: Optional[str] = None,
    source_type: str = "text",
    source_ref: Optional[str] = None,
    metadata: Optional[Any] = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
) -> VaultItem
```

If `source_ref` is provided, calls `find_item_by_source_ref` to look up an existing item by `(source_ref, namespace, project_id)`. If found, deletes existing chunks and updates the item in place (preserving the original `created_at`). If not found (or `source_ref` is `None`), behaves like `ingest_text`.

### Read — `get_item`

```python
async def get_item(item_id: str) -> VaultItem
```

Returns the `VaultItem` by primary key. Raises `VaultItemNotFoundError` if absent.

### Read — `list_items`

```python
async def list_items(
    namespace: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 100,
    include_content: bool = True,
) -> List[VaultItem]
```

Returns items ordered by `updated_at DESC`. When `include_content=False`, the `content` field is returned as an empty string — intended for lightweight list/picker UIs that do not need full body text.

### Read — `find_item_by_source_ref`

```python
async def find_item_by_source_ref(
    source_ref: str,
    namespace: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[VaultItem]
```

Looks up items by `source_ref`, filtered by optional `namespace` and `project_id`. Returns the most recently updated match (`ORDER BY updated_at DESC LIMIT 1`).

### Read — `list_chunks`

```python
async def list_chunks(item_id: str) -> List[VaultChunk]
```

Validates that the parent item exists (calls `get_item`), then returns all chunks ordered by `chunk_index ASC`.

### Read — `list_namespaces`

```python
async def list_namespaces() -> List[str]
```

Returns a sorted list of distinct namespace values currently in `vault_items`.

### Delete — `delete_item`

```python
async def delete_item(item_id: str) -> None
```

Deletes the item row; cascade removes all child chunks. Raises `VaultItemNotFoundError` if the item did not exist (checked via `rowcount`).

---

## Embedding Lifecycle

The `embedding_status` field on `VaultItem` was designed to support an asynchronous pipeline (`pending -> processing -> complete/error`). In the current implementation this pipeline does not exist. **`embedding_status` is always written as `"completed"`** at creation/update time, and the actual embedding computation happens synchronously inside `_write_item` before the database write.

The lifecycle as-implemented:

```
ingest_text / upsert_text
      |
      v
 _write_item (synchronous)
      |
      +-- chunk_text(content)           -> List[str]
      +-- _embed(chunk)  [per chunk]    -> List[float] (64-dim)
      +-- INSERT vault_items (embedding_status="completed")
          INSERT vault_chunks (embedding stored as JSON)
```

There is no background worker, queue, or status polling mechanism.

---

## Embedding Implementation (`_embed`)

```python
def _embed(self, text: str, dims: int = 64) -> List[float]
```

This is **not** a neural or semantic embedding. It is a deterministic hash-based bag-of-words projection:

1. Tokenise by whitespace; lowercase each token.
2. For each token, compute `SHA-256(token)`.
3. Map the token to a bucket: `index = int.from_bytes(digest[:2], "big") % dims`.
4. Assign sign: `+1` if `digest[2]` is even, else `-1`.
5. Accumulate into a 64-float vector, then L2-normalise.

**Similarity search** uses cosine similarity computed entirely in Python over all chunks matching the filter. There is no vector index; performance degrades linearly with the number of chunks.

---

## Chunker (`chunker.py`)

```python
def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> List[str]
```

Splits text using a **character-level sliding window**:

- `chunk_size`: maximum characters per chunk (default: 1000).
- `overlap`: characters shared between consecutive chunks (default: 150). Must be `< chunk_size`.
- Empty/whitespace-only input returns `[]`.
- Input is stripped before chunking.
- The final chunk is whatever remains after the last full step and is always included.

**Algorithm:**

```
start = 0
while start < len(text):
    end = min(len(text), start + chunk_size)
    emit text[start:end]
    if end == len(text): break
    start = end - overlap
```

### Validation Errors

| Condition | Error |
|---|---|
| `chunk_size <= 0` | `ValueError: chunk_size must be > 0` |
| `overlap < 0` | `ValueError: overlap must be >= 0` |
| `overlap >= chunk_size` | `ValueError: overlap must be smaller than chunk_size` |

---

## MCPBroker (`mcp_broker.py`)

`MCPBroker` is a thin adapter that exposes `VaultManager.search` through a structured "context pull" interface. It does not implement the full MCP (Model Context Protocol) specification — there is no protocol transport, tool registration, or server lifecycle management.

### Constructor

```python
MCPBroker(vault_manager: VaultManager)
```

### `pull_context`

```python
async def pull_context(
    query: str,
    namespace: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 5,
) -> Dict[str, Any]
```

Calls `vault_manager.search(query, namespace, project_id, limit)` and formats the results as:

```json
{
    "query": "<original query>",
    "context_count": 3,
    "contexts": [
        {
            "item_id": "...",
            "chunk_id": "...",
            "content": "...",
            "score": 0.87,
            "metadata": {
                "title": "...",
                "namespace": "...",
                "project_id": "...",
                "chunk_index": 0
            }
        }
    ]
}
```

Results are already sorted by cosine score descending (handled by `VaultManager.search`).

---

## Known Issues / Refactor Notes

### VaultManager

- **`embedding_status` is vestigial**: the field is hardcoded to `"completed"` and cannot be `"pending"` or `"processing"` through any current code path. If an async embedding pipeline is ever added, callers will need a mechanism to poll or subscribe for status changes.
- **Hash embedding is not semantic**: `_embed` is a deterministic bag-of-words approximation. It does not capture synonyms, context, or word order. Search quality will be significantly worse than a real embedding model (e.g., OpenAI `text-embedding-*`, sentence-transformers).
- **Full table scan on search**: `search()` fetches all `vault_chunks` rows matching the namespace/project filter into Python memory, then scores and sorts them. This will not scale as the vault grows. A vector index (e.g., `sqlite-vss`, `pgvector`, FAISS) is needed for production use.
- **`chunk.created_at` bug**: in `_write_item` (line 202), `VaultChunk` is constructed with `created_at=updated_at` instead of `created_at=created_at`. For new items `created_at == updated_at` so the effect is invisible, but for upserts the chunk's `created_at` will be set to the upsert timestamp rather than the item's original creation time.
- **`upsert_text` with `source_ref=None`**: when `source_ref` is `None`, the lookup is skipped and a new item is always created. Callers expecting idempotent upsert behaviour must always provide a `source_ref`.
- **No transaction wrapping across item + chunk inserts**: within `_write_item` the item row and all chunk rows are inserted inside a single `open_sqlite` context manager, so they are atomic within that connection. However, the `asyncio.Lock` is held for the entire DB write block, serialising all writes through a single coroutine at a time.

### MCPBroker

- **Not a real MCP server**: the class is named `MCPBroker` and the docstring says "MCP protocol integration", but there is no MCP transport layer, no tool schema, and no server lifecycle. It is a plain Python adapter over `VaultManager.search`.
- **No caching**: every `pull_context` call performs a full search with a table scan. Repeated identical queries are not cached.
