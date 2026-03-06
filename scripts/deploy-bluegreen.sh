#!/usr/bin/env sh
set -eu

echo "[deploy] starting blue/green deploy runner"
COMPOSE_PROJECT_NAME="${NEXUSAI_COMPOSE_PROJECT_NAME:-nexusai}"
COMPOSE_ARGS="-p $COMPOSE_PROJECT_NAME -f docker-compose.bluegreen.yml"

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

echo "[deploy] fetching latest main"
git fetch origin main
git checkout main
git pull --ff-only origin main

CURRENT_COLOR_FILE="data/active_color.txt"
CURRENT_COLOR="blue"
if [ -f "$CURRENT_COLOR_FILE" ]; then
  CURRENT_COLOR="$(cat "$CURRENT_COLOR_FILE" | tr -d '\r\n' || true)"
fi

if [ "$CURRENT_COLOR" = "blue" ]; then
  NEXT_COLOR="green"
else
  NEXT_COLOR="blue"
fi

echo "[deploy] current color: $CURRENT_COLOR"
echo "[deploy] candidate color: $NEXT_COLOR"

echo "[deploy] ensuring gateway is running"
docker compose $COMPOSE_ARGS up -d dashboard_gateway

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
sh -lc "$SWITCH_CMD"

echo "$NEXT_COLOR" > "$CURRENT_COLOR_FILE"

echo "[deploy] stopping previous color: $CURRENT_COLOR"
docker compose $COMPOSE_ARGS --profile "$CURRENT_COLOR" stop "dashboard_$CURRENT_COLOR" || true

echo "[deploy] completed"
