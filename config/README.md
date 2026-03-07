# Config Directory

This directory contains all YAML configuration files for NexusAI.

## Structure

```
config/
├── nexus_config.yaml       # Main control-plane configuration
├── workers/                # One YAML file per worker
│   ├── example_worker.yaml
│   └── local_worker.yaml
└── bots/                   # One YAML file per bot
    ├── example_bot.yaml
    └── assistant_bot.yaml
```

## `nexus_config.yaml`

Loaded at startup via `ConfigLoader.load_config(CONFIG_PATH)` where `CONFIG_PATH` defaults to `config/nexus_config.yaml`.  Override the path with the `NEXUS_CONFIG_PATH` environment variable.

Top-level keys:

| Key | Description |
|-----|-------------|
| `control_plane.host` | Bind address for the API server |
| `control_plane.port` | TCP port for the API server |
| `control_plane.workers_config_dir` | Directory scanned for worker YAMLs |
| `control_plane.bots_config_dir` | Directory scanned for bot YAMLs |
| `control_plane.seed_workers_from_config` | If `true`, seed missing workers from YAML files at startup. Defaults to `false` for UI-first installs. |
| `control_plane.seed_bots_from_config` | If `true`, seed missing bots from YAML files at startup. Defaults to `false` for UI-first installs. |
| `control_plane.heartbeat_timeout_seconds` | Seconds before a silent worker is marked offline |
| `logging.level` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Adding a Worker

For normal installs, create and manage workers in the UI or let standalone worker nodes self-register. YAML worker files are optional bootstrap inputs only when `control_plane.seed_workers_from_config: true`.

If you do want declarative worker seeding, create a new YAML file in `config/workers/`. The file is validated against the `Worker` model (`shared/models.py`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique worker identifier |
| `name` | string | Yes | Human-readable name |
| `host` | string | Yes | Hostname or IP address of the worker |
| `port` | integer | Yes | Port the worker agent listens on |
| `capabilities` | list | Yes | List of capability objects (may be empty `[]`) |
| `status` | string | — | `"online"`, `"offline"`, or `"degraded"` (default: `"offline"`) |
| `metrics` | object | — | Optional `WorkerMetrics` object |

Each entry in `capabilities` must include:

| Field | Type | Required |
|-------|------|----------|
| `type` | `"llm"` \| `"embedding"` \| `"tool"` \| `"custom"` | Yes |
| `provider` | `"ollama"` \| `"vllm"` \| `"lmstudio"` \| `"openai"` \| `"claude"` \| `"gemini"` \| `"cli"` \| `"custom"` | Yes |
| `models` | list of strings | Yes |
| `gpus` | list of strings | — |

Example (`config/workers/local_worker.yaml`):

```yaml
id: "local-worker-01"
name: "Local Worker 01"
host: "worker_agent"
port: 8001
status: "offline"
capabilities: []
```

## Adding a Bot

For normal installs, create and manage bots in the UI. YAML bot files are optional bootstrap inputs only when `control_plane.seed_bots_from_config: true`.

If you do want declarative bot seeding, create a new YAML file in `config/bots/`. The file is validated against the `Bot` model (`shared/models.py`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique bot identifier |
| `name` | string | Yes | Human-readable name |
| `role` | string | Yes | Role description (e.g. `"coding"`, `"general_assistant"`) |
| `priority` | integer | — | Scheduling priority (default: `0`) |
| `enabled` | boolean | — | Whether the bot is active (default: `true`) |
| `backends` | list | Yes | Ordered list of `BackendConfig` objects |

Each entry in `backends` must include:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"local_llm"` \| `"remote_llm"` \| `"cloud_api"` \| `"cli"` \| `"custom"` | Yes | |
| `provider` | string | Yes | e.g. `"ollama"`, `"claude"`, `"openai"` |
| `model` | string | Yes | Model identifier |
| `worker_id` | string | — | ID of the worker that hosts this backend |
| `gpu_id` | string | — | GPU label on the worker |
| `api_key_ref` | string | — | Environment variable name holding the API key |
| `params` | object | — | Optional `BackendParams` (`temperature`, `max_tokens`, `top_p`) |

Example (`config/bots/assistant_bot.yaml`):

```yaml
id: "assistant-bot"
name: "Assistant Bot"
role: "general_assistant"
priority: 0
enabled: true
backends:
  - type: "local_llm"
    provider: "ollama"
    model: "llama3"
    worker_id: "local-worker-01"
```

## Environment Variable Override

Set `NEXUS_CONFIG_PATH` to load a different main config file:

```bash
NEXUS_CONFIG_PATH=/etc/nexusai/nexus_config.yaml uvicorn control_plane.main:app
```

