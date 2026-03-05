# Blue/Green Deployment Guide

This guide prepares NexusAI for repeatable, low-disruption dashboard deployments using the Settings `Deploy` tab.

## 1. Prerequisites

- Docker + Docker Compose installed on the target host
- Repository cloned on target host (example path: `/opt/nexusai`)
- `.env` created from `.env.example`

## 2. Deployment Assets Included

- `docker-compose.bluegreen.yml`
- `deploy/nginx/default.conf`
- `deploy/nginx/default.blue.conf`
- `deploy/nginx/default.green.conf`
- `scripts/bootstrap_bluegreen.sh`
- `scripts/deploy-bluegreen.sh`
- `scripts/switch-dashboard-color.sh`
- `scripts/preflight_deploy.py`

## 3. Configure `.env`

Set:

```bash
NEXUSAI_DEPLOY_ENABLE=1
NEXUSAI_DEPLOY_STRATEGY=bluegreen
NEXUSAI_BLUEGREEN_SWITCH_CMD=./scripts/switch-dashboard-color.sh
NEXUSAI_DEPLOY_RUN_CMD=docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /opt/nexusai:/workspace -w /workspace docker:27-cli sh -lc "./scripts/deploy-bluegreen.sh"
```

Notes:

- Keep `NEXUSAI_DEPLOY_ENABLE=0` until you finish preflight.
- Update `/opt/nexusai` in `NEXUSAI_DEPLOY_RUN_CMD` to your actual repo path.

## 4. Run Preflight

```bash
python scripts/preflight_deploy.py
```

Expected result: `Preflight passed.`

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

- starts candidate color container (`blue` or `green`)
- waits for candidate `/health` to pass
- switches gateway route atomically
- stops previous color only after switch succeeds

## 7. Rollback

If needed, switch back immediately:

```bash
NEXUSAI_TARGET_COLOR=blue sh ./scripts/switch-dashboard-color.sh blue
# or
NEXUSAI_TARGET_COLOR=green sh ./scripts/switch-dashboard-color.sh green
```

## 8. Operational Caveat

This implementation blue/greens dashboard traffic path. For full-stack no-disruption cutover (control plane + workers), add:

- task draining and quiesce checks
- backend state compatibility checks
- controlled switchover for API and worker planes
