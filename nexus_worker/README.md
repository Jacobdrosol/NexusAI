# nexus_worker

Standalone worker package for NexusAI.

## Features

- Registers to control plane and sends heartbeat metrics.
- Hardware profile endpoint (CPU/RAM/GPU).
- Local model discovery (`/models/local`) with Ollama + optional vLLM list.
- Model compatibility hints + ETA examples (`/capabilities`).
- Inference endpoints:
  - `POST /infer`
  - `POST /infer/stream` (SSE chunks + final event)
- Cloud context privacy policy:
  - `NEXUS_WORKER_CLOUD_CONTEXT_POLICY=allow|redact|block` (default: `redact`)

## Config

Use `nexus_worker/config.yaml.example` as a base.

Environment variables:

- `NEXUS_WORKER_CONFIG_PATH` (default: `nexus_worker/config.yaml.example`)
- `CONTROL_PLANE_URL` (default: `http://localhost:8000`)
- `CONTROL_PLANE_API_TOKEN` (optional)
- `HEARTBEAT_INTERVAL` (default: `15`)
- `NEXUS_WORKER_CLOUD_CONTEXT_POLICY` (default: `redact`)
- `VLLM_MODELS` (optional comma-separated names)

## Run

```bash
python -m nexus_worker
```

or if installed with entry points:

```bash
nexus-worker
```

## Bootstrap a Worker Node

Generate a worker config, env file, discovered capability metadata, and
background-service install assets:

```bash
python -m nexus_worker.bootstrap \
  --control-plane-url http://YOUR_CONTROL_PLANE_HOST:8000 \
  --control-plane-api-token YOUR_SHARED_TOKEN \
  --worker-name "Worker Node 01"
```

or via the installed entry point:

```bash
nexus-worker-bootstrap \
  --control-plane-url http://YOUR_CONTROL_PLANE_HOST:8000 \
  --control-plane-api-token YOUR_SHARED_TOKEN \
  --worker-name "Worker Node 01"
```

See `docs/WORKER_NODE_BOOTSTRAP.md` for the full flow.
