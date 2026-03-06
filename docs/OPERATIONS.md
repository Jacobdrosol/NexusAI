# NexusAI Operations Guide

This guide covers production-minded operations, security controls, troubleshooting, backup, and maintenance.

## 1. Security Baseline

Required for non-dev environments:

- set strong `NEXUSAI_SECRET_KEY`
- set `CONTROL_PLANE_API_TOKEN`
- run behind TLS reverse proxy
- restrict direct access to internal ports
- keep secrets only in key vault/API key endpoints

Recommended initial privacy posture:

- `NEXUSAI_CLOUD_CONTEXT_POLICY=block`
- `NEXUS_WORKER_CLOUD_CONTEXT_POLICY=block`

Set both values in `.env` before service startup. They are operator-generated shared secrets, not values issued by a third party.

## 2. Webhook Security Controls

Controls and defaults:

- `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DELIVERY_ID=1`
- `NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS=300`
- `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DATE_HEADER=0`
- `NEXUSAI_GITHUB_WEBHOOK_DEDUP_TTL_SECONDS=86400`

Operational checks:

- duplicate delivery IDs should return `409`
- invalid signature should return `401`
- stale date header outside skew window should return `401`

## 3. Observability

Metrics endpoints:

- control plane: `http://<host>:8000/metrics`
- worker agent: `http://<host>:8001/metrics`
- standalone nexus worker: `http://<host>:8010/metrics` (if enabled)

Prometheus:

- compose service runs at `http://localhost:9090`
- verify targets at `/targets`

Important signals:

- request rate and latency histograms
- 5xx error counters
- task status gauges
- worker queue depth and load
- scheduler in-flight and latency EMA

## 4. Capacity and Performance Tuning

Scheduler controls:

- `NEXUSAI_WORKER_LATENCY_EMA_ALPHA`
- `NEXUSAI_WORKER_DEFAULT_LATENCY_MS`

Guard rails:

- `CP_MAX_BODY_BYTES_<ROUTE>`
- `CP_RATE_LIMIT_<ROUTE>_COUNT`
- `CP_RATE_LIMIT_<ROUTE>_WINDOW_SECONDS`

Guidance:

- increase rate limits only after observing baseline latency/error metrics
- tune body limits for large vault/chat payloads with strict monitoring

## 5. Backup and Restore

Primary persistent data:

- SQLite DB in `data/` (or configured DB URL)
- compose volumes:
  - `nexus-data`
  - `prometheus-data`

Backup minimum:

1. stop writes (or stop services)
2. copy `data/` and persistent volumes
3. store encrypted off-host

Restore minimum:

1. restore data paths/volumes
2. start services
3. run health checks and smoke tests

## 6. Troubleshooting

### 6.1 Services not starting

- verify `.env` exists
- verify ports are free (`5000`, `8000`, `8001`, `9090`)
- inspect logs:
  - `docker compose logs control_plane`
  - `docker compose logs worker_agent`
  - `docker compose logs dashboard`
  - `docker compose logs prometheus`

### 6.2 Worker offline

- check worker can reach control plane URL
- verify API token matches control plane token
- inspect heartbeat requests

### 6.3 Chat or task failures

- confirm bot backend chain is valid
- confirm model/provider alignment
- check cloud API keys or local model availability
- inspect task error payloads and audit log

### 6.4 Webhook not accepted

- verify HMAC signature and secret
- verify `X-GitHub-Delivery` uniqueness
- verify event type is supported

## 7. Pre-UAT and Release Gate

Before wider testing:

1. `pytest -q` is green.
2. `docs/UAT_RUNBOOK.md` checklist completed.
3. metrics and audit events validate expected behavior.
4. privacy controls (`block` or `redact`) match policy requirements.

## 8. Maintenance Checklist

Daily:

- check service health and error counters
- check worker online status and queue depth

Weekly:

- review audit events for privileged actions
- rotate tokens and verify provider keys
- review dependency updates and rerun tests

Release cycle:

- run full regression tests
- run UAT script and manual flow
- capture baseline metrics snapshots before deploy

## 9. Planned Security Improvement

Password reset and recovery should move fully into authenticated application workflows.

Planned direction:

- add in-app admin password reset flow with audit logging
- add secure self-service password change flow for logged-in users
- add tokenized password reset workflow (time-limited, one-time use) for recovery
- avoid dependence on direct database command sequences for routine credential operations

Rationale:

- reduces operational risk from manual database access
- limits accidental secret exposure in shared logs/docs/history
- provides safer support workflow for open-source users and operators

## 10. Deploy Tab (Blue/Green Guardrails)

The Settings `Deploy` tab exposes:

- local commit (`HEAD`)
- remote commit (`origin/main`)
- last deployed commit
- deploy state and log tail
- `Check For Updates` and `Deploy Latest Commit` actions

Safety behavior:

- deploy execution is disabled by default
- deploy requires `NEXUSAI_DEPLOY_ENABLE=1`
- deploy requires `NEXUSAI_DEPLOY_STRATEGY=bluegreen`
- deploy requires `NEXUSAI_DEPLOY_RUN_CMD` to be explicitly configured

Recommended run command pattern (temporary deploy container):

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/nexusai:/workspace \
  -w /workspace \
  docker:27-cli \
  sh -lc "./scripts/deploy-bluegreen.sh"
```

Recommended environment:

```bash
NEXUSAI_DEPLOY_ENABLE=1
NEXUSAI_DEPLOY_STRATEGY=bluegreen
NEXUSAI_DEPLOY_RUN_CMD=docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /opt/nexusai:/workspace -w /workspace docker:27-cli sh -lc "./scripts/deploy-bluegreen.sh"
NEXUSAI_BLUEGREEN_SWITCH_CMD=./scripts/switch-dashboard-color.sh
```

Blue/green runtime assets:

- `docker-compose.bluegreen.yml` (gateway + blue/green dashboard services)
- `deploy/nginx/default.blue.conf`
- `deploy/nginx/default.green.conf`
- `deploy/nginx/default.conf` (active route config)
- `scripts/check_db_drift.sh` (fail-closed DB consistency guard)
- `scripts/switch-dashboard-color.sh` (atomic route switch + reload)

Operational note:

- the dashboard will refuse to run deployment when guardrails are not satisfied
- this prevents accidental in-place restarts that can disrupt running workloads
- bootstrap/deploy scripts now block when host DB and legacy volume DB diverge
