import bcrypt


def _seed_user():
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    db = get_db()
    try:
        if db.query(User).count() == 0:
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            db.add(User(email="admin@test.com", password_hash=password_hash, role="admin", is_active=True))
            db.commit()
    finally:
        db.close()
    return password


def test_dashboard_auth_api_login_session_and_logout(dashboard_client):
    password = _seed_user()

    before = dashboard_client.get("/api/auth/session")
    assert before.status_code == 401
    assert before.get_json()["authenticated"] is False

    login = dashboard_client.post(
        "/api/auth/login",
        json={"email": "admin@test.com", "password": password},
    )
    assert login.status_code == 200
    payload = login.get_json()
    assert payload["ok"] is True
    assert payload["user"]["email"] == "admin@test.com"

    after = dashboard_client.get("/api/auth/session")
    assert after.status_code == 200
    assert after.get_json()["authenticated"] is True

    logout = dashboard_client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert logout.get_json()["ok"] is True

    final = dashboard_client.get("/api/auth/session")
    assert final.status_code == 401
