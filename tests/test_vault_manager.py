"""Unit tests for VaultManager."""

import pytest

from shared.exceptions import VaultItemNotFoundError


@pytest.mark.anyio
async def test_ingest_and_get_item(tmp_path):
    from control_plane.vault.vault_manager import VaultManager

    mgr = VaultManager(db_path=str(tmp_path / "vault.db"))
    item = await mgr.ingest_text(
        title="Spec",
        content="NexusAI has a distributed control plane and workers.",
        namespace="global",
    )
    fetched = await mgr.get_item(item.id)
    chunks = await mgr.list_chunks(item.id)
    assert fetched.title == "Spec"
    assert len(chunks) >= 1


@pytest.mark.anyio
async def test_search_returns_ranked_results(tmp_path):
    from control_plane.vault.vault_manager import VaultManager

    mgr = VaultManager(db_path=str(tmp_path / "vault.db"))
    await mgr.ingest_text(title="Doc A", content="Python FastAPI service orchestration")
    await mgr.ingest_text(title="Doc B", content="Gardening tomatoes and plants")

    results = await mgr.search(query="FastAPI service", limit=2)
    assert len(results) >= 1
    assert results[0]["title"] == "Doc A"


@pytest.mark.anyio
async def test_missing_item_raises(tmp_path):
    from control_plane.vault.vault_manager import VaultManager

    mgr = VaultManager(db_path=str(tmp_path / "vault.db"))
    with pytest.raises(VaultItemNotFoundError):
        await mgr.get_item("missing")
