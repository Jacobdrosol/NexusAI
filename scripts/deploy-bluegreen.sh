#!/usr/bin/env sh
set -eu

echo "[deploy] starting blue/green deploy runner"

if [ "${NEXUSAI_DEPLOY_STRATEGY:-}" != "bluegreen" ]; then
  echo "[deploy] blocked: NEXUSAI_DEPLOY_STRATEGY must be 'bluegreen'"
  exit 2
fi

if [ ! -f "docker-compose.bluegreen.yml" ]; then
  echo "[deploy] blocked: docker-compose.bluegreen.yml not found"
  echo "[deploy] no in-place restart is allowed by this runner"
  exit 2
fi

if [ -z "${NEXUSAI_BLUEGREEN_SWITCH_CMD:-}" ]; then
  echo "[deploy] blocked: NEXUSAI_BLUEGREEN_SWITCH_CMD is not set"
  echo "[deploy] this command must atomically switch traffic to the new color"
  exit 2
fi

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

echo "[deploy] building and starting candidate stack"
docker compose -f docker-compose.bluegreen.yml --profile "$NEXT_COLOR" up -d --build

echo "[deploy] waiting for candidate stack health"
sleep 5
docker compose -f docker-compose.bluegreen.yml ps

echo "[deploy] switching traffic to $NEXT_COLOR"
sh -lc "$NEXUSAI_BLUEGREEN_SWITCH_CMD"

echo "$NEXT_COLOR" > "$CURRENT_COLOR_FILE"

echo "[deploy] stopping previous color: $CURRENT_COLOR"
docker compose -f docker-compose.bluegreen.yml --profile "$CURRENT_COLOR" stop || true

echo "[deploy] completed"
