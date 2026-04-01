# Control Plane

The Control Plane is a **FastAPI** service that acts as the orchestration hub for NexusAI. It manages bots, workers, tasks, chat, vault, keys, audit, and GitHub integrations.

---

## Module Tree

```
control_plane/
├── main.py                      # FastAPI app factory, lifespan startup/shutdown
├── models.py                    # API-layer request/response models
├── observability.py             # Prometheus metrics setup
├── sqlite_helpers.py            # open_sqlite() context manager with WAL mode
├── task_result_files.py         # Extracts file candidates from bot result artifacts
├── orchestration_workspace_store.py  # Temp workspace dir management per orchestration
├── repo_workspace.py            # Git clone/pull/commit/push helpers
├── repo_workspace_usage_store.py     # Records repo workspace command run metrics
│
├── api/                         # FastAPI routers (see api/README.md)
│   ├── audit.py                 # GET /v1/audit/events
│   ├── bots.py                  # CRUD + trigger + run history for bots
│   ├── chat.py                  # Conversations, messages, SSE streaming, @assign
│   ├── database.py              # External DB connection management
│   ├── keys.py                  # API key vault CRUD
│   ├── models_catalog.py        # LLM model catalog CRUD
│   ├── projects.py              # Projects, GitHub PAT, repo workspace, webhooks
│   ├── tasks.py                 # Task queue CRUD + retry + artifacts
│   ├── vault.py                 # Vault item ingest, search, CRUD
│   └── workers.py               # Worker registration + heartbeat
│
├── audit/
│   ├── audit_log.py             # AuditLog: SQLite-backed event recorder
│   └── utils.py                 # record_audit_event() helper for API routes
│
├── chat/
│   ├── chat_manager.py          # ChatManager: conversations + messages + memory
│   ├── pm_orchestrator.py       # PMOrchestrator: @assign workflow entry point
│   └── workspace_tools.py       # File/repo snippet helpers for chat tool access
│
├── database/
│   ├── connection_repository.py # ConnectionRepository: external DB connection CRUD
│   ├── database_engineer.py     # DatabaseEngineer: schema introspection/migrations
│   └── schema_manager.py        # SchemaManager: DDL helpers, migration tracking
│
├── github/
│   └── webhook_store.py         # GitHubWebhookStore: webhook event persistence
│
├── keys/
│   └── key_vault.py             # KeyVault: Fernet-encrypted API key storage
│
├── registry/
│   ├── bot_registry.py          # BotRegistry: in-memory + SQLite bot store
│   ├── model_registry.py        # ModelRegistry: LLM catalog
│   ├── project_registry.py      # ProjectRegistry: project CRUD
│   └── worker_registry.py       # WorkerRegistry: worker CRUD + heartbeat tracking
│
├── scheduler/
│   └── scheduler.py             # Scheduler: backend selection + inference dispatch
│
├── security/
│   └── guards.py                # enforce_body_size(), enforce_rate_limit()
│
├── task_manager/
│   └── task_manager.py          # TaskManager: task lifecycle, trigger dispatch
│
└── vault/
    ├── vault_manager.py         # VaultManager: text ingest, chunking, search
    ├── chunker.py               # chunk_text() sliding window splitter
    └── mcp_broker.py            # MCPBroker: standardized context-pull interface
```

---

## How to Run

```bash
# From repo root
NEXUS_CONFIG_PATH=config/nexus_config.yaml \
NEXUS_MASTER_KEY=<your-secret> \
uvicorn control_plane.main:app --host 0.0.0.0 --port 8000

# Or via Docker Compose
docker compose up control_plane
```

---

## Key Configuration

Set in `config/nexus_config.yaml` (see [config/README.md](../../config/README.md)):

```yaml
control_plane:
  host: 0.0.0.0
  port: 8000
  workers_config_dir: config/workers   # YAML worker seed files
  bots_config_dir: config/bots         # YAML bot seed files
  seed_bots_from_config: true          # Seed on startup
  force_seed_bots_from_config: true    # Overwrite existing on startup
  heartbeat_timeout_seconds: 30        # Mark workers offline after N seconds silence
```

Key environment variables:

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_CONFIG_PATH` | `config/nexus_config.yaml` | Main config path |
| `CONTROL_PLANE_API_TOKEN` | _(none)_ | Optional auth token; if set, all routes except `/health`, `/docs`, and bot trigger require `X-Nexus-API-Key` header |
| `NEXUS_MASTER_KEY` | _(insecure fallback)_ | Fernet encryption key for API keys — **set this in production** |
| `DATABASE_URL` | _(sqlite path)_ | Override SQLite path (`sqlite:///path/to/db`) |
| `NEXUSAI_CLOUD_API_TIMEOUT_SECONDS` | `900` | Cloud API timeout |

---

## SQLite Database

Path: `data/nexusai.db` (relative to repo root, or as set by `DATABASE_URL`).

All tables are created lazily at service startup via `CREATE TABLE IF NOT EXISTS`. See [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) for the full table list.

---

## API Overview

See [`api/README.md`](api/README.md) for the full endpoint table.

The control plane API prefix is `/v1/`. All endpoints return JSON. Auth via `X-Nexus-API-Key` header (when `CONTROL_PLANE_API_TOKEN` is set).

## Known Issues

- The scheduler imports dashboard SQLAlchemy models at runtime for connection row lookups — circular dependency between packages.
- Rate limit and auth middleware stores are per-process, not shared across multiple uvicorn workers.
- SQLite write contention under parallel task dispatch (see [docs/REFACTOR_PRIORITIES.md](../../docs/REFACTOR_PRIORITIES.md)).
