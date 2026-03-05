#!/usr/bin/env sh
set -eu

TARGET_COLOR="${1:-${NEXUSAI_TARGET_COLOR:-}}"
if [ -z "$TARGET_COLOR" ]; then
  echo "[switch] blocked: target color not provided"
  exit 2
fi

if [ "$TARGET_COLOR" != "blue" ] && [ "$TARGET_COLOR" != "green" ]; then
  echo "[switch] blocked: target color must be blue or green"
  exit 2
fi

SOURCE_CONF="deploy/nginx/default.$TARGET_COLOR.conf"
ACTIVE_CONF="deploy/nginx/default.conf"
if [ ! -f "$SOURCE_CONF" ]; then
  echo "[switch] blocked: missing $SOURCE_CONF"
  exit 2
fi

echo "[switch] applying nginx route config for $TARGET_COLOR"
cp "$SOURCE_CONF" "$ACTIVE_CONF"

echo "[switch] reloading gateway"
docker compose -f docker-compose.bluegreen.yml exec -T dashboard_gateway nginx -s reload

echo "[switch] verifying gateway health"
docker compose -f docker-compose.bluegreen.yml exec -T dashboard_gateway wget -q -O - http://localhost:5000/health >/dev/null

echo "[switch] done"
