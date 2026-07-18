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


@pytest.mark.anyio
async def test_worker_probe_store_lists_only_requested_worker_records(tmp_path):
    store = WorkerProbeStore(db_path=str(tmp_path / "worker-probes.db"))
    await store.record({"worker_id": "worker-1", "probe_status": "ready", "checks": []})
    await store.record({"worker_id": "removed-worker", "probe_status": "degraded", "checks": []})

    probes = await store.list_for_workers(["worker-1", "worker-2", "worker-1"])

    assert set(probes) == {"worker-1"}
    assert probes["worker-1"]["worker_id"] == "worker-1"
    assert probes["worker-1"]["probe_status"] == "ready"
    assert probes["worker-1"]["checks"] == []
    assert probes["worker-1"]["checked_at"]
