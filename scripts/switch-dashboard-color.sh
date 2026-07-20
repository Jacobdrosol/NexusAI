#!/usr/bin/env sh
set -eu

TARGET_COLOR="${1:-${NEXUSAI_TARGET_COLOR:-}}"
COMPOSE_PROJECT_NAME="${NEXUSAI_COMPOSE_PROJECT_NAME:-nexusai}"
COMPOSE_ARGS="-p $COMPOSE_PROJECT_NAME -f docker-compose.bluegreen.yml"
RUNTIME_DATA_DIR="${NEXUSAI_RUNTIME_DATA_DIR:-}"
if [ -z "$RUNTIME_DATA_DIR" ] && [ -f .env ]; then
  RUNTIME_DATA_DIR="$(sed -n 's/^NEXUSAI_RUNTIME_DATA_DIR=//p' .env | tail -n 1 | tr -d '\r')"
fi
RUNTIME_DATA_DIR="${RUNTIME_DATA_DIR:-data}"
export NEXUSAI_RUNTIME_DATA_DIR="$RUNTIME_DATA_DIR"
if [ -z "$TARGET_COLOR" ]; then
  echo "[switch] blocked: target color not provided"
  exit 2
fi

if [ "$TARGET_COLOR" != "blue" ] && [ "$TARGET_COLOR" != "green" ]; then
  echo "[switch] blocked: target color must be blue or green"
  exit 2
fi

SOURCE_CONF="deploy/nginx/default.$TARGET_COLOR.conf"
ACTIVE_CONF_DIR="$RUNTIME_DATA_DIR/nginx"
ACTIVE_CONF="$ACTIVE_CONF_DIR/default.conf"
if [ ! -f "$SOURCE_CONF" ]; then
  echo "[switch] blocked: missing $SOURCE_CONF"
  exit 2
fi

echo "[switch] applying nginx route config for $TARGET_COLOR"
mkdir -p "$ACTIVE_CONF_DIR"
if cp "$SOURCE_CONF" "$ACTIVE_CONF" 2>/dev/null; then
  echo "[switch] updated host runtime nginx config"
else
  echo "[switch] host runtime config not writable; applying config directly in gateway container"
  docker cp "$SOURCE_CONF" nexus-dashboard-gateway:/etc/nginx/conf.d/default.conf
fi

echo "[switch] reloading gateway"
docker compose $COMPOSE_ARGS exec -T dashboard_gateway nginx -s reload

echo "[switch] verifying gateway health"
ATTEMPTS=0
until docker compose $COMPOSE_ARGS exec -T dashboard_gateway wget -q -O - http://127.0.0.1:5000/health >/dev/null; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge 20 ]; then
    echo "[switch] health verification failed after reload"
    exit 3
  fi
  sleep 1
done

echo "[switch] done"
