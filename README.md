# NexusAI

**NexusAI** is a modular, distributed LLM Control Plane that orchestrates multiple machines, GPUs, cloud APIs, and CLI-based models via specialized **bots** (logical agents) and **workers** (compute backends).

---

## Quick Start with Docker

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your values
3. Run: `docker compose up --build`
4. Open http://localhost:5000 to access the dashboard
5. The control plane API is at http://localhost:8000
6. The worker agent is at http://localhost:8001
7. Prometheus is at http://localhost:9090

---

## Documentation

### User & Operator Docs
- Getting started: `docs/GETTING_STARTED.md`
- Chat/PM/workspace setup: `docs/CHAT_PM_WORKSPACE_SETUP.md`
- Product usage: `docs/USER_GUIDE.md`
- Operations and security: `docs/OPERATIONS.md`
- Blue/green deployment: `docs/DEPLOY_BLUEGREEN.md`
- Worker node bootstrap: `worker_node/docs/WORKER_NODE_BOOTSTRAP.md`
- UAT checklist: `docs/UAT_RUNBOOK.md`
- Configuration reference: `config/README.md`

### Developer / Refactor Reference
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Full system architecture, ASCII diagram, database schema, blue/green model, known debt
- **[docs/PM_WORKFLOW.md](docs/PM_WORKFLOW.md)** — Complete PM orchestration DAG, stage roles, fan-out/join, scope lock, docs-only mode, known bugs
- **[docs/REFACTOR_PRIORITIES.md](docs/REFACTOR_PRIORITIES.md)** — Prioritized refactor list with file references and rationale

### Per-Module READMEs
| Module | README |
|--------|--------|
| `control_plane/` | [control_plane/README.md](control_plane/README.md) |
| `control_plane/api/` | [control_plane/api/README.md](control_plane/api/README.md) — full endpoint table |
| `control_plane/task_manager/` | [control_plane/task_manager/README.md](control_plane/task_manager/README.md) |
| `control_plane/scheduler/` | [control_plane/scheduler/README.md](control_plane/scheduler/README.md) |
| `control_plane/chat/` | [control_plane/chat/README.md](control_plane/chat/README.md) |
| `control_plane/registry/` | [control_plane/registry/README.md](control_plane/registry/README.md) |
| `control_plane/vault/` | [control_plane/vault/README.md](control_plane/vault/README.md) |
| `control_plane/database/` | [control_plane/database/README.md](control_plane/database/README.md) |
| `control_plane/audit/` | [control_plane/audit/README.md](control_plane/audit/README.md) |
| `control_plane/security/` | [control_plane/security/README.md](control_plane/security/README.md) |
| `control_plane/keys/` | [control_plane/keys/README.md](control_plane/keys/README.md) |
| `control_plane/github/` | [control_plane/github/README.md](control_plane/github/README.md) |
| `shared/` | [shared/README.md](shared/README.md) |
| `dashboard/` | [dashboard/README.md](dashboard/README.md) |
| `dashboard/routes/` | [dashboard/routes/README.md](dashboard/routes/README.md) |
| `worker_agent/` | [worker_agent/README.md](worker_agent/README.md) |
| `worker_agent/backends/` | [worker_agent/backends/README.md](worker_agent/backends/README.md) |
| `tests/` | [tests/README.md](tests/README.md) |

---

## Architecture

| Service | Framework | Port |
|---|---|---|
| `control_plane` | FastAPI | `8000` |
| `worker_agent` | FastAPI (uvicorn) | `8001` |
| `dashboard` | Flask / Gunicorn | `5000` |

```
┌─────────────────────────────────────────────────────────────┐
│                      NexusAI Control Plane                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Bot Registry│  │Worker Registry│  │  Task Manager    │  │
│  └──────┬───────┘  └──────┬────────┘  └────────┬─────────┘  │
│         └─────────────────┴──────────────┬──────┘            │
│                                    ┌─────▼──────┐            │
│                                    │  Scheduler  │            │
│                                    └─────┬──────┘            │
│  REST API /v1/tasks /v1/bots /v1/workers │                   │
└──────────────────────────────────────────┼──────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────┐
              │                            │                    │
     ┌────────▼────────┐       ┌───────────▼──────┐   ┌────────▼────────┐
     │  Worker Agent   │       │  Worker Agent     │   │  Cloud APIs     │
     │  (Ollama/vLLM)  │       │  (LM Studio)      │   │ (OpenAI/Claude/ │
     │  GPU Machine A  │       │  GPU Machine B    │   │  Gemini)        │
     │  port 8001      │       │  port 8001        │   │                 │
     └─────────────────┘       └──────────────────┘   └─────────────────┘

     ┌─────────────────┐
     │   Dashboard     │
     │  Flask/Gunicorn │
     │  port 5000      │
     └─────────────────┘
```

---

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config/nexus_config.yaml` to set your control plane host/port.

Add worker definitions in `config/workers/` and bot definitions in `config/bots/`.

### 3. Run the Control Plane

```bash
NEXUS_CONFIG_PATH=config/nexus_config.yaml python -m control_plane.main
# or
uvicorn control_plane.main:app --host 0.0.0.0 --port 8000
```

### 4. Run a Worker Agent

```bash
WORKER_CONFIG_PATH=config/workers/example_worker.yaml \
CONTROL_PLANE_URL=http://localhost:8000 \
python -m worker_agent.main
# or
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8001
```

### 4b. Run the Standalone Worker Node Project

```bash
cd worker_node
NEXUS_WORKER_CONFIG_PATH=nexus_worker/config.yaml.example \
CONTROL_PLANE_URL=http://localhost:8000 \
python -m nexus_worker
# or, after install:
nexus-worker
```

### 5. Run the Dashboard

```bash
CONTROL_PLANE_URL=http://localhost:8000 \
gunicorn --bind 0.0.0.0:5000 --workers 2 "dashboard.app:create_app()"
```

Then open http://localhost:5000 in your browser.

## Implemented Capabilities

Dashboard and workflow:

- Navigation pages: `Overview`, `Projects`, `Chat`, `Bots`, `Pipelines`, `Workers`, `Vault`, `Settings`.
- Improved dashboard link contrast for worker and bot names on dark tables/cards.
- Worker detail pages with live load, queue, and GPU graphs.
- Bot detail editor with backend chain management, workflow triggers, saved input contracts, saved launch profiles, test runs, run history, and task board.
- Bot export from the bot detail page and bot import from the bots index page, including bot configuration and bot-scoped connections, with overwrite confirmation on ID conflicts.
- Bot-scoped external Connections workspace for HTTP/OpenAPI and database integration setup.
- Attached connection schemas are injected into model-backed bot runs as authoring context, so bots can follow shared API and JSON structure definitions without exposing auth secrets in prompts.
- Connection-context rules can also fetch live JSON from attached HTTP connections before inference, which lets a bot pull platform-owned schemas or examples from a payload-driven list such as requested block types.
- OpenAPI action discovery and in-dashboard connection test runner for bot connections.
- Project detail pages with bridge management and scoped resources.
- Pipeline run tracking page that groups all tasks in a saved-launch workflow, with per-pipeline status summaries, token usage, artifacts, and task-level retry/download actions.

Chat and orchestration:

- Persistent chat conversations with SSE streaming.
- Context picker and one-click chat-to-vault ingestion.
- Project-scoped and bridged chats can optionally attach semantic repo context from synced vault namespaces (`project:<id>:repo`) per message with `include_project_context=true`.
- Optional workspace tooling in chat supports Codex-style repo help with strict three-level access control: bot policy, project policy, and chat policy must all be enabled or workspace tool access is denied.
- Workspace tooling supports two independently controlled capabilities: repository search snippets and filesystem file snippet reads rooted to the project's configured workspace root.
- Message-level workspace tool usage can be toggled on/off (`use_workspace_tools`) so operators can keep a chat configured for tools but disable tool use for specific prompts.
- Inline `@assign` orchestration with PM task decomposition.
- PM orchestration task payloads now include step-level acceptance criteria, deliverables, and quality gates for stronger implementation/test/review handoffs.
- PM orchestration is operator-safe by default: it stops at specification, implementation, test execution, verification, and final reporting. It does not create CI/CD workflows, open GitHub issues/boards, merge PRs, tag releases, deploy, or finalize changelogs unless that behavior is explicitly added later.
- Task status streaming and DAG viewer actions in chat.

Vault and context:

- File/URL/text ingestion, namespace management, search, preview, and bulk actions.
- Filesystem-backed project data vault per project with individual-file upload, recursive folder upload, and in-UI ingest tracking.
- Project-scoped database connections with schema snapshot ingestion into the vault.
- Automatic run-report artifacts and project-level report visibility for long-running bot work.
- Browser sends vault item IDs; control plane resolves content server-side for privacy.

GitHub integration:

- Per-project PAT connect/disconnect.
- Webhook ingestion for `push`, `pull_request`, and `issues`.
- Signature verification, delivery-id deduplication, and timestamp skew checks.
- Repo context sync into vault with two operator modes:
  - `Full Ingest`: all repo files, commits, PRs, issues, and discussion threads
  - `Update Ingest`: only changed/newer repository data since the last successful sync
- Optional PR review task workflow.
- Long GitHub ingests now run as background jobs and report status back to the project page instead of holding the HTTP request open.
- Project-scoped `Repository Workspace` controls on Project Detail for:
  - configuring managed repository workspace policy and clone defaults
  - repository clone/status/pull/commit/push operations from the dashboard
  - optional guarded command execution for test/build commands (`run`) with an allowlist policy
- Repository workspace run history with per-run resource metrics (duration, CPU, peak memory, IO) and aggregate summaries for internal tracking.
- Optional isolated temporary workspace runs for checks (`use_temp_workspace`) with dependency bootstrap helpers for Python, Node, .NET, Go, Rust, and C/C++ projects.

Repository workspace runtime notes:

- Repo workspace commands and PM-generated test runs execute in the configured workspace runtime for the project, typically your hosted VM/container, not in the operator's local browser session.
- In the default Docker deployment, that runtime is the `control_plane` container.
- The default `docker compose` build now preloads common repo-workspace toolchains into the `control_plane` image through `.env` build args:
  - `NEXUSAI_REPO_RUNTIME_TOOLCHAINS=node,dotnet,go,rust,cpp`
  - `NEXUSAI_REPO_RUNTIME_DOTNET_CHANNEL=8.0`
- After changing those values, rebuild the stack with `docker compose up --build`.
- Python test environments created by PM assignment runs are now stored outside the repository workspace, so NexusAI does not leave `.venv`-style directories as untracked repo files.
- Assignment execution now inherits runtime choices from the repo's declared stack markers. It will not introduce a new runtime for a repo just because a bot emitted files in a different language.
- The runtime must have the toolchains you expect to use installed there. Current built-in assignment execution supports:
  - Python: `venv`, `pip`, `pytest`
  - Node/JavaScript/TypeScript: `npm`, `pnpm`, or `yarn`
  - .NET: `dotnet`
  - Go: `go`
  - Rust: `cargo`
  - C/C++: `cmake`/`ctest` or `make test`
- Built-in coverage artifact generation is strongest for Python, Node, .NET, and Go. Rust and C/C++ test execution are supported, but coverage file production still depends on repo-native tooling.
- If a required tool is missing in that runtime, assignment test execution now fails with a direct blocker such as `repo workspace runtime is missing required tools: dotnet`.

Security and ops:

- Optional control-plane token auth.
- Request-size and rate-limit guards for high-risk endpoints.
- Structured audit events at `GET /v1/audit/events`.
- Session inactivity timeout enforcement in dashboard auth.
- Prometheus-compatible metrics for control plane and workers.

For detailed walkthroughs, use:

- `docs/GETTING_STARTED.md`
- `docs/USER_GUIDE.md`
- `docs/OPERATIONS.md`
- `docs/PM_BOT_PACK_OLLAMA_CLOUD.md` (manual PM bot import workflow reference)

---

## Environment Variables

Copy `.env.example` to `.env` and set the following variables before starting the stack:

| Variable | Default | Description |
|---|---|---|
| `NEXUSAI_SECRET_KEY` | `dev-secret-change-in-production` | Flask session secret key — **must be changed in production** |
| `DATABASE_URL` | `sqlite:///data/nexusai.db` | SQLAlchemy connection URL (SQLite or PostgreSQL) |
| `CONTROL_PLANE_URL` | — | URL the dashboard and worker use to reach the control plane, e.g. `http://localhost:8000` |
| `CONTROL_PLANE_API_TOKEN` | — | Optional shared token for control-plane API auth; when set, dashboard/worker send `X-Nexus-API-Key` and CP enforces auth on API routes |
| `CP_INGEST_TIMEOUT` | `1800` | Dashboard timeout in seconds for long-running project ingestion calls such as GitHub full-context sync |
| `NEXUSAI_CLOUD_CONTEXT_POLICY` | `allow` | Cloud egress policy for context blocks (`allow`, `redact`, `block`) on control-plane scheduler cloud backends |
| `NEXUS_WORKER_CLOUD_CONTEXT_POLICY` | `redact` | Standalone worker cloud egress policy for context blocks (`allow`, `redact`, `block`) |
| `NEXUSAI_WORKER_LATENCY_EMA_ALPHA` | `0.30` | Scheduler EMA smoothing factor for worker latency scoring (0.01-1.0) |
| `NEXUSAI_WORKER_DEFAULT_LATENCY_MS` | `800` | Default worker latency estimate used before dispatch history exists |
| `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DELIVERY_ID` | `1` | Require `X-GitHub-Delivery` header for webhook replay protection |
| `NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS` | `300` | Allowed request timestamp skew when `Date` header is present |
| `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DATE_HEADER` | `0` | Require `Date` header on GitHub webhooks (`1`/`true` to enforce) |
| `NEXUSAI_GITHUB_WEBHOOK_DEDUP_TTL_SECONDS` | `86400` | Retention window for delivery-ID deduplication records |
| `NEXUSAI_PROJECT_DATA_ROOT` | `data/project_data` | Filesystem root for per-project data vault folders shown in Project Detail |
| `NEXUS_WORKER_CONFIG_PATH` | `worker_node/nexus_worker/config.yaml.example` | Path to standalone worker-node YAML config when running from the repo root |
| `VLLM_MODELS` | — | Optional comma-separated vLLM model names for local model discovery in `nexus_worker` |
| `CP_MAX_BODY_BYTES_<ROUTE>` | route default | Optional request body size override per guarded route (e.g. `CHAT_MESSAGES`, `CHAT_STREAM`, `VAULT_INGEST`, `GITHUB_WEBHOOK`) |
| `CP_RATE_LIMIT_<ROUTE>_COUNT` / `CP_RATE_LIMIT_<ROUTE>_WINDOW_SECONDS` | route defaults | Optional per-route rate limit override for guarded control-plane endpoints |
| `NEXUS_CONFIG_PATH` | — | Path to `nexus_config.yaml` for the control plane |
| `WORKER_CONFIG_PATH` | — | Path to a worker YAML file for the worker agent |
| `DASHBOARD_PORT` | `5000` | Port the dashboard listens on (used when running directly) |
| `OPENAI_API_KEY` | — | OpenAI API key for cloud LLM backends |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |

Notes:

- Preferred cloud-key path is Dashboard `Settings -> API Keys` (encrypted at rest, named keys, multi-provider, multiple keys/provider).
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` are fallback-only environment variables.

---

## Project Data Vault

Each project now has a filesystem-backed data area intended for docs, exports, notes, and other source material before ingestion.

Default location:

- `data/project_data/<project_id>/`

Default folders created automatically:

- `docs`
- `inbox`
- `exports`
- `notes`

Workflow:

1. Open `Projects -> <project>`.
2. Use `Project Data Vault` to create folders and upload files or a whole folder tree.
3. Run `Run Data Ingest` on the project page or use the CLI fallback:

```bash
python scripts/ingest_project_data.py --project-id <project_id> --namespace project:<project_id>:data
```

The ingest runner reads the project data files from disk and posts them into the control-plane vault so they are chunked and embedded for retrieval.

Projects can also store database connections directly on the project page. Use `Project Database Context` to save a DSN / connection string, run a test query, and ingest a schema snapshot so bots can retrieve table, column, key, and foreign-key structure as project context.

For self-hosted deployments, the bundled dashboard nginx gateway now sets `client_max_body_size 0` so large project-data uploads are not capped by a hidden default at the app gateway layer. If you place NexusAI behind another proxy or CDN, that outer layer may still impose its own upload limit.

The project-data explorer also supports edit-mode batch deletion with confirmation, shows item timestamps, and preserves older files by auto-renaming newer duplicates instead of overwriting them.

---

## Bot Orchestration

Bots can now be configured with simple trigger-based orchestration directly from the bot detail page.

Current capabilities:

- Trigger another bot when a task completes or fails.
- Gate triggers on `always`, `has_result`, or `has_error`.
- Match trigger routing on a structured result field such as `qc_status=pass`.
- Enforce input contracts before a task is queued so malformed upstream payloads fail fast.
- Enforce output contracts with required top-level fields, required non-empty nested fields, and configurable fallback policy.
- Preserve project/conversation metadata across triggered runs.
- Queue one-off bot test runs from the dashboard.
- Inspect run history and generated artifacts per bot.
- Route a trigger back to the source bot using `{{source_bot_id}}`.

Loop safety:

- Trigger chains are capped by the `bot_trigger_max_depth` runtime setting (default `20`) to prevent accidental infinite handoff loops.
- Operators can change this in `Settings` without restarting the stack.

Saved launch profiles:

- A saved launch profile can be marked as a pipeline entry point from the bot detail page.
- Launching a saved pipeline run assigns a shared orchestration ID to the root task and all downstream triggered tasks.
- The `Pipelines` page then shows the grouped run with status, usage, reports, and rerun/download controls.

External bot triggers:

- Each bot can expose a dedicated external trigger intake endpoint from the bot detail page.
- External trigger calls create normal queued tasks, so existing workflow triggers/fan-out/join behavior runs unchanged.
- Per-bot external trigger auth is configurable (`require_auth`, header name, token), so integrations are not tied to a single global workflow.
- Optional `payload_field` lets you map nested webhook envelopes (for example `event.data`) to the queued task payload.
- Optional `allow_metadata` permits limited caller metadata overrides (`project_id`, `priority`, `conversation_id`, `orchestration_id`).

API endpoints:

- `GET /v1/bots/{bot_id}/runs`
- `GET /v1/bots/{bot_id}/artifacts`
- `POST /v1/bots/{bot_id}/trigger`

Runtime settings for external trigger intake:

- `external_trigger_default_auth_header`
- `external_trigger_default_source`
- `external_trigger_max_body_bytes`
- `external_trigger_rate_limit_count`
- `external_trigger_rate_limit_window_seconds`

QC pattern:

- Worker bot trigger: `task_completed -> qc-bot`
- QC bot result contract: return a structured result such as `{"qc_status":"pass"}` or `{"qc_status":"fail","issues":[...]}`
- QC pass trigger: `result_field=qc_status`, `result_equals=pass`, `target_bot_id=bot-publisher`
- QC fail trigger: `result_field=qc_status`, `result_equals=fail`, `target_bot_id={{source_bot_id}}`

Recommended contract settings for long multi-stage content pipelines:

- Use `input_contract.required_fields` and `input_contract.non_empty_fields` so every stage rejects malformed or partial upstream payloads before inference starts.
- Use `required_fields` for the structural envelope a stage must return.
- Use `non_empty_fields` for the fields that prove the stage actually did the work, such as `course_structure.units`, `unit_blueprint.lesson_plans`, or `lesson_output.blocks`.
- Set `fallback_mode=disabled` for generation and QC stages where silent backfill would create false positives.
- Reserve `fallback_mode=missing_only` or `fallback_mode=parse_failure` for intake/normalization bots where deterministic defaults are intentional.
- For join aggregators, consume the collected branch payload array plus the helper fields `join_results` and `join_task_ids` instead of reconstructing the merge from deeply nested wrappers.
- When multiple bots need the same platform schema or OpenAPI definition, attach the same connection to each bot instead of duplicating the connection definition.
- For large block libraries, store only block names in the task payload and configure `connection_context` to fetch per-block schemas/examples from the attached platform connection at runtime.

Bot export validation helper:

- Use `py scripts/validate_bot_exports.py <exports_dir>` before importing changed bot exports.
- The validator checks trigger targets, detects dead-end bots, and warns when `bot.workflow.triggers` and `routing_rules.workflow.triggers` diverge.
- Use `--strict-dead-ends` when you want non-terminal dead-end stages to fail validation.
- Use `--strict-contracts` to enforce deterministic output contracts (description, example output, required/non-empty fields, and fail-closed model fallback policy).

---

## Bootstrap Secrets

Two values should always be set in `.env` for secure deployments:

- `NEXUSAI_SECRET_KEY`: signs dashboard sessions and CSRF state.
- `CONTROL_PLANE_API_TOKEN`: protects control-plane API routes (`/v1/*`).

Generate values:

Linux/macOS:

```bash
python - <<'PY'
import secrets
print("NEXUSAI_SECRET_KEY=" + secrets.token_urlsafe(64))
print("CONTROL_PLANE_API_TOKEN=" + secrets.token_urlsafe(64))
PY
```

Windows PowerShell:

```powershell
python -c "import secrets; print('NEXUSAI_SECRET_KEY=' + secrets.token_urlsafe(64)); print('CONTROL_PLANE_API_TOKEN=' + secrets.token_urlsafe(64))"
```

---

## Production Hardening

- Put the dashboard and control-plane behind a reverse proxy (Nginx/Caddy/Traefik) with TLS enabled.
- Restrict direct access to internal service ports (`8000`, `8001`) at the network layer; expose only the proxy entrypoint.
- Set `CONTROL_PLANE_API_TOKEN` and require token-authenticated control-plane calls from dashboard/workers.
- Use a strong `NEXUSAI_SECRET_KEY` and rotate it for production environments.
- Store PAT/API secrets only through encrypted vault endpoints (never in plain YAML committed to repo).
- Configure GitHub webhook secrets per project and verify signatures (already enforced by the control-plane endpoint).
- Enforce webhook replay controls by keeping `X-GitHub-Delivery` checks enabled and using a short skew window for signed payloads.
- For Gemini backends, keep API keys in request headers (`x-goog-api-key`), not URL query params.
- Keep the control-plane API on private subnets/VPN where possible; do not expose unauthenticated management paths publicly.
- Run with least-privilege service accounts and file permissions for the `data/` directory.

---

## Pre-UAT Guide

- Full step-by-step runbook: `docs/UAT_RUNBOOK.md`
- First in-app checkpoint: after login, open `Overview` and clear the `Open-Source Setup Checklist` required items before broader UAT.
- Automated preflight script:
  - `scripts/pre_uat_security_checks.ps1`

---

## Observability

Prometheus is included in `docker-compose.yml` and scrapes:

- `control_plane:8000/metrics`
- `worker_agent:8001/metrics`

Local URLs:

- Control plane metrics: `http://localhost:8000/metrics`
- Worker agent metrics: `http://localhost:8001/metrics`
- Prometheus UI: `http://localhost:9090`

Quick checks in Prometheus:

- `nexus_control_plane_http_requests_total`
- `nexus_control_plane_http_request_duration_seconds_count`
- `nexus_worker_agent_http_requests_total`
- `nexus_worker_agent_inference_inflight`

---

## First-Run Onboarding

On the first visit to the dashboard (before any admin account exists), NexusAI presents a **5-step onboarding wizard** at `/onboarding`:

| Step | Path | Description |
|---|---|---|
| 1 | `/onboarding/step1` | **Welcome** — introduction screen |
| 2 | `/onboarding/step2` | **Admin Account** — create the first admin user (email + password) |
| 3 | `/onboarding/step3` | **LLM Backend** — choose the default LLM provider (`ollama`, `openai`, `claude`, `gemini`) |
| 4 | `/onboarding/step4` | **Worker Node** — optionally register the first compute worker |
| 5 | `/onboarding/step5` | **Complete** — wizard summary and redirect to login |

Once an admin account exists every `/onboarding` request redirects to `/login`.

---

## Configuration Guide

### `config/nexus_config.yaml`

```yaml
control_plane:
  host: 0.0.0.0
  port: 8000
  workers_config_dir: config/workers    # directory of worker YAML files
  bots_config_dir: config/bots          # directory of bot YAML files
  heartbeat_timeout_seconds: 30         # workers go offline after this

dashboard:
  host: 0.0.0.0
  port: 5000
  enabled: true

logging:
  level: INFO
  file_path: data/nexus.log
```

### Worker YAML (`config/workers/<name>.yaml`)

```yaml
id: worker-main-4070
name: Main 4070 Box
host: 192.168.1.10
port: 8001
capabilities:
  - type: llm
    provider: ollama
    models:
      - llama3-8b-instruct-q4
    gpus:
      - GPU-0
```

### Bot YAML (`config/bots/<name>.yaml`)

```yaml
id: bot-coder-14b
name: Coder 14B
role: coding
priority: 10
enabled: true
backends:
  - type: local_llm
    worker_id: worker-main-4070
    model: llama3-8b-instruct-q4
    provider: ollama
    gpu_id: GPU-0
    params:
      temperature: 0.1
      max_tokens: 1024
  - type: cloud_api
    provider: claude
    model: claude-3-5-sonnet
    api_key_ref: ANTHROPIC_API_KEY    # env var name
    params:
      temperature: 0.1
      max_tokens: 2048
```

Backends are tried in order. If the first fails, the next is attempted.

---

## API Reference

### Control Plane (`http://localhost:8000`)

#### Tasks

```bash
# Create a task
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"bot_id": "bot-coder-14b", "payload": [{"role": "user", "content": "Hello!"}]}'

# Create a dependent task (starts as blocked until dependency completes)
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"bot_id":"bot-coder-14b","payload":{"step":"write tests"},"depends_on":["<task_id_from_previous_call>"]}'

# Get task status
curl http://localhost:8000/v1/tasks/{task_id}

# List all tasks
curl http://localhost:8000/v1/tasks
```

#### Bots

```bash
# List bots
curl http://localhost:8000/v1/bots

# Get bot
curl http://localhost:8000/v1/bots/{bot_id}

# Create bot
curl -X POST http://localhost:8000/v1/bots \
  -H "Content-Type: application/json" \
  -d '{...bot JSON...}'

# Enable / Disable bot
curl -X POST http://localhost:8000/v1/bots/{bot_id}/enable
curl -X POST http://localhost:8000/v1/bots/{bot_id}/disable

# Delete bot
curl -X DELETE http://localhost:8000/v1/bots/{bot_id}

# List bot run history
curl http://localhost:8000/v1/bots/{bot_id}/runs

# List bot artifacts
curl http://localhost:8000/v1/bots/{bot_id}/artifacts
```

#### Workers

```bash
# List workers
curl http://localhost:8000/v1/workers

# Register worker
curl -X POST http://localhost:8000/v1/workers \
  -H "Content-Type: application/json" \
  -d '{...worker JSON...}'

# Worker heartbeat
curl -X POST http://localhost:8000/v1/workers/{worker_id}/heartbeat

# Remove worker
curl -X DELETE http://localhost:8000/v1/workers/{worker_id}
```

#### Projects

```bash
# Create project
curl -X POST http://localhost:8000/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"id":"proj-1","name":"Project 1","mode":"isolated"}'

# List projects
curl http://localhost:8000/v1/projects

# Bridge two bridged-mode projects
curl -X POST http://localhost:8000/v1/projects/proj-a/bridges/proj-b

# Ingest a GitHub webhook event (signature + delivery ID required)
curl -X POST http://localhost:8000/v1/projects/proj-a/github/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=<hmac_hex>" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: <uuid>" \
  -d '{...event payload...}'
```

#### API Keys

```bash
# Create or update an API key (encrypted at rest)
curl -X POST http://localhost:8000/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"name":"openai-dev","provider":"openai","value":"sk-..."}'

# List key metadata (no secret values returned)
curl http://localhost:8000/v1/keys
```

#### Model Catalog

```bash
# Register a model
curl -X POST http://localhost:8000/v1/models \
  -H "Content-Type: application/json" \
  -d '{"id":"openai-gpt-4o-mini","name":"gpt-4o-mini","provider":"openai","capabilities":["chat"]}'

# List catalog models
curl http://localhost:8000/v1/models
```

#### Chat

```bash
# Create conversation
curl -X POST http://localhost:8000/v1/chat/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"Build auth API"}'

# Post a message (optionally with bot_id)
curl -X POST http://localhost:8000/v1/chat/conversations/{conversation_id}/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"Draft the endpoint design","bot_id":"bot-coder-14b"}'

# Stream a turn over SSE
curl -N -X POST http://localhost:8000/v1/chat/conversations/{conversation_id}/stream \
  -H "Content-Type: application/json" \
  -d '{"content":"Continue","bot_id":"bot-coder-14b"}'
```

#### Vault + MCP Context

```bash
# Ingest text into the vault
curl -X POST http://localhost:8000/v1/vault/items \
  -H "Content-Type: application/json" \
  -d '{"title":"Auth notes","content":"JWT auth middleware and refresh token flow","namespace":"global"}'

# Search vault chunks
curl -X POST http://localhost:8000/v1/vault/search \
  -H "Content-Type: application/json" \
  -d '{"query":"JWT auth","limit":5}'

# Pull standardized MCP-style context for a query
curl -X POST http://localhost:8000/v1/vault/context \
  -H "Content-Type: application/json" \
  -d '{"query":"refresh token","limit":3}'

# Prometheus-compatible metrics
curl http://localhost:8000/metrics
```

### Worker Agent (`http://localhost:8001`)

```bash
# Health check
curl http://localhost:8001/health

# Get capabilities
curl http://localhost:8001/capabilities

# Run inference
curl -X POST http://localhost:8001/infer \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3-8b-instruct-q4", "provider": "ollama", "messages": [{"role": "user", "content": "Hi"}]}'

# Prometheus-compatible metrics
curl http://localhost:8001/metrics
```

---

## How to Add a New Worker Machine

1. Create a YAML file in `config/workers/`, e.g. `config/workers/gpu-box-2.yaml`:

```yaml
id: worker-gpu-box-2
name: GPU Box 2
host: 192.168.1.20
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
WORKER_CONFIG_PATH=config/workers/gpu-box-2.yaml \
CONTROL_PLANE_URL=http://<control-plane-ip>:8000 \
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8001
```

The worker will self-register and begin sending heartbeats.

---

## How to Define a New Bot

1. Create a YAML file in `config/bots/`, e.g. `config/bots/summarizer.yaml`:

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

---

## Integration with agent-orchestrator

[`Jacobdrosol/agent-orchestrator`](https://github.com/Jacobdrosol/agent-orchestrator) can be used as a worker backend by:

1. Running `agent-orchestrator` on a machine.
2. Creating a worker YAML that points to it:

```yaml
id: worker-orchestrator
name: Agent Orchestrator
host: 192.168.1.30
port: 8090
capabilities:
  - type: llm
    provider: custom
    models:
      - orchestrator-pipeline
```

3. Create a bot with `type: remote_llm` pointing to this worker.
4. NexusAI will POST inference requests to `http://192.168.1.30:8090/infer`.

---

## Next Priorities

- [ ] End-to-end UAT execution and bug triage using `docs/UAT_RUNBOOK.md`
- [x] Add robust load-aware scheduling (queue depth/latency weighted worker selection)
- [x] Add metrics/observability export (Prometheus + structured latency/error dashboards)
- [x] Extend automated security tests for webhook replay protections and secret-rotation workflows
- [ ] Add complete in-app password reset/recovery workflows (no direct DB command dependency)
- [ ] Stabilize deployment profile (compose + reverse proxy reference stack)

## Future Enhancements

Workflow and pipeline UX ideas currently planned, but not yet implemented:

- A dedicated visual pipeline designer for building reusable multi-bot workflows without editing each bot page individually.
- Start-from-step execution so operators can launch a pipeline from a chosen stage with explicit input.
- Fan-out branch replay so operators can rerun one failed unit/lesson branch without rerunning the whole workflow.
- Resume-from-checkpoint execution after a partial failure or operator correction.
- Queue and concurrency controls at the pipeline level so large fan-out stages can be drained safely without overwhelming providers or workers.
- First-class pipeline templates that remain user-defined and modular rather than being seeded with project-specific assumptions.
