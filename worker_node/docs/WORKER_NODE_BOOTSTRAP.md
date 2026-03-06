# Worker Node Bootstrap

This guide prepares a standalone `nexus_worker` node that:

- discovers local Ollama models and common CLI tools
- writes a worker config and env file
- generates background-service install assets for Linux, macOS, or Windows
- connects back to the NexusAI control plane

## 1. Prerequisites

- Python 3.11+
- NexusAI checked out on the worker node
- network reachability from worker node to control plane
- optional local runtimes already installed:
  - Ollama
  - Codex CLI
  - GitHub CLI
  - Docker

## 2. Run the Bootstrap Utility

From the repo root on the worker node:

```bash
python -m nexus_worker.bootstrap \
  --control-plane-url http://YOUR_CONTROL_PLANE_HOST:8000 \
  --control-plane-api-token YOUR_SHARED_TOKEN \
  --worker-name "Linux Worker 01" \
  --output-dir ./generated/worker-node
```

Optional:

```bash
python -m nexus_worker.bootstrap \
  --control-plane-url http://YOUR_CONTROL_PLANE_HOST:8000 \
  --control-plane-api-token YOUR_SHARED_TOKEN \
  --worker-name "Linux Worker 01" \
  --pull-ollama-model llama3.1:8b \
  --pull-ollama-model nomic-embed-text
```

Generated assets:

- `generated/worker-node/nexus-worker.yaml`
- `generated/worker-node/nexus-worker.env`
- `generated/worker-node/bootstrap-summary.json`
- service install assets for the current OS

## 3. What Gets Discovered

The bootstrap utility detects:

- Ollama local models
- common CLI tools:
  - `codex`
  - `gh`
  - `git`
  - `python`
  - `node`
  - `npm`
  - `docker`
  - `ollama`

CLI metadata includes:

- executable path
- version string when available
- whether the tool usually needs approval/auth setup first
- approval/setup hints

This metadata is written into `nexus-worker.yaml` and exposed by `GET /capabilities`.

## 4. Install as a Background Service

### Linux

The bootstrap output includes:

- `nexus-worker-<worker-id>.service`
- `install-service.sh`

Run:

```bash
cd generated/worker-node
sh ./install-service.sh
```

### macOS

The bootstrap output includes:

- `nexus-worker-<worker-id>.plist`
- `install-service.sh`

Run:

```bash
cd generated/worker-node
sh ./install-service.sh
```

### Windows

The bootstrap output includes:

- `run-nexus-worker.cmd`
- `install-service.ps1`

Run PowerShell as Administrator:

```powershell
cd .\generated\worker-node
.\install-service.ps1
```

Windows uses a startup scheduled task in this first pass. That keeps the worker running in the background without adding a separate service wrapper dependency.

## 5. Verify the Worker

After the service starts, verify:

```bash
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/capabilities
```

Then in NexusAI:

- open `Workers`
- confirm the node is registered and online
- inspect worker capabilities

## 6. Current Scope

This bootstrap flow does:

- background service install assets
- worker config/env generation
- CLI/tool discovery
- optional Ollama pulls

It does not yet provide:

- full UI-driven remote worker installation
- CLI approval policy enforcement
- tool-specific auth/setup wizards

Those should be built on top of the generated CLI metadata and worker capability reporting in a later pass.
