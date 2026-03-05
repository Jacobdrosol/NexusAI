"""Tests for dashboard deploy API endpoints."""

import bcrypt


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


def test_deploy_run_endpoint_blocked_without_explicit_enable(dashboard_client):
    _login_admin(dashboard_client)
    resp = dashboard_client.post("/api/settings/deploy/run")
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["status"] == "blocked"
    assert "error" in data
