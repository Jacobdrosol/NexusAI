#!/usr/bin/env sh
set -eu

echo "[deploy] starting blue/green deploy runner"
COMPOSE_PROJECT_NAME="${NEXUSAI_COMPOSE_PROJECT_NAME:-nexusai}"
COMPOSE_ARGS="-p $COMPOSE_PROJECT_NAME -f docker-compose.bluegreen.yml"
STOP_PREVIOUS_COLOR="${NEXUSAI_STOP_PREVIOUS_COLOR:-0}"

echo "[deploy] checking DB drift guard"
sh ./scripts/check_db_drift.sh

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

echo "[deploy] fetching latest main"
git fetch origin main
git checkout main
git pull --ff-only origin main

echo "[deploy] current color: $CURRENT_COLOR"
echo "[deploy] candidate color: $NEXT_COLOR"

echo "[deploy] ensuring gateway is running (no recreate)"
GATEWAY_ID="$(docker compose $COMPOSE_ARGS ps -q dashboard_gateway || true)"
if [ -z "$GATEWAY_ID" ]; then
  docker compose $COMPOSE_ARGS up -d dashboard_gateway
else
  GATEWAY_RUNNING="$(docker inspect -f '{{.State.Running}}' "$GATEWAY_ID" 2>/dev/null || echo false)"
  if [ "$GATEWAY_RUNNING" != "true" ]; then
    docker compose $COMPOSE_ARGS up -d dashboard_gateway
  fi
fi

echo "[deploy] building and starting candidate dashboard"
docker compose $COMPOSE_ARGS --profile "$NEXT_COLOR" up -d --build "dashboard_$NEXT_COLOR"

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

echo "$NEXT_COLOR" > "$CURRENT_COLOR_FILE"

if [ "$STOP_PREVIOUS_COLOR" = "1" ]; then
  echo "[deploy] stopping previous color: $CURRENT_COLOR"
  docker compose $COMPOSE_ARGS --profile "$CURRENT_COLOR" stop "dashboard_$CURRENT_COLOR" || true
else
  echo "[deploy] leaving previous color running (NEXUSAI_STOP_PREVIOUS_COLOR=$STOP_PREVIOUS_COLOR)"
fi

echo "[deploy] completed"
trap - EXIT
