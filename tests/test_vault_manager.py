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


@pytest.mark.anyio
async def test_upsert_reuses_existing_source_ref(tmp_path):
    from control_plane.vault.vault_manager import VaultManager

    mgr = VaultManager(db_path=str(tmp_path / "vault.db"))
    first = await mgr.upsert_text(
        title="Doc",
        content="v1",
        namespace="project:test:data",
        project_id="test",
        source_ref="project-data://test/docs/readme.md",
    )
    second = await mgr.upsert_text(
        title="Doc",
        content="v2",
        namespace="project:test:data",
        project_id="test",
        source_ref="project-data://test/docs/readme.md",
    )
    items = await mgr.list_items(project_id="test", limit=20)
    assert first.id == second.id
    assert len(items) == 1
    assert items[0].content == "v2"
