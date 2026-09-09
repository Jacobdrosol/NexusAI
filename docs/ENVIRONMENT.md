# NexusAI Environment Variables

Copy `.env.example` to `.env` and set the following variables before starting the stack.

| Variable | Default | Description |
|---|---|---|
| `NEXUSAI_SECRET_KEY` | `dev-secret-change-in-production` | Flask session secret key — **must be changed in production** |
| `NEXUSAI_ENV` | `development` | Set `production` or `prod` to reject development vault-encryption keys at startup |
| `NEXUS_MASTER_KEY` | — | Dedicated encryption seed for the API-key vault; use a long random value in production |
| `DATABASE_URL` | `sqlite:///data/nexusai.db` | SQLAlchemy connection URL (SQLite or PostgreSQL) |
| `CONTROL_PLANE_URL` | — | Control-plane URL for the dashboard and standalone workers; use a host-reachable URL for a split blue/green dashboard topology |
| `WORKER_CONTROL_PLANE_URL` | `http://control_plane:8000` | Control-plane URL for the bundled Docker `worker_agent`; keep the Docker-network default unless it runs outside the compose stack |
| `CONTROL_PLANE_API_TOKEN` | — | Optional shared token for control-plane API auth; when set, the dashboard/worker send `X-Nexus-API-Key` and the control plane enforces auth on API routes |
| `CP_INGEST_TIMEOUT` | `1800` | Dashboard timeout in seconds for long-running project ingestion calls such as GitHub full-context sync |
| `NEXUSAI_CLOUD_CONTEXT_POLICY` | `allow` | Cloud egress policy for context blocks (`allow`, `redact`, `block`) on control-plane scheduler cloud backends |
| `NEXUS_WORKER_CLOUD_CONTEXT_POLICY` | `redact` | Standalone worker cloud egress policy for context blocks (`allow`, `redact`, `block`) |
| `NEXUSAI_TOKEN_GOVERNOR_ENABLED` | `0` | Enable dispatch-time token guardrails for model-backed tasks |
| `NEXUSAI_TOKEN_GOVERNOR_GLOBAL_HOURLY_LIMIT` | `0` | Rolling one-hour token ceiling across all model-backed task results; `0` disables this ceiling |
| `NEXUSAI_TOKEN_GOVERNOR_BOT_HOURLY_LIMIT` | `0` | Rolling one-hour token ceiling for any single bot; `0` disables this ceiling |
| `NEXUSAI_TOKEN_GOVERNOR_LLM_CONCURRENCY` | `0` | Maximum model-backed tasks allowed to run at once; `0` disables this ceiling |
| `NEXUSAI_TOKEN_GOVERNOR_ESTIMATED_TOKENS_PER_TASK` | `20000` | Pre-launch token reservation used to prevent bursts before provider usage is recorded |
| `NEXUSAI_TOKEN_GOVERNOR_BOT_ESTIMATES` | — | Optional per-bot token reservations as JSON (`{"qc-bot":1800}`) or comma form (`qc-bot=1800`) so cheap workers are not blocked by the global default estimate |
| `NEXUSAI_TOKEN_GOVERNOR_MAX_QUEUED_LLM_TASKS_PER_BOT` | `1` | Maximum queued model-backed tasks allowed for one bot before new task creation is rejected; `0` disables this queue-admission ceiling |
| `NEXUSAI_WORKER_LATENCY_EMA_ALPHA` | `0.30` | Scheduler EMA smoothing factor for worker latency scoring (0.01-1.0) |
| `NEXUSAI_WORKER_DEFAULT_LATENCY_MS` | `800` | Default worker latency estimate used before dispatch history exists |
| `NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DELIVERY_ID` | `1` | Require `X-GitHub-Delivery` header for webhook replay protection |
| `NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS` | `300` | Allowed request timestamp skew when a `Date` header is present |
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
| `NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED` | `0` | Allow Platform AI to change bot configuration; otherwise it records proposals for review only |
| `NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED` | `0` | Allow Platform AI to launch and relaunch autonomous pipeline iterations |
| `NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED` | `0` | Allow Platform AI privileged runner actions (repo edit, deploy, project edit); all disabled by default |
| `NEXUS_PLATFORM_AI_OWNER_ALLOWLIST` | — | Optional comma-separated owner allowlist for privileged Platform AI actions |
| `NEXUSAI_MOBILE_ANDROID_MIN_VERSION_CODE` / `NEXUSAI_MOBILE_ANDROID_LATEST_VERSION_CODE` | `1` | Android client version contract served through `/api/mobile/bootstrap` |
| `NEXUSAI_MOBILE_ANDROID_RELEASE_URL` | — | Hosted APK URL advertised to the Android client for self-hosted updates |
| `OPENAI_API_KEY` | — | OpenAI API key for cloud LLM backends |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |

## Notes

- The preferred cloud-key path is Dashboard `Settings -> API Keys` (encrypted at rest, named keys, multi-provider, multiple keys/provider).
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` are fallback-only environment variables.
- Chat token-governor caps are managed from the Settings token-governor editor, not environment variables.
