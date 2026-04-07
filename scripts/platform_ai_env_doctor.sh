#!/usr/bin/env bash
set -euo pipefail

# Platform AI env validator for VM deployments.
# Usage:
#   source /opt/NexusAI/.env
#   bash /opt/NexusAI/scripts/platform_ai_env_doctor.sh

ok()   { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
err()  { printf '[error] %s\n' "$*"; }

fail_count=0

is_enabled() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  [[ "$raw" == "1" || "$raw" == "true" || "$raw" == "yes" || "$raw" == "on" ]]
}

require_non_empty() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$(echo "$value" | xargs)" ]]; then
    ok "$name is set"
  else
    err "$name is required but empty"
    fail_count=$((fail_count + 1))
  fi
}

check_dir_exists() {
  local label="$1"
  local path="$2"
  if [[ -z "$path" ]]; then
    warn "$label not set"
    return
  fi
  if [[ -d "$path" ]]; then
    ok "$label exists: $path"
  else
    err "$label does not exist: $path"
    fail_count=$((fail_count + 1))
  fi
}

echo "== Platform AI env doctor =="

if is_enabled "${NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED:-0}"; then
  ok "NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED is enabled"
  require_non_empty NEXUS_PLATFORM_AI_OWNER_ALLOWLIST
else
  warn "NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED is disabled"
fi

if is_enabled "${NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED:-0}"; then
  ok "NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED is enabled"
  require_non_empty NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD
fi

if is_enabled "${NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED:-0}"; then
  ok "NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED is enabled"
  require_non_empty NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_RUN_CMD
fi

if is_enabled "${NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED:-0}"; then
  ok "NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED is enabled"
  require_non_empty NEXUS_PLATFORM_AI_PROJECT_EDIT_RUN_CMD
fi

if is_enabled "${NEXUS_PLATFORM_AI_ENFORCE_PROJECT_ID:-0}"; then
  ok "NEXUS_PLATFORM_AI_ENFORCE_PROJECT_ID is enabled"
  if [[ -z "$(echo "${NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ID:-}" | xargs)" ]] && [[ -z "$(echo "${NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ALLOWLIST:-}" | xargs)" ]]; then
    err "Project binding is enabled but neither NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ID nor NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ALLOWLIST is set"
    fail_count=$((fail_count + 1))
  else
    ok "Platform project binding is configured"
  fi
fi

if is_enabled "${NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID:-0}"; then
  ok "NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID is enabled"
  if [[ -z "$(echo "${NEXUS_PLATFORM_AI_PROJECT_EDIT_PROJECT_ALLOWLIST:-}" | xargs)" ]]; then
    warn "NEXUS_PLATFORM_AI_PROJECT_EDIT_PROJECT_ALLOWLIST is empty (allowed but broad)"
  else
    ok "NEXUS_PLATFORM_AI_PROJECT_EDIT_PROJECT_ALLOWLIST is configured"
  fi
fi

check_dir_exists "NEXUS_PLATFORM_AI_REPO_EDIT_CWD" "${NEXUS_PLATFORM_AI_REPO_EDIT_CWD:-}"
check_dir_exists "NEXUS_PLATFORM_AI_PROJECT_EDIT_CWD" "${NEXUS_PLATFORM_AI_PROJECT_EDIT_CWD:-}"

if [[ "$fail_count" -gt 0 ]]; then
  err "env doctor found $fail_count blocking issue(s)"
  exit 1
fi

ok "env doctor passed"
