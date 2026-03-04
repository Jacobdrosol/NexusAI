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
| 1 | **`shared/` not in Docker build context for some services** | `control_plane/Dockerfile`, `worker_agent/Dockerfile` | Import errors at runtime — `from shared.xxx import` will fail if `shared/` is not copied in |
| 2 | **Settings port mismatch** | `shared/settings_manager.py` line ~65 | `control_plane_port` defaults to `"8080"` but control plane runs on `8000`; dashboard CPClient will connect to wrong port |
| 3 | **`Bot` ORM has `routing_rules` column; shared Pydantic `Bot` model does not** | `dashboard/models.py` vs `shared/models.py` | Schema mismatch; serialization will silently drop or error on `routing_rules` |
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

- [ ] **Projects**: `projects` DB table, `ProjectRegistry`, `/v1/projects` REST API, isolation + bridge logic
- [ ] **API Key Vault**: `api_keys` DB table (encrypted), CRUD API, Scheduler resolution
- [ ] **Model Catalog**: `models` DB table, CRUD API, used by Scheduler + UI
- [ ] **Chat System**: `conversations` + `messages` tables, `ChatManager`, streaming SSE, context injection
- [ ] **Data Vault**: `vault_items` + `vault_chunks` tables, `VaultManager`, ingestion pipeline, vector search
- [ ] **MCP Broker**: standardized context pull interface for bots pre-inference
- [ ] **Dependency Engine**: `task_dependencies` table, `depends_on` on Task, DAG resolver, `blocked` status

### Phase 4 — Dashboard UI

#### 4a. Design System + Navigation Refresh
- [ ] Expand nav: add Projects, Chat, Vault
- [ ] Consistent dark theme, loading/empty/error states, responsive layout

#### 4b. Worker Detail Page
- [ ] Hardware profile card, model capability list, live resource graphs, current task list
- [ ] Worker enable/disable/delete/ping actions

#### 4c. Bot Detail + Task Board
- [ ] Bot edit form: name, role, system prompt, backend chain editor
- [ ] Backend chain: add/remove/reorder backends, model picker, worker picker, API key picker
- [ ] Task board Kanban: Queued | Blocked | Running | Completed | Failed
- [ ] Task detail modal, backlog view

#### 4d. Projects Page
- [ ] Project list + create modal
- [ ] Project detail: bots, task board, vault items, settings overrides
- [ ] Bridge management UI

#### 4e. Chat Page
- [ ] Conversation sidebar, streaming message area, model/bot selector
- [ ] Context picker (vault items, files, past chats), scope selector
- [ ] Inline task assignment (`@assign` → PM Bot), task assignment modal
- [ ] Message actions: copy, re-run, send to vault
- [ ] Chat history persistence + retrieval

#### 4f. Vault Page
- [ ] Upload panel (file, URL, paste), vault item list with search/filter
- [ ] Item detail: preview, chunk count, embedding status, metadata
- [ ] Namespace manager, bulk actions
- [ ] "Ingest this chat" button on chat page

#### 4g. Settings Additions
- [ ] API Keys tab, Model Catalog tab, Projects tab
- [ ] Fix control plane port setting (Bug #2)

#### 4h. Overview Page Enhancement
- [ ] Recent activity feed, worker health mini-bars, quick links, system alerts

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
