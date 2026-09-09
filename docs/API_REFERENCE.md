# NexusAI API Reference

Example `curl` calls against the control plane (`http://localhost:8000`) and worker agent (`http://localhost:8001`).

## Control Plane

### Tasks

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

### Bots

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

### Workers

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

### Projects

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

### API Keys

```bash
# Create or update an API key (encrypted at rest)
curl -X POST http://localhost:8000/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"name":"openai-dev","provider":"openai","value":"sk-..."}'

# List key metadata (no secret values returned)
curl http://localhost:8000/v1/keys
```

### Model Catalog

```bash
# Register a model
curl -X POST http://localhost:8000/v1/models \
  -H "Content-Type: application/json" \
  -d '{"id":"openai-gpt-4o-mini","name":"gpt-4o-mini","provider":"openai","capabilities":["chat"]}'

# List catalog models
curl http://localhost:8000/v1/models
```

### Chat

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

### Vault + MCP Context

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

## Worker Agent

```bash
# Health check
curl http://localhost:8001/health

# Get capabilities
curl http://localhost:8001/capabilities

# Run inference
curl -X POST http://localhost:8001/infer \
  -H "Content-Type: application/json" \
  -d '{"model": "example-model", "provider": "ollama", "messages": [{"role": "user", "content": "Hi"}]}'

# Prometheus-compatible metrics
curl http://localhost:8001/metrics
```

For the complete endpoint table, see `control_plane/api/README.md`.
