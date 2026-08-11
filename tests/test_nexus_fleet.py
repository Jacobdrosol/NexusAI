import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "nexus_fleet.py"


def _load_fleet_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("nexus_fleet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_fleet(tmp_path: Path) -> Path:
    summary = {
        "compose_project_name": "test-fleet",
        "workers": [
            {"id": "worker-a-01", "name": "Worker A", "service": "svc-a-01", "bot_id": "bot-a-01", "restart_policy": "auto"},
            {"id": "worker-a-02", "name": "Worker A2", "service": "svc-a-02", "bot_id": "bot-a-02", "restart_policy": "auto"},
            {"id": "worker-b-01", "name": "Worker B", "service": "svc-b-01", "bot_id": "bot-b-01", "restart_policy": "manual"},
            {"id": "worker-c-01", "name": "Worker C", "service": "svc-c-01", "bot_id": "bot-c-01", "restart_policy": "always"},
        ],
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    compose = {
        "name": "test-fleet",
        "services": {w["service"]: {"image": "test:latest", "restart": "no"} for w in summary["workers"]},
        "networks": {"nexus-net": {"name": "test-net"}},
    }
    (tmp_path / "docker-compose.worker-node.generated.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False), encoding="utf-8"
    )
    return tmp_path


def test_fleet_cli_workers_in_group(tmp_path):
    cli = _load_fleet_cli()
    summary = json.loads((_write_fleet(tmp_path) / "summary.json").read_text())

    auto_workers = cli.workers_in_group(summary, "auto")
    assert [w["id"] for w in auto_workers] == ["worker-a-01", "worker-a-02"]

    manual_workers = cli.workers_in_group(summary, "manual")
    assert [w["id"] for w in manual_workers] == ["worker-b-01"]

    always_workers = cli.workers_in_group(summary, "always")
    assert [w["id"] for w in always_workers] == ["worker-c-01"]

    critical_workers = cli.workers_in_group(summary, "critical")
    assert [w["id"] for w in critical_workers] == ["worker-c-01"]

    all_workers = cli.workers_in_group(summary, "all")
    assert len(all_workers) == 4


def test_fleet_cli_filter_by_worker(tmp_path):
    cli = _load_fleet_cli()
    summary = json.loads((_write_fleet(tmp_path) / "summary.json").read_text())

    matched = cli.filter_by_worker(summary["workers"], "worker-a-01")
    assert len(matched) == 1
    assert matched[0]["id"] == "worker-a-01"

    matched_by_service = cli.filter_by_worker(summary["workers"], "svc-b-01")
    assert len(matched_by_service) == 1
    assert matched_by_service[0]["id"] == "worker-b-01"

    with pytest.raises(SystemExit):
        cli.filter_by_worker(summary["workers"], "nonexistent")


def test_fleet_cli_dry_run_start(tmp_path):
    fleet_dir = _write_fleet(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--summary", str(fleet_dir / "summary.json"), "--compose", str(fleet_dir / "docker-compose.worker-node.generated.yml"), "--dry-run", "start", "--group", "auto"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "svc-a-01" in result.stdout
    assert "svc-a-02" in result.stdout
    assert "svc-b-01" not in result.stdout
    assert "[dry-run]" in result.stdout


def test_fleet_cli_dry_run_start_critical(tmp_path):
    fleet_dir = _write_fleet(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--summary", str(fleet_dir / "summary.json"), "--compose", str(fleet_dir / "docker-compose.worker-node.generated.yml"), "--dry-run", "start", "--group", "critical"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "svc-c-01" in result.stdout
    assert "svc-a-01" not in result.stdout


def test_fleet_cli_dry_run_stop_specific_worker(tmp_path):
    fleet_dir = _write_fleet(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--summary", str(fleet_dir / "summary.json"), "--compose", str(fleet_dir / "docker-compose.worker-node.generated.yml"), "--dry-run", "stop", "--worker", "worker-b-01"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "svc-b-01" in result.stdout
    assert "svc-a-01" not in result.stdout