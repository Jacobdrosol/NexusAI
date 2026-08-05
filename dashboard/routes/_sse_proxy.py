from __future__ import annotations

import json
import queue
import threading
from typing import Callable, ContextManager, Iterator


def _coerce_heartbeat_seconds(raw: object, default: float = 15.0) -> float:
    try:
        parsed = float(raw)
    except Exception:
        parsed = default
    if parsed < 1.0:
        return 1.0
    if parsed > 120.0:
        return 120.0
    return parsed


def proxy_upstream_sse_lines(
    open_upstream: Callable[[], ContextManager[object]],
    *,
    heartbeat_seconds: float = 15.0,
) -> Iterator[str]:
    """Proxy upstream SSE lines while emitting heartbeat comments during idle periods."""
    line_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    heartbeat = _coerce_heartbeat_seconds(heartbeat_seconds)

    def _pump() -> None:
        try:
            with open_upstream() as upstream:
                raise_for_status = getattr(upstream, "raise_for_status", None)
                if callable(raise_for_status):
                    raise_for_status()
                iter_lines = getattr(upstream, "iter_lines", None)
                if not callable(iter_lines):
                    raise RuntimeError("upstream response does not support iter_lines")
                for raw_line in iter_lines(decode_unicode=True):
                    if raw_line is None:
                        continue
                    line_queue.put(("line", str(raw_line)))
        except Exception as exc:
            line_queue.put(("error", str(exc)))
        finally:
            line_queue.put(("eof", ""))

    thread = threading.Thread(target=_pump, name="sse-proxy-pump", daemon=True)
    thread.start()

    while True:
        try:
            kind, payload = line_queue.get(timeout=heartbeat)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if kind == "line":
            yield f"{payload}\n"
            continue
        if kind == "error":
            yield "event: error\n"
            yield f"data: {json.dumps({'error': str(payload)})}\n\n"
            continue
        if kind == "eof":
            return
