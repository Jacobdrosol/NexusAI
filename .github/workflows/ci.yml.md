# Disabled GitHub Actions workflow

This file preserves the previous CI workflow configuration, but its `.md` extension prevents GitHub Actions from loading or running it.

To restore the workflow, rename this file back to `ci.yml`. Review the workflow and expected Actions usage before re-enabling it.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements.txt ruff pytest
      - name: Verify public release hygiene
        run: python scripts/verify_public_release.py
      - name: Lint
        run: ruff check . --ignore E501
      - name: Test
        run: pytest --tb=short -q

  docker-build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - name: Build dashboard image
        run: docker build -f dashboard/Dockerfile -t nexusai-dashboard .
      - name: Build control_plane image
        run: docker build -f control_plane/Dockerfile -t nexusai-control-plane .
      - name: Build worker_agent image
        run: docker build -f worker_agent/Dockerfile -t nexusai-worker-agent .
```
