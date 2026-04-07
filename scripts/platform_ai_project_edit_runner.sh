#!/usr/bin/env bash
set -Eeuo pipefail

# Platform AI public project-edit runner
# - applies patch proposals (default: aider) and runs quality/documentation checks
# - explicitly does NOT commit or push

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${NEXUS_PLATFORM_AI_PROJECT_EDIT_CWD:-$DEFAULT_REPO_ROOT}"
LOG_DIR="${NEXUS_PLATFORM_AI_RUNNER_LOG_DIR:-$REPO_ROOT/data}"
SESSION_ID="${NEXUS_PLATFORM_AI_SESSION_ID:-unknown}"
SESSION_PROJECT_ID="${NEXUS_PLATFORM_AI_SESSION_PROJECT_ID:-}"
SESSION_MODE="${NEXUS_PLATFORM_AI_SESSION_MODE:-}"
PROMPT="${NEXUS_PLATFORM_AI_OPERATOR_INSTRUCTION:-Autonomous project improvement pass}"

BASE_BRANCH="${NEXUS_PLATFORM_AI_PROJECT_EDIT_BASE_BRANCH:-main}"
SYNC_BASE="${NEXUS_PLATFORM_AI_PROJECT_EDIT_SYNC_BASE:-1}"
PATCH_CMD="${NEXUS_PLATFORM_AI_PROJECT_EDIT_PATCH_CMD:-}"
TEST_CMD="${NEXUS_PLATFORM_AI_PROJECT_EDIT_TEST_CMD:-pytest -q tests/test_platform_ai_runtime.py tests/test_pipeline_overhaul_api.py}"
DOC_CMD="${NEXUS_PLATFORM_AI_PROJECT_EDIT_DOC_CMD:-}"
AIDER_TIMEOUT_SECONDS="${NEXUS_PLATFORM_AI_RUNNER_AIDER_TIMEOUT_SECONDS:-1800}"

mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/platform_ai_project_edit_${SESSION_ID}_${STAMP}.log"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

info() { printf '[project-runner] %s\n' "$*"; }
warn() { printf '[project-runner][warn] %s\n' "$*"; }
fail() { printf '[project-runner][error] %s\n' "$*" >&2; exit 1; }

bool_env() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  [[ "$raw" == "1" || "$raw" == "true" || "$raw" == "yes" || "$raw" == "on" ]]
}

csv_contains() {
  local needle="$1"
  local csv="$2"
  local item
  IFS=',' read -r -a _items <<< "$csv"
  for item in "${_items[@]}"; do
    item="$(echo "$item" | xargs)"
    [[ -n "$item" && "$item" == "$needle" ]] && return 0
  done
  return 1
}

on_exit() {
  local rc="$1"
  if [[ "$rc" -eq 0 ]]; then
    info "completed successfully (session=$SESSION_ID mode=$SESSION_MODE project_id=$SESSION_PROJECT_ID log=$LOG_FILE)"
  else
    warn "failed rc=$rc (session=$SESSION_ID mode=$SESSION_MODE project_id=$SESSION_PROJECT_ID log=$LOG_FILE)"
  fi
}
trap 'on_exit $?' EXIT

[[ -d "$REPO_ROOT" ]] || fail "repo root not found: $REPO_ROOT"
cd "$REPO_ROOT"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "not a git work tree: $REPO_ROOT"
command -v git >/dev/null 2>&1 || fail "git is not installed or not in PATH"

if bool_env "${NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID:-0}" && [[ -z "$SESSION_PROJECT_ID" ]]; then
  fail "project scope denied: NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID=1 requires NEXUS_PLATFORM_AI_SESSION_PROJECT_ID"
fi

PROJECT_ALLOWLIST="${NEXUS_PLATFORM_AI_PROJECT_EDIT_PROJECT_ALLOWLIST:-}"
if [[ -n "$(echo "$PROJECT_ALLOWLIST" | xargs)" ]]; then
  [[ -n "$SESSION_PROJECT_ID" ]] || fail "project scope denied: project allowlist configured but session has no project_id"
  csv_contains "$SESSION_PROJECT_ID" "$PROJECT_ALLOWLIST" || fail "project scope denied: session project_id '$SESSION_PROJECT_ID' not in '$PROJECT_ALLOWLIST'"
fi

if [[ "$SYNC_BASE" == "1" ]]; then
  if git diff --quiet && git diff --cached --quiet; then
    info "syncing local branch with origin/$BASE_BRANCH"
    git fetch origin "$BASE_BRANCH"
    git checkout "$BASE_BRANCH"
    git pull --ff-only origin "$BASE_BRANCH"
  else
    warn "working tree is dirty before run; skipping checkout/pull sync"
  fi
fi

if [[ -n "$PATCH_CMD" ]]; then
  info "running patch command from NEXUS_PLATFORM_AI_PROJECT_EDIT_PATCH_CMD"
  bash -lc "$PATCH_CMD"
else
  command -v aider >/dev/null 2>&1 || fail "aider is not installed or not in PATH (or set NEXUS_PLATFORM_AI_PROJECT_EDIT_PATCH_CMD)"
  info "running aider for session=$SESSION_ID mode=$SESSION_MODE project_id=$SESSION_PROJECT_ID"
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM "$AIDER_TIMEOUT_SECONDS" \
      aider --yes --no-auto-commits --message "$PROMPT"
  else
    warn "timeout command not found; running aider without hard timeout"
    aider --yes --no-auto-commits --message "$PROMPT"
  fi
fi

if [[ -n "$TEST_CMD" ]]; then
  info "running test gate: $TEST_CMD"
  bash -lc "$TEST_CMD"
fi

if [[ -n "$DOC_CMD" ]]; then
  info "running documentation gate: $DOC_CMD"
  bash -lc "$DOC_CMD"
fi

info "changed files after patch/test gate:"
git status --short || true

if git diff --quiet && git diff --cached --quiet; then
  info "no changes detected; exiting without commit/push"
  exit 0
fi

info "public project-edit run completed. Commit/push intentionally skipped."
