"""Smoke tests for the Flask dashboard."""


def test_health(dashboard_client):
    resp = dashboard_client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


def test_root_redirects_to_login_or_onboarding(dashboard_client):
    resp = dashboard_client.get("/", follow_redirects=False)
    # Should redirect (either to login or onboarding)
    assert resp.status_code in (302, 301)


def test_login_page_loads(dashboard_client):
    # With no admin user, /login redirects to onboarding; follow to reach a page
    resp = dashboard_client.get("/login", follow_redirects=True)
    assert resp.status_code == 200


def test_onboarding_index_redirects_to_step1(dashboard_client):
    resp = dashboard_client.get("/onboarding/", follow_redirects=False)
    assert resp.status_code == 302
    assert "step1" in resp.headers.get("Location", "")


def test_onboarding_step1_loads(dashboard_client):
    resp = dashboard_client.get("/onboarding/step1")
    assert resp.status_code == 200
