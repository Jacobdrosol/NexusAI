# NexusAI

**NexusAI** is a modular, distributed LLM Control Plane that orchestrates multiple machines, GPUs, cloud APIs, and CLI-based models via specialized **bots** (logical agents) and **workers** (compute backends).

> **⚠️ Platform AI is in active development:** The in-platform copilot supports typed specialist proposals, preflight validation, and operator approval. Autonomous pipeline tuning and privileged execution remain opt-in and require deployment-specific validation before production use. See `control_plane/platform_ai/README.md` for scope and known limitations.

---

## What NexusAI Does

- **Distributed LLM compute** — any number of machines act as worker nodes running local or cloud-backed models.
- **Project management** — projects map to one or more repos, with bots, tasks, backlogs, dependencies, and completion tracking.
- **Conversational AI** — a first-class chat UI where you interact with any configured model and can assign tasks inline.
- **Data vault + context** — ingest files, URLs, code, and chat history into a searchable vault; attach contexts to chats and bots.
- **Hardware-aware scheduling** — worker nodes self-report CPU/RAM/GPU profiles; the platform estimates which models each node can run.
- **Moldable for any use case** — configure bots, projects, models, and workflows entirely from the UI.

Highlights: chat with attachments, memory profiles, workspace tools and inline coding, PM task orchestration, bot/worker readiness and tooling guards, token-usage and capacity controls, an operations Work page, GitHub integration, schedules and issue-to-task automation, Platform AI, and a native Android client.

**Full feature inventory:** [docs/FEATURES.md](docs/FEATURES.md)

---

## Quick Start with Docker

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your values (see [Environment Variables](docs/ENVIRONMENT.md))
3. Run: `docker compose up --build`
4. Open http://localhost:5000 to access the dashboard
5. The control plane API is at http://localhost:8000
6. The worker agent is at http://localhost:8001
7. Prometheus is at http://localhost:9090

## Quickstart (without Docker)

```bash
pip install -r requirements.txt

# 1. Run the control plane
NEXUS_CONFIG_PATH=config/nexus_config.yaml python -m control_plane.main

# 2. Run a worker agent
WORKER_CONFIG_PATH=config/workers/example_worker.yaml \
CONTROL_PLANE_URL=http://localhost:8000 \
python -m worker_agent.main

# 3. Run the dashboard
CONTROL_PLANE_URL=http://localhost:8000 \
gunicorn --bind 0.0.0.0:5000 --workers 2 "dashboard.app:create_app()"
```

Then open http://localhost:5000.

Standalone worker node: `cd worker_node && NEXUS_WORKER_CONFIG_PATH=nexus_worker/config.yaml.example CONTROL_PLANE_URL=http://localhost:8000 python -m nexus_worker` (installs as `nexus-worker`).

---

## Architecture

| Service | Framework | Port |
|---|---|---|
| `control_plane` | FastAPI | `8000` |
| `worker_agent` | FastAPI (uvicorn) | `8001` |
| `dashboard` | Flask / Gunicorn | `5000` |

```
                    NexusAI Control Plane
         Bot Registry  │  Worker Registry  │  Task Manager
                        └──────────┬────────┘
                                   ▼
                              Scheduler
       REST API /v1/tasks /v1/bots /v1/workers
         ▲                               ▲
         │                               │
   Worker Agent                    Worker Agent            Cloud APIs
   (Ollama/vLLM)                  (LM Studio)          (OpenAI/Claude/
   GPU Machine A                  GPU Machine B           Gemini)
      port 8001                      port 8001

         Dashboard (Flask/Gunicorn, port 5000)
```

Full system architecture, database schema, and known debt: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## First-Run Onboarding

The first visit to the dashboard presents a **5-step onboarding wizard** at `/onboarding`:

| Step | Path | Description |
|---|---|---|
| 1 | `/onboarding/step1` | Welcome |
| 2 | `/onboarding/step2` | Admin account (email + password) |
| 3 | `/onboarding/step3` | Default LLM provider |
| 4 | `/onboarding/step4` | Optional first compute worker |
| 5 | `/onboarding/step5` | Summary + redirect to login |

Once an admin account exists, every `/onboarding` request redirects to `/login`.

---

## Documentation

### User & Operator Docs
- Getting started: [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)
- Product usage: [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- Operations and security: [docs/OPERATIONS.md](docs/OPERATIONS.md)
- Chat/PM/workspace setup: [docs/CHAT_PM_WORKSPACE_SETUP.md](docs/CHAT_PM_WORKSPACE_SETUP.md)
- Blue/green deployment: [docs/DEPLOY_BLUEGREEN.md](docs/DEPLOY_BLUEGREEN.md)
- Worker node bootstrap: [worker_node/docs/WORKER_NODE_BOOTSTRAP.md](worker_node/docs/WORKER_NODE_BOOTSTRAP.md)
- UAT checklist: [docs/UAT_RUNBOOK.md](docs/UAT_RUNBOOK.md)
- Chat-history migration staging: [docs/CHAT_HISTORY_MIGRATION.md](docs/CHAT_HISTORY_MIGRATION.md)

### Feature & Reference Docs
- Feature inventory: [docs/FEATURES.md](docs/FEATURES.md)
- Environment variables: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)
- API reference: [docs/API_REFERENCE.md](docs/API_REFERENCE.md)
- Configuration reference: [config/README.md](config/README.md)
- PM workflow: [docs/PM_WORKFLOW.md](docs/PM_WORKFLOW.md)
- Refactor priorities: [docs/REFACTOR_PRIORITIES.md](docs/REFACTOR_PRIORITIES.md)
- Worklogs: [docs/worklogs/](docs/worklogs/)

### Per-Module READMEs
| Module | README |
|--------|--------|
| `control_plane/` | [control_plane/README.md](control_plane/README.md) |
| `control_plane/api/` | [control_plane/api/README.md](control_plane/api/README.md) — full endpoint table |
| `control_plane/task_manager/` | [control_plane/task_manager/README.md](control_plane/task_manager/README.md) |
| `control_plane/scheduler/` | [control_plane/scheduler/README.md](control_plane/scheduler/README.md) |
| `control_plane/chat/` | [control_plane/chat/README.md](control_plane/chat/README.md) |
| `control_plane/registry/` | [control_plane/registry/README.md](control_plane/registry/README.md) |
| `control_plane/vault/` | [control_plane/vault/README.md](control_plane/vault/README.md) |
| `control_plane/database/` | [control_plane/database/README.md](control_plane/database/README.md) |
| `control_plane/audit/` | [control_plane/audit/README.md](control_plane/audit/README.md) |
| `control_plane/security/` | [control_plane/security/README.md](control_plane/security/README.md) |
| `control_plane/keys/` | [control_plane/keys/README.md](control_plane/keys/README.md) |
| `control_plane/github/` | [control_plane/github/README.md](control_plane/github/README.md) |
| `control_plane/platform_ai/` ⚠️ | [control_plane/platform_ai/README.md](control_plane/platform_ai/README.md) — in testing, not yet stable |
| `control_plane/orchestration/` | [control_plane/orchestration/README.md](control_plane/orchestration/README.md) |
| `control_plane/agent_scheduler/` | [control_plane/agent_scheduler/README.md](control_plane/agent_scheduler/README.md) |
| `control_plane/connections/` | [control_plane/connections/README.md](control_plane/connections/README.md) |
| `shared/` | [shared/README.md](shared/README.md) |
| `dashboard/` | [dashboard/README.md](dashboard/README.md) |
| `dashboard/routes/` | [dashboard/routes/README.md](dashboard/routes/README.md) |
| `worker_agent/` | [worker_agent/README.md](worker_agent/README.md) |
| `worker_agent/backends/` | [worker_agent/backends/README.md](worker_agent/backends/README.md) |
| `tests/` | [tests/README.md](tests/README.md) |

---

## Security and Hardening

- Bootstrap secrets: `NEXUSAI_SECRET_KEY` (sessions/CSRF), `NEXUS_MASTER_KEY` (credential encryption), `CONTROL_PLANE_API_TOKEN` (control-plane auth). Generate secure values with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
- Put the dashboard and control plane behind a reverse proxy with TLS; restrict direct access to internal ports (`8000`, `8001`).
- Store PAT/API secrets only through the encrypted vault (never in committed YAML).
- Keep GitHub webhook replay controls enabled (`X-GitHub-Delivery`, short skew window).
- Keep the control-plane API on private subnets/VPN where possible.

Pre-UAT runbook: [docs/UAT_RUNBOOK.md](docs/UAT_RUNBOOK.md) · automated preflight: `scripts/pre_uat_security_checks.ps1`

---

## Observability

Prometheus is included in `docker-compose.yml` and scrapes `control_plane:8000/metrics` and `worker_agent:8001/metrics`.

- Control plane metrics: http://localhost:8000/metrics
- Worker agent metrics: http://localhost:8001/metrics
- Prometheus UI: http://localhost:9090

Check: `nexus_control_plane_http_requests_total`, `nexus_control_plane_http_request_duration_seconds_count`, `nexus_worker_agent_http_requests_total`, `nexus_worker_agent_inference_inflight`.

---

## Extending NexusAI

[How to add a new worker machine and define a new bot](docs/FEATURES.md#bot-orchestration) is covered in the feature guide. The config examples under `config/workers/` and `config/bots/` are generic references; keep real worker/bot files outside the public checkout.

---

## Next Priorities

- [x] Load-aware scheduling (queue depth/latency weighted worker selection)
- [x] Metrics/observability export (Prometheus + structured latency/error dashboards)
- [x] Automated security tests for webhook replay protections and secret-rotation workflows
- [x] Bot/worker readiness visibility, tooling preflight guards, and recommended operator actions
- [x] Chat token governor, usage telemetry, and effective-context preflight
- [x] Memory profiles, schedules, issue-to-task automation, and a native Android client
- [ ] End-to-end UAT execution and bug triage using `docs/UAT_RUNBOOK.md`
- [ ] Complete in-app password reset/recovery workflows
- [ ] Stabilize deployment profile (compose + reverse proxy reference stack)
- [ ] Build the staged external chat-history importer (see `docs/CHAT_HISTORY_MIGRATION.md`)

## Future Enhancements

Workflow and pipeline UX ideas planned but not yet implemented:

- A dedicated visual pipeline designer for reusable multi-bot workflows.
- Start-from-step execution so operators can launch a pipeline from a chosen stage.
- Fan-out branch replay to rerun one failed branch without rerunning the whole workflow.
- Resume-from-checkpoint execution after a partial failure or operator correction.
- Pipeline-level queue and concurrency controls for safe fan-out draining.
- First-class, user-defined pipeline templates.
- Android client follow-ups: file uploads, streamed tokens, work monitoring, notifications, and agentic controls.
- Multi-instance hardening for the agent scheduler (distributed coordination and retry policy).
- A real chat-history importer once the staged migration contract is validated (`docs/CHAT_HISTORY_MIGRATION.md`).
