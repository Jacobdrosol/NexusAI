"""Unit tests for ModelRegistry."""

import pytest

from shared.exceptions import CatalogModelNotFoundError
from shared.models import CatalogModel


@pytest.mark.anyio
async def test_register_and_get(tmp_path):
    from control_plane.registry.model_registry import ModelRegistry

    reg = ModelRegistry(db_path=str(tmp_path / "models.db"))
    m = CatalogModel(id="openai-gpt-4o-mini", name="gpt-4o-mini", provider="openai")
    await reg.register(m)
    result = await reg.get("openai-gpt-4o-mini")
    assert result.name == "gpt-4o-mini"


@pytest.mark.anyio
async def test_get_not_found(tmp_path):
    from control_plane.registry.model_registry import ModelRegistry

    reg = ModelRegistry(db_path=str(tmp_path / "models.db"))
    with pytest.raises(CatalogModelNotFoundError):
        await reg.get("missing-model")


@pytest.mark.anyio
async def test_exists_checks_provider_and_model(tmp_path):
    from control_plane.registry.model_registry import ModelRegistry

    reg = ModelRegistry(db_path=str(tmp_path / "models.db"))
    await reg.register(CatalogModel(id="g1", name="gpt-4o-mini", provider="openai"))
    assert await reg.exists("openai", "gpt-4o-mini") is True
    assert await reg.exists("openai", "gpt-4.1") is False
