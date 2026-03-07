# Blue/Green Deployment Guide

This guide prepares NexusAI for repeatable, low-disruption dashboard deployments using the Settings `Deploy` tab.

## 1. Prerequisites

- Docker + Docker Compose installed on the target host
- Repository cloned on target host (example path: `/opt/nexusai`)
- `.env` created from `.env.example`
- Control plane reachable from dashboard containers (`CONTROL_PLANE_URL`)
- Matching `CONTROL_PLANE_API_TOKEN` configured on both dashboard and control plane

## 2. Deployment Assets Included

- `docker-compose.bluegreen.yml`
- `deploy/nginx/default.blue.conf`
- `deploy/nginx/default.green.conf`
- `data/nginx/default.conf` (runtime-generated active route file)
- `scripts/check_db_drift.sh`
- `scripts/bootstrap_bluegreen.sh`
- `scripts/deploy-bluegreen.sh`
- `scripts/switch-dashboard-color.sh`
- `scripts/preflight_deploy.py`

## 3. Configure `.env`

Set:

```bash
NEXUSAI_DEPLOY_ENABLE=1
NEXUSAI_DEPLOY_STRATEGY=bluegreen
NEXUSAI_STOP_PREVIOUS_COLOR=0
NEXUSAI_DB_DRIFT_AUTO_SYNC=1
NEXUSAI_BLUEGREEN_SWITCH_CMD=./scripts/switch-dashboard-color.sh
NEXUSAI_COMPOSE_PROJECT_NAME=nexusai
NEXUSAI_DEPLOY_RUN_CMD=docker run --rm -e NEXUSAI_DEPLOY_STRATEGY=bluegreen -e NEXUSAI_BLUEGREEN_SWITCH_CMD=./scripts/switch-dashboard-color.sh -e NEXUSAI_COMPOSE_PROJECT_NAME=nexusai -e NEXUSAI_LEGACY_DATA_VOLUME=nexusai_nexus-data -e NEXUSAI_DB_DRIFT_AUTO_SYNC=1 -v /var/run/docker.sock:/var/run/docker.sock -v /opt/NexusAI:/opt/NexusAI -w /opt/NexusAI docker:27-cli sh -lc "sh ./scripts/deploy-bluegreen.sh"
```

Notes:

- Keep `NEXUSAI_DEPLOY_ENABLE=0` until you finish preflight.
- Update both `/opt/NexusAI` path segments in `NEXUSAI_DEPLOY_RUN_CMD` to your actual host repo path.
- Use an absolute host path in both `-v` and `-w`; avoid `/workspace` unless that exact path exists on the host.
- Avoid spaces inside `-e KEY=VALUE` entries in `NEXUSAI_DEPLOY_RUN_CMD` (for example, prefer `./scripts/switch-dashboard-color.sh` over `sh ./scripts/...`).
- `NEXUSAI_STOP_PREVIOUS_COLOR=0` keeps both colors running after switch for faster rollback and lower disruption risk.
- `NEXUSAI_DB_DRIFT_AUTO_SYNC=1` auto-reconciles host/legacy DB copies during deploy to reduce manual operator commands.
- The deploy runner now defaults to blue/green strategy and `./scripts/switch-dashboard-color.sh` if unset, but explicit `-e` pass-through is recommended.
- `NEXUSAI_DEPLOY_RECREATE_CORE=1` is the default and recreates `control_plane` and `worker_agent` from `docker-compose.yml` before switching dashboard traffic.
- Set `NEXUSAI_DEPLOY_CORE_SERVICES` if you need a different core service list.
- `NEXUSAI_DEPLOY_RECREATE_GATEWAY=1` is the default and force-recreates `dashboard_gateway` on each deploy so gateway config changes are applied without manual shell work.

## 4. Run Preflight

```bash
python scripts/preflight_deploy.py
```

Expected result: `Preflight passed.`

## 4b. DB Drift Guard (Critical)

Before bootstrap/deploy, NexusAI now runs:

```bash
sh ./scripts/check_db_drift.sh
```

Behavior with default `NEXUSAI_DB_DRIFT_AUTO_SYNC=1`:

- host missing + volume present -> host DB auto-restored from volume
- host present + volume missing -> volume auto-seeded from host
- host and volume differ -> newer DB copy is treated as canonical and synchronized to the older copy

Set `NEXUSAI_DB_DRIFT_AUTO_SYNC=0` for strict fail-closed mode (manual reconciliation required).

If strict mode is enabled and drift is reported:

1. back up host `data/` and legacy volume DB
2. choose canonical DB copy
3. synchronize canonical DB to the other location
4. rerun `sh ./scripts/check_db_drift.sh`
5. only then rerun deploy

## 5. Bootstrap First Active Color

```bash
sh ./scripts/bootstrap_bluegreen.sh
```

This starts:

- `dashboard_gateway` on `:5000`
- `dashboard_blue` as initial active target

## 6. Use the Settings Deploy Tab

1. Open `http://<host>:5000/settings`
2. Go to `Deploy` tab
3. Click `Check For Updates`
4. Click `Deploy Latest Commit` when commits differ

Deployment behavior:

- refreshes core runtime services first (`control_plane`, `worker_agent` by default)
- verifies control plane health before proceeding
- force-recreates `dashboard_gateway` by default so nginx/runtime routing changes are applied
- starts candidate color container (`blue` or `green`)
- waits for candidate `/health` to pass
- switches gateway route atomically
- verifies gateway health after switch
- verifies the active dashboard route through the gateway after switch
- rolls back traffic to previous color automatically if post-switch checks fail
- keeps previous color running by default (`NEXUSAI_STOP_PREVIOUS_COLOR=0`)

## 7. Rollback

If needed, switch back immediately:

```bash
NEXUSAI_TARGET_COLOR=blue sh ./scripts/switch-dashboard-color.sh blue
# or
NEXUSAI_TARGET_COLOR=green sh ./scripts/switch-dashboard-color.sh green
```

## 8. Operational Caveat

This implementation now refreshes the core runtime before switching the dashboard traffic path. For full-stack no-disruption cutover beyond simple recreate, add:

- task draining and quiesce checks
- backend state compatibility checks
- controlled switchover for API and worker planes
