"""Unit tests for MCPBroker."""

import pytest


@pytest.mark.anyio
async def test_pull_context_returns_standard_shape(tmp_path):
    from control_plane.vault.mcp_broker import MCPBroker
    from control_plane.vault.vault_manager import VaultManager

    mgr = VaultManager(db_path=str(tmp_path / "vault.db"))
    await mgr.ingest_text(title="Auth API", content="JWT auth endpoints and refresh token flow.")
    broker = MCPBroker(vault_manager=mgr)

    result = await broker.pull_context(query="JWT auth", limit=3)
    assert "contexts" in result
    assert result["context_count"] >= 1
    first = result["contexts"][0]
    assert "content" in first
    assert "metadata" in first
