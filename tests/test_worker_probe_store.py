import pytest

from control_plane.worker_probe_store import WorkerProbeStore


@pytest.mark.anyio
async def test_worker_probe_store_persists_latest_result(tmp_path):
    db_path = str(tmp_path / "worker-probes.db")
    store = WorkerProbeStore(db_path=db_path)
    first = {
        "worker_id": "worker-1",
        "checked_at": "2026-07-18T00:00:00+00:00",
        "probe_status": "degraded",
        "checks": [],
    }
    latest = {
        "worker_id": "worker-1",
        "checked_at": "2026-07-18T00:05:00+00:00",
        "probe_status": "ready",
        "checks": [{"name": "health", "status": "pass", "detail": "ok"}],
    }

    await store.record(first)
    await store.record(latest)

    restored = await WorkerProbeStore(db_path=db_path).get("worker-1")

    assert restored == latest


@pytest.mark.anyio
async def test_worker_probe_store_rejects_missing_worker_id(tmp_path):
    store = WorkerProbeStore(db_path=str(tmp_path / "worker-probes.db"))

    with pytest.raises(ValueError, match="missing worker_id"):
        await store.record({"probe_status": "ready"})
