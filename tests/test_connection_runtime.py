from shared.connection_runtime import test_http_connection as run_http_connection


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
