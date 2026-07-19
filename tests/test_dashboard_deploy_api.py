"""Tests for dashboard deploy API endpoints."""

import bcrypt
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _login_admin(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    pw = "password123"
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        if db.query(User).count() == 0:
            db.add(User(email="admin@test.com", password_hash=pw_hash, role="admin", is_active=True))
            db.commit()
    finally:
        db.close()

    resp = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": pw},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_bluegreen_dashboard_services_restart_after_host_reboot():
    compose = yaml.safe_load((ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8"))

    for service_name in ("dashboard_gateway", "dashboard_blue", "dashboard_green"):
        assert compose["services"][service_name]["restart"] == "unless-stopped"


def test_bootstrap_can_recover_without_rebuilding_images():
    bootstrap = (ROOT / "scripts" / "bootstrap_bluegreen.sh").read_text(encoding="utf-8")

    assert 'BOOTSTRAP_BUILD="${NEXUSAI_BOOTSTRAP_BUILD:-1}"' in bootstrap
    assert "--profile blue up -d --build dashboard_gateway dashboard_blue" in bootstrap
    assert "--profile blue up -d dashboard_gateway dashboard_blue" in bootstrap


def test_deploy_runner_prunes_inactive_builder_cache_by_default():
    deploy = (ROOT / "scripts" / "deploy-bluegreen.sh").read_text(encoding="utf-8")

    assert 'PRUNE_BUILD_CACHE="${NEXUSAI_DEPLOY_PRUNE_BUILD_CACHE:-1}"' in deploy
    assert "docker builder prune -af || true" in deploy


def test_systemd_recovery_unit_uses_shell_and_skips_boot_rebuilds():
    unit = (ROOT / "deploy" / "systemd" / "nexusai.service").read_text(encoding="utf-8")

    assert "Environment=NEXUSAI_BOOTSTRAP_BUILD=0" in unit
    assert "ExecStart=/usr/bin/docker compose up -d control_plane worker_agent prometheus" in unit
    assert "ExecStart=/bin/sh -lc 'cd /opt/NexusAI && sh ./scripts/bootstrap_bluegreen.sh'" in unit
    assert "Restart=on-failure" in unit
    assert "--build" not in unit


def test_core_and_bluegreen_services_have_resource_envelopes():
    core = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bluegreen = yaml.safe_load((ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8"))

    expected_core = {
        "control_plane": ("${NEXUSAI_CONTROL_PLANE_MEM_LIMIT:-4g}", "${NEXUSAI_CONTROL_PLANE_PIDS_LIMIT:-768}"),
        "worker_agent": ("${NEXUSAI_CORE_WORKER_MEM_LIMIT:-1g}", "${NEXUSAI_CORE_WORKER_PIDS_LIMIT:-256}"),
        "dashboard": ("${NEXUSAI_DASHBOARD_MEM_LIMIT:-1g}", "${NEXUSAI_DASHBOARD_PIDS_LIMIT:-256}"),
        "prometheus": ("${NEXUSAI_PROMETHEUS_MEM_LIMIT:-1g}", "${NEXUSAI_PROMETHEUS_PIDS_LIMIT:-256}"),
    }
    for service_name, (memory_limit, pids_limit) in expected_core.items():
        service = core["services"][service_name]
        assert service["restart"] == "unless-stopped"
        assert service["mem_limit"] == memory_limit
        assert service["pids_limit"] == pids_limit

    gateway = bluegreen["services"]["dashboard_gateway"]
    assert gateway["mem_limit"] == "${NEXUSAI_GATEWAY_MEM_LIMIT:-256m}"
    assert gateway["pids_limit"] == "${NEXUSAI_GATEWAY_PIDS_LIMIT:-128}"

    for service_name in ("dashboard_blue", "dashboard_green"):
        service = bluegreen["services"][service_name]
        assert service["mem_limit"] == "${NEXUSAI_DASHBOARD_MEM_LIMIT:-1g}"
        assert service["pids_limit"] == "${NEXUSAI_DASHBOARD_PIDS_LIMIT:-256}"


def test_deploy_status_endpoint_returns_payload(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.get("/api/settings/deploy/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "state" in data
    assert "local_commit" in data
    assert "remote_commit" in data
    assert "remote_check_error" in data
    assert "deploy_allowed" in data
    assert "active_color" in data
    assert "next_color" in data
    assert "run_id" in data
    assert "log_updated_at" in data


def test_deploy_status_endpoint_reloads_latest_log_from_disk(dashboard_client):
    from dashboard.deploy_manager import DeployManager

    _login_admin(dashboard_client)
    manager = DeployManager.instance()
    with manager._lock:
        manager._state["log_tail"] = ["persisted deploy log"]
        manager._save_state()
        manager._state["log_tail"] = []

    resp = dashboard_client.get("/api/settings/deploy/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["log_tail"] == ["persisted deploy log"]


def test_deploy_run_endpoint_blocked_without_explicit_enable(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/settings/deploy/run")
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["status"] == "blocked"
    assert "error" in data


def test_deploy_log_clear_endpoint_returns_ok(dashboard_client):
    from dashboard.deploy_manager import DeployManager

    _login_admin(dashboard_client)
    manager = DeployManager.instance()
    with manager._lock:
        manager._state["log_tail"] = ["stale deploy log"]
        manager._save_state()

    resp = dashboard_client.post("/api/settings/deploy/log/clear")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["deploy_status"]["log_tail"] == []


def test_deploy_status_recovers_stale_running_state(dashboard_client):
    from dashboard.deploy_manager import DeployManager

    _login_admin(dashboard_client)
    manager = DeployManager.instance()
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with manager._lock:
        manager._state["state"] = "running"
        manager._state["run_id"] = "stale-run"
        manager._state["started_at"] = stale_at
        manager._state["log_updated_at"] = stale_at
        manager._state["finished_at"] = None
        manager._state["last_error"] = None
        manager._state["log_tail"] = ["[old] still running"]
        manager._thread = None
        manager._save_state()

    resp = dashboard_client.get("/api/settings/deploy/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["state"] == "failed"
    assert "no longer active" in str(data.get("last_error") or "").lower()
    assert any("stale running state detected" in str(line) for line in (data.get("log_tail") or []))


def test_deploy_runner_forces_stop_previous_off_by_default(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, **kwargs):
            env = kwargs.get("env")
            if isinstance(env, dict):
                captured["env"] = env
            self.stdout = iter(["ok\n"])

        def wait(self):
            return 0

    def _fake_popen(*args, **kwargs):
        return _FakeProc(**kwargs)

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.Popen", _fake_popen)
    monkeypatch.setenv("NEXUSAI_DEPLOY_RUN_CMD", "echo deploy")
    monkeypatch.delenv("NEXUSAI_DEPLOY_STOP_PREVIOUS_COLOR_FROM_DASHBOARD", raising=False)

    manager._run_deploy("tester")
    env = captured.get("env")
    assert isinstance(env, dict)
    assert env.get("NEXUSAI_STOP_PREVIOUS_COLOR") == "0"


def test_subcontainer_runner_forces_safe_cleanup_flags(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    monkeypatch.setattr(manager, "_detect_runner_image", lambda: "nexusai-dashboard_blue:latest")
    monkeypatch.setenv("NEXUSAI_DEPLOY_HOST_REPO_ROOT", "/opt/NexusAI")

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append([str(part) for part in args])
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="runner-container-id", stderr="")

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.run", _fake_run)

    runner_name, err = manager._launch_subcontainer_runner(run_id="1234567890ab", run_cmd="echo deploy")
    assert err is None
    assert runner_name == "nexus-deploy-runner-1234567890ab"
    assert len(calls) >= 2
    run_cmd = calls[1]
    assert "-e" in run_cmd
    assert "NEXUSAI_DEPLOY_RUNNER_MODE=subcontainer" in run_cmd
    assert "NEXUSAI_STOP_PREVIOUS_COLOR=0" in run_cmd
    assert "NEXUSAI_REMOVE_PREVIOUS_COLOR_CONTAINER=0" in run_cmd


def test_post_success_cleanup_targets_previous_color(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    with manager._lock:
        manager._state["initial_active_color"] = "blue"
        manager._save_state()

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append([str(part) for part in args])
        cmd = [str(part) for part in args]
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.run", _fake_run)
    manager._cleanup_previous_color_after_success()

    assert len(calls) >= 3
    assert calls[0][0:2] == ["docker", "inspect"]
    assert "nexus-dashboard-blue" in calls[0]
    assert calls[1][0:2] == ["docker", "stop"]
    assert "nexus-dashboard-blue" in calls[1]
    assert calls[2][0:2] == ["docker", "rm"]
    assert "nexus-dashboard-blue" in calls[2]


def test_sync_subcontainer_runner_succeeds_without_local_commit(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    with manager._lock:
        manager._state["runner_container_name"] = "runner-test"
        manager._state["runner_log_since"] = None
        manager._state["runner_log_line_count"] = 0
        manager._state["state"] = "running"
        manager._save_state()

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        cmd = [str(part) for part in args]
        calls.append(cmd)
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="[deploy] completed\n", stderr="")
        if cmd[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.run", _fake_run)
    monkeypatch.setattr(manager, "_runner_status", lambda _name: ("exited", 0, None))
    monkeypatch.setattr(manager, "_current_commit", lambda: None)
    monkeypatch.setattr(manager, "_cleanup_previous_color_after_success", lambda: None)

    with manager._lock:
        manager._sync_subcontainer_runner()
        assert manager._state["state"] == "succeeded"
        assert manager._state["runner_container_name"] is None
        assert manager._state["last_error"] is None


def test_sync_subcontainer_runner_treats_completed_marker_as_success(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    with manager._lock:
        manager._state["runner_container_name"] = "runner-test-2"
        manager._state["runner_log_since"] = None
        manager._state["runner_log_line_count"] = 0
        manager._state["state"] = "running"
        manager._state["log_tail"] = ["[deploy] completed"]
        manager._save_state()

    def _fake_run(args, **kwargs):
        cmd = [str(part) for part in args]
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.run", _fake_run)
    monkeypatch.setattr(manager, "_runner_status", lambda _name: ("exited", 1, None))
    monkeypatch.setattr(manager, "_current_commit", lambda: None)
    monkeypatch.setattr(manager, "_cleanup_previous_color_after_success", lambda: None)

    with manager._lock:
        manager._sync_subcontainer_runner()
        assert manager._state["state"] == "succeeded"
        assert manager._state["last_error"] is None


def test_subcontainer_run_cmd_normalizes_nested_docker_wrapper(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    monkeypatch.setenv("NEXUSAI_DEPLOY_NORMALIZE_NESTED_RUN_CMD", "1")
    raw = (
        "docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "
        "-v /opt/NexusAI:/opt/NexusAI -w /opt/NexusAI docker:27-cli "
        "sh -lc \"sh ./scripts/deploy-bluegreen.sh\""
    )
    normalized = manager._normalize_subcontainer_run_cmd(raw)
    assert normalized == "sh ./scripts/deploy-bluegreen.sh"


def test_subcontainer_run_cmd_normalization_is_opt_in(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    monkeypatch.delenv("NEXUSAI_DEPLOY_NORMALIZE_NESTED_RUN_CMD", raising=False)
    raw = (
        "docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "
        "-v /opt/NexusAI:/opt/NexusAI -w /opt/NexusAI docker:27-cli "
        "sh -lc \"sh ./scripts/deploy-bluegreen.sh\""
    )
    normalized = manager._normalize_subcontainer_run_cmd(raw)
    assert normalized == raw


def test_run_git_forces_safe_directory(monkeypatch):
    from dashboard.deploy_manager import DeployManager

    manager = DeployManager.instance()
    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append([str(part) for part in args])
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("dashboard.deploy_manager.subprocess.run", _fake_run)
    out, err = manager._run_git(["rev-parse", "HEAD"])
    assert err is None
    assert out == "ok"
    assert calls
    cmd = calls[0]
    assert cmd[0] == "git"
    assert cmd[1] == "-c"
    assert str(cmd[2]).startswith("safe.directory=")
    assert cmd[3:] == ["rev-parse", "HEAD"]
