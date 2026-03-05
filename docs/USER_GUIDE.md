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
- Standalone mode: run `python -m nexus_worker` with valid control plane URL/token.

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

## 4. Projects

Project modes:

- `isolated`: no cross-project sharing.
- `bridged`: explicit sharing with other bridged projects.

Use projects to separate environments (for example, `prod-assistant`, `dev-assistant`, `research`).

## 5. Vault

### 5.1 Ingestion

Vault supports:

- pasted text
- file upload
- URL import
- chat and task outputs

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
- sync repository context into vault
- enable PR review workflow with bot assignment

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
3. Keep cloud context policy at `block` during initial rollout.
4. Enable `redact` only after validating output quality and data controls.
5. Use audit logs and metrics as release gates.
