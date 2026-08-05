import json
import time

from dashboard.routes._sse_proxy import proxy_upstream_sse_lines


def test_proxy_upstream_sse_lines_emits_heartbeat_during_idle_gap():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=True):
            yield "event: status"
            yield 'data: {"label":"running"}'
            time.sleep(1.2)
            yield "event: done"
            yield "data: {}"

    lines = list(proxy_upstream_sse_lines(lambda: FakeResponse(), heartbeat_seconds=1))
    assert any(line.startswith(": keepalive") for line in lines)
    assert "event: done\n" in lines
    assert "data: {}\n" in lines


def test_proxy_upstream_sse_lines_surfaces_upstream_errors():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            raise RuntimeError("upstream failed")

        def iter_lines(self, decode_unicode=True):
            yield "event: ignored"

    lines = list(proxy_upstream_sse_lines(lambda: FakeResponse(), heartbeat_seconds=1))
    assert lines[0] == "event: error\n"
    assert "upstream failed" in lines[1]


def test_proxy_upstream_sse_lines_json_escapes_error_payloads():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            raise RuntimeError('upstream failed on "chat"\npath C:\\tmp')

        def iter_lines(self, decode_unicode=True):
            yield "event: ignored"

    lines = list(proxy_upstream_sse_lines(lambda: FakeResponse(), heartbeat_seconds=1))
    assert lines[0] == "event: error\n"
    payload = json.loads(lines[1].removeprefix("data: ").strip())
    assert payload["error"] == 'upstream failed on "chat"\npath C:\\tmp'
