import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from nexus_worker.metrics import MetricsStore


def install_observability(app: FastAPI) -> None:
    metrics = MetricsStore()
    metrics.register_counter(
        "nexus_worker_http_requests_total",
        "Total HTTP requests handled by nexus_worker",
        ["method", "path", "status"],
    )
    metrics.register_counter(
        "nexus_worker_http_errors_total",
        "Total HTTP 5xx responses emitted by nexus_worker",
        ["method", "path", "status"],
    )
    metrics.register_histogram(
        "nexus_worker_http_request_duration_seconds",
        "nexus_worker request latency in seconds",
        ["method", "path"],
        [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    )
    metrics.register_gauge(
        "nexus_worker_inference_inflight",
        "Current number of in-flight inference requests",
        [],
    )
    app.state.metrics_store = metrics
    app.state.inference_inflight = int(getattr(app.state, "inference_inflight", 0) or 0)

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
            "nexus_worker_http_requests_total",
            {"method": method, "path": path, "status": status},
        )
        metrics.observe_histogram(
            "nexus_worker_http_request_duration_seconds",
            {"method": method, "path": path},
            elapsed,
        )
        if response.status_code >= 500:
            metrics.inc_counter(
                "nexus_worker_http_errors_total",
                {"method": method, "path": path, "status": status},
            )
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Any:
        inflight = int(getattr(request.app.state, "inference_inflight", 0) or 0)
        metrics.set_gauge("nexus_worker_inference_inflight", {}, inflight)
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")
