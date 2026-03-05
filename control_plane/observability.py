import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from shared.observability import MetricsStore


def install_observability(app: FastAPI) -> None:
    metrics = MetricsStore()
    metrics.register_counter(
        "nexus_control_plane_http_requests_total",
        "Total HTTP requests handled by control plane",
        ["method", "path", "status"],
    )
    metrics.register_counter(
        "nexus_control_plane_http_errors_total",
        "Total HTTP 5xx responses emitted by control plane",
        ["method", "path", "status"],
    )
    metrics.register_histogram(
        "nexus_control_plane_http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["method", "path"],
        [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
    )
    metrics.register_gauge(
        "nexus_control_plane_tasks_by_status",
        "Current number of tasks by status",
        ["status"],
    )
    metrics.register_gauge(
        "nexus_control_plane_workers_queue_depth",
        "Current worker queue depth",
        ["worker_id"],
    )
    metrics.register_gauge(
        "nexus_control_plane_workers_load",
        "Current worker load percentage",
        ["worker_id"],
    )
    metrics.register_gauge(
        "nexus_control_plane_scheduler_worker_latency_ms",
        "Scheduler EMA latency estimate for worker dispatch",
        ["worker_id"],
    )
    metrics.register_gauge(
        "nexus_control_plane_scheduler_worker_inflight",
        "Scheduler in-flight dispatch count per worker",
        ["worker_id"],
    )
    app.state.metrics_store = metrics

    @app.middleware("http")
    async def _http_metrics_middleware(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        method = request.method.upper()
        status = str(response.status_code)
        elapsed = time.perf_counter() - started
        metrics.inc_counter(
            "nexus_control_plane_http_requests_total",
            {"method": method, "path": path, "status": status},
        )
        metrics.observe_histogram(
            "nexus_control_plane_http_request_duration_seconds",
            {"method": method, "path": path},
            elapsed,
        )
        if response.status_code >= 500:
            metrics.inc_counter(
                "nexus_control_plane_http_errors_total",
                {"method": method, "path": path, "status": status},
            )
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Any:
        task_manager = getattr(request.app.state, "task_manager", None)
        worker_registry = getattr(request.app.state, "worker_registry", None)
        scheduler = getattr(request.app.state, "scheduler", None)

        if task_manager:
            statuses = ["queued", "blocked", "running", "completed", "failed"]
            counts = {s: 0 for s in statuses}
            tasks = await task_manager.list_tasks()
            for task in tasks:
                if task.status in counts:
                    counts[task.status] += 1
            for status, count in counts.items():
                metrics.set_gauge("nexus_control_plane_tasks_by_status", {"status": status}, count)

        if worker_registry:
            workers = await worker_registry.list()
            for worker in workers:
                queue_depth = int(getattr(worker.metrics, "queue_depth", 0) or 0)
                load = float(getattr(worker.metrics, "load", 0.0) or 0.0)
                metrics.set_gauge(
                    "nexus_control_plane_workers_queue_depth",
                    {"worker_id": worker.id},
                    queue_depth,
                )
                metrics.set_gauge(
                    "nexus_control_plane_workers_load",
                    {"worker_id": worker.id},
                    load,
                )

        if scheduler and hasattr(scheduler, "get_worker_runtime_metrics"):
            runtime = scheduler.get_worker_runtime_metrics()
            for worker_id, row in runtime.items():
                metrics.set_gauge(
                    "nexus_control_plane_scheduler_worker_latency_ms",
                    {"worker_id": worker_id},
                    float(row.get("latency_ema_ms", 0.0)),
                )
                metrics.set_gauge(
                    "nexus_control_plane_scheduler_worker_inflight",
                    {"worker_id": worker_id},
                    float(row.get("inflight", 0.0)),
                )

        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

