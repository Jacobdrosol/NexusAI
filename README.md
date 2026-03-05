# NexusAI

**NexusAI** is a modular, distributed LLM Control Plane that orchestrates multiple machines, GPUs, cloud APIs, and CLI-based models via specialized **bots** (logical agents) and **workers** (compute backends).

---

## Quick Start with Docker

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your values
3. Run: `docker compose up --build`
4. Open http://localhost:5000 to access the dashboard
5. The control plane API is at http://localhost:8000
6. The worker agent is at http://localhost:8001

---

## Architecture

| Service | Framework | Port |
|---|---|---|
| `control_plane` | FastAPI | `8000` |
| `worker_agent` | FastAPI (uvicorn) | `8001` |
| `dashboard` | Flask / Gunicorn | `5000` |

```
┌─────────────────────────────────────────────────────────────┐
│                      NexusAI Control Plane                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Bot Registry│  │Worker Registry│  │  Task Manager    │  │
│  └──────┬───────┘  └──────┬────────┘  └────────┬─────────┘  │
│         └─────────────────┴──────────────┬──────┘            │
│                                    ┌─────▼──────┐            │
│                                    │  Scheduler  │            │
│                                    └─────┬──────┘            │
│  REST API /v1/tasks /v1/bots /v1/workers │                   │
└──────────────────────────────────────────┼──────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────┐
              │                            │                    │
     ┌────────▼────────┐       ┌───────────▼──────┐   ┌────────▼────────┐
     │  Worker Agent   │       │  Worker Agent     │   │  Cloud APIs     │
     │  (Ollama/vLLM)  │       │  (LM Studio)      │   │ (OpenAI/Claude/ │
     │  GPU Machine A  │       │  GPU Machine B    │   │  Gemini)        │
     │  port 8001      │       │  port 8001        │   │                 │
     └─────────────────┘       └──────────────────┘   └─────────────────┘

     ┌─────────────────┐
     │   Dashboard     │
     │  Flask/Gunicorn │
     │  port 5000      │
     └─────────────────┘
```

---

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config/nexus_config.yaml` to set your control plane host/port.

Add worker definitions in `config/workers/` and bot definitions in `config/bots/`.

### 3. Run the Control Plane

```bash
NEXUS_CONFIG_PATH=config/nexus_config.yaml python -m control_plane.main
# or
uvicorn control_plane.main:app --host 0.0.0.0 --port 8000
```

### 4. Run a Worker Agent

```bash
WORKER_CONFIG_PATH=config/workers/example_worker.yaml \
CONTROL_PLANE_URL=http://localhost:8000 \
python -m worker_agent.main
# or
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8001
```

### 4b. Run `nexus_worker` Standalone Package

```bash
NEXUS_WORKER_CONFIG_PATH=nexus_worker/config.yaml.example \
CONTROL_PLANE_URL=http://localhost:8000 \
python -m nexus_worker
# or, after install:
nexus-worker
```

### 5. Run the Dashboard

```bash
CONTROL_PLANE_URL=http://localhost:8000 \
gunicorn --bind 0.0.0.0:5000 --workers 2 "dashboard.app:create_app()"
```

Then open http://localhost:5000 in your browser.

Dashboard navigation now includes `Projects`, `Chat`, and `Vault` pages connected to the control-plane APIs, plus a bot detail/task-board view at `/bots/<bot_id>`.
The Chat page also supports one-click conversation ingestion into Vault.
Workers now include a detail view at `/workers/<worker_id>` and Settings includes API Keys / Model Catalog / Projects tabs.
Worker detail now includes live resource graphs (load, queue, GPU utilization) with periodic polling.
Bot detail (`/bots/<bot_id>`) now includes backend chain editing (add/edit/remove/reorder) with model/worker/key pickers and a task kanban/detail modal.
Projects now include a detail page at `/projects/<project_id>` with bridge management and project-scoped bots/tasks/vault panels.
Overview now includes system alerts, worker health mini-bars, quick links, and a recent activity feed.
Chat now supports context picking, stream-send, inline `@assign` task routing, and per-message actions (copy/re-run/send-to-vault). Vault now supports file/URL/paste upload modes plus item detail preview/chunk metadata.
Vault also includes namespace management and bulk-delete actions, and the dashboard uses a consistent dark theme with reusable loading/empty/error state components.
`@assign` now triggers PM-driven decomposition into dependency-ordered multi-bot tasks, streams task-status events in chat, and posts an aggregated completion summary back into the same conversation.
Assignment summary messages now include a `View DAG` action that opens a visual dependency graph for the orchestration.
Projects now include GitHub PAT connection management (connect/test/disconnect) at the project detail page for per-project repository integration.
Projects now also support GitHub webhook ingestion for `push`, `pull_request`, and `issues` events with HMAC signature verification and stored event history.
GitHub integration now also includes repo-context sync into Vault (file tree + selected file contents) and optional PR review task automation via pull request webhooks.
Control-plane privileged write actions are now persisted in a structured audit log and available via `GET /v1/audit/events`.
Dashboard auth now enforces inactivity expiration based on `session_timeout_minutes` (Settings -> Auth).
Chat vault context selection now passes vault item IDs and resolves content server-side in control-plane for lower payload bloat and reduced client-side exposure.

---

## Environment Variables

Copy `.env.example` to `.env` and set the following variables before starting the stack:

| Variable | Default | Description |
|---|---|---|
| `NEXUSAI_SECRET_KEY` | `dev-secret-change-in-production` | Flask session secret key — **must be changed in production** |
| `DATABASE_URL` | `sqlite:///data/nexusai.db` | SQLAlchemy connection URL (SQLite or PostgreSQL) |
| `CONTROL_PLANE_URL` | — | URL the dashboard and worker use to reach the control plane, e.g. `http://localhost:8000` |
| `CONTROL_PLANE_API_TOKEN` | — | Optional shared token for control-plane API auth; when set, dashboard/worker send `X-Nexus-API-Key` and CP enforces auth on API routes |
| `NEXUSAI_CLOUD_CONTEXT_POLICY` | `allow` | Cloud egress policy for context blocks (`allow`, `redact`, `block`) on control-plane scheduler cloud backends |
| `NEXUS_WORKER_CLOUD_CONTEXT_POLICY` | `redact` | Standalone worker cloud egress policy for context blocks (`allow`, `redact`, `block`) |
| `NEXUSAI_WORKER_LATENCY_EMA_ALPHA` | `0.30` | Scheduler EMA smoothing factor for worker latency scoring (0.01-1.0) |
| `NEXUSAI_WORKER_DEFAULT_LATENCY_MS` | `800` | Default worker latency estimate used before dispatch history exists |
| `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DELIVERY_ID` | `1` | Require `X-GitHub-Delivery` header for webhook replay protection |
| `NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS` | `300` | Allowed request timestamp skew when `Date` header is present |
| `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DATE_HEADER` | `0` | Require `Date` header on GitHub webhooks (`1`/`true` to enforce) |
| `NEXUSAI_GITHUB_WEBHOOK_DEDUP_TTL_SECONDS` | `86400` | Retention window for delivery-ID deduplication records |
| `NEXUS_WORKER_CONFIG_PATH` | `nexus_worker/config.yaml.example` | Path to standalone `nexus_worker` YAML config |
| `VLLM_MODELS` | — | Optional comma-separated vLLM model names for local model discovery in `nexus_worker` |
| `CP_MAX_BODY_BYTES_<ROUTE>` | route default | Optional request body size override per guarded route (e.g. `CHAT_MESSAGES`, `CHAT_STREAM`, `VAULT_INGEST`, `GITHUB_WEBHOOK`) |
| `CP_RATE_LIMIT_<ROUTE>_COUNT` / `CP_RATE_LIMIT_<ROUTE>_WINDOW_SECONDS` | route defaults | Optional per-route rate limit override for guarded control-plane endpoints |
| `NEXUS_CONFIG_PATH` | — | Path to `nexus_config.yaml` for the control plane |
| `WORKER_CONFIG_PATH` | — | Path to a worker YAML file for the worker agent |
| `DASHBOARD_PORT` | `5000` | Port the dashboard listens on (used when running directly) |
| `OPENAI_API_KEY` | — | OpenAI API key for cloud LLM backends |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |

---

## Production Hardening

- Put the dashboard and control-plane behind a reverse proxy (Nginx/Caddy/Traefik) with TLS enabled.
- Restrict direct access to internal service ports (`8000`, `8001`) at the network layer; expose only the proxy entrypoint.
- Set `CONTROL_PLANE_API_TOKEN` and require token-authenticated control-plane calls from dashboard/workers.
- Use a strong `NEXUSAI_SECRET_KEY` and rotate it for production environments.
- Store PAT/API secrets only through encrypted vault endpoints (never in plain YAML committed to repo).
- Configure GitHub webhook secrets per project and verify signatures (already enforced by the control-plane endpoint).
- Enforce webhook replay controls by keeping `X-GitHub-Delivery` checks enabled and using a short skew window for signed payloads.
- For Gemini backends, keep API keys in request headers (`x-goog-api-key`), not URL query params.
- Keep the control-plane API on private subnets/VPN where possible; do not expose unauthenticated management paths publicly.
- Run with least-privilege service accounts and file permissions for the `data/` directory.

---

## Pre-UAT Guide

- Full step-by-step runbook: `docs/UAT_RUNBOOK.md`
- Automated preflight script:
  - `scripts/pre_uat_security_checks.ps1`

---

## First-Run Onboarding

On the first visit to the dashboard (before any admin account exists), NexusAI presents a **5-step onboarding wizard** at `/onboarding`:

| Step | Path | Description |
|---|---|---|
| 1 | `/onboarding/step1` | **Welcome** — introduction screen |
| 2 | `/onboarding/step2` | **Admin Account** — create the first admin user (email + password) |
| 3 | `/onboarding/step3` | **LLM Backend** — choose the default LLM provider (`ollama`, `openai`, `claude`, `gemini`) |
| 4 | `/onboarding/step4` | **Worker Node** — optionally register the first compute worker |
| 5 | `/onboarding/step5` | **Complete** — wizard summary and redirect to login |

Once an admin account exists every `/onboarding` request redirects to `/login`.

---

## Configuration Guide

### `config/nexus_config.yaml`

```yaml
control_plane:
  host: 0.0.0.0
  port: 8000
  workers_config_dir: config/workers    # directory of worker YAML files
  bots_config_dir: config/bots          # directory of bot YAML files
  heartbeat_timeout_seconds: 30         # workers go offline after this

dashboard:
  host: 0.0.0.0
  port: 5000
  enabled: true

logging:
  level: INFO
  file_path: data/nexus.log
```

### Worker YAML (`config/workers/<name>.yaml`)

```yaml
id: worker-main-4070
name: Main 4070 Box
host: 192.168.1.10
port: 8001
capabilities:
  - type: llm
    provider: ollama
    models:
      - llama3-8b-instruct-q4
    gpus:
      - GPU-0
```

### Bot YAML (`config/bots/<name>.yaml`)

```yaml
id: bot-coder-14b
name: Coder 14B
role: coding
priority: 10
enabled: true
backends:
  - type: local_llm
    worker_id: worker-main-4070
    model: llama3-8b-instruct-q4
    provider: ollama
    gpu_id: GPU-0
    params:
      temperature: 0.1
      max_tokens: 1024
  - type: cloud_api
    provider: claude
    model: claude-3-5-sonnet
    api_key_ref: ANTHROPIC_API_KEY    # env var name
    params:
      temperature: 0.1
      max_tokens: 2048
```

Backends are tried in order. If the first fails, the next is attempted.

---

## API Reference

### Control Plane (`http://localhost:8000`)

#### Tasks

```bash
# Create a task
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"bot_id": "bot-coder-14b", "payload": [{"role": "user", "content": "Hello!"}]}'

# Create a dependent task (starts as blocked until dependency completes)
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"bot_id":"bot-coder-14b","payload":{"step":"write tests"},"depends_on":["<task_id_from_previous_call>"]}'

# Get task status
curl http://localhost:8000/v1/tasks/{task_id}

# List all tasks
curl http://localhost:8000/v1/tasks
```

#### Bots

```bash
# List bots
curl http://localhost:8000/v1/bots

# Get bot
curl http://localhost:8000/v1/bots/{bot_id}

# Create bot
curl -X POST http://localhost:8000/v1/bots \
  -H "Content-Type: application/json" \
  -d '{...bot JSON...}'

# Enable / Disable bot
curl -X POST http://localhost:8000/v1/bots/{bot_id}/enable
curl -X POST http://localhost:8000/v1/bots/{bot_id}/disable

# Delete bot
curl -X DELETE http://localhost:8000/v1/bots/{bot_id}
```

#### Workers

```bash
# List workers
curl http://localhost:8000/v1/workers

# Register worker
curl -X POST http://localhost:8000/v1/workers \
  -H "Content-Type: application/json" \
  -d '{...worker JSON...}'

# Worker heartbeat
curl -X POST http://localhost:8000/v1/workers/{worker_id}/heartbeat

# Remove worker
curl -X DELETE http://localhost:8000/v1/workers/{worker_id}
```

#### Projects

```bash
# Create project
curl -X POST http://localhost:8000/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"id":"proj-1","name":"Project 1","mode":"isolated"}'

# List projects
curl http://localhost:8000/v1/projects

# Bridge two bridged-mode projects
curl -X POST http://localhost:8000/v1/projects/proj-a/bridges/proj-b

# Ingest a GitHub webhook event (signature + delivery ID required)
curl -X POST http://localhost:8000/v1/projects/proj-a/github/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=<hmac_hex>" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: <uuid>" \
  -d '{...event payload...}'
```

#### API Keys

```bash
# Create or update an API key (encrypted at rest)
curl -X POST http://localhost:8000/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"name":"openai-dev","provider":"openai","value":"sk-..."}'

# List key metadata (no secret values returned)
curl http://localhost:8000/v1/keys
```

#### Model Catalog

```bash
# Register a model
curl -X POST http://localhost:8000/v1/models \
  -H "Content-Type: application/json" \
  -d '{"id":"openai-gpt-4o-mini","name":"gpt-4o-mini","provider":"openai","capabilities":["chat"]}'

# List catalog models
curl http://localhost:8000/v1/models
```

#### Chat

```bash
# Create conversation
curl -X POST http://localhost:8000/v1/chat/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"Build auth API"}'

# Post a message (optionally with bot_id)
curl -X POST http://localhost:8000/v1/chat/conversations/{conversation_id}/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"Draft the endpoint design","bot_id":"bot-coder-14b"}'

# Stream a turn over SSE
curl -N -X POST http://localhost:8000/v1/chat/conversations/{conversation_id}/stream \
  -H "Content-Type: application/json" \
  -d '{"content":"Continue","bot_id":"bot-coder-14b"}'
```

#### Vault + MCP Context

```bash
# Ingest text into the vault
curl -X POST http://localhost:8000/v1/vault/items \
  -H "Content-Type: application/json" \
  -d '{"title":"Auth notes","content":"JWT auth middleware and refresh token flow","namespace":"global"}'

# Search vault chunks
curl -X POST http://localhost:8000/v1/vault/search \
  -H "Content-Type: application/json" \
  -d '{"query":"JWT auth","limit":5}'

# Pull standardized MCP-style context for a query
curl -X POST http://localhost:8000/v1/vault/context \
  -H "Content-Type: application/json" \
  -d '{"query":"refresh token","limit":3}'

# Prometheus-compatible metrics
curl http://localhost:8000/metrics
```

### Worker Agent (`http://localhost:8001`)

```bash
# Health check
curl http://localhost:8001/health

# Get capabilities
curl http://localhost:8001/capabilities

# Run inference
curl -X POST http://localhost:8001/infer \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3-8b-instruct-q4", "provider": "ollama", "messages": [{"role": "user", "content": "Hi"}]}'

# Prometheus-compatible metrics
curl http://localhost:8001/metrics
```

---

## How to Add a New Worker Machine

1. Create a YAML file in `config/workers/`, e.g. `config/workers/gpu-box-2.yaml`:

```yaml
id: worker-gpu-box-2
name: GPU Box 2
host: 192.168.1.20
port: 8001
capabilities:
  - type: llm
    provider: ollama
    models:
      - codellama-13b
    gpus:
      - GPU-0
      - GPU-1
```

2. On the new machine, run the worker agent:

```bash
WORKER_CONFIG_PATH=config/workers/gpu-box-2.yaml \
CONTROL_PLANE_URL=http://<control-plane-ip>:8000 \
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8001
```

The worker will self-register and begin sending heartbeats.

---

## How to Define a New Bot

1. Create a YAML file in `config/bots/`, e.g. `config/bots/summarizer.yaml`:

```yaml
id: bot-summarizer
name: Summarizer
role: summarization
priority: 5
enabled: true
backends:
  - type: cloud_api
    provider: openai
    model: gpt-4o-mini
    api_key_ref: OPENAI_API_KEY
    params:
      temperature: 0.3
      max_tokens: 512
```

2. Restart the control plane (or `POST /v1/bots` to register at runtime).

---

## Integration with agent-orchestrator

[`Jacobdrosol/agent-orchestrator`](https://github.com/Jacobdrosol/agent-orchestrator) can be used as a worker backend by:

1. Running `agent-orchestrator` on a machine.
2. Creating a worker YAML that points to it:

```yaml
id: worker-orchestrator
name: Agent Orchestrator
host: 192.168.1.30
port: 8090
capabilities:
  - type: llm
    provider: custom
    models:
      - orchestrator-pipeline
```

3. Create a bot with `type: remote_llm` pointing to this worker.
4. NexusAI will POST inference requests to `http://192.168.1.30:8090/infer`.

---

## Next Priorities

- [ ] End-to-end UAT execution and bug triage using `docs/UAT_RUNBOOK.md`
- [x] Add robust load-aware scheduling (queue depth/latency weighted worker selection)
- [x] Add metrics/observability export (Prometheus + structured latency/error dashboards)
- [x] Extend automated security tests for webhook replay protections and secret-rotation workflows
- [ ] Stabilize deployment profile (compose + reverse proxy reference stack)
