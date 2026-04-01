#!/usr/bin/env sh
set -eu

echo "[deploy] starting blue/green deploy runner"
COMPOSE_PROJECT_NAME="${NEXUSAI_COMPOSE_PROJECT_NAME:-nexusai}"
export COMPOSE_PROJECT_NAME
COMPOSE_ARGS="-p $COMPOSE_PROJECT_NAME -f docker-compose.bluegreen.yml"
CORE_COMPOSE_ARGS="-p $COMPOSE_PROJECT_NAME -f docker-compose.yml"
STOP_PREVIOUS_COLOR="${NEXUSAI_STOP_PREVIOUS_COLOR:-0}"

echo "[deploy] checking DB drift guard"
sh ./scripts/check_db_drift.sh

CORE_RECREATE="${NEXUSAI_DEPLOY_RECREATE_CORE:-1}"
CORE_SERVICES="${NEXUSAI_DEPLOY_CORE_SERVICES:-control_plane worker_agent}"
GATEWAY_RECREATE="${NEXUSAI_DEPLOY_RECREATE_GATEWAY:-1}"

STRATEGY="${NEXUSAI_DEPLOY_STRATEGY:-bluegreen}"
if [ "$STRATEGY" != "bluegreen" ]; then
  echo "[deploy] blocked: NEXUSAI_DEPLOY_STRATEGY must be 'bluegreen'"
  exit 2
fi

if [ ! -f "docker-compose.bluegreen.yml" ]; then
  echo "[deploy] blocked: docker-compose.bluegreen.yml not found"
  echo "[deploy] no in-place restart is allowed by this runner"
  exit 2
fi

SWITCH_CMD="${NEXUSAI_BLUEGREEN_SWITCH_CMD:-./scripts/switch-dashboard-color.sh}"
CURRENT_COLOR_FILE="data/active_color.txt"
CURRENT_COLOR="blue"
SWITCHED=0

if [ -f "$CURRENT_COLOR_FILE" ]; then
  CURRENT_COLOR="$(cat "$CURRENT_COLOR_FILE" | tr -d '\r\n' || true)"
fi

if [ "$CURRENT_COLOR" != "blue" ] && [ "$CURRENT_COLOR" != "green" ]; then
  CURRENT_COLOR="blue"
fi

if [ "$CURRENT_COLOR" = "blue" ]; then
  NEXT_COLOR="green"
else
  NEXT_COLOR="blue"
fi

ensure_runtime_nginx_conf() {
  mkdir -p data/nginx
  if [ ! -f data/nginx/default.conf ]; then
    cp "deploy/nginx/default.$CURRENT_COLOR.conf" data/nginx/default.conf
  fi
}

ensure_service_running() {
  SERVICE_NAME="$1"
  SERVICE_PROFILE="${2:-}"
  SERVICE_ID="$(docker compose $COMPOSE_ARGS ps -q "$SERVICE_NAME" 2>/dev/null || true)"

  if [ -z "$SERVICE_ID" ]; then
    if [ -n "$SERVICE_PROFILE" ]; then
      docker compose $COMPOSE_ARGS --profile "$SERVICE_PROFILE" up -d "$SERVICE_NAME"
    else
      docker compose $COMPOSE_ARGS up -d "$SERVICE_NAME"
    fi
    return 0
  fi

  SERVICE_RUNNING="$(docker inspect -f '{{.State.Running}}' "$SERVICE_ID" 2>/dev/null || echo false)"
  if [ "$SERVICE_RUNNING" != "true" ]; then
    if ! docker compose $COMPOSE_ARGS start "$SERVICE_NAME"; then
      if [ -n "$SERVICE_PROFILE" ]; then
        docker compose $COMPOSE_ARGS --profile "$SERVICE_PROFILE" up -d "$SERVICE_NAME"
      else
        docker compose $COMPOSE_ARGS up -d "$SERVICE_NAME"
      fi
    fi
  fi
}

run_switch_command() {
  if [ -f "$SWITCH_CMD" ]; then
    if [ -x "$SWITCH_CMD" ]; then
      "$SWITCH_CMD"
    else
      sh "$SWITCH_CMD"
    fi
  else
    sh -lc "$SWITCH_CMD"
  fi
}

remove_legacy_dashboard_bindings() {
  if [ -f "docker-compose.yml" ]; then
    LEGACY_DASHBOARD_ID="$(docker compose $CORE_COMPOSE_ARGS ps -q dashboard 2>/dev/null || true)"
    if [ -n "$LEGACY_DASHBOARD_ID" ]; then
      echo "[deploy] removing legacy dashboard service that binds port 5000"
      docker compose $CORE_COMPOSE_ARGS rm -sf dashboard || true
    fi
  fi

  EXISTING_GATEWAY_ID="$(docker ps -aq -f name=^nexus-dashboard-gateway$ 2>/dev/null || true)"
  if [ -n "$EXISTING_GATEWAY_ID" ]; then
    echo "[deploy] removing existing dashboard gateway container"
    docker rm -f nexus-dashboard-gateway >/dev/null 2>&1 || true
  fi
}

rollback_on_error() {
  RC=$?
  if [ "$RC" -ne 0 ]; then
    echo "[deploy] failed with exit code $RC"
    if [ "$SWITCHED" -eq 1 ]; then
      echo "[deploy] attempting rollback to $CURRENT_COLOR"
      docker compose $COMPOSE_ARGS --profile "$CURRENT_COLOR" up -d "dashboard_$CURRENT_COLOR" || true
      export NEXUSAI_TARGET_COLOR="$CURRENT_COLOR"
      run_switch_command || true
      echo "[deploy] rollback attempted"
    fi
  fi
  exit "$RC"
}

trap 'rollback_on_error' EXIT

ensure_runtime_nginx_conf

print_control_plane_diagnostics() {
  echo "[deploy] control_plane diagnostics:"
  docker compose $CORE_COMPOSE_ARGS ps control_plane || true
  docker compose $CORE_COMPOSE_ARGS logs --tail=120 control_plane || true
}

wait_for_control_plane_health() {
  ATTEMPTS=0
  while true; do
    CONTAINER_ID="$(docker compose $CORE_COMPOSE_ARGS ps -q control_plane 2>/dev/null || true)"
    if [ -n "$CONTAINER_ID" ]; then
      HEALTH_STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER_ID" 2>/dev/null || true)"
      if [ "$HEALTH_STATUS" = "healthy" ] || [ "$HEALTH_STATUS" = "running" ]; then
        return 0
      fi
    fi

    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 30 ]; then
      echo "[deploy] control_plane health check failed"
      print_control_plane_diagnostics
      exit 5
    fi
    sleep 2
  done
}

ensure_active_dashboard_available() {
  ACTIVE_SERVICE="dashboard_$CURRENT_COLOR"
  echo "[deploy] ensuring active dashboard backend is available: $ACTIVE_SERVICE"
  # Never recreate the currently-active color during deploy prechecks.
  # If it exists, keep it as-is and only verify health.
  ensure_service_running "$ACTIVE_SERVICE" "$CURRENT_COLOR"

  ATTEMPTS=0
  TARGET_HOST="nexus-dashboard-$CURRENT_COLOR"
  until docker run --rm --network "${COMPOSE_PROJECT_NAME}_nexus-bluegreen" alpine:latest sh -lc "wget -q -O - http://$TARGET_HOST:5000/health | grep -q '\"status\"'"; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 30 ]; then
      echo "[deploy] active dashboard backend $ACTIVE_SERVICE failed health check"
      docker compose $COMPOSE_ARGS logs --tail=120 "$ACTIVE_SERVICE" || true
      exit 8
    fi
    sleep 2
  done
}

wait_for_dashboard_control_plane_health() {
  DASHBOARD_SERVICE="$1"
  ATTEMPTS=0
  while true; do
    if docker compose $COMPOSE_ARGS exec -T "$DASHBOARD_SERVICE" sh -lc "python -c \"import os, urllib.request; base=(os.environ.get('CONTROL_PLANE_URL') or 'http://control_plane:8000').rstrip('/'); resp=urllib.request.urlopen(base + '/health', timeout=5); body=resp.read().decode('utf-8', 'ignore'); import sys; sys.exit(0 if 'ok' in body else 1)\""; then
      return 0
    fi
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 30 ]; then
      echo "[deploy] $DASHBOARD_SERVICE cannot reach the control plane"
      docker compose $COMPOSE_ARGS logs --tail=120 "$DASHBOARD_SERVICE" || true
      exit 7
    fi
    sleep 2
  done
}

echo "[deploy] fetching latest main"
git fetch origin main
git checkout main
git pull --ff-only origin main

if [ "$CORE_RECREATE" = "1" ] && [ -f "docker-compose.yml" ]; then
  echo "[deploy] recreating core runtime services against persistent ./data state"
  echo "[deploy] compose project: $COMPOSE_PROJECT_NAME"
  echo "[deploy] core services: $CORE_SERVICES"
  docker compose $CORE_COMPOSE_ARGS up -d --build --force-recreate $CORE_SERVICES
else
  echo "[deploy] skipping core runtime recreate (NEXUSAI_DEPLOY_RECREATE_CORE=$CORE_RECREATE)"
fi

if [ "$CORE_RECREATE" = "1" ] && [ -f "docker-compose.yml" ]; then
  echo "[deploy] verifying core runtime health"
  wait_for_control_plane_health
fi

echo "[deploy] current color: $CURRENT_COLOR"
echo "[deploy] candidate color: $NEXT_COLOR"

ensure_active_dashboard_available

if [ "$GATEWAY_RECREATE" = "1" ]; then
  remove_legacy_dashboard_bindings
  echo "[deploy] recreating dashboard gateway"
  docker compose $COMPOSE_ARGS up -d --force-recreate dashboard_gateway
else
  echo "[deploy] ensuring gateway is running (no recreate)"
  ensure_service_running "dashboard_gateway"
fi

echo "[deploy] building and starting candidate dashboard"
docker compose $COMPOSE_ARGS --profile "$NEXT_COLOR" up -d --build --force-recreate "dashboard_$NEXT_COLOR"

echo "[deploy] waiting for candidate health"
ATTEMPTS=0
TARGET_HOST="nexus-dashboard-$NEXT_COLOR"
until docker compose $COMPOSE_ARGS exec -T dashboard_gateway sh -lc "wget -q -O - http://$TARGET_HOST:5000/health | grep -q '\"status\"'"; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge 30 ]; then
    echo "[deploy] candidate dashboard_$NEXT_COLOR failed health check"
    exit 3
  fi
  sleep 2
done
echo "[deploy] candidate dashboard_$NEXT_COLOR is healthy"

echo "[deploy] verifying candidate dashboard can reach control plane"
wait_for_dashboard_control_plane_health "dashboard_$NEXT_COLOR"

export NEXUSAI_TARGET_COLOR="$NEXT_COLOR"
export NEXUSAI_PREVIOUS_COLOR="$CURRENT_COLOR"

echo "[deploy] switching traffic to $NEXT_COLOR"
run_switch_command
SWITCHED=1

echo "[deploy] verifying post-switch health"
ATTEMPTS=0
until docker compose $COMPOSE_ARGS exec -T dashboard_gateway sh -lc "wget -q -O - http://127.0.0.1:5000/health | grep -q '\"status\"'"; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge 20 ]; then
    echo "[deploy] post-switch gateway health check failed"
    exit 4
  fi
  sleep 1
done

echo "[deploy] verifying active dashboard route via gateway"
ATTEMPTS=0
until docker compose $COMPOSE_ARGS exec -T dashboard_gateway sh -lc "wget -q -O - http://127.0.0.1:5000/health | grep -q '\"status\"'"; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge 20 ]; then
    echo "[deploy] active dashboard route failed verification"
    exit 6
  fi
  sleep 1
done

echo "$NEXT_COLOR" > "$CURRENT_COLOR_FILE"

if [ "$STOP_PREVIOUS_COLOR" = "1" ]; then
  echo "[deploy] stopping previous color: $CURRENT_COLOR"
  docker compose $COMPOSE_ARGS --profile "$CURRENT_COLOR" stop "dashboard_$CURRENT_COLOR" || true
else
  echo "[deploy] leaving previous color running (NEXUSAI_STOP_PREVIOUS_COLOR=$STOP_PREVIOUS_COLOR)"
fi

echo "[deploy] completed"
trap - EXIT
