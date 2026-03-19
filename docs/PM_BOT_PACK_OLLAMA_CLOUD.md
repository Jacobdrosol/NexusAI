# PM Bot Workflow Reference (Ollama Cloud)

This guide defines the required Project Manager workflow for `@assign` orchestration in NexusAI.

PM bot configs are managed as manually imported `*.bot.json` files outside this repository.

This repository does not include or support a setup/apply/export pack generator for PM bots.

## Topology

Use these role-aligned bots:

1. `pm-orchestrator` (`role: pm`)
2. `pm-research-analyst` (`role: researcher`)
3. `pm-engineer` (`role: engineer`)
4. `pm-coder` (`role: coder`)
5. `pm-tester` (`role: tester`)
6. `pm-security-reviewer` (`role: security-reviewer`)
7. `pm-database-engineer` (`role: dba-sql`)
8. `pm-ui-tester` (`role: ui-tester`)
9. `pm-final-qc` (`role: final-qc`)

### How they connect

- The user sends `@assign <instruction>` in chat.
- `pm-orchestrator` builds a dependency plan with explicit `bot_id` values.
- `PMOrchestrator` preserves those explicit bot IDs and only falls back to role-based routing when a plan step omits `bot_id`.
- The workers execute in dependency order and return a summarized assignment result.

The imported PM bot configs should include explicit workflow triggers on the worker bots:

- Forward routing:
  - `pm-orchestrator ->` three parallel `pm-research-analyst` branches for repo/code research, requirements/data context, and external/online research when needed
  - the three research branches converge into one `pm-engineer`
  - `pm-engineer -> pm-coder` with fan-out when implementation workstreams require it
  - `pm-coder -> pm-tester -> pm-security-reviewer`
  - all approved security branches join into `pm-database-engineer`
  - `pm-database-engineer -> pm-ui-tester -> pm-final-qc`
- Allowed backward routing only:
  - `pm-tester -> pm-coder`
  - `pm-security-reviewer -> pm-coder`
  - `pm-ui-tester -> pm-database-engineer` for `ui_data_issue` and `ui_config_issue`
  - `pm-ui-tester -> pm-engineer` for `ui_render_issue`, `environment_blocker`, and hard UI execution failures
  - `pm-final-qc -> pm-engineer`
- UI scope guard: `pm-ui-tester` can return `skip` when no UI deliverables are present, then continue to final QC.
- Terminal stage: `pm-final-qc` is the end-of-workflow quality gate and should not be used as a branch-local retry step.

## Model Policy

All bots use `backends[].type = cloud_api` and `provider = ollama_cloud`.

Recommended model split:

1. Planning/review/UI validation: `gpt-oss:120b-cloud`
2. Coding/research/database: `qwen3.5:397b-cloud`
3. (Optional alternative for targeted creative generation) `glm-5:cloud`

## Import Policy

Import PM bots manually from your operator-managed `*.bot.json` files.

Recommended operator practice:

1. Keep the PM bot JSON files in a separate import folder outside this repository.
2. Import them through `Bots -> Import`.
3. Re-import updated JSON files when workflow routing changes.
4. Validate the workflow in chat with a small `@assign` run before using it on larger tasks.

## Database Workflows

For DB schema/query/migration tasks:

1. Attach a DB connection to `pm-database-engineer`.
2. Attach read-only schema discovery to `pm-coder` and `pm-tester` when needed.
3. Keep write credentials scoped to `pm-database-engineer`.
4. Require rollback notes and migration safety checks in acceptance criteria.

## Notes

- If a PM bot returns invalid plan JSON, orchestration falls back to a deterministic heuristic plan.
- Keep roles explicit (`pm`, `researcher`, `engineer`, `coder`, `tester`, `security-reviewer`, `dba-sql`, `ui-tester`, `final-qc`) so routing stays predictable.
- The PM workflow intentionally uses structured output contracts so worker triggers can route by explicit `failure_type` values instead of heuristics.
- When a worker is expected to produce repo files or reports, it should return full file contents in an `artifacts` array of `{path, content}` objects so downstream validation can inspect concrete outputs.
- Workspace tools use strict three-switch gating:
  1. bot routing rules (`chat_tool_access`)
  2. project chat workspace tool policy
  3. conversation chat tool access plus per-message `use_workspace_tools`
