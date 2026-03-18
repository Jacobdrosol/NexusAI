# PM Bot Pack (Ollama Cloud)

This guide defines a reusable Project Manager workflow bot pack for `@assign` orchestration in NexusAI.

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

### How they connect

- The user sends `@assign <instruction>` in chat.
- `pm-orchestrator` builds a dependency plan with explicit `bot_id` values.
- `PMOrchestrator` preserves those explicit bot IDs and only falls back to role-based routing when a plan step omits `bot_id`.
- The workers execute in dependency order and return a summarized assignment result.

The generated PM pack also includes explicit workflow triggers on the worker bots:

- Linear forward routing: `pm-research-analyst -> pm-engineer -> pm-coder -> pm-tester -> pm-security-reviewer -> pm-database-engineer -> pm-ui-tester`
- Deterministic backward routing: QA/review bots route only to explicitly configured earlier bots based on structured `failure_type` values.
- Terminal stage: `pm-ui-tester` ends the loop on pass; it only routes backward to allowed fix owners on failure.

## Model Policy

All bots use `backends[].type = cloud_api` and `provider = ollama_cloud`.

Recommended model split:

1. Planning/review/UI validation: `gpt-oss:120b-cloud`
2. Coding/research/database: `qwen3.5:397b-cloud`
3. (Optional alternative for targeted creative generation) `glm-5:cloud`

## Install / Export

Use the setup script to generate import bundles and/or apply directly to control plane.

Generate import bundles:

```bash
py scripts/setup_pm_bot_pack.py --export-dir "<path-to-export-bundles>"
```

Generate bundles with bot-level chat workspace tools pre-enabled:

```bash
py scripts/setup_pm_bot_pack.py --export-dir "<path-to-export-bundles>" --chat-tools-mode repo_and_filesystem
```

Apply directly to control plane:

```bash
py scripts/setup_pm_bot_pack.py --apply --base-url http://127.0.0.1:8000 --api-token <token>
```

Generate and apply in one command:

```bash
py scripts/setup_pm_bot_pack.py --export-dir "<path-to-export-bundles>" --apply --base-url http://127.0.0.1:8000 --api-token <token>
```

### Chat Tool Access Modes

`setup_pm_bot_pack.py` supports bot-level chat workspace tool defaults:

1. `--chat-tools-mode off` (default)
2. `--chat-tools-mode repo_search`
3. `--chat-tools-mode repo_and_filesystem`

These flags only set the bot-level gate. Runtime access is still denied unless project-level and chat-level tool access are also enabled.

## Database Workflows

For DB schema/query/migration tasks:

1. Attach a DB connection to `pm-database-engineer`.
2. Attach read-only schema discovery to `pm-coder` and `pm-tester` when needed.
3. Keep write credentials scoped to `pm-database-engineer`.
4. Require rollback notes and migration safety checks in acceptance criteria.

## Notes

- If a PM bot returns invalid plan JSON, orchestration falls back to a deterministic heuristic plan.
- Keep roles explicit (`pm`, `researcher`, `engineer`, `coder`, `tester`, `security-reviewer`, `dba-sql`, `ui-tester`) so routing stays predictable.
- The PM pack intentionally uses structured output contracts so worker triggers can route by explicit `failure_type` values instead of heuristics.
- Workspace tools use strict three-switch gating:
  1. bot routing rules (`chat_tool_access`)
  2. project chat workspace tool policy
  3. conversation chat tool access plus per-message `use_workspace_tools`
