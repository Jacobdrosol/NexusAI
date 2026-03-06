#!/usr/bin/env sh
set -eu

# Fail-closed guard: detect DB drift between host-mounted data and legacy volume data.
# This prevents accidental startup against an empty/new DB when historical data exists elsewhere.

HOST_DB="data/nexusai.db"
LEGACY_VOL="${NEXUSAI_LEGACY_DATA_VOLUME:-nexusai_nexus-data}"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

if ! has_cmd docker; then
  echo "[db-check] docker not found; cannot verify DB drift"
  exit 2
fi

if ! docker volume inspect "$LEGACY_VOL" >/dev/null 2>&1; then
  echo "[db-check] no legacy volume '$LEGACY_VOL' found; no drift detected"
  exit 0
fi

HOST_EXISTS=0
if [ -f "$HOST_DB" ]; then
  HOST_EXISTS=1
fi

VOL_EXISTS=0
if docker run --rm -v "$LEGACY_VOL:/from" alpine sh -lc "[ -f /from/nexusai.db ]"; then
  VOL_EXISTS=1
fi

if [ "$HOST_EXISTS" -eq 0 ] && [ "$VOL_EXISTS" -eq 1 ]; then
  echo "[db-check] drift detected: host DB missing but legacy volume has nexusai.db"
  echo "[db-check] restore first:"
  echo "  docker run --rm -v $LEGACY_VOL:/from -v \$(pwd)/data:/to alpine sh -lc 'cp -av /from/nexusai.db /to/nexusai.db'"
  exit 3
fi

if [ "$HOST_EXISTS" -eq 1 ] && [ "$VOL_EXISTS" -eq 0 ]; then
  echo "[db-check] host DB present and legacy volume DB missing; no drift detected"
  exit 0
fi

if [ "$HOST_EXISTS" -eq 0 ] && [ "$VOL_EXISTS" -eq 0 ]; then
  echo "[db-check] no DB found in host or legacy volume (fresh environment)"
  exit 0
fi

HOST_SHA="$(sha256sum "$HOST_DB" | awk '{print $1}')"
VOL_SHA="$(docker run --rm -v "$LEGACY_VOL:/from" alpine sh -lc "sha256sum /from/nexusai.db | awk '{print \$1}'")"

if [ "$HOST_SHA" != "$VOL_SHA" ]; then
  echo "[db-check] drift detected: host DB and legacy volume DB differ"
  echo "[db-check] host sha:   $HOST_SHA"
  echo "[db-check] volume sha: $VOL_SHA"
  echo "[db-check] choose canonical DB and synchronize before continuing"
  exit 4
fi

echo "[db-check] DBs are consistent"
exit 0
