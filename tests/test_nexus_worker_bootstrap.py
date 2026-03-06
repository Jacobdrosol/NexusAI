"""Tests for nexus_worker bootstrap helpers."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from nexus_worker.bootstrap import bootstrap_worker_node
from nexus_worker.manager.cli_tools import discover_cli_tools


def test_discover_cli_tools_reports_metadata():
    with patch("nexus_worker.manager.cli_tools.shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in {"codex", "git"} else None):
        with patch("nexus_worker.manager.cli_tools._read_version", side_effect=lambda command, version_args: f"{command} 1.0"):
            tools = discover_cli_tools()

    names = {tool["name"] for tool in tools}
    assert "codex" in names
    assert "git" in names
    codex = next(tool for tool in tools if tool["name"] == "codex")
    assert codex["requires_approval"] is True
    assert codex["approval_hints"]


async def test_bootstrap_worker_node_generates_assets(tmp_path: Path):
    args = Namespace(
        output_dir=str(tmp_path),
        worker_id="worker-01",
        worker_name="Worker 01",
        host="worker-host",
        port=8010,
        python="/usr/bin/python3",
        ollama_host="http://localhost:11434",
        control_plane_url="http://control-plane:8000",
        control_plane_api_token="secret-token",
        generate_token=False,
        pull_ollama_model=[],
    )

    with patch("nexus_worker.bootstrap.discover_local_models", return_value={"models": [{"provider": "ollama", "name": "llama3.1:8b"}], "errors": []}):
        with patch(
            "nexus_worker.bootstrap.discover_cli_tools",
            return_value=[{"name": "codex", "path": "/usr/bin/codex", "version": "codex 1.0", "requires_approval": True, "approval_hints": ["Run codex login"]}],
        ):
            summary = await bootstrap_worker_node(args)

    assert summary["worker_id"] == "worker-01"
    config_path = tmp_path / "nexus-worker.yaml"
    env_path = tmp_path / "nexus-worker.env"
    summary_path = tmp_path / "bootstrap-summary.json"
    assert config_path.exists()
    assert env_path.exists()
    assert summary_path.exists()

    config_text = config_path.read_text(encoding="utf-8")
    assert "worker-01" in config_text
    assert "codex" in config_text
    assert "llama3.1:8b" in config_text

    env_text = env_path.read_text(encoding="utf-8")
    assert "CONTROL_PLANE_URL=http://control-plane:8000" in env_text
    assert "CONTROL_PLANE_API_TOKEN=secret-token" in env_text

    summary_json = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary_json["service_name"].startswith("nexus-worker-worker-01")

