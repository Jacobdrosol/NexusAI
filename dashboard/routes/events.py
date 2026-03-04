"""SSE endpoint — streams live status updates to browser clients."""
from __future__ import annotations

import json
import time
from typing import Generator

from flask import Blueprint, Response, stream_with_context
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Bot, Task, Worker

bp = Blueprint("events", __name__)


def _build_snapshot() -> str:
    """Query current stats and return as an SSE-formatted data line."""
    db = get_db()
    try:
        total_workers = db.query(Worker).count()
        online_workers = db.query(Worker).filter(Worker.status == "online").count()
        offline_workers = db.query(Worker).filter(Worker.status == "offline").count()
        active_bots = db.query(Bot).filter(Bot.enabled.is_(True)).count()
        queued_tasks = db.query(Task).filter(Task.status == "queued").count()
        running_tasks = db.query(Task).filter(Task.status == "running").count()
        completed_tasks = db.query(Task).filter(Task.status == "completed").count()
        failed_tasks = db.query(Task).filter(Task.status == "failed").count()
        payload = {
            "workers": {
                "total": total_workers,
                "online": online_workers,
                "offline": offline_workers,
            },
            "bots": {"active": active_bots},
            "tasks": {
                "queued": queued_tasks,
                "running": running_tasks,
                "completed": completed_tasks,
                "failed": failed_tasks,
            },
        }
        return f"data: {json.dumps(payload)}\n\n"
    finally:
        db.close()


def _event_stream() -> Generator[str, None, None]:
    """Yield SSE frames every 5 seconds until the client disconnects."""
    while True:
        yield _build_snapshot()
        time.sleep(5)


@bp.get("/events")
@login_required
def sse_stream() -> Response:
    """SSE endpoint; clients connect once and receive periodic JSON snapshots."""
    return Response(
        stream_with_context(_event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
