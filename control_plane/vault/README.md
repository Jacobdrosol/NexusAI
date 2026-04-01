# Vault

The vault provides semantic search over ingested context items (text, files, URLs, chat transcripts, task results). It is the primary retrieval mechanism for providing bots with project-relevant context.

---

## Files

| File | Class | Purpose |
|------|-------|---------|
| `vault_manager.py` | `VaultManager` | CRUD + chunking + search |
| `chunker.py` | `chunk_text()` | Sliding window text splitter |
| `mcp_broker.py` | `MCPBroker` | Standardised context-pull interface |

---

## VaultManager (`vault_manager.py`)

### SQLite Tables

**`vault_items`**:
| Column | Description |
|--------|-------------|
| `id` | UUID |
| `source_type` | `text/file/url/chat/task/custom` |
| `source_ref` | Stable reference key (for upsert deduplication) |
| `title` | Human-readable title |
| `content` | Full text content |
| `namespace` | Logical grouping (e.g., `global`, `project:<id>:repo`) |
| `project_id` | Optional project scope |
| `metadata` | JSON extra fields |
| `embedding_status` | `pending/indexed` (note: actual vector embedding not implemented) |
| `created_at` / `updated_at` | ISO 8601 UTC |

**`vault_chunks`**:
| Column | Description |
|--------|-------------|
| `id` | UUID |
| `item_id` | FK → vault_items |
| `chunk_index` | 0-based sequence |
| `content` | Chunk text |
| `embedding` | JSON array of 64 floats (hash-based) |
| `metadata` | JSON |
| `created_at` | ISO 8601 UTC |

### Key Methods

| Method | Description |
|--------|-------------|
| `ingest_text(title, content, namespace, ...)` | Creates new vault item + chunks |
| `upsert_text(title, content, namespace, source_ref, ...)` | Upserts by source_ref if provided |
| `get_item(item_id)` | Raises `VaultItemNotFoundError` |
| `list_items(namespace, project_id, limit)` | Filtered listing |
| `delete_item(item_id)` | Deletes item + chunks (cascade) |
| `search(query, namespace, project_id, limit)` | Cosine similarity search over chunks |
| `find_item_by_source_ref(source_ref, namespace, project_id)` | Lookup for upsert deduplication |
| `get_chunks(item_id)` | Returns all chunks for an item |

### Search Algorithm

1. Embeds the query using `_embed(query, dims=64)` — SHA-256 hash-based 64-dim vector.
2. Loads all chunk embeddings from `vault_chunks` matching the namespace/project filter.
3. Computes cosine similarity for each chunk.
4. Returns top `limit` chunks sorted by score, along with `item_id`, `title`, `namespace`, `project_id`.

**Known issue**: Hash-based embeddings have no semantic understanding. The 64-dimensional space is too small for meaningful similarity. Results are based on token overlap, not meaning.

### Namespace Conventions

| Namespace | Usage |
|-----------|-------|
| `global` | Platform-wide context |
| `project:<id>` | Project-scoped context |
| `project:<id>:repo` | GitHub repo sync context |
| `project:<id>:data` | Uploaded project data files |

---

## Chunker (`chunker.py`)

```python
chunk_text(text, chunk_size=1000, overlap=150) -> List[str]
```

Splits text into overlapping character-based chunks. Simple sliding window:
- `start` begins at 0
- Each chunk is `text[start:start+chunk_size]`
- Next `start` = `end - overlap`
- Stops when `end == len(text)`

**No sentence-boundary awareness**: chunks may split mid-sentence. For better chunking, a sentence-aware splitter would be needed.

Used by:
- `VaultManager.ingest_text()` — default `chunk_size=1000, overlap=150`
- `ChatManager._reindex_message()` — `chunk_size=800, overlap=120`

---

## MCPBroker (`mcp_broker.py`)

```python
await mcp_broker.pull_context(query, namespace, project_id, limit) -> Dict
```

Returns:
```json
{
  "query": "...",
  "context_count": 3,
  "contexts": [
    {
      "item_id": "...",
      "chunk_id": "...",
      "content": "...",
      "score": 0.87,
      "metadata": {"title": "...", "namespace": "...", "chunk_index": 0}
    }
  ]
}
```

Thin wrapper over `VaultManager.search()`. Provides a consistent interface for the scheduler and chat API to pull vault context.

---

## Embedding Status

`VaultItem.embedding_status` tracks the state of real vector embedding (planned feature):
- `pending` — item has been ingested but not yet embedded by a real model
- `indexed` — embedding is complete

Currently all items use the hash-based fallback; the status field is set to `indexed` after hash-embedding completes. A real embedding pipeline would need to:
1. Leave `embedding_status = "pending"` on ingest
2. Queue the item for processing by an embedding worker
3. Update chunks with real vectors and set `embedding_status = "indexed"`

---

## Known Issues

- Hash-based 64-dim embeddings provide minimal semantic retrieval quality.
- `_embed` is duplicated between `VaultManager` and `ChatManager`.
- No pagination for `search()` results — all chunks are loaded into memory before ranking.
- `vault_chunks` has no index on `item_id` + `namespace` — full table scans on search.
