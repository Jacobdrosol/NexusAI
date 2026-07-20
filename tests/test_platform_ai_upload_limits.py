import io
from unittest.mock import patch

import bcrypt
import pytest

from dashboard.routes.platform_ai import (
    _UPLOAD_REQUEST_OVERHEAD_BYTES,
    _UploadLimitExceeded,
    _platform_ai_upload_limits,
    _save_upload_limited,
    _validate_upload_batch,
)


class _Upload:
    def __init__(self, content: bytes) -> None:
        self.stream = io.BytesIO(content)


def _login_admin(client):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    db = get_db()
    try:
        db.add(
            User(
                email="admin@test.com",
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                role="admin",
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()
    response = client.post("/login", data={"email": "admin@test.com", "password": password})
    assert response.status_code in {302, 303}


def test_platform_ai_upload_hard_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILES", "3")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILE_BYTES", "10")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_TOTAL_BYTES", "20")

    limits = _platform_ai_upload_limits()

    assert limits == {"max_files": 3, "max_file_bytes": 10, "max_total_bytes": 20}
    with pytest.raises(_UploadLimitExceeded, match="at most 3 files"):
        _validate_upload_batch(
            existing_file_count=2,
            existing_total_bytes=0,
            submitted_file_count=2,
            declared_content_length=None,
            limits=limits,
        )
    with pytest.raises(_UploadLimitExceeded, match="session already uses"):
        _validate_upload_batch(
            existing_file_count=1,
            existing_total_bytes=20,
            submitted_file_count=1,
            declared_content_length=None,
            limits=limits,
        )
    with pytest.raises(_UploadLimitExceeded, match="remaining session storage"):
        _validate_upload_batch(
            existing_file_count=1,
            existing_total_bytes=18,
            submitted_file_count=1,
            declared_content_length=3 + _UPLOAD_REQUEST_OVERHEAD_BYTES,
            limits=limits,
        )


def test_platform_ai_upload_streaming_cap_removes_partial_file(tmp_path):
    target = tmp_path / "oversized.txt"

    with pytest.raises(_UploadLimitExceeded, match="per-file limit"):
        _save_upload_limited(
            _Upload(b"0123456789"),
            target,
            max_file_bytes=8,
            max_total_remaining_bytes=20,
        )

    assert not target.exists()


def test_platform_ai_upload_streaming_cap_writes_allowed_file(tmp_path):
    target = tmp_path / "allowed.txt"

    written = _save_upload_limited(
        _Upload(b"allowed"),
        target,
        max_file_bytes=10,
        max_total_remaining_bytes=10,
    )

    assert written == 7
    assert target.read_bytes() == b"allowed"


def test_platform_ai_context_upload_rejects_oversized_file_without_persisting(dashboard_client, tmp_path, monkeypatch):
    _login_admin(dashboard_client)
    monkeypatch.setenv("NEXUSAI_PLATFORM_AI_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILE_BYTES", "3")
    monkeypatch.setenv("NEXUS_PLATFORM_AI_UPLOAD_MAX_TOTAL_BYTES", "20")

    class FakeCP:
        def __init__(self):
            self.patches = []

        def get_platform_ai_session(self, session_id):
            return {"id": session_id, "metadata": {"context_files": []}}

        def patch_platform_ai_session(self, session_id, body):
            self.patches.append((session_id, body))
            return {"id": session_id}

        def post_platform_ai_message(self, session_id, body):
            raise AssertionError("rejected upload must not create a session message")

    cp = FakeCP()
    with patch("dashboard.routes.platform_ai.get_cp_client", return_value=cp):
        response = dashboard_client.post(
            "/api/platform-ai/sessions/session-1/context-files",
            data={"files": (io.BytesIO(b"four"), "oversized.txt")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 413
    assert "per-file limit" in response.get_json()["error"]
    assert cp.patches == []
    assert not list((tmp_path / "uploads").rglob("*") if (tmp_path / "uploads").exists() else [])
