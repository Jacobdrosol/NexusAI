# NexusAI User Guide

This guide explains daily usage of NexusAI from dashboard setup to project workflows.

## 1. Core Concepts

- Worker: runtime endpoint that executes inference requests.
- Bot: logical agent with ordered backend chain and role-specific behavior.
- Task: unit of execution with lifecycle (`queued`, `blocked`, `running`, `completed`, `failed`).
- Project: isolation boundary for settings, tasks, vault data, and integrations.
- Vault: searchable context store for files, text, and chat-derived data.
- Conversation: persisted chat thread that can trigger bot execution and task orchestration.

## 2. Workers

### 2.1 Registering

- Docker mode: `worker_agent` auto-registers on startup.
- Standalone mode: run the worker from `worker_node/` with valid control plane URL/token.

### 2.2 Monitoring

Worker detail pages display:

- online/offline state
- load indicators
- queue depth
- model capability list

If workers flap between online and offline, check heartbeat settings and connectivity.

## 3. Bots

### 3.1 Creating a Bot

In dashboard:

1. Open `Bots`.
2. Create bot with `id`, `name`, `role`.
3. Add backend chain in preferred order.

Backend behavior:

- backends are tried in order
- if first backend fails, scheduler attempts next backend

### 3.2 Backend Types

- `local_llm`: run on worker with local provider (for example, Ollama).
- `remote_llm`: worker-hosted remote endpoint wrapper.
- `cloud_api`: direct provider call from scheduler.
- `cli`: shell command backend on worker.

### 3.3 Keys and Security

For cloud backends:

- store provider keys in API Key Vault
- reference them by `api_key_ref`
- do not hardcode secrets in bot YAML files

### 3.4 Bot Connections (HTTP/OpenAPI and Database)

Each bot now has a `Connections` workspace for external systems.

Supported connection kinds:

- `HTTP / API`: base URL + authentication + optional OpenAPI schema
- `Database`: DSN connection string + readonly query mode

What you can do:

1. Open `Bots -> <bot> -> Connections`
2. Create and attach one or more connections to that bot
3. Paste OpenAPI schema (YAML/JSON) to discover actions
4. Run test calls/queries directly from the dashboard

Connection behavior:

- multiple connections per bot are supported
- credentials are encrypted at rest
- secret values are masked in UI/API responses
- API action extraction is derived from OpenAPI `paths` and `operationId`

Security guidance:

- prefer least-privilege credentials
- keep database connections in readonly mode unless writes are explicitly required
- scope API keys/tokens to the exact endpoints needed by each bot
- rotate credentials periodically and after any suspected exposure

### 3.5 Bot Workflows, Triggers, and Run History

Open `Bots -> <bot>` to configure orchestration for that bot.

Available actions:

1. Add or reorder backend chains as before.
2. Add workflow triggers that launch another bot when a run completes or fails.
3. Define a `Run Input Contract` when operators should fill out structured fields or send a default JSON payload to the bot.
4. Queue a manual `Run Test` task to validate prompts, backends, structured inputs, and downstream triggers.
5. Inspect `Run History` to see status, source, trigger rule, and task linkage.
6. Inspect `Artifacts` to review stored payloads, results, errors, and explicit file-style outputs reported by the run.

Trigger guidance:

- use `task_completed` when a downstream bot should process successful output
- use `task_failed` for fallback, escalation, or recovery bots
- use `has_result` or `has_error` conditions to avoid noisy follow-on runs
- use `result_field` and `result_equals` when a QC or validator bot returns a structured decision such as `qc_status`
- keep trigger chains linear at first; add branching only after you trust each handoff

Safety behavior:

- triggered runs inherit project and conversation metadata by default
- trigger chains are capped to prevent accidental infinite loops
- every run is recorded even when the scheduler fails

QC bot pattern:

1. Configure the worker bot to trigger the QC bot on `task_completed`.
2. Make the QC bot return a structured result such as:
   - `{"qc_status":"pass"}`
   - `{"qc_status":"fail","issues":["missing tests","bad citation"]}`
3. Add a pass trigger on the QC bot:
   - `event=task_completed`
   - `result_field=qc_status`
   - `result_equals=pass`
   - `target_bot_id=<next bot>`
4. Add a fail trigger on the QC bot:
   - `event=task_completed`
   - `result_field=qc_status`
   - `result_equals=fail`
   - `target_bot_id={{source_bot_id}}`

That gives you a practical loop of `worker -> qc -> publish` or `worker -> qc -> worker`.

## 4. Projects

Project modes:

- `isolated`: no cross-project sharing.
- `bridged`: explicit sharing with other bridged projects.

Use projects to separate environments (for example, `prod-assistant`, `dev-assistant`, `research`).

### 4.1 Project Data Vault

Each project includes a filesystem-backed data area for source material that should later become searchable context.

Use it for:

- product docs
- exported notes
- research files
- API references
- architecture decisions
- repository-adjacent documents that should not live in the app database first

In dashboard:

1. Open `Projects -> <project>`.
2. Use `Project Data Vault` to create folders and upload files or an entire folder tree.
3. Keep material organized under the default folders:
   - `docs`
   - `inbox`
   - `exports`
   - `notes`

The filesystem root defaults to:

- `data/project_data/<project_id>/`

You can move that root with:

- `NEXUSAI_PROJECT_DATA_ROOT`

Project Data Vault ingest:

- use `Run Data Ingest` from the project page to push vault files into the searchable backend
- the UI now shows a live status object with discovered, ingested, skipped, and failed counts
- rerunning ingest is safe for the same file paths because project-data items are upserted by `project-data://...` source reference
- file-size safeguards are now backend-managed; users do not need to tune a max-bytes field in the UI
- the explorer shows file timestamps so you can see how old a file or folder is
- use `Edit` in the explorer to select multiple files or folders, then confirm before deletion
- if you upload a file with the same name into the same path, the new file is preserved with an auto-generated name such as `(1) filename.ext`

### 4.2 Project Database Context

Projects can also store database connections directly on the project page.

Use this for:

- project-specific Postgres/MySQL/SQLite or compatible databases
- schema snapshots that should become searchable context
- table, column, primary-key, and foreign-key structure that bots should understand

In dashboard:

1. Open `Projects -> <project>`.
2. In `Project Database Context`, save a connection name and DSN / connection string.
3. Keep the connection in readonly mode unless you explicitly need writes.
4. Run `Test` to confirm the query path works.
5. Run `Ingest Schema` to inspect the database structure and push a schema snapshot into the project vault namespace.

What schema ingest captures:

- dialect
- schemas
- tables
- columns and types
- primary keys
- foreign keys
- views

That snapshot is stored as a vault document so project bots can retrieve it during task execution or chat.

### 4.3 Run Reports

- each bot run now records a `Run Report` artifact
- project pages show the latest reports from project bots
- reports summarize status, lineage, and the bot's result or error in a human-readable format

## 5. Vault

### 5.1 Ingestion

Vault supports:

- pasted text
- file upload
- URL import
- chat and task outputs
- project data vault ingestion via local runner

### 5.3 Project Data Ingest Runner

After placing files into a project's data vault, run:

```bash
python scripts/ingest_project_data.py --project-id <project_id> --namespace project:<project_id>:data
```

What it does:

- scans the project's filesystem data area
- skips common binary/build folders
- uploads text-like files into the control-plane vault
- stores source references as `project-data://<project_id>/<relative_path>`
- triggers normal chunking and embedding on ingest

Recommended pattern:

1. Keep raw documents in the project data vault.
2. Run the ingest script after adding or updating files.
3. Use the project namespace in retrieval and chat context selection.

### 5.2 Search and Retrieval

- use vault search for semantic retrieval
- attach selected context to chat requests
- current implementation sends vault item IDs from UI and resolves content server-side for privacy

## 6. Chat and Task Orchestration

### 6.1 Conversations

- Create conversations scoped to global or project context.
- Messages are persisted and available for replay and retrieval.

### 6.2 Streaming

- Chat streaming uses SSE for incremental response updates.

### 6.3 Assignment Workflow

- Use `@assign` to route a request through PM orchestration.
- PM bot decomposes work into dependency-ordered tasks.
- Task graph updates stream back into chat.

## 7. GitHub Integration

Per project you can:

- connect PAT
- set webhook secret
- run `Full Ingest` to pull the entire repo corpus into the project vault namespace
- run `Update Ingest` to refresh only changed/newer files, commits, PRs, issues, and discussion threads
- enable PR review workflow with bot assignment

GitHub ingest behavior:

- `Full Ingest` walks the full repository dataset, not just a user-entered sample cap
- `Update Ingest` uses the last successful sync state stored on the project to decide what needs to be refreshed
- file refresh is SHA-aware, so unchanged repo files are skipped on update
- commits, pull requests, and issues are upserted by GitHub source reference instead of duplicated
- long ingests run as background jobs; use the project page status panel to watch `queued`, `running`, `completed`, or `failed`

Webhook security controls include:

- HMAC signature verification
- delivery ID deduplication
- optional date-skew checks

## 8. Model Routing and Scheduling

Scheduler behavior:

- validates model availability when model catalog exists
- enforces cloud context policy (`allow`, `redact`, `block`)
- for unpinned local/remote backends, auto-selects capable workers using weighted scoring:
  - queue depth
  - in-flight load
  - CPU load
  - GPU utilization
  - latency EMA

Cloud context policy hierarchy (configured per project in Project Detail):

- provider baseline `allow`: bot override may be `allow`, `redact`, or `block`
- provider baseline `redact`: bot override may be `redact` or `block` (not `allow`)
- provider baseline `block`: all bot overrides are effectively `block`

This allows strict provider-level controls while preserving per-bot privacy choices.

## 9. API Usage Patterns

Common sequence:

1. register worker
2. create bot
3. create project
4. create conversation
5. send chat message
6. poll tasks and audit events

Primary endpoints:

- `/v1/workers`
- `/v1/bots`
- `/v1/tasks`
- `/v1/projects`
- `/v1/chat`
- `/v1/vault`
- `/v1/audit/events`

## 10. Recommended Team Workflow

1. One project per repository or major initiative.
2. One PM bot plus specialist bots per project.
3. Use the Project Data Vault for docs, exports, notes, and source material before ingestion.
4. Start bot orchestration with simple trigger chains and verify run history before adding branching logic.
5. Keep cloud context policy at `block` during initial rollout.
6. Enable `redact` only after validating output quality and data controls.
7. Use audit logs, run history, and metrics as release gates.
