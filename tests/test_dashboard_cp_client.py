"""Tests for dashboard control-plane client diagnostics."""

from dashboard.cp_client import CPClient


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
