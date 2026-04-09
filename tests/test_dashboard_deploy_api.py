"""Tests for dashboard deploy API endpoints."""

import bcrypt
from datetime import datetime, timedelta, timezone


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
