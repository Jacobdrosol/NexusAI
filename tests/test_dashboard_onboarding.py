"""Tests for the onboarding wizard flow."""


def test_step1_to_step2(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 1}
    resp = dashboard_client.post("/onboarding/step1", follow_redirects=False)
    assert resp.status_code == 302
    assert "step2" in resp.headers.get("Location", "")


def test_step2_invalid_email(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 2}
    resp = dashboard_client.post("/onboarding/step2", data={
        "email": "not-an-email",
        "password": "password123",
        "confirm_password": "password123",
    })
    assert resp.status_code == 400


def test_step2_password_mismatch(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 2}
    resp = dashboard_client.post("/onboarding/step2", data={
        "email": "admin@test.com",
        "password": "password123",
        "confirm_password": "different",
    })
    assert resp.status_code == 400


def test_step2_short_password(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 2}
    resp = dashboard_client.post("/onboarding/step2", data={
        "email": "admin@test.com",
        "password": "short",
        "confirm_password": "short",
    })
    assert resp.status_code == 400


def test_step2_valid_creates_admin(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 2}
    resp = dashboard_client.post("/onboarding/step2", data={
        "email": "admin@test.com",
        "password": "password123",
        "confirm_password": "password123",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "step3" in resp.headers.get("Location", "")


def test_step3_loads(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 3}
    resp = dashboard_client.get("/onboarding/step3")
    assert resp.status_code == 200


def test_step4_skip_worker(dashboard_client):
    with dashboard_client.session_transaction() as sess:
        sess["wizard"] = {"step": 4}
    resp = dashboard_client.post("/onboarding/step4", data={"skip_worker": "1"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "step5" in resp.headers.get("Location", "")
