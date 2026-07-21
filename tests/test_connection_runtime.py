from shared.connection_runtime import test_http_connection as run_http_connection
from shared.connection_runtime import parse_openapi_actions
import json


def test_parse_openapi_actions_accepts_native_operation_catalog():
    schema = json.dumps(
        {
            "apiVersion": "1.0",
            "operations": [
                {
                    "operationId": "listLessonBlocksPage",
                    "method": "GET",
                    "path": "/api/agent/lesson-blocks/{lessonId}?skip={skip}&take={take}",
                },
                {
                    "operationId": "updateLessonBlock",
                    "method": "PATCH",
                    "path": "/api/agent/lesson-blocks/{blockId}",
                },
            ],
        }
    )

    assert parse_openapi_actions(schema) == [
        {
            "operation_id": "listLessonBlocksPage",
            "method": "GET",
            "path": "/api/agent/lesson-blocks/{lessonId}?skip={skip}&take={take}",
        },
        {
            "operation_id": "updateLessonBlock",
            "method": "PATCH",
            "path": "/api/agent/lesson-blocks/{blockId}",
        },
    ]


def test_http_connection_redacts_query_auth_from_returned_url(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self, _limit):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "shared.connection_runtime.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    result = run_http_connection(
        config={"base_url": "https://api.example.test"},
        auth={
            "type": "api_key",
            "name": "access_token",
            "in": "query",
            "api_key": "private-token",
        },
        schema_text="",
        payload={"method": "GET", "path": "/records"},
    )

    assert result["ok"] is True
    assert "private-token" not in result["url"]
    assert "access_token=%5BREDACTED%5D" in result["url"]


def test_http_connection_redacts_sensitive_supplied_query_params(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self, _limit):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "shared.connection_runtime.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    result = run_http_connection(
        config={"base_url": "https://api.example.test"},
        auth={"type": "none"},
        schema_text="",
        payload={
            "method": "GET",
            "path": "/records",
            "query_params": {"access_token": "private-token", "page": "2"},
        },
    )

    assert result["ok"] is True
    assert "private-token" not in result["url"]
    assert "access_token=%5BREDACTED%5D" in result["url"]
    assert "page=2" in result["url"]


def test_http_connection_injects_one_time_remote_approval_without_returning_token(monkeypatch):
    captured_headers = []

    class FakeResponse:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self, _limit):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    responses = iter(
        [
            FakeResponse(b'{"token":"remote-one-time-token"}'),
            FakeResponse(b'{"updated":true}'),
        ]
    )

    def fake_urlopen(request, **_kwargs):
        captured_headers.append(dict(request.header_items()))
        return next(responses)

    monkeypatch.setattr("shared.connection_runtime.urllib.request.urlopen", fake_urlopen)
    schema = json.dumps(
        {
            "openapi": "3.0.0",
            "paths": {
                "/approvals": {"post": {"operationId": "createApproval"}},
                "/courses/{courseId}": {"patch": {"operationId": "updateCourse"}},
            },
        }
    )
    result = run_http_connection(
        config={"base_url": "https://api.example.test"},
        auth={"type": "none"},
        schema_text=schema,
        payload={
            "operation_id": "updateCourse",
            "path_params": {"courseId": 78},
            "body_json": {"summary": "Updated draft summary"},
            "agent_approval": {
                "action": {
                    "operation_id": "createApproval",
                    "body_json": {"scope": "Course.Update"},
                },
                "response_token_field": "token",
                "inject_header": "X-GLOBEIQ-AGENT-APPROVAL",
            },
        },
    )

    assert result["ok"] is True
    assert result["agent_approval"]["operation_id"] == "createApproval"
    assert "remote-one-time-token" not in json.dumps(result)
    assert captured_headers[1]["X-globeiq-agent-approval"] == "remote-one-time-token"


def test_http_connection_rejects_html_when_json_is_required(monkeypatch):
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, _limit):
            return b"<html>fallback</html>"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "shared.connection_runtime.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    result = run_http_connection(
        config={"base_url": "https://api.example.test"},
        auth={"type": "none"},
        schema_text="",
        payload={"method": "GET", "path": "/records", "expect_json": True},
    )

    assert result["ok"] is False
    assert result["status"] == 200
    assert result["content_type"] == "text/html; charset=utf-8"
    assert "Expected a JSON response" in result["error"]
