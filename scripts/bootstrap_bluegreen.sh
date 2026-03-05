#!/usr/bin/env sh
set -eu

echo "[bootstrap] running preflight checks"
python scripts/preflight_deploy.py

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
