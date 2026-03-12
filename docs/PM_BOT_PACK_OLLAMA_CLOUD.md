# PM Bot Pack (Ollama Cloud)

This guide defines a reusable Project Manager workflow bot pack for `@assign` orchestration in NexusAI.

## Topology

Use these role-aligned bots:

1. `pm-orchestrator` (`role: pm`)
2. `pm-research-analyst` (`role: researcher`)
3. `pm-coder` (`role: coder`)
4. `pm-tester` (`role: tester`)
5. `pm-security-reviewer` (`role: security-reviewer`)
6. `pm-database-engineer` (`role: dba-sql`)

### How they connect

- The user sends `@assign <instruction>` in chat.
- `pm-orchestrator` builds a dependency plan with `role_hint` values.
- `PMOrchestrator` creates dependent tasks and maps each task to the best bot by role.
- The workers execute in dependency order and return a summarized assignment result.

No explicit workflow triggers are required between these bots; the dependency graph is managed by chat orchestration logic.

## Model Policy

All bots use `backends[].type = cloud_api` and `provider = ollama_cloud`.

Recommended model split:

1. Planning/review: `gpt-oss:120b-cloud`
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
- Keep roles explicit (`pm`, `coder`, `tester`, `reviewer`, `security`, `dba`) so routing stays predictable.
- Workspace tools use strict three-switch gating:
  1. bot routing rules (`chat_tool_access`)
  2. project chat workspace tool policy
  3. conversation chat tool access plus per-message `use_workspace_tools`
