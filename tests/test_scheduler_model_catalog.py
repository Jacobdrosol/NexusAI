"""Tests for scheduler model-catalog checks."""

from unittest.mock import AsyncMock

import pytest

from shared.exceptions import BackendError
from shared.models import BackendConfig


@pytest.mark.anyio
async def test_validate_model_passes_when_catalog_empty():
    from control_plane.scheduler.scheduler import Scheduler

    model_registry = AsyncMock()
    model_registry.has_any.return_value = False
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        model_registry=model_registry,
    )
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai")

    await scheduler._validate_model_if_catalog_present(backend)


@pytest.mark.anyio
async def test_validate_model_raises_for_missing_catalog_model():
    from control_plane.scheduler.scheduler import Scheduler

    model_registry = AsyncMock()
    model_registry.has_any.return_value = True
    model_registry.exists.return_value = False
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        model_registry=model_registry,
    )
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai")

    with pytest.raises(BackendError):
        await scheduler._validate_model_if_catalog_present(backend)


@pytest.mark.anyio
async def test_validate_model_skips_catalog_for_declared_http_connection():
    from control_plane.scheduler.scheduler import Scheduler

    model_registry = AsyncMock()
    model_registry.has_any.return_value = True
    model_registry.exists.return_value = False
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        model_registry=model_registry,
    )
    backend = BackendConfig(
        type="custom",
        provider="http_connection",
        model="declared-catalog-api",
    )

    await scheduler._validate_model_if_catalog_present(backend)

    model_registry.has_any.assert_not_awaited()
    model_registry.exists.assert_not_awaited()
