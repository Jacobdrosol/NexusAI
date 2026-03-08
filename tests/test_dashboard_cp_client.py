"""Tests for dashboard control-plane client diagnostics."""

from unittest.mock import Mock, patch

from dashboard.cp_client import CPClient, _CHAT_TIMEOUT, _INGEST_TIMEOUT


def test_unavailable_reason_for_unauthorized():
    cp = CPClient(base_url="http://example.invalid", timeout=0.1)
    cp._record_error(method="GET", path="/v1/projects", status_code=401, detail="unauthorized")
    msg = cp.unavailable_reason()
    assert "401" in msg
    assert "CONTROL_PLANE_API_TOKEN" in msg


def test_unavailable_reason_for_network_error():
    cp = CPClient(base_url="http://example.invalid", timeout=0.1)
    cp._record_error(method="GET", path="/v1/projects", status_code=None, detail="timed out")
    msg = cp.unavailable_reason()
    assert "CONTROL_PLANE_URL" in msg


def test_probe_paths_reports_status_codes_and_errors():
    cp = CPClient(base_url="http://example.invalid", timeout=0.1)

    ok_resp = Mock()
    ok_resp.status_code = 200
    ok_resp.text = '{"status":"ok"}'

    unauthorized_resp = Mock()
    unauthorized_resp.status_code = 401
    unauthorized_resp.text = '{"detail":"unauthorized"}'

    with patch("dashboard.cp_client.requests.get", side_effect=[ok_resp, unauthorized_resp, RuntimeError("timed out")]):
        results = cp.probe_paths(["/health", "/v1/projects", "/v1/workers"])

    assert results[0]["path"] == "/health"
    assert results[0]["ok"] is True
    assert results[0]["status_code"] == 200
    assert "ok" in results[0]["detail"]

    assert results[1]["path"] == "/v1/projects"
    assert results[1]["ok"] is False
    assert results[1]["status_code"] == 401
    assert "unauthorized" in results[1]["detail"]

    assert results[2]["path"] == "/v1/workers"
    assert results[2]["ok"] is False
    assert results[2]["status_code"] is None
    assert "timed out" in results[2]["detail"]


def test_post_message_uses_chat_timeout():
    cp = CPClient(base_url="http://example.invalid", timeout=0.1)

    ok_resp = Mock()
    ok_resp.raise_for_status.return_value = None
    ok_resp.text = '{"assistant_message":{"id":"m1"}}'
    ok_resp.json.return_value = {"assistant_message": {"id": "m1"}}

    with patch("dashboard.cp_client.requests.post", return_value=ok_resp) as post_mock:
        cp.post_message("conv-1", {"content": "hello"})

    assert post_mock.call_args.kwargs["timeout"] == _CHAT_TIMEOUT


def test_project_context_sync_uses_ingest_timeout():
    cp = CPClient(base_url="http://example.invalid", timeout=0.1)

    ok_resp = Mock()
    ok_resp.raise_for_status.return_value = None
    ok_resp.text = '{"status":"ok"}'
    ok_resp.json.return_value = {"status": "ok"}

    with patch("dashboard.cp_client.requests.post", return_value=ok_resp) as post_mock:
        cp.sync_project_github_context(project_id="proj-1", sync_mode="full")

    assert post_mock.call_args.kwargs["timeout"] == _INGEST_TIMEOUT
    assert post_mock.call_args.kwargs["json"]["sync_mode"] == "full"
