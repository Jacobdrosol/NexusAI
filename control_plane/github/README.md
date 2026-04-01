# GitHub Integration

The GitHub integration provides webhook ingestion, per-project PAT management, and repository context sync into the vault.

---

## GitHubWebhookStore (`webhook_store.py`)

### SQLite Table: `github_webhook_events`

| Column | Description |
|--------|-------------|
| `id` | UUID |
| `project_id` | Owning project |
| `delivery_id` | GitHub `X-GitHub-Delivery` header (for deduplication) |
| `event_type` | `push`, `pull_request`, `issues`, etc. |
| `action` | Sub-action (e.g., `opened`, `closed`, `synchronize`) |
| `repository_full_name` | `owner/repo` |
| `payload` | Full webhook JSON payload |
| `created_at` | ISO 8601 UTC |

### Key Methods

| Method | Description |
|--------|-------------|
| `store_event(project_id, delivery_id, event_type, action, repo, payload)` | Persists a webhook event; deduplicates by `delivery_id` |
| `list_events(project_id, event_type, limit)` | Filtered listing |
| `get_event(event_id)` | By UUID |

### Deduplication

GitHub may redeliver webhooks on failure. The store deduplicates by `delivery_id` — a duplicate delivery is silently ignored.

---

## Webhook Processing (in `api/projects.py`)

`POST /v1/projects/{id}/github/webhook` handles incoming webhooks:

1. **Signature verification**: validates `X-Hub-Signature-256` HMAC against the project's stored webhook secret. Returns 401 if signature is invalid or secret is not set.
2. **Delivery-ID deduplication**: checks `X-GitHub-Delivery` header; ignores if already stored.
3. **Timestamp skew check**: rejects webhooks with a timestamp more than 5 minutes in the past (using `Date` header).
4. **Storage**: calls `GitHubWebhookStore.store_event()`.
5. **PR review task**: if a PR review bot is configured and the event is `pull_request` with action `opened` or `synchronize`, spawns a bot task.

---

## GitHub PAT Integration

`POST /v1/projects/{id}/github/connect` stores a GitHub PAT in `KeyVault` under the name `github_pat:<project_id>`. The PAT is used for:
- Authenticated GitHub API calls during repo sync
- Clone/push operations in the repo workspace

`DELETE /v1/projects/{id}/github/disconnect` removes the stored PAT.

---

## Repo Context Sync

`POST /v1/projects/{id}/github/sync` synchronises repo content into the vault:

| `sync_mode` | Behaviour |
|-------------|-----------|
| `full` | Clears existing vault items in the repo namespace, then ingests all files, commits, PRs, issues |
| `update` | Only ingests changed/newer items since the last successful sync |

Content is ingested under namespace `project:<id>:repo`.

Long-running syncs execute as background tasks and report status back to the project record.

---

## PR Review Task Workflow

When `POST /v1/projects/{id}/github/pr-review` configures a PR review bot, incoming `pull_request.opened` or `pull_request.synchronize` webhooks spawn a task for that bot with the PR diff and metadata as payload.

---

## Known Issues

- Webhook secret is stored via `KeyVault` (Fernet encrypted), but PATs are also stored via `KeyVault`. There is no distinction between webhook secret type and PAT type beyond the key name prefix.
- The timestamp skew check uses the HTTP `Date` header, which can be spoofed. Real protection comes from HMAC signature verification.
- Long-running sync background tasks do not have cancellation support.
