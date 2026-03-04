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

---

## Architecture

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
     └─────────────────┘       └──────────────────┘   └─────────────────┘
              │
     ┌────────▼────────┐
     │   Dashboard     │
     │  (port 8080)    │
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
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8080
```

### 5. Run the Dashboard

```bash
CONTROL_PLANE_URL=http://localhost:8000 \
uvicorn dashboard.main:app --host 0.0.0.0 --port 8081
```

Then open http://localhost:8081/dashboard in your browser.

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
  port: 8080
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
port: 8080
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

### Worker Agent (`http://localhost:8080`)

```bash
# Health check
curl http://localhost:8080/health

# Get capabilities
curl http://localhost:8080/capabilities

# Run inference
curl -X POST http://localhost:8080/infer \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3-8b-instruct-q4", "provider": "ollama", "messages": [{"role": "user", "content": "Hi"}]}'
```

---

## How to Add a New Worker Machine

1. Create a YAML file in `config/workers/`, e.g. `config/workers/gpu-box-2.yaml`:

```yaml
id: worker-gpu-box-2
name: GPU Box 2
host: 192.168.1.20
port: 8080
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
uvicorn worker_agent.main:app --host 0.0.0.0 --port 8080
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

## Roadmap

- [ ] SQLite persistence for tasks, bots, and workers (via `aiosqlite`)
- [ ] Load-based routing in scheduler (route to least-loaded worker)
- [ ] Multi-GPU support with per-GPU queue management
- [ ] Web dashboard improvements: real-time updates via WebSocket
- [ ] Authentication / API key support for control plane
- [ ] Metrics export (Prometheus)
- [ ] Docker Compose deployment example

NexusAI is a modular, distributed LLM Control Plane that orchestrates multiple machines, GPUs, cloud APIs, and CLI-based models via specialized bots and worker nodes.

> Full implementation coming in the next PR.
