#!/usr/bin/env bash
set -Eeuo pipefail

# Platform AI repo-edit runner
# - consumes instruction from NEXUS_PLATFORM_AI_OPERATOR_INSTRUCTION
# - applies edits via aider (non-interactive)
# - runs a configurable test gate
# - commits and pushes only when changes exist

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${NEXUS_PLATFORM_AI_REPO_EDIT_CWD:-$DEFAULT_REPO_ROOT}"
LOG_DIR="${NEXUS_PLATFORM_AI_RUNNER_LOG_DIR:-$REPO_ROOT/data}"
SESSION_ID="${NEXUS_PLATFORM_AI_SESSION_ID:-unknown}"
KIND="${NEXUS_PLATFORM_AI_REPO_EDIT_KIND:-repo_edit}"
PROMPT="${NEXUS_PLATFORM_AI_OPERATOR_INSTRUCTION:-Autonomous platform improvement pass}"

# Main branch workflow is the default in this repository/deploy flow.
BASE_BRANCH="${NEXUS_PLATFORM_AI_REPO_EDIT_BASE_BRANCH:-main}"
PUSH_REF="${NEXUS_PLATFORM_AI_REPO_EDIT_PUSH_REF:-$BASE_BRANCH}"
COMMIT_PREFIX="${NEXUS_PLATFORM_AI_REPO_EDIT_COMMIT_PREFIX:-platform-ai: autonomous update}"
TEST_CMD="${NEXUS_PLATFORM_AI_REPO_EDIT_TEST_CMD:-pytest -q tests/test_platform_ai_runtime.py tests/test_pipeline_overhaul_api.py}"
AIDER_TIMEOUT_SECONDS="${NEXUS_PLATFORM_AI_RUNNER_AIDER_TIMEOUT_SECONDS:-1800}"
SYNC_BASE="${NEXUS_PLATFORM_AI_REPO_EDIT_SYNC_BASE:-1}"

mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/platform_ai_repo_edit_${SESSION_ID}_${STAMP}.log"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

info() { printf '[runner] %s\n' "$*"; }
warn() { printf '[runner][warn] %s\n' "$*"; }
fail() { printf '[runner][error] %s\n' "$*" >&2; exit 1; }

on_exit() {
  local rc="$1"
  if [[ "$rc" -eq 0 ]]; then
    info "completed successfully (session=$SESSION_ID kind=$KIND log=$LOG_FILE)"
  else
    warn "failed rc=$rc (session=$SESSION_ID kind=$KIND log=$LOG_FILE)"
  fi
}
trap 'on_exit $?' EXIT

[[ -d "$REPO_ROOT" ]] || fail "repo root not found: $REPO_ROOT"
cd "$REPO_ROOT"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "not a git work tree: $REPO_ROOT"
command -v aider >/dev/null 2>&1 || fail "aider is not installed or not in PATH"
command -v git >/dev/null 2>&1 || fail "git is not installed or not in PATH"

if [[ -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" && -z "${GEMINI_API_KEY:-}" && -z "${OPENROUTER_API_KEY:-}" ]]; then
  warn "no common LLM API key env var detected; aider may fail unless configured via other means"
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

info "running aider for session=$SESSION_ID kind=$KIND"
if command -v timeout >/dev/null 2>&1; then
  timeout --signal=TERM "$AIDER_TIMEOUT_SECONDS" \
    aider --yes --no-auto-commits --message "$PROMPT"
else
  warn "timeout command not found; running aider without hard timeout"
  aider --yes --no-auto-commits --message "$PROMPT"
fi

if [[ -n "$TEST_CMD" ]]; then
  info "running test gate: $TEST_CMD"
  bash -lc "$TEST_CMD"
fi

info "staging changes"
git add -A
if git diff --cached --quiet; then
  info "no staged changes; nothing to commit"
  exit 0
fi

COMMIT_MSG="$COMMIT_PREFIX (session $SESSION_ID, kind $KIND, $STAMP)"
info "committing"
git commit -m "$COMMIT_MSG"

info "pushing to origin/$PUSH_REF"
git push origin "$PUSH_REF"

