"""Work overview blueprint for project and manager operational visibility."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from dashboard.cp_client import get_cp_client
from dashboard.work_overview import build_work_overview

bp = Blueprint("work", __name__)


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        fallback_kwargs = {key: value for key, value in kwargs.items() if key != "timeout"}
        try:
            return fn(*args, **fallback_kwargs)
        except Exception:
            return None
    except Exception:
        return None


def _load_work_overview() -> dict[str, Any]:
    cp = get_cp_client()
    tasks = _safe_call(cp.list_tasks, limit=500, include_content=False, timeout=1.5) or []
    projects = _safe_call(cp.list_projects, timeout=1.0) or []
    bots = _safe_call(cp.list_bots, timeout=1.0) or []
    workers = _safe_call(cp.list_workers, timeout=1.0) or []
    overview = build_work_overview(tasks=tasks, projects=projects, bots=bots, workers=workers)
    task_usage = getattr(cp, "task_usage", None)
    overview["usage"] = _safe_call(task_usage, hours=24, limit_bots=25, timeout=1.5) if callable(task_usage) else None
    return overview


@bp.get("/work")
@login_required
def work_page() -> str:
    return render_template("work.html", overview=_load_work_overview(), error=None)


@bp.get("/api/work/overview")
@login_required
def api_work_overview():
    return jsonify(_load_work_overview())
