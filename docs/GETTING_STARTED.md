# NexusAI Getting Started

This guide is for first-time users who want to download the repository and run a working NexusAI environment.

## 1. What You Are Running

NexusAI includes these core services:

- `control_plane` (FastAPI, port `8000`): orchestration API, scheduler, tasks, chat, vault, project integrations.
- `worker_agent` (FastAPI, port `8001`): model execution worker with heartbeat and inference endpoints.
- `dashboard` (Flask, port `5000`): web UI for onboarding and operations.
- `prometheus` (port `9090`): metrics scraping and query UI.

Optional:

- `nexus_worker` standalone package, useful when splitting worker runtime into a separate repository.

## 2. Prerequisites

- OS: Windows, macOS, or Linux.
- Docker Desktop with Compose plugin (recommended), or Python `3.11+` for local process mode.
- At least 8 GB RAM for basic local testing.
- Optional cloud provider keys:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `GEMINI_API_KEY`

## 3. Clone and Configure

```bash
git clone <your-fork-or-origin-url>
cd NexusAI
```

Create `.env` from `.env.example` and fill required values.

Minimum recommended values for safe first run:

- `NEXUSAI_SECRET_KEY`: long random string.
- `CONTROL_PLANE_API_TOKEN`: long random token.
- `NEXUSAI_CLOUD_CONTEXT_POLICY=block`
- `NEXUS_WORKER_CLOUD_CONTEXT_POLICY=block`

## 4. Start the Stack (Docker)

```bash
docker compose up --build
```

Open:

- Dashboard: `http://localhost:5000`
- Control Plane health: `http://localhost:8000/health`
- Worker Agent health: `http://localhost:8001/health`
- Prometheus: `http://localhost:9090`

## 5. First Login and Onboarding

On first dashboard visit, complete onboarding:

1. Welcome
2. Create admin account
3. Select default LLM backend
4. Optionally define first worker
5. Complete and log in

If onboarding is already complete, you will be redirected to login.

## 6. First Working Flow (End-to-End)

After login:

1. Open `Workers` and confirm at least one worker is online.
2. Open `Bots` and create or verify a bot with a valid backend.
3. Open `Projects` and create one project.
4. Open `Vault` and ingest a small test document.
5. Open `Chat`:
   - create a conversation
   - send a normal message
   - run streaming message
   - test `@assign` task orchestration
6. Open `Tasks` and verify status transitions.

## 7. Verify Metrics

Prometheus target check:

- `http://localhost:9090/targets`

Expected jobs:

- `nexus_control_plane`
- `nexus_worker_agent`

Useful metric queries:

- `nexus_control_plane_http_requests_total`
- `nexus_control_plane_http_request_duration_seconds_count`
- `nexus_control_plane_tasks_by_status`
- `nexus_worker_agent_inference_inflight`

## 8. Run Tests

Run full test suite:

```bash
pytest -q
```

Use this before and after local changes.

## 9. Local Process Mode (No Docker)

Install dependencies:

```bash
pip install -r requirements.txt
```

Run in separate terminals:

```bash
python -m control_plane.main
python -m worker_agent.main
python -m dashboard.app
python -m nexus_worker
```

## 10. Where to Go Next

- End-to-end feature usage: `docs/USER_GUIDE.md`
- Security, operations, troubleshooting: `docs/OPERATIONS.md`
- UAT checklist: `docs/UAT_RUNBOOK.md`

