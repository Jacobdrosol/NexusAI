# Nexus Worker Node

This folder is the standalone worker-node project for NexusAI.

It is intentionally separated from the dashboard, control plane, and worker-agent code so it can be moved into its own repository and deployed to other machines without cloning the full application repo.

## What Lives Here

- `nexus_worker/`: standalone worker runtime and bootstrap utility
- `docs/WORKER_NODE_BOOTSTRAP.md`: install and service setup guide
- `tests/`: worker-only tests
- `pyproject.toml`: standalone packaging and CLI entrypoints

## Local Development

From this folder:

```bash
pip install -e .[test]
pytest -q
```

Run the worker:

```bash
python -m nexus_worker
```

Generate bootstrap assets:

```bash
python -m nexus_worker.bootstrap --control-plane-url http://localhost:8000
```

## Future Repo Split

This folder is structured so you can turn it into its own Git repo with minimal cleanup:

1. Copy or move `worker_node/` to a new repository root.
2. Keep `pyproject.toml`, `requirements.txt`, `README.md`, `docs/`, `tests/`, and `nexus_worker/`.
3. Install it independently on worker machines.
