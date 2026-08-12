import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.api import database


class _SchemaRepository:
    async def get_schema_snapshot(self, connection_id: str):
        assert connection_id == "90"
        return {"openapi": "3.1.0", "paths": {}}


class _DatabaseEngineer:
    _connection_repo = _SchemaRepository()

    async def list_connections(self, *, enabled_only: bool):
        assert enabled_only is False
        return [
            {
                "id": 90,
                "name": "acme-agent-api",
                "connection_string": "https://example.invalid/secret",
                "config_json": {},
            }
        ]


@pytest.mark.anyio
async def test_database_connection_reads_use_request_aware_guards(monkeypatch):
    app = FastAPI()
    app.include_router(database.router)
    monkeypatch.setattr(database, "get_database_engineer", lambda: _DatabaseEngineer())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        connections = await client.get("/v1/database/connections")
        schema = await client.get("/v1/database/connections/90/schema")

    assert connections.status_code == 200
    assert connections.json()[0]["connection_string"] == "[REDACTED]"
    assert schema.status_code == 200
    assert schema.json() == {"schema": {"openapi": "3.1.0", "paths": {}}}
