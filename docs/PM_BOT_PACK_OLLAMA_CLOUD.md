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

## Model Catalog Setup

Before a bot can use a model, the model **must** be registered in the NexusAI Model Catalog
(`Settings → Model Catalog`) **and** the model must be present on the Ollama Cloud endpoint.

### Current Workflow (Manual Pull Required)

The Ollama Cloud endpoint used by NexusAI exposes a chat API only. The `/api/pull` endpoint is
not available on most hosted Ollama Cloud deployments, so models cannot be downloaded
automatically through the dashboard. Until automated pull support is added (see roadmap below),
follow this process for any new model:

1. **SSH into the Ollama server** (the machine running your Ollama Cloud endpoint).
2. Run:
   ```
   ollama pull <model-name>
   ```
   Example:
   ```
   ollama pull qwen3-coder-next:80b-cloud
   ollama pull kimi-k2.5:cloud
   ```
3. Wait for the pull to complete. Large models can take 10–60 minutes depending on size and
   network speed.
4. **Register the model in the NexusAI dashboard**:
   - Go to `Settings → Model Catalog`.
   - Type the exact model name (e.g. `qwen3-coder-next:80b-cloud`) in the input box.
   - Select provider `ollama_cloud`.
   - Click **Find** — it should show ✅ if the pull succeeded.
   - Click **Add to Catalog**.
5. Update any bot configs that should use this model to set `"model": "<exact-name>"`.

**Tip:** Use the **List All** button in the Model Catalog to see every model currently pulled on
your endpoint, with clickable chips to auto-fill the input. This avoids tag typos.

### Discovering Available Model Tags

Model names on Ollama Cloud follow the format `<base-model>:<size>-<tag>`, for example:
- `qwen3.5:397b-cloud`
- `gpt-oss:120b-cloud`
- `qwen3-coder-next:80b-cloud`

The bare model name (e.g. `qwen3-next`) without a tag will be rejected with a 404. Always
use the full tag. Use **List All** in the Model Catalog to confirm available names before
assigning them to bot configs.

### Roadmap — Automated Model Management (Future)

The following improvements are planned to remove the need for manual SSH pulls:

| # | Feature | Description |
|---|---------|-------------|
| 1 | **Ollama pull API support** | When the Ollama Cloud endpoint implements `POST /api/pull`, NexusAI will automatically pull any model that returns a 404 at chat time. The scheduler already has this retry logic — it just needs the endpoint to exist. |
| 2 | **Pull-on-demand from dashboard** | The **Pull Model** button in `Settings → Model Catalog` is already wired. Once the Ollama endpoint supports pulls, clicking Pull will download the model without SSH. |
| 3 | **Auto-pull on first use** | When a bot tries to use a model not yet on the endpoint, the scheduler auto-pulls it (up to 30-min timeout) before retrying the inference request. This is implemented in `scheduler.py:_pull_ollama_cloud_model` and activates automatically when pull support becomes available. |
| 4 | **Scheduled model sync** | A future settings option will let you define a list of models to keep available. A background job will check `/api/tags` periodically and pull any missing ones, keeping the endpoint in sync without manual intervention. |
| 5 | **SSH agent for pull** | For deployments where the Ollama server is SSH-accessible from the control plane, NexusAI could trigger pulls via SSH rather than the API. This would work around endpoints that will never expose `/api/pull` directly. |

Until items 1 or 5 are implemented, manual SSH pull is the required path. Once completed, all
new models can be added and used entirely from the dashboard.

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
