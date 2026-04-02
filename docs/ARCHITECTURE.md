# NexusAI Architecture

## Overview

NexusAI is a distributed LLM orchestration platform consisting of three independently deployable services that communicate over HTTP, backed by a shared SQLite database and YAML-based configuration.

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        User's Browser                                   │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTP (port 5000)
┌───────────────────────────────▼─────────────────────────────────────────┐
│  Dashboard  (Flask / Gunicorn / Flask-Login / Flask-WTF)                │
│  dashboard/app.py — Blueprint router                                     │
│  ┌─────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ routes/bots │ │routes/   │ │routes/   │ │routes/   │ │routes/   │  │
│  │             │ │ chat     │ │ tasks    │ │projects  │ │ vault    │  │
│  └─────────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│  cp_client.py → HTTP → Control Plane API                                │
│  dashboard/db.py → SQLite (data/nexusai.db, separate User/Connection    │
│                            tables via SQLAlchemy)                        │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTP (port 8000)
                                │ X-Nexus-API-Key header (optional)
┌───────────────────────────────▼─────────────────────────────────────────┐
│  Control Plane  (FastAPI / uvicorn)   control_plane/main.py             │
│                                                                          │
│  ┌────────────────┐  ┌──────────────────────────────────────────────┐   │
│  │  Registries    │  │ Task Manager  (cp_tasks, cp_bot_runs, …)    │   │
│  │  BotRegistry   │  │ task_manager/task_manager.py                 │   │
│  │  WorkerRegistry│  └──────────────────────┬───────────────────────┘   │
│  │  ProjectReg.   │                         │                           │
│  │  ModelRegistry │  ┌──────────────────────▼───────────────────────┐   │
│  └────────────────┘  │ Scheduler  scheduler/scheduler.py            │   │
│                      │ (backend selection, payload building,         │   │
│                      │  context reduction, system-prompt injection)  │   │
│  ┌────────────────┐  └──────────────────────┬───────────────────────┘   │
│  │ Chat Manager   │                         │ HTTP infer POST           │
│  │ PM Orchestrator│  ┌──────────────────────▼───────────────────────┐   │
│  └────────────────┘  │ Worker Agent(s) OR Cloud APIs                │   │
│                      └──────────────────────────────────────────────┘   │
│  ┌────────────────┐  ┌────────────┐  ┌──────────────┐  ┌───────────┐   │
│  │ VaultManager   │  │ KeyVault   │  │ AuditLog     │  │ Database  │   │
│  │ MCPBroker      │  │ (Fernet)   │  │              │  │ Engineer  │   │
│  └────────────────┘  └────────────┘  └──────────────┘  └───────────┘   │
│                                                                          │
│  SQLite: data/nexusai.db  (single file, shared by all services)         │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTP (port 8001)
┌───────────────────────────────▼─────────────────────────────────────────┐
│  Worker Agent  (FastAPI / uvicorn)   worker_agent/main.py               │
│  ┌───────────────┐  ┌───────────────────────────────────────────────┐   │
│  │ /health       │  │ Backends:  ollama / openai / gemini / claude  │   │
│  │ /capabilities │  │            / cli                               │   │
│  │ /v1/infer     │  └───────────────────────────────────────────────┘   │
│  └───────────────┘                                                       │
│  Registers & heartbeats to Control Plane at startup                     │
└─────────────────────────────────────────────────────────────────────────┘

         ┌─────────────────────────────────┐
         │  Prometheus  (port 9090)         │
         │  /metrics from CP + Worker       │
         └─────────────────────────────────┘
```

---

## Service Roles

| Service | Framework | Port | Role |
|---------|-----------|------|------|
| `control_plane` | FastAPI + uvicorn | 8000 | Orchestration hub: task scheduling, bot/worker management, vault, chat, audit |
| `worker_agent` | FastAPI + uvicorn | 8001 | Local inference backend; proxies to Ollama/vLLM/LM Studio |
| `dashboard` | Flask + Gunicorn | 5000 | Web UI; all actions proxy through `cp_client.py` to the control plane |
| `prometheus` | Prometheus | 9090 | Metrics scraping from `/metrics` on CP and Worker |

---

## Data Flow: `@assign` Chat Message

1. **User** types `@assign <instruction>` in the chat UI.
2. **Dashboard** (`routes/chat.py`) POSTs to `/v1/chat/conversations/{id}/messages` with `is_assign=true`.
3. **Control Plane** `api/chat.py` calls `pm_orchestrator.orchestrate_assignment(...)`.
4. **PMOrchestrator** (`chat/pm_orchestrator.py`):
   - Extracts scope lock, docs-only mode, constraint hints from the instruction.
   - Calls `task_manager.create_task(bot_id=<pm_bot_id>, payload={...assignment_scope...})`.
   - Returns `{orchestration_id, tasks: [pm_task]}`.
5. **TaskManager** (`task_manager/task_manager.py`) persists the task to `cp_tasks` as `queued`, then calls `scheduler.dispatch(task)`.
6. **Scheduler** (`scheduler/scheduler.py`):
   - Selects backend from bot's `backends` list (first enabled, matching worker online).
   - Builds message list from payload, injects system prompt + output contract + scope suffix.
   - For local workers: POSTs to `http://<worker_host>:<port>/v1/infer`.
   - For cloud APIs: calls OpenAI/Claude/Gemini directly with resolved API key.
7. **Worker Agent** proxies to Ollama (or other local backend), returns `{output, usage, finish_reason}`.
8. **TaskManager** receives result, checks output contract (required_fields, JSON format).
   - If output contract satisfied → marks task `completed`, fires workflow triggers.
   - If contract fails → marks `failed` or auto-retries with incremented `max_tokens`.
9. **Workflow triggers** (`BotWorkflowTrigger`) on the PM bot fire new tasks for each downstream bot (pm-research-analyst x3, pm-engineer, etc.), fan-out and join handled by `DependencyEngine`.
10. **Dashboard** SSE stream (`routes/events.py`) polls for task status updates and streams to the browser.

---

## Database Schema

All services share **`data/nexusai.db`** (SQLite). Tables are created lazily on first use.

### Control Plane Tables

| Table | Owner | Purpose |
|-------|-------|---------|
| `cp_tasks` | TaskManager | Task queue: id, bot_id, payload, metadata, status, result, error |
| `cp_task_dependencies` | TaskManager | Dependency graph: task_id → depends_on_task_id |
| `cp_bot_runs` | TaskManager | Per-run tracking: started_at, completed_at, triggered_by_task_id |
| `cp_bot_run_artifacts` | TaskManager | Artifacts: kind (payload/result/error/file/note), label, content, path |
| `cp_bots` | BotRegistry | Bot configs stored as JSON blobs |
| `conversations` | ChatManager | Chat conversations metadata |
| `messages` | ChatManager | Chat messages with role/content/bot_id/model |
| `chat_message_memory` | ChatManager | Chunked embeddings for semantic search within conversations |
| `vault_items` | VaultManager | Ingested context items: title, content, namespace, project_id |
| `vault_chunks` | VaultManager | Chunked text with 64-dim hash embeddings for similarity search |
| `api_keys` | KeyVault | Fernet-encrypted API keys by name+provider |
| `audit_events` | AuditLog | actor, action, resource, status, details log |
| `github_webhook_events` | GitHubWebhookStore | Ingested GitHub webhook payloads by project |
| `database_connections` | ConnectionRepository | External DB connection configs |
| `nexus_settings` | SettingsManager | Runtime-editable key/value settings with audit trail |
| `nexus_settings_audit` | SettingsManager | Settings change history |

### Dashboard Tables (SQLAlchemy)

| Table | Purpose |
|-------|---------|
| `users` | Dashboard user accounts |
| `connections` | Bot-scoped HTTP/OpenAPI/DB connection definitions |
| `bot_connections` | M:M link between bots and connections |

---

## Bot and Worker Configuration

**Config source of truth:** YAML files in `config/bots/` and `config/workers/`.

- At startup, `main.py` calls `ConfigLoader.load_all_from_dir(bots_dir)` and seeds the `BotRegistry` if `seed_bots_from_config: true`.
- Bots are then persisted as JSON in `cp_bots` and served from in-memory dict.
- Workers are registered at startup (YAML seed) or self-register at runtime (worker agent `POST /v1/workers`).
- Changes made in the dashboard are persisted to SQLite and override the YAML seed (unless `force_seed_bots_from_config: true`).

---

## Blue/Green Deployment

`docker-compose.bluegreen.yml` provides zero-downtime dashboard deploys:

- `dashboard_blue` and `dashboard_green` are separate containers (Docker Compose profiles `blue` / `green`).
- `dashboard_gateway` is an nginx container that proxies port 5000 to whichever slot is active.
- Nginx config in `data/nginx/` is swapped by `deploy_manager.py` to shift traffic.
- Both slots mount the same `./data` volume, so the SQLite database is shared.
- Control plane and worker agent are NOT blue/greened — only the dashboard is.

Standard deployment uses `docker-compose.yml` with a single dashboard container.

---

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_CONFIG_PATH` | `config/nexus_config.yaml` | Main config file path |
| `CONTROL_PLANE_API_TOKEN` | _(none)_ | Optional bearer token for CP auth |
| `NEXUS_MASTER_KEY` | _(insecure default)_ | Fernet master key for API key encryption |
| `DATABASE_URL` | _(sqlite:///data/nexusai.db)_ | SQLite URL override |
| `CONTROL_PLANE_URL` | `http://localhost:8000` | Used by dashboard and worker agent |
| `NEXUSAI_CLOUD_API_TIMEOUT_SECONDS` | `900` | Cloud API timeout override |
| `NEXUSAI_REPO_RUNTIME_TOOLCHAINS` | `node,dotnet,go,rust,cpp` | Toolchains baked into CP image |

---

## Platform AI (In-Platform Autonomous Tuner)

> **⚠️ Status: Active Development / Testing — Not Yet Stable**

As of April 2026, the platform includes an in-platform AI copilot called **Platform AI** (`control_plane/platform_ai/`). This is a separate autonomous layer that monitors running PM workflows, evaluates output quality, and iteratively refines bot prompts to improve pipeline convergence — the same goal as the external NexusAI-Audit tool, but built directly into the platform runtime.

```
Operator → Platform AI Session (dashboard /platform-ai/)
              │
              ▼
          PlatformAISessionRuntime (async background loop per session)
              │   1. Snapshot: poll task_manager for live task state
              │   2. Evaluate: run quality test suite (assertions on task outputs)
              │   3. Refine: patch bot system_prompt with failure analysis
              │   4. Launch: create new orchestration via AssignmentService
              │   5. Repeat until converged or max_iterations
              │
              ▼
          AssignmentService (control_plane/orchestration/)
              │   - Creates/splices orchestration runs
              │   - Tracks run lineage in OrchestrationRunStore
              │
              ▼
          AgentScheduleEngine (control_plane/agent_scheduler/)
                  - Time-based (cron) autonomous agent dispatch
                  - Not yet integrated with Platform AI sessions
```

**New modules introduced with Platform AI:**

| Module | Purpose |
|--------|---------|
| `control_plane/platform_ai/` | Session management + autonomous runtime loop |
| `control_plane/orchestration/` | Assignment service + run store (lineage, graph, splice) |
| `control_plane/agent_scheduler/` | Cron-based scheduled agent dispatch |
| `control_plane/connections/` | Project/bot connection resolver |

**New database tables (Platform AI):**

| Table | Purpose |
|-------|---------|
| `platform_ai_sessions` | Sessions: mode, status, orchestration bindings |
| `platform_ai_events` | Immutable action trace per session |
| `platform_ai_messages` | Operator ↔ AI conversation history |
| `platform_ai_test_suites` | Quality assertion definitions |
| `platform_ai_test_runs` | Test execution results with scores |
| `orchestration_runs` | Run lineage: parent/child, graph snapshots, node overrides |
| `agent_schedules` | Cron schedule definitions |
| `agent_schedule_runs` | Dispatch history per schedule |

**Current status:** The autonomous tuner loop has been implemented and gone through several fix iterations (dead loop stop, state reset on resume, exhausted state handling). As of April 2, 2026 it is still under active testing and **not functioning reliably**. Known issues include race conditions in session loop spawning, incomplete control action stubs, and stalled-state detection that terminates prematurely.

---

## Known Architectural Debt

1. **Single SQLite file for everything**: All services share one SQLite file with no connection pooling. High-concurrency write workloads (many parallel tasks) will hit SQLite write-lock contention. `aiosqlite` serializes writes but doesn't eliminate the bottleneck.

2. **In-memory registry with SQLite persistence**: `BotRegistry`, `WorkerRegistry`, `ProjectRegistry`, `ModelRegistry` hold an in-memory dict backed by SQLite. If the control plane restarts mid-write, in-memory and DB state can diverge (though the init-lock pattern mitigates this).

3. **Naive 64-dim hash embedding**: Both `VaultManager` and `ChatManager` use a SHA-256 hash-based 64-dimensional embedding for similarity search. This is purely a bag-of-words approximation — semantic quality is poor. Real vector embeddings (via a worker embedding model) are not wired up.

4. **Scheduler imports dashboard models**: `scheduler.py` has a runtime import of `dashboard.db` and `dashboard.models` to resolve bot-scoped connection rows. This creates a circular dependency between control_plane and dashboard packages.

5. **Rate limit store is per-process**: `guards.py` stores rate limit buckets in `request.app.state`. Under multi-worker Gunicorn/uvicorn this is per-worker, not shared. Rate limits are not effective in multi-process deploys.

6. **No task queue**: Tasks are dispatched synchronously inside `create_task()`. Under load, the HTTP call to the worker blocks the asyncio event loop for the duration of inference. A proper queue (Celery, ARQ, etc.) is absent.

7. **`_extract_scope_lock` heuristics are fragile**: The scope lock extraction in `pm_orchestrator.py` uses keyword matching on a small hardcoded phrase catalog. It does not generalise well to arbitrary instructions.

8. **Duplicate `_transform_template_value` / `_lookup_payload_path`**: These functions are defined independently in both `task_manager.py` and `scheduler.py` with slightly different signatures and behaviours.
