# NexusAI — Master Progress Document

> **Living document.** After every pull request, a new dated entry is appended to the [Changelog](#changelog) section at the bottom. Nothing is ever deleted from this file.

---

## 📋 Table of Contents

1. [Vision & Purpose](#vision--purpose)
2. [Full System Design](#full-system-design)
3. [Component Breakdown](#component-breakdown)
4. [Current State — Snapshot 2026-03-04 12:00](#current-state--snapshot-2026-03-04-1200)
5. [What Is Built](#what-is-built)
6. [Known Issues & Bugs](#known-issues--bugs)
7. [Complete Build Roadmap](#complete-build-roadmap)
8. [File Structure — End State](#file-structure--end-state)
9. [Tech Stack](#tech-stack)
10. [Changelog](#changelog)

---

## Vision & Purpose

NexusAI is a **self-hosted, distributed AI orchestration platform**. It is simultaneously:

- A **distributed LLM compute cluster** — any number of PCs/servers act as worker nodes, running local or cloud-backed models
- A **project management system** — multiple projects (each mapping to one or more repos), with bots, tasks, backlogs, dependencies, and completion tracking
- A **conversational AI interface** — a first-class chat UI (like GitHub Copilot Chat / ChatGPT / Claude), where you interact with any configured model and can assign tasks inline
- A **data vault + MCP system** — ingest files, URLs, code, and chat history into a searchable vault; assign contexts to chats and bots; use the Model Context Protocol (MCP) for standardized context delivery
- A **hardware-aware scheduler** — worker nodes self-report their CPU/RAM/GPU hardware profile; the platform calculates which local models each node can run and estimates task completion time
- **Moldable for any use case** — configure bots, projects, models, and workflows entirely from the UI; no code changes required

### The Core User Story

> You open the dashboard, run the onboarding wizard, connect your GPU rigs as worker nodes, define specialized bots (coder, reviewer, PM, researcher), connect your repos as projects, open Chat, and say:
> *"Build me a REST API for user authentication with tests."*
>
> The Project Manager Bot decomposes this into tasks with dependencies, assigns them to specialist bots, and those bots execute on your worker nodes — streaming results back to your chat window in real time. You watch the task board update live as each bot completes its work.

---

## Full System Design

```
╔══════════════════════════════════════════════════════════════════════════╗
║                         NEXUSAI PLATFORM                                ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║  ┌─────────────────────────────────────────────────────────────────┐    ║
║  │                    DASHBOARD  (port 5000)                        │    ║
║  │                                                                  │    ║
║  │  Nav: Overview │ Projects │ Chat │ Bots │ Workers │ Vault │ ⚙   │    ║
║  │                                                                  │    ║
║  │  Overview     Live stats, recent tasks, system health            │    ║
║  │  Projects     Multi-repo projects, bridged or isolated           │    ║
║  │  Chat         Streaming chat, model selector, task assignment    │    ║
║  │  Bots         Create/edit bots, task board, backlog/done         │    ║
║  │  Workers      Detail page: hardware, loaded models, GPU graphs   │    ║
║  │  Vault        Data ingestion, chat history, MCP contexts         │    ║
║  │  Settings     API keys, model catalog, users, platform config    │    ║
║  └─────────────────────────────────────────────────────────────────┘    ║
║                                │                                         ║
║  ┌─────────────────────────────────────────────────────────────────┐    ║
║  │                 CONTROL PLANE  (port 8000)                       │    ║
║  │                                                                  │    ║
║  │  ProjectRegistry   BotRegistry    WorkerRegistry                 │    ║
║  │  TaskManager       Scheduler      DependencyEngine               │    ║
║  │  ChatManager       VaultManager   MCPBroker                      │    ║
║  │  ApiKeyVault       ModelCatalog   HardwareAnalyzer               │    ║
║  └─────────────────────────────────────────────────────────────────┘    ║
║                                │                                         ║
║         ┌──────────────────────┼──────────────────────┐                 ║
║         ▼                      ▼                      ▼                 ║
║  ┌─────────────┐       ┌─────────────┐        ┌─────────────────┐      ║
║  │ Worker Node │       │ Worker Node │        │   Cloud APIs    │      ║
║  │  (PC / GPU) │       │  (PC / CPU) │        │ OpenAI / Claude │      ║
║  │             │       │             │        │ Gemini / etc    │      ║
║  │ nexus-worker│       │ nexus-worker│        └─────────────────┘      ║
║  │ standalone  │       │ standalone  │                                   ║
║  │ program     │       │ program     │                                   ║
║  │             │       │             │                                   ║
║  │ Hardware    │       │ Hardware    │                                   ║
║  │ detector    │       │ detector    │                                   ║
║  │ Model runner│       │ Model runner│                                   ║
║  │ Task queue  │       │ Task queue  │                                   ║
║  │ GPU/CPU mon │       │ CPU/RAM mon │                                   ║
║  └─────────────┘       └─────────────┘                                  ║
╚══════════════════════════════════════════════════════════════════════════╝
```

### Data Flow — Chat to Task Execution

```
User types in Chat:
  "Build me a REST API for user auth with tests"
           │
           ▼
  Project Manager Bot receives message
           │
           ▼
  PM decomposes into dependency-ordered tasks:
    Task 1: Design API schema          → Architecture Bot
    Task 2: Write auth endpoints       → Code Bot     (depends on Task 1)
    Task 3: Write unit tests           → Test Bot     (depends on Task 2)
    Task 4: Code review                → Review Bot   (depends on Task 3)
           │
           ▼
  DependencyEngine resolves DAG:
    - Task 1 dispatched immediately
    - Tasks 2, 3, 4 queued as "blocked"
    - Each unblocks when its dependency reaches "completed"
           │
           ▼
  Scheduler selects worker for each task:
    - Checks bot backend priority list
    - Checks worker health + GPU availability
    - Estimates completion time via HardwareAnalyzer
    - Dispatches to worker node
           │
           ▼
  Worker executes → streams result back
  Task status updates: queued → running → completed
  Results appear in Chat + Task Board in real time
```

---

## Component Breakdown

### 1. Projects
- Maps to one or more GitHub repos (or standalone — no repo required)
- **Isolated mode**: bots, tasks, vault data, and chat are completely siloed
- **Bridged mode**: explicitly linked projects can share bots, reference each other's vault, cross-assign tasks
- Per-project: bot roster, task board, chat history, vault namespace, settings overrides
- GitHub integration: webhooks, PR context, code-aware bot context

### 2. Chat System
- Persistent conversation history per project (or global/cross-project scope)
- **Streaming responses** via SSE (worker → control plane → browser)
- **Model selector per conversation** — pick any configured bot/model for this session
- **Context assignment** — attach vault documents, project files, or past chats as context
- **Scope modes**: wide open (all projects), project-specific, or cross-bridge
- **Inline task assignment** — `@assign` or button in chat sends task to PM Bot
- Chat history automatically saved to DB and optionally ingested into the vault
- Schema: `conversations` table → `messages` table (role, content, model used, token counts, timestamp)
- Can ingest past chats as training/context data for future sessions

### 3. Bots
- Each bot: name, role, system prompt, backends (priority-ordered with fallback), enabled/disabled
- **Data backend config**: which vault namespaces and contexts the bot can access
- **Task board per bot**: Queued → Running → Done (Kanban-style)
- **Backlog**: tasks waiting on dependencies before they can run
- **Project Manager Bot** (special): receives high-level requests, uses agentic decomposition to create sub-tasks with dependency ordering, assigns to specialist bots
- All bot config editable in UI — no YAML required (though YAML still works for bulk setup)
- Support for multiple API keys per provider, selectable per bot backend

### 4. Worker Nodes — `nexus-worker` Standalone Program
- **Completely standalone** — runs on any PC independently, registers with control plane on startup
- **Hardware detection on startup**:
  - CPU: cores, threads, clock speed, architecture
  - RAM: total, available, used
  - GPU(s): name, VRAM total/used, utilization, temperature (NVIDIA/ROCm/Metal/CPU-only)
- **Model compatibility calculator**: given hardware profile → list of runnable models with notes (fits in VRAM / needs CPU offload / too large)
- **Task time estimator**: given prompt token count + model + hardware → estimated seconds to complete
- **Local model runner**: Ollama, vLLM, LM Studio, llama.cpp — whatever is installed on that machine
- **Streaming inference** via `/infer/stream` endpoint (SSE back to control plane)
- **Live health metrics**: CPU%, RAM used/total, per-GPU utilization + VRAM, temperature, queue depth, currently loaded model
- Worker detail page in dashboard: hardware profile card, model capability list, live resource graphs, current task list

### 5. Data Vault + MCP
- **Ingest anything**: files (PDF, Markdown, code, plain text), URLs, past chat sessions, task results, API responses
- **Vault namespaces**: global, per-project, per-bot — controls which bots/chats can see what
- **Vector search** via embeddings — semantic retrieval of relevant chunks
- **MCP (Model Context Protocol)**: standardized interface for bots to pull relevant context before inference
- Chat sessions can be flagged for vault ingestion, then retrieved as context in future sessions
- Ingestion pipeline: raw content → chunking → embedding → stored with metadata (source, project, timestamp, tags)
- Vault browser in dashboard: list, search, preview, delete, manage namespaces

### 6. API Key Vault
- Multiple named keys per provider (e.g., "OpenAI Prod", "OpenAI Dev")
- Keys stored encrypted in SQLite
- Referenced by `api_key_ref` string in bot backend configs — never hardcoded
- CRUD management in Settings → API Keys tab
- Providers: OpenAI, Anthropic (Claude), Google (Gemini), custom

### 7. Model Catalog
- Registry of all available models: local (per worker) + cloud
- Per model: name, provider, context window size, capabilities (chat/embedding/tool), cost per token (for cloud), notes
- Used by scheduler to match tasks to appropriate models
- Displayed in bot backend config UI as a dropdown

### 8. Dependency Engine
- Tasks have an optional `depends_on: [task_id, ...]` list
- Scheduler checks deps before dispatching: only runs a task when all dependencies are `completed`
- PM Bot generates dependency graphs when decomposing complex requests
- Future: visual DAG viewer in task board

### 9. Settings
- **API Keys tab**: CRUD for named keys per provider
- **Model Catalog tab**: define available models
- **General tab**: site name, control plane host/port
- **Auth tab**: session timeout, registration policy, secret key
- **LLM tab**: default model, embedding model, heartbeat interval
- **Logging tab**: log level, log file path
- **Advanced tab**: import/export YAML/JSON config, audit log viewer
- All settings stored in SQLite via `SettingsManager` singleton, editable at runtime without restart

---

## Current State — Snapshot 2026-03-04 12:00

This is the authoritative snapshot of what exists in the repository at the start of active development.

### Repository Structure (as of 2026-03-04)

```
NexusAI/
├── control_plane/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory, lifespan, heartbeat checker
│   ├── Dockerfile
│   ├── api/
│   │   ├── bots.py              # GET/POST/DELETE /v1/bots, enable/disable
│   │   ├── tasks.py             # GET/POST /v1/tasks, GET /v1/tasks/{id}
│   │   └── workers.py           # GET/POST/DELETE /v1/workers, heartbeat
│   ├── registry/
│   │   ├── bot_registry.py      # Async in-memory bot store (CRUD + enable/disable)
│   │   └── worker_registry.py   # Async in-memory worker store + heartbeat tracking
│   ├── scheduler/
│   │   └── scheduler.py         # Dispatches tasks to backends with fallback chain
│   └── task_manager/
│       └── task_manager.py      # Creates/tracks tasks, persists to SQLite via aiosqlite
│
├── worker_agent/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, self-registers with CP, sends heartbeats
│   ├── Dockerfile
│   ├── gpu_monitor.py           # pynvml-based GPU metrics
│   ├── api/
│   │   ├── capabilities.py      # GET /capabilities
│   │   ├── health.py            # GET /health
│   │   └── infer.py             # POST /infer — routes to backend
│   └── backends/
│       ├── base.py              # Abstract BaseBackend
│       ├── ollama_backend.py    # Ollama inference
│       ├── openai_backend.py    # OpenAI API inference
│       ├── claude_backend.py    # Anthropic Claude inference
│       ├── gemini_backend.py    # Google Gemini inference
│       └── cli_backend.py       # CLI subprocess inference
│
├── dashboard/
│   ├── __init__.py
│   ├── app.py                   # Flask app factory, all blueprint registration
│   ├── Dockerfile
│   ├── auth.py                  # Login/logout, bcrypt password hashing
│   ├── cp_client.py             # Sync HTTP client → control plane API
│   ├── db.py                    # SQLAlchemy engine + session factory
│   ├── models.py                # ORM: User, Bot, Worker, Task
│   ├── onboarding.py            # 5-step first-run wizard
│   ├── settings.py              # SettingsManager-backed settings blueprint
│   ├── routes/
│   │   ├── bots.py              # /bots page + /api/bots CRUD
│   │   ├── events.py            # GET /events — SSE live stats stream
│   │   ├── tasks.py             # /tasks page + /api/tasks CRUD
│   │   ├── users.py             # /users page + /api/users CRUD
│   │   └── workers.py           # /workers page + /api/workers CRUD
│   ├── templates/
│   │   ├── base.html            # Nav, layout, flash messages
│   │   ├── index.html           # Overview — stat cards + SSE live update
│   │   ├── bots.html            # Bots table + add/edit/delete modals
│   │   ├── workers.html         # Workers table + add/edit/delete modals
│   │   ├── tasks.html           # Tasks table + expandable detail rows
│   │   ├── users.html           # Users table + invite/delete modals
│   │   ├── settings.html        # Tabbed settings panel + audit log
│   │   ├── login.html           # Login form
│   │   └── onboarding/
│   │       ├── base.html
│   │       ├── step1_welcome.html
│   │       ├── step2_admin.html
│   │       ├── step3_llm.html
│   │       ├── step4_worker.html
│   │       └── step5_complete.html
│   └── static/
│       └── style.css            # Dark theme CSS
│
├── shared/
│   ├── __init__.py
│   ├── models.py                # Pydantic: Bot, Worker, Task, BackendConfig, etc.
│   ├── exceptions.py            # NexusError hierarchy
│   ├── config_loader.py         # YAML loading + merging utilities
│   └── settings_manager.py      # Thread-safe SQLite-backed settings singleton
│
├── config/
│   ├── README.md
│   ├── nexus_config.yaml
│   ├── workers/
│   │   ├── example_worker.yaml
│   │   └── local_worker.yaml
│   └── bots/
│       ├── example_bot.yaml
│       └── assistant_bot.yaml
│
├── tests/
│   ├── conftest.py
│   ├── test_bot_registry.py
│   ├── test_worker_registry.py
│   ├── test_task_manager.py
│   ├── test_control_plane_api.py
│   ├── test_worker_agent_backends.py
│   ├── test_dashboard_smoke.py
│   ├── test_dashboard_onboarding.py
│   └── test_shared_models.py
│
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
├── .env.example
└── .gitignore
```

---

## What Is Built

### ✅ Fully Built and Working

| Component | Files | Notes |
|---|---|---|
| **Control Plane FastAPI app** | `control_plane/main.py` | Lifespan startup, config loading, heartbeat checker task |
| **Bot Registry** | `control_plane/registry/bot_registry.py` | Async in-memory CRUD + enable/disable |
| **Worker Registry** | `control_plane/registry/worker_registry.py` | Async in-memory + heartbeat tracking + auto-offline |
| **Scheduler** | `control_plane/scheduler/scheduler.py` | Backend fallback chain: local_llm → remote_llm → cloud_api → cli |
| **Task Manager** | `control_plane/task_manager/task_manager.py` | Create/track tasks, SQLite persistence via aiosqlite |
| **Control Plane REST API** | `control_plane/api/` | `/v1/workers`, `/v1/bots`, `/v1/tasks` with full CRUD |
| **Worker Agent FastAPI app** | `worker_agent/main.py` | Self-registers with CP on startup, sends heartbeats every 15s with GPU metrics |
| **Worker Inference Backends** | `worker_agent/backends/` | Ollama, OpenAI, Claude, Gemini, CLI (subprocess) |
| **GPU Monitor** | `worker_agent/gpu_monitor.py` | pynvml-based GPU memory + utilization |
| **Worker API** | `worker_agent/api/` | `/health`, `/capabilities`, `/infer` |
| **Dashboard Flask App** | `dashboard/app.py` | App factory, CSRF, Flask-Login, all blueprints registered |
| **Auth** | `dashboard/auth.py` | Login/logout, bcrypt, open-redirect protection |
| **Onboarding Wizard** | `dashboard/onboarding.py` + templates | 5-step: Welcome → Admin → LLM Backend → Worker → Complete |
| **Dashboard Routes** | `dashboard/routes/` | Workers, Bots, Tasks, Users, Events — all with page + JSON API |
| **SSE Live Events** | `dashboard/routes/events.py` | `/events` streams worker/bot/task stats every 5s |
| **CPClient** | `dashboard/cp_client.py` | Sync HTTP client to CP with fallback to local DB |
| **Settings System** | `dashboard/settings.py` + `shared/settings_manager.py` | SQLite-backed, tabbed UI, audit log, YAML/JSON export/import |
| **Dashboard Templates** | `dashboard/templates/` | All pages: overview, bots, workers, tasks, users, settings, login, onboarding |
| **Dashboard ORM Models** | `dashboard/models.py` | SQLAlchemy: User, Bot, Worker, Task |
| **Shared Pydantic Models** | `shared/models.py` | Bot, Worker, Task, BackendConfig, Capability, WorkerMetrics, etc. |
| **Shared Exceptions** | `shared/exceptions.py` | Full NexusError hierarchy |
| **Config Loader** | `shared/config_loader.py` | YAML load, merge, load-all-from-dir |
| **Docker Compose** | `docker-compose.yml` | Orchestrates all 3 services with healthchecks |
| **Test Suite** | `tests/` | Unit + integration tests for registry, scheduler, task manager, backends, dashboard |

---

## Known Issues & Bugs

These are confirmed issues that must be fixed before serious testing:

| # | Issue | Location | Impact |
|---|---|---|---|
| 1 | ~~**`shared/` not in Docker build context for some services**~~ ✅ **RESOLVED** | `control_plane/Dockerfile`, `worker_agent/Dockerfile`, `dashboard/Dockerfile` | Added `ENV PYTHONPATH=/app` to all three Dockerfiles so `shared/` is always importable regardless of working directory |
| 2 | ~~**Settings port mismatch**~~ ✅ **RESOLVED** | `shared/settings_manager.py` line ~65 | `control_plane_port` default changed from `"8080"` to `"8000"`; dashboard CPClient now connects to the correct port |
| 3 | ~~**`Bot` ORM has `routing_rules` column; shared Pydantic `Bot` model does not**~~ ✅ **RESOLVED** | `dashboard/models.py` vs `shared/models.py` | Added `routing_rules: Optional[Any] = None` to shared Pydantic `Bot` model; field now round-trips correctly |
| 4 | **`Worker.enabled` in dashboard ORM but not in shared Pydantic `Worker` model** | `dashboard/models.py` vs `shared/models.py` | Inconsistent model; `enabled` field will not round-trip through CP API |
| 5 | **No `api_key_ref` resolution in Scheduler** | `control_plane/scheduler/scheduler.py` | `api_key_ref` is stored on `BackendConfig` but the scheduler reads raw env var keys — no vault lookup implemented |
| 6 | **Task execution has no dependency engine** | `control_plane/task_manager/task_manager.py` | Tasks run immediately on creation; no "wait for Task A before Task B" logic |
| 7 | **No streaming inference** | `worker_agent/api/infer.py`, `control_plane/scheduler/scheduler.py` | All inference is blocking request/response; chat UI will be unusable without streaming |
| 8 | **No control plane authentication** | `control_plane/api/` | CP API is fully open; any network client can register workers, create bots, submit tasks |

---

## Complete Build Roadmap

### Phase 1 — Fix Foundation

- [x] Fix `shared/` Docker context — ensure all Dockerfiles copy `shared/` correctly
- [x] Fix settings port mismatch — change default `control_plane_port` to `"8000"`
- [x] Fix `Bot` schema mismatch — add `routing_rules` to shared Pydantic `Bot` model (optional field)
- [x] Fix `Worker` schema mismatch — add `enabled` to shared Pydantic `Worker` model
- [x] Add `api_key_ref` resolution in Scheduler — look up key from env-var or settings store
- [x] Verify all 3 services start, communicate, and pass health checks with `docker compose up`

### Phase 2 — Worker Node Standalone Program (`nexus-worker`)

- [ ] Create `nexus_worker/` as a standalone Python package
- [ ] Hardware detection module (CPU, RAM, GPU via psutil + pynvml)
- [ ] Model compatibility calculator (given hardware → runnable models list)
- [ ] Task time estimator (prompt tokens + model + hardware → ETA)
- [ ] Streaming inference: add `/infer/stream` SSE endpoint
- [ ] Local model manager: query Ollama/vLLM for installed models
- [ ] Packaging: entry point, `config.yaml.example`, README
- [ ] Worker detail page in dashboard: hardware card, model list, live CPU/RAM/GPU graphs

### Phase 3 — Data + Chat + Vault Backend

- [x] **Projects**: `projects` DB table, `ProjectRegistry`, `/v1/projects` REST API, isolation + bridge logic
- [x] **API Key Vault**: `api_keys` DB table (encrypted), CRUD API, Scheduler resolution
- [x] **Model Catalog**: `models` DB table, CRUD API, used by Scheduler + UI
- [x] **Chat System**: `conversations` + `messages` tables, `ChatManager`, streaming SSE, context injection
- [x] **Data Vault**: `vault_items` + `vault_chunks` tables, `VaultManager`, ingestion pipeline, vector search
- [x] **MCP Broker**: standardized context pull interface for bots pre-inference
- [x] **Dependency Engine**: `task_dependencies` table, `depends_on` on Task, DAG resolver, `blocked` status

### Phase 4 — Dashboard UI

#### 4a. Design System + Navigation Refresh
- [x] Expand nav: add Projects, Chat, Vault
- [ ] Consistent dark theme, loading/empty/error states, responsive layout

#### 4b. Worker Detail Page
- [x] Hardware profile card, model capability list, live resource graphs, current task list
- [x] Worker enable/disable/delete/ping actions

#### 4c. Bot Detail + Task Board
- [x] Bot edit form: name, role, system prompt, backend chain editor
- [x] Backend chain: add/remove/reorder backends, model picker, worker picker, API key picker
- [x] Task board Kanban: Queued | Blocked | Running | Completed | Failed
- [x] Task detail modal, backlog view

#### 4d. Projects Page
- [x] Project list + create modal
- [x] Project detail: bots, task board, vault items, settings overrides
- [x] Bridge management UI

#### 4e. Chat Page
- [x] Conversation sidebar, streaming message area, model/bot selector
- [x] Context picker (vault items, files, past chats), scope selector
- [x] Inline task assignment (`@assign` → PM Bot), task assignment modal
- [x] Message actions: copy, re-run, send to vault
- [x] Chat history persistence + retrieval

#### 4f. Vault Page
- [x] Upload panel (file, URL, paste), vault item list with search/filter
- [x] Item detail: preview, chunk count, embedding status, metadata
- [ ] Namespace manager, bulk actions
- [x] "Ingest this chat" button on chat page

#### 4g. Settings Additions
- [x] API Keys tab, Model Catalog tab, Projects tab
- [x] Fix control plane port setting (Bug #2) — **RESOLVED**

#### 4h. Overview Page Enhancement
- [x] Recent activity feed, worker health mini-bars, quick links, system alerts

### Phase 5 — Agentic Workflow

- [ ] PM Bot system prompt + task decomposition logic
- [ ] Chat → PM Bot → dependency graph creation → multi-bot assignment
- [ ] Results aggregation back to originating chat conversation
- [ ] Task status events streamed to chat window in real time
- [ ] Optional: visual DAG viewer for task dependency graphs

### Phase 6 — GitHub Integration

- [ ] GitHub OAuth / PAT connection per project
- [ ] Webhook ingestion: PR, push, issue events
- [ ] Code-aware bot context: repo file tree + file contents as vault items
- [ ] PR review bot workflow

---

## File Structure — End State

```
NexusAI/
├── control_plane/
│   ├── api/
│   │   ├── bots.py
│   │   ├── chat.py              ← NEW (Phase 3)
│   │   ├── keys.py              ← NEW (Phase 3)
│   │   ├── models_catalog.py    ← NEW (Phase 3)
│   │   ├── projects.py          ← NEW (Phase 3)
│   │   ├── tasks.py
│   │   ├── vault.py             ← NEW (Phase 3)
│   │   └── workers.py
│   ├── chat/
│   │   └── chat_manager.py      ← NEW (Phase 3)
│   ├── keys/
│   │   └── key_vault.py         ← NEW (Phase 3)
│   ├── registry/
│   │   ├── bot_registry.py
│   │   ├── project_registry.py  ← NEW (Phase 3)
│   │   └── worker_registry.py
│   ├── scheduler/
│   │   ├── dependency_engine.py ← NEW (Phase 3)
│   │   └── scheduler.py
│   ├── task_manager/
│   │   └── task_manager.py
│   └── vault/
│       ├── chunker.py           ← NEW (Phase 3)
│       ├── mcp_broker.py        ← NEW (Phase 3)
│       └── vault_manager.py     ← NEW (Phase 3)
│
├── nexus_worker/                ← NEW standalone package (Phase 2)
│   ├── __main__.py
│   ├── agent.py
│   ├── api/
│   │   ├── health.py
│   │   ├── infer.py
│   │   └── infer_stream.py
│   ├── backends/
│   │   ├── ollama_backend.py
│   │   ├── openai_backend.py
│   │   ├── claude_backend.py
│   │   ├── gemini_backend.py
│   │   └── cli_backend.py
│   ├── hardware/
│   │   ├── detector.py          ← NEW (Phase 2)
│   │   └── model_advisor.py     ← NEW (Phase 2)
│   ├── config.yaml.example
│   └── README.md
│
├── worker_agent/                ← keep for Docker-based deployment
│   └── ... (extend with streaming + hardware detection)
│
├── dashboard/
│   ├── routes/
│   │   ├── bots.py
│   │   ├── chat.py              ← NEW (Phase 4e)
│   │   ├── events.py
│   │   ├── projects.py          ← NEW (Phase 4d)
│   │   ├── tasks.py
│   │   ├── users.py
│   │   ├── vault.py             ← NEW (Phase 4f)
│   │   └── workers.py
│   └── templates/
│       ├── base.html            ← UPDATE (Phase 4a)
│       ├── bots.html
│       ├── bot_detail.html      ← NEW (Phase 4c)
│       ├── chat.html            ← NEW (Phase 4e)
│       ├── index.html           ← UPDATE (Phase 4h)
│       ├── projects.html        ← NEW (Phase 4d)
│       ├── project_detail.html  ← NEW (Phase 4d)
│       ├── settings.html        ← UPDATE (Phase 4g)
│       ├── tasks.html
│       ├── users.html
│       ├── vault.html           ← NEW (Phase 4f)
│       ├── worker_detail.html   ← NEW (Phase 4b)
│       └── workers.html
│
├── shared/
│   ├── models.py                ← UPDATE (Phase 1)
│   ├── exceptions.py
│   ├── config_loader.py
│   └── settings_manager.py     ← UPDATE (Phase 1, fix port)
│
├── tests/
│   └── ... (expand with each phase)
│
├── PROGRESS.md                  ← THIS FILE
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Control Plane | Python 3.11, FastAPI, Uvicorn, aiosqlite, Pydantic v2, httpx |
| Worker Node | Python 3.11, FastAPI, Uvicorn, httpx, psutil, pynvml |
| Dashboard | Python 3.11, Flask 3, Flask-Login, Flask-WTF, SQLAlchemy 2, Gunicorn |
| Database | SQLite (aiosqlite for async CP, SQLAlchemy sync for dashboard) |
| Embeddings (planned) | sentence-transformers or Ollama embedding models |
| Vector Search (planned) | SQLite + manual cosine similarity (start), or ChromaDB/pgvector (scale) |
| Frontend | Jinja2 templates, vanilla JS, SSE for live updates |
| Containerization | Docker, Docker Compose |
| Linting/Formatting | Ruff |
| Testing | pytest, pytest-asyncio, anyio, httpx (ASGI transport) |

---

## Changelog

### 2026-03-04 12:00 — Initial Snapshot

**Status:** Foundation built across 14 merged PRs. System is architecturally sound but not yet production-ready or feature-complete.

**What was accomplished before this snapshot:**
- Full control plane with FastAPI, bot/worker registries, scheduler with backend fallback, task manager with SQLite persistence
- Worker agent with self-registration, heartbeats, GPU monitoring, and backends for Ollama/OpenAI/Claude/Gemini/CLI
- Dashboard with Flask, full auth, 5-step onboarding wizard, settings system, all route blueprints (workers/bots/tasks/users/events/settings), all HTML templates, SSE live events
- Shared Pydantic models, exceptions, config loader, settings manager
- Comprehensive test suite (unit + integration)
- Docker Compose orchestration
- `PROGRESS.md` created (this file)

**Known issues identified** — see Known Issues section above.

**Next:** Begin Phase 1 — Fix Foundation (Docker shared/ mounting, port mismatch, schema mismatches)  
*This document is maintained by the development team and updated after every pull request. Last updated: 2026-03-04 18:26:59*

---

### 2026-03-04 18:43 — Phase 1: Fix Foundation

**Status:** All 6 Phase 1 foundation issues resolved.

**Changes made:**

- **Fix 1 — Dockerfiles rewritten** (`control_plane/Dockerfile`, `worker_agent/Dockerfile`, `dashboard/Dockerfile`): Replaced `COPY . .` with explicit `COPY shared/ shared/`, `COPY <service>/ <service>/`, and `COPY config/ config/` lines. This makes it unambiguous that `shared/` is always present in every service image and avoids any future confusion about build context.

- **Fix 2 — Settings port corrected** (`shared/settings_manager.py`): Changed `control_plane_port` default value from `"8080"` to `"8000"`. The dashboard's `CPClient` reads this setting, so with the wrong port every CP API call was silently failing and falling back to the local DB.

- **Fix 3 — `Bot` Pydantic model updated** (`shared/models.py`): Added `system_prompt: Optional[str] = None` and `routing_rules: Optional[Any] = None` to the shared `Bot` model so it matches the dashboard ORM `Bot` and can round-trip without schema errors.

- **Fix 4 — `Worker` Pydantic model updated** (`shared/models.py`): Added `enabled: bool = True` to the shared `Worker` model so it matches the dashboard ORM `Worker`.

- **Fix 5 — Scheduler error messages improved** (`control_plane/scheduler/scheduler.py`): Updated `_call_openai`, `_call_claude`, and `_call_gemini` to use `.strip()` on the retrieved API key and to emit actionable error messages that name the exact environment variable the user needs to set.

- **Tests added** (`tests/test_shared_models.py`): Added `test_worker_model_has_enabled_field`, `test_bot_model_has_routing_rules_field`, and `test_bot_model_has_system_prompt_field` to verify the new model fields.

---

### 2026-03-04 21:20 — Phase 3: Projects Backend (Slice 1)

**Status:** Core projects backend is implemented and tested.

**Changes made:**

- Added shared `Project` model with isolation/bridge fields (`shared/models.py`).
- Added `ProjectNotFoundError` (`shared/exceptions.py`).
- Added persistent `ProjectRegistry` with SQLite-backed storage and bridge consistency logic (`control_plane/registry/project_registry.py`).
- Added full `/v1/projects` REST API with CRUD + bridge add/remove endpoints (`control_plane/api/projects.py`).
- Wired project registry and routes into control plane app startup (`control_plane/main.py`) and test app fixtures (`tests/conftest.py`).
- Updated packaging metadata to prevent dependency/install drift:
  - Added runtime dependencies to `[project.dependencies]` (`pyproject.toml`)
  - Added explicit setuptools package discovery for multi-package layout (`pyproject.toml`)
- Added tests for project registry behavior and API endpoints:
  - `tests/test_project_registry.py`
  - updates in `tests/test_control_plane_api.py`

**Validation:**

- `pytest -q tests/test_project_registry.py tests/test_control_plane_api.py tests/test_bot_registry.py tests/test_worker_registry.py tests/test_task_manager.py` → **28 passed**
- `pytest -q` → **56 passed**

---

### 2026-03-04 21:48 — Phase 3: API Key Vault (Slice 2)

**Status:** Encrypted API key storage and scheduler key-resolution are implemented.

**Changes made:**

- Added encrypted key vault backed by SQLite `api_keys` table (`control_plane/keys/key_vault.py`).
- Added API key CRUD endpoints (`control_plane/api/keys.py`):
  - `POST /v1/keys`
  - `GET /v1/keys`
  - `GET /v1/keys/{name}`
  - `DELETE /v1/keys/{name}`
- Wired key vault into control plane startup/state and registered key routes (`control_plane/main.py`).
- Updated scheduler to resolve `api_key_ref` from key vault first, then fall back to environment variables for backward compatibility (`control_plane/scheduler/scheduler.py`).
- Added `APIKeyNotFoundError` (`shared/exceptions.py`).
- Updated test fixture wiring to include a test key vault (`tests/conftest.py`).
- Added tests:
  - `tests/test_key_vault.py`
  - `tests/test_scheduler_api_keys.py`
  - key API coverage in `tests/test_control_plane_api.py`
- Added `cryptography` runtime dependency (`requirements.txt`, `pyproject.toml`).

**Validation:**

- `pytest -q tests/test_key_vault.py tests/test_scheduler_api_keys.py tests/test_control_plane_api.py` → **18 passed**
- `pytest -q` → **63 passed**

---

### 2026-03-04 22:10 — Phase 3: Model Catalog (Slice 3)

**Status:** Model catalog backend and scheduler integration are implemented.

**Changes made:**

- Added shared catalog model type (`shared/models.py`): `CatalogModel`.
- Added `CatalogModelNotFoundError` (`shared/exceptions.py`).
- Added persistent model registry backed by SQLite `models` table (`control_plane/registry/model_registry.py`).
- Added model catalog CRUD API (`control_plane/api/models_catalog.py`):
  - `POST /v1/models`
  - `GET /v1/models`
  - `GET /v1/models/{model_id}`
  - `PUT /v1/models/{model_id}`
  - `DELETE /v1/models/{model_id}`
- Wired model registry and routes into control-plane startup and test app fixture:
  - `control_plane/main.py`
  - `tests/conftest.py`
- Updated scheduler to enforce catalog compatibility when catalog entries exist:
  - Validates `backend.provider` + `backend.model` against enabled catalog entries
  - Keeps backward compatibility when the catalog is empty
  - (`control_plane/scheduler/scheduler.py`)
- Added tests:
  - `tests/test_model_registry.py`
  - `tests/test_scheduler_model_catalog.py`
  - model API coverage in `tests/test_control_plane_api.py`

**Validation:**

- `pytest -q tests/test_model_registry.py tests/test_scheduler_model_catalog.py tests/test_control_plane_api.py` → **20 passed**
- `pytest -q` → **70 passed**

---

### 2026-03-04 22:32 — Phase 3: Chat System (Slice 4)

**Status:** Chat persistence and API streaming endpoints are implemented.

**Changes made:**

- Added shared chat models (`shared/models.py`):
  - `ChatConversation`
  - `ChatMessage`
- Added `ConversationNotFoundError` (`shared/exceptions.py`).
- Added chat manager with SQLite-backed persistence:
  - `conversations` table
  - `messages` table
  - conversation/message CRUD helpers
  - (`control_plane/chat/chat_manager.py`)
- Added chat API routes (`control_plane/api/chat.py`):
  - `POST /v1/chat/conversations`
  - `GET /v1/chat/conversations`
  - `GET /v1/chat/conversations/{conversation_id}`
  - `GET /v1/chat/conversations/{conversation_id}/messages`
  - `POST /v1/chat/conversations/{conversation_id}/messages`
  - `POST /v1/chat/conversations/{conversation_id}/stream` (SSE)
- Implemented context injection support for message execution via `context_items`.
- Wired chat manager/router into control-plane startup and test fixture wiring:
  - `control_plane/main.py`
  - `tests/conftest.py`
- Added package init modules:
  - `control_plane/chat/__init__.py`
  - `control_plane/keys/__init__.py`
- Added tests:
  - `tests/test_chat_manager.py`
  - `tests/test_chat_api.py`

**Validation:**

- `pytest -q tests/test_chat_manager.py tests/test_chat_api.py tests/test_control_plane_api.py` → **19 passed**
- `pytest -q` → **74 passed**

---

### 2026-03-04 22:55 — Phase 3: Data Vault + MCP Broker (Slice 5)

**Status:** Vault ingestion/search and MCP context retrieval are implemented.

**Changes made:**

- Added shared vault models:
  - `VaultItem`
  - `VaultChunk`
  - (`shared/models.py`)
- Added `VaultItemNotFoundError` (`shared/exceptions.py`).
- Implemented vault chunking utility (`control_plane/vault/chunker.py`).
- Implemented `VaultManager` with SQLite-backed storage:
  - `vault_items` table
  - `vault_chunks` table
  - ingestion pipeline (text -> chunks -> deterministic embeddings)
  - vector-style similarity search
  - (`control_plane/vault/vault_manager.py`)
- Implemented `MCPBroker` standardized context pull interface (`control_plane/vault/mcp_broker.py`).
- Added vault API routes (`control_plane/api/vault.py`):
  - `POST /v1/vault/items`
  - `GET /v1/vault/items`
  - `GET /v1/vault/items/{item_id}`
  - `GET /v1/vault/items/{item_id}/chunks`
  - `POST /v1/vault/search`
  - `POST /v1/vault/context` (MCP-style context response)
- Wired vault and MCP broker into control-plane app startup and test fixture:
  - `control_plane/main.py`
  - `tests/conftest.py`
- Added tests:
  - `tests/test_chunker.py`
  - `tests/test_vault_manager.py`
  - `tests/test_mcp_broker.py`
  - API coverage updates in `tests/test_control_plane_api.py`

**Validation:**

- `pytest -q tests/test_chunker.py tests/test_vault_manager.py tests/test_mcp_broker.py tests/test_control_plane_api.py` → **23 passed**
- `pytest -q` → **82 passed**

---

### 2026-03-04 23:16 — Phase 3: Dependency Engine (Slice 6)

**Status:** Task dependency persistence and blocked/unblocked execution flow are implemented.

**Changes made:**

- Added task dependency semantics to shared model:
  - `Task.depends_on: List[str]`
  - `Task.status` now includes `blocked`
  - (`shared/models.py`)
- Added dependency resolver utility (`control_plane/scheduler/dependency_engine.py`).
- Extended task persistence and migration in `TaskManager`:
  - Added `depends_on` column support in `tasks` table
  - Added `task_dependencies` table
  - Added migration step for existing task tables
  - Persisted dependencies for each task
  - (`control_plane/task_manager/task_manager.py`)
- Implemented blocked task lifecycle:
  - Tasks with dependencies start as `blocked`
  - Blocked tasks automatically transition to `queued` and run when all dependencies are `completed`
  - Unblocking check runs after task terminal updates
- Updated task create API to accept `depends_on` (`control_plane/api/tasks.py`).
- Added tests:
  - `tests/test_dependency_engine.py`
  - dependency flow test in `tests/test_task_manager.py`

**Validation:**

- `pytest -q tests/test_task_manager.py tests/test_dependency_engine.py tests/test_control_plane_api.py tests/test_vault_manager.py tests/test_chat_api.py` → **28 passed**
- `pytest -q` → **85 passed**

---

### 2026-03-04 23:45 — Phase 4: UI Navigation + Core Pages Scaffold (Slice 7)

**Status:** Phase 4 frontend is started with real, connected pages for Projects, Chat, and Vault.

**Changes made:**

- Added dashboard route blueprints:
  - `dashboard/routes/projects.py`
  - `dashboard/routes/chat.py`
  - `dashboard/routes/vault.py`
- Wired new blueprints into app factory (`dashboard/app.py`).
- Extended control-plane client with new endpoint helpers:
  - projects, models, chat conversations/messages, vault list/ingest/search
  - (`dashboard/cp_client.py`)
- Added new templates:
  - `dashboard/templates/projects.html`
  - `dashboard/templates/chat.html`
  - `dashboard/templates/vault.html`
- Updated navigation and layout shell:
  - added Projects, Chat, Vault nav items in `dashboard/templates/base.html`
- Refreshed base visual system and responsive behavior:
  - updated palette, ambient background, sticky nav, panel/list/chat layout helpers
  - responsive breakpoints for mobile
  - (`dashboard/static/style.css`)
- Added dashboard tests for new pages:
  - `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **15 passed**
- `pytest -q` → **88 passed**

---

### 2026-03-05 00:05 — Phase 4: Bot Detail + Task Board Scaffold (Slice 8)

**Status:** Bot detail and task-board UI foundation is in place, with connected Projects/Chat/Vault pages and route coverage.

**Changes made:**

- Added bot detail route and page scaffold:
  - `GET /bots/<bot_id>` in `dashboard/routes/bots.py`
  - template `dashboard/templates/bot_detail.html`
- Bot detail now shows:
  - core bot metadata
  - backend chain summary
  - kanban-style task board columns (`blocked`, `queued`, `running`, `completed`, `failed`)
- Updated bots table to link each bot to its detail page (`dashboard/templates/bots.html`).
- Fixed client-side action ID handling for non-numeric control-plane IDs:
  - `dashboard/templates/bots.html`
  - `dashboard/templates/workers.html`
- Extended style system for kanban/task-board UI (`dashboard/static/style.css`).
- Added dashboard coverage for new route:
  - `tests/test_dashboard_phase4_pages.py` includes `/bots/<id>` load test.

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py tests/test_control_plane_api.py` → **33 passed**
- `pytest -q` → **89 passed**

---

### 2026-03-05 00:24 — Phase 4: Chat-to-Vault Ingestion (Slice 9)

**Status:** Chat page can now ingest an entire conversation into the vault.

**Changes made:**

- Added chat ingestion API on dashboard:
  - `POST /api/chat/ingest` in `dashboard/routes/chat.py`
  - collects conversation + messages from control plane
  - writes a consolidated chat transcript into vault (`source_type=chat`)
- Added `"Ingest This Chat"` action in `dashboard/templates/chat.html`.
- Added validation coverage for ingest endpoint in dashboard tests:
  - update to `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **17 passed**
- `pytest -q` → **90 passed**

---

### 2026-03-05 00:58 — Phase 4: Worker Detail + Settings Tabs (Slice 10)

**Status:** Worker detail page and Settings additive tabs are implemented and wired to control-plane APIs.

**Changes made:**

- Added worker detail route and page:
  - `GET /workers/<worker_id>` in `dashboard/routes/workers.py`
  - template `dashboard/templates/worker_detail.html`
- Worker detail includes:
  - worker identity/status/capabilities
  - live metrics summary (load, queue depth, GPU utilization samples)
  - running task list snapshot
  - actions: ping, enable/disable, delete
- Added worker action endpoint:
  - `POST /api/workers/<worker_id>/ping`
- Extended control-plane workers API with update support:
  - `PUT /v1/workers/{worker_id}` in `control_plane/api/workers.py`
  - worker registry update method in `control_plane/registry/worker_registry.py`
- Updated dashboard workers/bots local API routes to handle string IDs and CP passthrough cleanly:
  - `dashboard/routes/workers.py`
  - `dashboard/routes/bots.py`
- Expanded dashboard control-plane client:
  - worker update/heartbeat helpers
  - key/model/project management helpers
  - (`dashboard/cp_client.py`)
- Added Settings additions (4g):
  - API Keys tab
  - Model Catalog tab
  - Projects tab
  - routes in `dashboard/settings.py`
  - UI controls in `dashboard/templates/settings.html`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py tests/test_control_plane_api.py tests/test_worker_registry.py` → **43 passed**
- `pytest -q` → **93 passed**

---

### 2026-03-05 01:14 — Phase 4: Worker Live Graphs (Slice 11)

**Status:** Worker detail page now includes live resource graphs and live data polling.

**Changes made:**

- Added worker live data endpoint:
  - `GET /api/workers/<worker_id>/live`
  - returns worker object + running task snapshot
  - (`dashboard/routes/workers.py`)
- Upgraded worker detail UI:
  - Added resource graph section with canvas line charts for:
    - load
    - queue depth
    - average GPU utilization
  - Added polling loop (`5s`) against `/api/workers/<id>/live`
  - Running task table now refreshes from live endpoint
  - (`dashboard/templates/worker_detail.html`)
- Added graph layout styling:
  - `graph-grid`, `graph-card`, `graph-title`
  - mobile responsive behavior
  - (`dashboard/static/style.css`)
- Added test coverage:
  - worker detail page asserts resource graph section
  - live endpoint payload shape test
  - (`tests/test_dashboard_phase4_pages.py`)

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py tests/test_control_plane_api.py` → **38 passed**
- `pytest -q` → **94 passed**

---

### 2026-03-05 01:37 — Phase 4: Bot Backend Chain Editor (Slice 12)

**Status:** Bot detail page now supports end-to-end backend chain editing and task detail inspection.

**Changes made:**

- Enhanced bot detail route data hydration (`dashboard/routes/bots.py`):
  - loads workers, model catalog, and API keys for backend pickers
- Rebuilt bot detail template (`dashboard/templates/bot_detail.html`) with:
  - bot edit modal (name, role, priority, system prompt, enabled)
  - backend chain editor:
    - add/edit/remove backend entries
    - reorder up/down
    - picker support for models, workers, API keys
  - save flow via `PUT /api/bots/<bot_id>`
  - backlog section (blocked tasks)
  - task detail modal (JSON payload/result/error snapshot)
  - full kanban columns: blocked, queued, running, completed, failed
- Added page assertions for editor/backlog in dashboard tests:
  - `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **20 passed**
- `pytest -q` → **94 passed**

---

### 2026-03-05 02:03 — Phase 4: Project Detail + Bridge Management (Slice 13)

**Status:** Projects UI now includes detail pages with bridge operations and project-scoped data panels.

**Changes made:**

- Added project detail route:
  - `GET /projects/<project_id>` in `dashboard/routes/projects.py`
- Added bridge management APIs in dashboard layer:
  - `POST /api/projects/<project_id>/bridges`
  - `DELETE /api/projects/<project_id>/bridges/<target_project_id>`
- Extended CP client for project detail and bridge actions:
  - `get_project`
  - `add_project_bridge`
  - `remove_project_bridge`
  - (`dashboard/cp_client.py`)
- Added project detail template (`dashboard/templates/project_detail.html`) with:
  - project metadata and settings-overrides panel
  - bridge management controls (add/remove)
  - project bots list
  - project task snapshot
  - project vault item snapshot
- Updated projects list template to link into project detail:
  - `dashboard/templates/projects.html`
- Added dashboard test coverage for project detail handling:
  - `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **21 passed**
- `pytest -q` → **95 passed**

---

### 2026-03-05 02:28 — Phase 4: Overview Enhancement (Slice 14)

**Status:** Overview now includes operational insight panels and actionable navigation.

**Changes made:**

- Enhanced overview data assembly in `dashboard/app.py`:
  - worker health summary metrics (load, GPU avg, queue depth)
  - recent activity feed (latest tasks)
  - system alerts (CP availability, offline workers, failed tasks)
  - quick links
- Upgraded overview template (`dashboard/templates/index.html`) with:
  - System Alerts panel
  - Quick Links panel
  - Worker Health mini-bars panel
  - Recent Activity table
- Added supporting styles:
  - overview grid layout
  - alert row variants
  - quick-link cards
  - worker mini-bar components
  - (`dashboard/static/style.css`)
- Added dashboard test for enhanced overview sections:
  - `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **22 passed**
- `pytest -q` → **96 passed**

---

### 2026-03-05 03:01 — Phase 4: Chat + Vault UX Deepening (Slice 15)

**Status:** Chat page feature set is now fully implemented for Phase 4 scope; Vault page gained upload variants and item detail inspection.

**Changes made:**

- Expanded chat route and APIs (`dashboard/routes/chat.py`):
  - context-aware chat page data (vault items + conversation/bot context)
  - `@assign` inline task assignment path in `/api/chat/messages`
  - streaming proxy endpoint: `POST /api/chat/stream`
  - per-message ingestion endpoint: `POST /api/chat/message-to-vault`
- Upgraded chat UI (`dashboard/templates/chat.html`):
  - context picker (vault checkboxes + past chat selector)
  - scope selector on conversation creation
  - send vs stream-send controls
  - inline task assignment modal
  - message actions: copy / re-run / send-to-vault
  - retained chat-history retrieval and conversation sidebar
- Extended CP client for richer task/vault operations:
  - `create_task_full`
  - `get_vault_item`
  - `list_vault_chunks`
  - (`dashboard/cp_client.py`)
- Expanded vault route APIs (`dashboard/routes/vault.py`):
  - multipart/URL/paste upload endpoint: `POST /api/vault/upload`
  - item detail endpoint: `GET /api/vault/items/<item_id>/detail`
- Upgraded vault UI (`dashboard/templates/vault.html`):
  - upload panel modes: paste / file / URL
  - item detail modal with metadata, preview, chunk count, and chunk samples
  - existing list/search flow preserved
- Added/updated styles for chat context/actions in `dashboard/static/style.css`.
- Added dashboard test coverage for new route validations and page rendering:
  - updates in `tests/test_dashboard_phase4_pages.py`

**Validation:**

- `pytest -q tests/test_dashboard_phase4_pages.py tests/test_dashboard_smoke.py tests/test_dashboard_onboarding.py` → **25 passed**
- `pytest -q` → **99 passed**
