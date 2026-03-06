#!/usr/bin/env sh
set -eu

# DB drift guard: detect drift between host-mounted data and legacy volume data.
# Default behavior auto-synchronizes to reduce operator friction:
# - host missing + volume present => restore host from volume
# - host present + volume missing => seed volume from host
# - both present + differ => host is treated as canonical and copied to volume
# Set NEXUSAI_DB_DRIFT_AUTO_SYNC=0 for strict fail-closed behavior.

HOST_DB="data/nexusai.db"
LEGACY_VOL="${NEXUSAI_LEGACY_DATA_VOLUME:-nexusai_nexus-data}"
AUTO_SYNC="${NEXUSAI_DB_DRIFT_AUTO_SYNC:-1}"

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

mkdir -p data

sync_vol_to_host() {
  docker run --rm -v "$LEGACY_VOL:/from" -v "$(pwd)/data:/to" alpine sh -lc "cp -av /from/nexusai.db /to/nexusai.db"
}

sync_host_to_vol() {
  docker run --rm -v "$(pwd)/data:/from" -v "$LEGACY_VOL:/to" alpine sh -lc "cp -av /from/nexusai.db /to/nexusai.db"
}

HOST_EXISTS=0
if [ -f "$HOST_DB" ]; then
  HOST_EXISTS=1
fi

VOL_EXISTS=0
if docker run --rm -v "$LEGACY_VOL:/from" alpine sh -lc "[ -f /from/nexusai.db ]"; then
  VOL_EXISTS=1
fi

if [ "$HOST_EXISTS" -eq 0 ] && [ "$VOL_EXISTS" -eq 1 ]; then
  if [ "$AUTO_SYNC" = "1" ]; then
    echo "[db-check] host DB missing but legacy volume has nexusai.db; auto-restoring host DB"
    sync_vol_to_host
    echo "[db-check] auto-sync complete"
    exit 0
  fi
  echo "[db-check] drift detected: host DB missing but legacy volume has nexusai.db"
  echo "[db-check] set NEXUSAI_DB_DRIFT_AUTO_SYNC=1 to auto-restore host DB"
  exit 3
fi

if [ "$HOST_EXISTS" -eq 1 ] && [ "$VOL_EXISTS" -eq 0 ]; then
  if [ "$AUTO_SYNC" = "1" ]; then
    echo "[db-check] host DB present and legacy volume DB missing; auto-seeding legacy volume"
    sync_host_to_vol
    echo "[db-check] auto-sync complete"
    exit 0
  fi
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
  if [ "$AUTO_SYNC" = "1" ]; then
    echo "[db-check] drift detected: host DB and legacy volume DB differ"
    echo "[db-check] host sha:   $HOST_SHA"
    echo "[db-check] volume sha: $VOL_SHA"
    echo "[db-check] auto-sync enabled: treating host DB as canonical and updating legacy volume"
    sync_host_to_vol
    echo "[db-check] auto-sync complete"
    exit 0
  fi
  echo "[db-check] drift detected: host DB and legacy volume DB differ"
  echo "[db-check] host sha:   $HOST_SHA"
  echo "[db-check] volume sha: $VOL_SHA"
  echo "[db-check] choose canonical DB and synchronize before continuing"
  echo "[db-check] or set NEXUSAI_DB_DRIFT_AUTO_SYNC=1 to auto-sync host -> volume"
  exit 4
fi

echo "[db-check] DBs are consistent"
exit 0
