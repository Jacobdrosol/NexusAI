"""SSE endpoint — streams live status updates to browser clients."""
from __future__ import annotations

import json
import time
from typing import Generator

from flask import Blueprint, Response, stream_with_context
from flask_login import login_required
from sqlalchemy import func

from dashboard.db import get_db
from dashboard.models import Bot, Task, Worker

bp = Blueprint("events", __name__)


def _build_snapshot() -> str:
    """Query current stats and return as an SSE-formatted data line."""
    db = get_db()
    try:
        total_workers = db.query(func.count(Worker.id)).scalar() or 0
        online_workers = db.query(func.count(Worker.id)).filter(Worker.status == "online").scalar() or 0
        offline_workers = db.query(func.count(Worker.id)).filter(Worker.status == "offline").scalar() or 0
        active_bots = db.query(func.count(Bot.id)).filter(Bot.enabled.is_(True)).scalar() or 0
        queued_tasks = db.query(func.count(Task.id)).filter(Task.status == "queued").scalar() or 0
        blocked_tasks = db.query(func.count(Task.id)).filter(Task.status == "blocked").scalar() or 0
        running_tasks = db.query(func.count(Task.id)).filter(Task.status == "running").scalar() or 0
        completed_tasks = db.query(func.count(Task.id)).filter(Task.status == "completed").scalar() or 0
        failed_tasks = db.query(func.count(Task.id)).filter(Task.status == "failed").scalar() or 0
        retried_tasks = db.query(func.count(Task.id)).filter(Task.status == "retried").scalar() or 0
        cancelled_tasks = db.query(func.count(Task.id)).filter(Task.status == "cancelled").scalar() or 0
        payload = {
            "workers": {
                "total": total_workers,
                "online": online_workers,
                "offline": offline_workers,
            },
            "bots": {"active": active_bots},
            "tasks": {
                "queued": queued_tasks,
                "blocked": blocked_tasks,
                "running": running_tasks,
                "completed": completed_tasks,
                "failed": failed_tasks,
                "retried": retried_tasks,
                "cancelled": cancelled_tasks,
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
