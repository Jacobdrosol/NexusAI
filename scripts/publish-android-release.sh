#!/usr/bin/env sh
set -eu

RUNTIME_DATA_DIR="${NEXUSAI_RUNTIME_DATA_DIR:-data}"
RELEASE_DIR="$RUNTIME_DATA_DIR/nginx/releases"
MANIFEST="$RUNTIME_DATA_DIR/mobile-release.json"
VERSION_CODE="$(sed -n 's/^[[:space:]]*versionCode = \([0-9][0-9]*\).*/\1/p' android/app/build.gradle.kts | head -n 1)"
[ -n "$VERSION_CODE" ] || { echo '[android] versionCode not found'; exit 2; }
CURRENT="$(sed -n 's/.*"version_code"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$MANIFEST" 2>/dev/null || true)"
if [ -n "$CURRENT" ] && [ "$VERSION_CODE" -le "$CURRENT" ]; then echo "[android] version $VERSION_CODE already published"; exit 0; fi

SIGNING_ENV="${NEXUSAI_ANDROID_SIGNING_ENV:-$RUNTIME_DATA_DIR/android-signing/release-signing.env}"
. "$SIGNING_ENV"
export NEXUSAI_ANDROID_STORE_FILE NEXUSAI_ANDROID_STORE_PASSWORD NEXUSAI_ANDROID_KEY_ALIAS NEXUSAI_ANDROID_KEY_PASSWORD
ANDROID_BUILD_IMAGE="${NEXUSAI_ANDROID_BUILD_IMAGE:-ghcr.io/cirruslabs/android-sdk:36}"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$(pwd):/workspace" -w /workspace/android \
  -e NEXUSAI_ANDROID_STORE_FILE -e NEXUSAI_ANDROID_STORE_PASSWORD \
  -e NEXUSAI_ANDROID_KEY_ALIAS -e NEXUSAI_ANDROID_KEY_PASSWORD \
  "$ANDROID_BUILD_IMAGE" ./gradlew :app:assembleRelease
mkdir -p "$RELEASE_DIR"
cp android/app/build/outputs/apk/release/app-release.apk "$RELEASE_DIR/nexusai.apk"
printf '{"version_code":%s,"minimum_version_code":1,"release_url":"https://chat.globeiq.org/releases/nexusai.apk"}\n' "$VERSION_CODE" > "$MANIFEST"
