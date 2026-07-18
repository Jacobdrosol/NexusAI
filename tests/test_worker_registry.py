"""Unit tests for the persistent WorkerRegistry."""
from datetime import datetime, timezone

import pytest

from control_plane.registry.worker_registry import WorkerRegistry
from shared.exceptions import WorkerNotFoundError
from shared.models import Worker


def _registry(tmp_path):
    return WorkerRegistry(db_path=str(tmp_path / "workers.db"))


@pytest.mark.anyio
async def test_register_and_get(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="Worker 1", host="localhost", port=8001, capabilities=[]))

    result = await reg.get("w1")

    assert result.id == "w1"
    assert result.last_heartbeat_at is not None


@pytest.mark.anyio
async def test_get_not_found(tmp_path):
    reg = _registry(tmp_path)

    with pytest.raises(WorkerNotFoundError):
        await reg.get("nonexistent")


@pytest.mark.anyio
async def test_list_workers(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.register(Worker(id="w2", name="W2", host="h2", port=8001, capabilities=[]))

    workers = await reg.list()

    assert {worker.id for worker in workers} == {"w1", "w2"}


@pytest.mark.anyio
async def test_update_status(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.update_status("w1", "online")

    assert (await reg.get("w1")).status == "online"


@pytest.mark.anyio
async def test_remove_worker(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.remove("w1")

    with pytest.raises(WorkerNotFoundError):
        await reg.get("w1")


@pytest.mark.anyio
async def test_heartbeat_updates_status(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[], status="offline"))
    await reg.update_heartbeat("w1")

    worker = await reg.get("w1")

    assert worker.status == "online"
    assert worker.last_heartbeat_at is not None


@pytest.mark.anyio
async def test_update_preserves_registry_owned_heartbeat_timestamp(tmp_path):
    reg = _registry(tmp_path)
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    before = (await reg.get("w1")).last_heartbeat_at
    assert before is not None

    await reg.update(
        "w1",
        Worker(
            id="w1",
            name="Renamed",
            host="h1",
            port=8001,
            capabilities=[],
            last_heartbeat_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ),
    )

    assert (await reg.get("w1")).last_heartbeat_at == before


@pytest.mark.anyio
async def test_registration_survives_a_new_registry_instance(tmp_path):
    db_path = str(tmp_path / "workers.db")
    first = _registry(tmp_path)
    await first.register(Worker(id="persisted", name="Persisted", host="h1", port=8001, capabilities=[]))

    restored = WorkerRegistry(db_path=db_path)
    worker = await restored.get("persisted")

    assert worker.id == "persisted"
    assert worker.last_heartbeat_at is not None


@pytest.mark.anyio
async def test_provisioned_worker_starts_offline_and_persists(tmp_path):
    reg = _registry(tmp_path)
    await reg.provision(Worker(id="provisioned", name="Provisioned", host="h1", port=8001, capabilities=[], status="online"))

    worker = await reg.get("provisioned")
    restored = WorkerRegistry(db_path=str(tmp_path / "workers.db"))

    assert worker.status == "offline"
    assert worker.last_heartbeat_at is None
    assert (await restored.get("provisioned")).status == "offline"
