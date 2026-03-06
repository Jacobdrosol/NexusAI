# NexusAI Pre-UAT Runbook

This runbook is the fastest safe path to test a viable product without guessing.

## 1. Preconditions

- Repo is clean:
  - `git status --short`
- Dependencies are installed:
  - `pip install -r requirements.txt`
- Test baseline is green:
  - `pytest -q`

## 2. Security Baseline (Required)

Set these environment variables before booting services:

```powershell
$env:NEXUSAI_SECRET_KEY = "replace-with-long-random-secret"
$env:CONTROL_PLANE_API_TOKEN = "replace-with-long-random-token"
$env:NEXUSAI_CLOUD_CONTEXT_POLICY = "block"   # allow|redact|block
$env:NEXUS_WORKER_CLOUD_CONTEXT_POLICY = "block" # allow|redact|block
```

Notes:
- `block` is safest for first UAT.
- Move to `redact` only after initial confidence.

## 3. Start Services

Option A: Docker Compose

```powershell
docker compose up --build
```

Option B: Local processes (separate terminals)

```powershell
python -m control_plane.main
python -m worker_agent.main
python -m dashboard.app
cd worker_node; python -m nexus_worker
```

## 4. Automated Preflight Checks

Run:

```powershell
.\scripts\pre_uat_security_checks.ps1 `
  -ControlPlaneUrl "http://localhost:8000" `
  -DashboardUrl "http://localhost:5000" `
  -NexusWorkerUrl "http://localhost:8010" `
  -ApiToken $env:CONTROL_PLANE_API_TOKEN
```

Expected:
- Control-plane `/health` is `ok`
- Dashboard `/health` is `ok`
- `nexus_worker` `/health` is `ok`
- Control-plane token auth is enforced
- `nexus_worker` cloud-context `block` policy rejects context transfer with `403`
- Metrics endpoints are reachable (`/metrics` on control-plane/worker services)
- Prometheus target status is `UP` for `nexus_control_plane` and `nexus_worker_agent` (`http://localhost:9090/targets`)

## 5. Manual UI/UAT Flow

1. Open dashboard: `http://localhost:5000`
2. Complete onboarding/login.
3. Create/check:
   - Worker(s)
   - Bot(s) with at least one local backend and one cloud backend
   - Project
4. In Project detail:
   - Connect GitHub PAT
   - Set webhook secret
   - (Optional) configure PR review bot
5. In Vault:
   - Ingest text/file/url
   - Confirm search + detail + bulk actions
6. In Chat:
   - Send normal message
   - Send stream message
   - Use `@assign ...`
   - Verify task events + DAG view
7. In Worker detail:
   - Confirm live metrics/graphs update

## 6. Privacy / Leakage Validation

### 6.1 Browser-side context minimization
- In Chat context picker, select vault items and send.
- Confirm no full vault item content is serialized from browser as context payload (IDs are sent; server resolves content).

### 6.2 Cloud context blocking
- Keep `NEXUSAI_CLOUD_CONTEXT_POLICY=block` and `NEXUS_WORKER_CLOUD_CONTEXT_POLICY=block`.
- Attempt cloud-backed inference with context present.
- Expected: request fails before egress (policy block), not after provider call.

### 6.3 Cloud context redaction (optional second pass)
- Set policies to `redact`.
- Repeat cloud-backed inference with context.
- Expected: context is replaced by redaction marker in backend payload path.

### 6.4 GitHub webhook replay protection
- Send two webhook requests with the same `X-GitHub-Delivery` value.
- Expected: first request accepted, second rejected with `409`.
- If `Date` header skew is intentionally outside configured threshold, expected `401`.

## 7. Go / No-Go Gate

Go to broader testing only if all are true:
- Automated preflight script passes.
- `pytest -q` is green.
- No unexpected 5xx during manual flow above.
- Privacy checks pass for the selected policy mode.
- Audit events are present for privileged writes (`GET /v1/audit/events`).

## 8. Recommended First External Test Scope

- 1 project
- 2 bots (local + cloud)
- 1 worker_agent + 1 nexus_worker
- 1 hour mixed use:
  - chat streaming
  - vault ingest/search
  - one GitHub webhook event
  - one `@assign` DAG flow
