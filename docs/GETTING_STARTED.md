# NexusAI Getting Started

This guide is for first-time users who want to download the repository and run a working NexusAI environment.

## 1. What You Are Running

NexusAI includes these core services:

- `control_plane` (FastAPI, port `8000`): orchestration API, scheduler, tasks, chat, vault, project integrations.
- `worker_agent` (FastAPI, port `8001`): model execution worker with heartbeat and inference endpoints.
- `dashboard` (Flask, port `5000`): web UI for onboarding and operations.
- `prometheus` (port `9090`): metrics scraping and query UI.

Optional:

- `worker_node` standalone project, useful when deploying worker runtime outside the main app repo.

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

Generate both secrets quickly:

Linux/macOS:

```bash
python - <<'PY'
import secrets
print("NEXUSAI_SECRET_KEY=" + secrets.token_urlsafe(64))
print("CONTROL_PLANE_API_TOKEN=" + secrets.token_urlsafe(64))
PY
```

Windows PowerShell:

```powershell
python -c "import secrets; print('NEXUSAI_SECRET_KEY=' + secrets.token_urlsafe(64)); print('CONTROL_PLANE_API_TOKEN=' + secrets.token_urlsafe(64))"
```

## 4. Start the Stack (Docker)

```bash
docker compose up --build
```

Open:

- Dashboard: `http://localhost:5000`
- Control Plane health: `http://localhost:8000/health`
- Worker Agent health: `http://localhost:8001/health`
- Prometheus: `http://localhost:9090`

Important for blue/green dashboard deployments:

- `docker-compose.bluegreen.yml` runs dashboard + gateway only.
- Control plane and worker services must be running separately and reachable from dashboard containers.
- Set `CONTROL_PLANE_URL` and `CONTROL_PLANE_API_TOKEN` in `.env` before recreating the active dashboard color container.

Example (single-host split stack):

```bash
CONTROL_PLANE_URL=http://100.81.64.82:8000
CONTROL_PLANE_API_TOKEN=<same token used by control_plane>
CP_TIMEOUT=5
```

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

1. Open `Overview` and review the `Open-Source Setup Checklist`.
   - Required items cover the first bootstrap gate:
     - dashboard session secret
     - control-plane URL/token
     - control-plane health and `/v1/*` auth
     - safe cloud-context policy
     - admin account readiness
   - Recommended items help you finish first-use setup:
     - worker registration
     - bot configuration
     - project bootstrap
2. If the checklist shows `Control plane health and auth` as not ready, fix that before continuing.
   - A green `/health` alone is not enough; the dashboard also validates authenticated `/v1/projects`, `/v1/bots`, and `/v1/workers` access.
   - Use the `Control Plane Checks` table on `Overview` to see which endpoint is failing and whether the issue is auth (`401`), routing (`404`), or network reachability.
3. Open `Workers` and confirm at least one worker is online.
4. Open `Bots` and create or verify a bot with a valid backend.
   - For cloud providers, add keys in `Settings -> API Keys` and reference by nickname (`api_key_ref`).
   - The control plane now persists bots in SQLite; shipped example YAML bots are not auto-seeded unless you explicitly enable `control_plane.seed_bots_from_config: true`.
5. Open `Projects` and create one project.
6. Open `Vault` and ingest a small test document.
7. Open `Chat`:
   - create a conversation
   - send a normal message
   - run streaming message
   - test `@assign` task orchestration
8. Open `Tasks` and verify status transitions.

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

## 8b. Runtime Verification (Recommended)

After deploy or env changes, verify end-to-end connectivity:

```bash
curl -fsS http://127.0.0.1:5000/health
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8001/health
```

If dashboard shows `Control plane unavailable — showing local data`, verify:

1. control plane process is listening on `0.0.0.0:8000`
2. `CONTROL_PLANE_URL` is set to a reachable host from inside dashboard container
3. `CONTROL_PLANE_API_TOKEN` exactly matches control plane token

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
(cd worker_node && python -m nexus_worker)
```

## 10. Where to Go Next

- End-to-end feature usage: `docs/USER_GUIDE.md`
- Security, operations, troubleshooting: `docs/OPERATIONS.md`
- UAT checklist: `docs/UAT_RUNBOOK.md`
