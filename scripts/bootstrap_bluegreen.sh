#!/usr/bin/env sh
set -eu

echo "[bootstrap] running preflight checks"
if command -v python3 >/dev/null 2>&1; then
  python3 scripts/preflight_deploy.py
elif command -v python >/dev/null 2>&1; then
  python scripts/preflight_deploy.py
else
  echo "[bootstrap] blocked: neither python3 nor python found"
  exit 2
fi

echo "[bootstrap] checking DB drift guard"
sh ./scripts/check_db_drift.sh

mkdir -p data
if [ ! -f data/active_color.txt ]; then
  echo "blue" > data/active_color.txt
fi

echo "[bootstrap] starting gateway + blue dashboard"
docker compose -f docker-compose.bluegreen.yml --profile blue up -d --build dashboard_gateway dashboard_blue

echo "[bootstrap] switching gateway to blue"
NEXUSAI_TARGET_COLOR=blue ./scripts/switch-dashboard-color.sh blue

echo "[bootstrap] done"
echo "[bootstrap] dashboard is available at http://localhost:5000"
