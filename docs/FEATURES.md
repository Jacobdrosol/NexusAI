# NexusAI Feature Guide

This document catalogs the implemented capabilities of NexusAI. The README links to this page for the full feature inventory; keep the README short and direct readers here for detail.

---

## Dashboard and Workflow

- Navigation pages: `Overview`, `Projects`, `Chat`, `Memory`, `Work`, `Bots`, `Workers`, `Schedules`, `Supervision`, `Tasks`, `Pipelines`, `Platform AI`, `Vault`, `Users`, `Settings`.
- Worker detail pages with live load, queue, and GPU graphs.
- Bot detail editor with backend chain management, workflow triggers, saved input contracts, saved launch profiles, test runs, run history, and task board.
- Typed specialist blueprints for bounded roles such as content work, quality review, research, monitoring, operations reporting, and code review or implementation; each specialist is preflighted before activation.
- Bot export/import across the bot detail and bots index pages, including bot configuration and bot-scoped connections, with overwrite confirmation on ID conflicts.
- Bot-scoped external connections for HTTP/OpenAPI and database integration, with schema injection into model-backed runs, live JSON connection-context fetching, OpenAPI action discovery, and an in-dashboard connection test runner.
- Project detail pages with bridge management and scoped resources.
- Pipeline run tracking that groups saved-launch workflows by orchestration ID, with per-pipeline status summaries, token usage, artifacts, and task-level retry/download actions.

## Chat and Orchestration

- Persistent chat conversations with SSE streaming and per-message execution provenance (selected bot, provider/model route, bot update timestamp).
- Context picker and one-click chat-to-vault ingestion.
- Project-scoped and bridged chats can optionally attach semantic repo context per message.
- Optional workspace tooling with strict three-level access control: bot policy, project policy, and chat policy must all be enabled.
- Workspace tooling supports two independently controlled capabilities: repository search snippets and filesystem file snippet reads rooted to the project's configured workspace root.
- Message-level workspace tool usage can be toggled on/off so operators can keep a chat configured for tools but disable tool use for specific prompts.
- Inline `@assign` orchestration with PM task decomposition, acceptance criteria, deliverables, quality gates, task status streaming, and DAG viewer actions.
- Chat attachments: up to 15 files per message, text/source files delivered as bounded text context, images routed to vision-capable models, PDF/DOCX text extraction, and a Blob-backed viewer/download path (25 MB retained-bytes cap per file).
- Bot-gated DOCX generation and formatting-preserving DOCX editing from a retained source attachment (both disabled by default).
- Chat message-pair deletion with placeholders, project vault cleanup, and exclusion of PM/assignment messages.
- Chat response regeneration with variant tracking.
- Scoped web research through a self-hosted SearXNG service (bot-scoped, disabled by default).
- Voice input, unsent-draft persistence, a mobile conversation drawer, and conversation rename/archive/restore/delete lifecycle controls.
- Chat token governor with global and per-bot hourly caps, payload-aware admission estimates, and live status in Settings and Work.
- Chat usage telemetry grouped by conversation, bot, provider/model, and project, with attribution-health and spend-concentration warnings.
- Effective-context API exposing the effective bot, route, model, memory, workspace-tool, and inline-coding gates before sending.
- Readiness guards across create, route-default, message, and stream paths so unavailable bots cannot be dispatched from the browser or by bypassing the UI.

## Project Data Vault

Each project has a filesystem-backed data area intended for docs, exports, notes, and other source material before ingestion.

- Default location: `data/project_data/<project_id>/`
- Default folders created automatically: `docs`, `inbox`, `exports`, `notes`
- Open `Projects -> <project>`, use the `Project Data Vault` to create folders and upload files or a whole folder tree, then run `Run Data Ingest`.
- CLI fallback: `python scripts/ingest_project_data.py --project-id <project_id> --namespace project:<project_id>:data`
- Projects can store database connections directly on the project page (`Project Database Context`): save a DSN, run a test query, and ingest a schema snapshot so bots can retrieve table/column/key/foreign-key structure as project context.
- The project-data explorer supports edit-mode batch deletion with confirmation, item timestamps, and auto-renaming newer duplicate files instead of overwriting.
- For self-hosted deployments the bundled dashboard nginx gateway sets `client_max_body_size 0` so large project-data uploads are not capped at the app gateway layer.

## Vault and Context

- File/URL/text ingestion, namespace management, search, preview, and bulk actions.
- Filesystem-backed project data vault per project (see above).
- Project-scoped database connections with schema snapshot ingestion into the vault.
- Automatic run-report artifacts and project-level report visibility for long-running bot work.
- Browser sends vault item IDs; the control plane resolves content server-side for privacy.

## Bot Orchestration

Bots can be configured with trigger-based orchestration directly from the bot detail page.

- Trigger another bot when a task completes or fails; gate on `always`, `has_result`, or `has_error`.
- Match trigger routing on a structured result field (e.g. `qc_status=pass`).
- Enforce input contracts before queuing and output contracts with required/non-empty fields and configurable fallback policy.
- Preserve project/conversation metadata across triggered runs, queue one-off test runs, inspect run history and artifacts, and route a trigger back to the source bot with `{{source_bot_id}}`.
- Loop safety: trigger chains are capped by the `bot_trigger_max_depth` runtime setting (default `20`), changeable in Settings without restart.
- Saved launch profiles can be marked as pipeline entry points; launching assigns a shared orchestration ID to the root task and all downstream tasks, and the `Pipelines` page shows the grouped run with status, usage, reports, and rerun/download controls.
- Each bot can expose a dedicated external trigger intake endpoint with per-bot auth, optional `payload_field` mapping, and optional `allow_metadata` overrides.
- Runtime settings for external trigger intake: `external_trigger_default_auth_header`, `external_trigger_default_source`, `external_trigger_max_body_bytes`, `external_trigger_rate_limit_count`, `external_trigger_rate_limit_window_seconds`.
- API endpoints: `GET /v1/bots/{bot_id}/runs`, `GET /v1/bots/{bot_id}/artifacts`, `POST /v1/bots/{bot_id}/trigger`.
- Recommended contract settings: use `input_contract.required_fields` and `non_empty_fields` so every stage rejects malformed payloads before inference; use `fallback_mode=disabled` for generation/QC stages so silent backfill cannot create false positives; reserve fallback modes for intake/normalization bots; share one connection definition across bots instead of duplicating it.
- Bot export validation helper: `python scripts/validate_bot_exports.py <exports_dir>` checks trigger targets, detects dead-end bots, and warns on divergence between `bot.workflow.triggers` and `routing_rules.workflow.triggers` (`--strict-dead-ends`, `--strict-contracts`).

## How to Add a New Worker Machine

1. Create a YAML file outside the public checkout, e.g. `~/nexusai-private-configs/workers/gpu-box-2.yaml`:

```yaml
id: worker-gpu-box-2
name: GPU Box 2
host: worker.example.internal
port: 8001
capabilities:
  - type: llm
    provider: ollama
    models:
      - codellama-13b
    gpus:
      - GPU-0
      - GPU-1
```

2. On the new machine, run the worker agent:

```bash
WORKER_CONFIG_PATH=~/nexusai-private-configs/workers/gpu-box-2.yaml \
CONTROL_PLANE_URL=http://<control-plane-ip>:8000 \
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8001
```

The worker will self-register and begin sending heartbeats.

## How to Define a New Bot

1. Create a YAML file outside the public checkout, e.g. `~/nexusai-private-configs/bots/summarizer.yaml`:

```yaml
id: bot-summarizer
name: Summarizer
role: summarization
priority: 5
enabled: true
backends:
  - type: cloud_api
    provider: openai
    model: gpt-4o-mini
    api_key_ref: OPENAI_API_KEY
    params:
      temperature: 0.3
      max_tokens: 512
```

2. Restart the control plane (or `POST /v1/bots` to register at runtime).

Optional workflow trigger example:

```yaml
workflow:
  triggers:
    - id: summarize-output
      title: Summarize output
      event: task_completed
      condition: has_result
      target_bot_id: bot-summarizer
      inherit_metadata: true
      payload_template:
        instruction: Summarize the source bot result for the operator.
```

## Integration with agent-orchestrator

[`agent-orchestrator`](https://github.com/Jacobdrosol/agent-orchestrator) can be used as a worker backend:

1. Run `agent-orchestrator` on a machine.
2. Create a worker YAML that points to it:

```yaml
id: worker-orchestrator
name: Agent Orchestrator
host: orchestrator.example.internal
port: 8090
capabilities:
  - type: llm
    provider: custom
    models:
      - orchestrator-pipeline
```

3. Create a bot with `type: remote_llm` pointing to this worker.
4. NexusAI will POST inference requests to the worker's configured `/infer` endpoint.

## GitHub Integration

- Per-project PAT connect/disconnect.
- Webhook ingestion for `push`, `pull_request`, and `issues`, with signature verification, delivery-id deduplication, and timestamp skew checks.
- Repo context sync into the vault with `Full Ingest` and `Update Ingest` modes; long ingests run as background jobs.
- Optional PR review task workflow.
- Project-scoped `Repository Workspace` controls: managed policy, clone/status/pull/commit/push, and optional guarded command execution for test/build commands.
- Repository workspace run history with per-run resource metrics and aggregate summaries.
- Optional isolated temporary workspace runs with dependency bootstrap helpers for Python, Node, .NET, Go, Rust, and C/C++.

## Memory Profiles

- User-scoped personal memory with semantic retrieval injected as a bounded `Personal Memory Profile` system context block.
- Named memory profiles with per-profile management, password-protected clear, and provenance labels (manual, generated, imported, chat-derived).
- Memory is gated by chat, bot, and project switches; new bots and projects default to memory off, new chats default to memory on.
- Dashboard Memory page for listing, semantic search, manual add, edit, and delete. Routes always derive the user id from the signed-in account.

## Work Overview and Operations

- Work page groups active, waiting, and problem work by project and manager lane, with queue depth, worker load, token usage, and lane health labels.
- Attention rollup across problem tasks, stale work, metadata gaps, worker issues, and usage gaps, with recommended operator actions per lane.
- Lightweight `/api/work/brief` for monitors and mobile clients: active lane priority, usage pressure, queue-cap pressure, worker capacity, quality-gate status, and direct-chat risk summary.
- Token governor with global/bot/project/manager hourly caps, queue-admission ceilings, per-bot estimates, and chat-specific caps.
- Quality-gate visibility from test suites with per-suite recommended actions and failure detail.

## Bot and Worker Readiness

- Bot Tooling Readiness panel on the Bots page and `/api/bots/tooling-status`: grouped blocker causes, required worker tools, credential references, action scopes, worker bindings, probe states, and recommended next actions.
- Bot Detail operating summary with dispatch state, readiness, active/paused schedules, chat mode, chat tools, memory, backend routes, and worker-profile scope.
- Worker inventory and detail pages surface dependent-bot counts, backend routes, worker-profile scope, and runtime tool evidence.
- Project Detail assigned-bot scope table with required tools, action scopes, repo output, approval gates, credential references, and recommended actions.
- Readiness preflight guards on bot test runs and saved-launch profiles; quick-launch surfaces hide profiles that cannot run.

## Schedules and Issue Sources

- Agent Scheduler with cron-based dispatch, per-window deduplication, retry policy, run history, and dashboard management.
- Issue-to-task automation sources (GitHub issues, generic HTTP boards, CSV work items) with item lifecycle tracking and an approval gate before dispatch.

## Platform AI

- In-platform copilot with typed specialist proposals, readiness preflight, individual operator approval, and bounded session tuning.
- Reviewed bot proposals, approved CLI sessions, and fail-closed defaults; autonomous pipeline tuning and privileged runner actions remain opt-in and disabled by default.
- Quality test suites and runs with dashboard visibility and Work-page quality-gate summaries.

## Android Client

- Native Android client that connects to a user-owned NexusAI instance over HTTPS, authenticates via the dashboard session API, and stores the instance URL and session cookie in encrypted storage.
- Browse conversations, read messages, send normal text messages, native Markdown rendering, chat settings, and a compact work brief.
- Self-hosted release publishing: the deployment workflow builds and publishes the signed APK to the instance release endpoint with an update manifest served through the mobile bootstrap API.
- The client intentionally exposes no worker, repository, deployment, or automation controls.

## Security and Ops

- Optional control-plane token auth.
- Request-size and rate-limit guards for high-risk endpoints.
- Structured audit events at `GET /v1/audit/events`.
- Session inactivity timeout enforcement in dashboard auth.
- Prometheus-compatible metrics for the control plane and workers.
