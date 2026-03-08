from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dashboard.cp_client import get_cp_client
from dashboard.project_data import ensure_project_data_layout

_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".tgz", ".7z", ".exe", ".dll", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".mov", ".avi", ".class", ".jar", ".pyc",
}

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        yield path


def _read_text(path: Path) -> str | None:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return None


def _set_job(job: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        _JOBS[str(job["job_id"])] = dict(job)
        return dict(job)


def latest_job_for_project(project_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        jobs = [job for job in _JOBS.values() if str(job.get("project_id")) == str(project_id)]
    if not jobs:
        return None
    jobs.sort(key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)
    return dict(jobs[0])


def start_project_data_ingest(project_id: str, namespace: Optional[str] = None, max_bytes: int = 200_000) -> dict[str, Any]:
    existing = latest_job_for_project(project_id)
    if existing and existing.get("status") in {"queued", "running"}:
        return existing

    job = _set_job(
        {
            "job_id": f"project-data-{project_id}-{uuid.uuid4().hex[:10]}",
            "project_id": project_id,
            "namespace": namespace or f"project:{project_id}:data",
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "counts": {"discovered": 0, "ingested": 0, "skipped": 0, "failed": 0},
            "current_path": None,
            "errors": [],
        }
    )

    def _runner() -> None:
        root = ensure_project_data_layout(project_id)
        cp = get_cp_client()
        runtime = _set_job({**job, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        counts = dict(runtime["counts"])
        errors: list[str] = []
        try:
            for path in _iter_files(root):
                counts["discovered"] += 1
                relative_path = path.relative_to(root).as_posix()
                runtime = _set_job({**runtime, "counts": counts, "current_path": relative_path})
                size = path.stat().st_size
                if size > max_bytes:
                    counts["skipped"] += 1
                    runtime = _set_job({**runtime, "counts": counts})
                    continue

                text = _read_text(path)
                if not text or not text.strip():
                    counts["skipped"] += 1
                    runtime = _set_job({**runtime, "counts": counts})
                    continue

                payload = {
                    "title": f"{project_id}:{relative_path}",
                    "content": text,
                    "namespace": runtime["namespace"],
                    "project_id": project_id,
                    "source_type": "file",
                    "source_ref": f"project-data://{project_id}/{relative_path}",
                    "metadata": {
                        "ingest_kind": "project_data_file",
                        "relative_path": relative_path,
                        "size": size,
                    },
                }
                result = cp.upsert_vault_item(payload)
                if result is None:
                    counts["failed"] += 1
                    err = cp.last_error()
                    errors.append(f"{relative_path}: {str((err or {}).get('detail') or 'upsert failed')}")
                else:
                    counts["ingested"] += 1
                runtime = _set_job({**runtime, "counts": counts, "errors": errors[-20:]})

            _set_job(
                {
                    **runtime,
                    "status": "completed" if counts["failed"] == 0 else "completed_with_errors",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "current_path": None,
                    "counts": counts,
                    "errors": errors[-20:],
                }
            )
        except Exception as exc:
            _set_job(
                {
                    **runtime,
                    "status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "current_path": runtime.get("current_path"),
                    "counts": counts,
                    "errors": (errors + [str(exc)])[-20:],
                }
            )

    thread = threading.Thread(target=_runner, name=f"project-data-ingest-{project_id}", daemon=True)
    thread.start()
    return job
