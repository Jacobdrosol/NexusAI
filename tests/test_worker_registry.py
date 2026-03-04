"""Unit tests for WorkerRegistry."""
import pytest
from shared.models import Worker
from shared.exceptions import WorkerNotFoundError


@pytest.mark.anyio
async def test_register_and_get():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    w = Worker(id="w1", name="Worker 1", host="localhost", port=8001, capabilities=[])
    await reg.register(w)
    result = await reg.get("w1")
    assert result.id == "w1"


@pytest.mark.anyio
async def test_get_not_found():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    with pytest.raises(WorkerNotFoundError):
        await reg.get("nonexistent")


@pytest.mark.anyio
async def test_list_workers():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.register(Worker(id="w2", name="W2", host="h2", port=8001, capabilities=[]))
    workers = await reg.list()
    assert len(workers) == 2


@pytest.mark.anyio
async def test_update_status():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.update_status("w1", "online")
    w = await reg.get("w1")
    assert w.status == "online"


@pytest.mark.anyio
async def test_remove_worker():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[]))
    await reg.remove("w1")
    with pytest.raises(WorkerNotFoundError):
        await reg.get("w1")


@pytest.mark.anyio
async def test_heartbeat_updates_status():
    from control_plane.registry.worker_registry import WorkerRegistry
    reg = WorkerRegistry()
    await reg.register(Worker(id="w1", name="W1", host="h1", port=8001, capabilities=[], status="offline"))
    await reg.update_heartbeat("w1")
    w = await reg.get("w1")
    assert w.status == "online"
