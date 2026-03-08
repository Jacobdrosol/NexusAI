from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

_DEFAULT_SUBDIRECTORIES = ("docs", "inbox", "exports", "notes")
_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}


def project_data_base_dir() -> Path:
    configured = (os.environ.get("NEXUSAI_PROJECT_DATA_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "data" / "project_data").resolve()


def ensure_project_data_layout(project_id: str) -> Path:
    root = (project_data_base_dir() / project_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in _DEFAULT_SUBDIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def resolve_project_data_path(project_id: str, relative_path: str = "") -> Path:
    root = ensure_project_data_layout(project_id)
    candidate = (root / (relative_path or "")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes project data root")
    return candidate


def create_project_data_folder(project_id: str, parent_path: str, folder_name: str) -> Path:
    safe_name = secure_filename((folder_name or "").strip())
    if not safe_name:
        raise ValueError("folder_name is required")
    parent = resolve_project_data_path(project_id, parent_path)
    parent.mkdir(parents=True, exist_ok=True)
    target = (parent / safe_name).resolve()
    if target != parent and parent not in target.parents:
        raise ValueError("folder path escapes parent")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _sanitize_upload_relative_path(relative_path: str, fallback_name: str) -> Path:
    raw = (relative_path or "").replace("\\", "/").strip("/")
    raw_parts = [part for part in raw.split("/") if part and part not in {".", ".."}]
    safe_parts = [secure_filename(part) for part in raw_parts]
    safe_parts = [part for part in safe_parts if part]
    if safe_parts:
        return Path(*safe_parts)
    fallback = secure_filename(fallback_name or "")
    if not fallback:
        raise ValueError("file is required")
    return Path(fallback)


def _dedupe_target_path(target: Path) -> Path:
    if not target.exists():
        return target
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    index = 1
    while True:
        candidate = parent / f"({index}) {stem}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def save_project_data_upload(
    project_id: str,
    target_path: str,
    storage: FileStorage,
    *,
    relative_path: str = "",
) -> Path:
    relative_target = _sanitize_upload_relative_path(relative_path, storage.filename or "")
    target_dir = resolve_project_data_path(project_id, target_path)
    target = (target_dir / relative_target).resolve()
    if target_dir != target and target_dir not in target.parents:
        raise ValueError("upload path escapes target")
    target = _dedupe_target_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    storage.save(target)
    return target


def delete_project_data_path(project_id: str, relative_path: str) -> dict[str, Any]:
    target = resolve_project_data_path(project_id, relative_path)
    root = ensure_project_data_layout(project_id)
    if target == root:
        raise ValueError("cannot delete project data root")
    if not target.exists():
        raise ValueError("path not found")
    if target.is_dir():
        shutil.rmtree(target)
        return {"path": relative_path, "type": "directory"}
    target.unlink()
    return {"path": relative_path, "type": "file"}


def delete_project_data_paths(project_id: str, relative_paths: list[str]) -> list[dict[str, Any]]:
    cleaned = sorted(
        {
            str(path or "").strip().strip("/")
            for path in relative_paths
            if str(path or "").strip().strip("/")
        },
        key=lambda item: (item.count("/"), len(item)),
        reverse=True,
    )
    if not cleaned:
        raise ValueError("at least one path is required")
    deleted: list[dict[str, Any]] = []
    for path in cleaned:
        deleted.append(delete_project_data_path(project_id, path))
    return deleted


def build_project_data_tree(project_id: str, max_depth: int = 6, max_entries: int = 500) -> dict[str, Any]:
    root = ensure_project_data_layout(project_id)
    seen = {"count": 0}

    def walk(path: Path, depth: int) -> dict[str, Any]:
        rel_path = path.relative_to(root).as_posix() if path != root else ""
        node: dict[str, Any] = {
            "name": path.name if path != root else project_id,
            "path": rel_path,
            "type": "directory" if path.is_dir() else "file",
        }
        stat = path.stat()
        node["modified_at"] = stat.st_mtime
        if path.is_file():
            node["size"] = stat.st_size
            return node

        node["children"] = []
        if depth >= max_depth or seen["count"] >= max_entries:
            return node

        children = sorted(
            [p for p in path.iterdir() if p.name not in _SKIP_DIRECTORIES],
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
        for child in children:
            if seen["count"] >= max_entries:
                break
            seen["count"] += 1
            node["children"].append(walk(child, depth + 1))
        return node

    return walk(root, 0)


def list_project_data_files(project_id: str) -> list[dict[str, Any]]:
    root = ensure_project_data_layout(project_id)
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRECTORIES for part in path.parts):
            continue
        rel_path = path.relative_to(root).as_posix()
        stat = path.stat()
        rows.append(
            {
                "path": rel_path,
                "type": "directory" if path.is_dir() else "file",
                "size": stat.st_size if path.is_file() else None,
                "modified_at": stat.st_mtime,
            }
        )
    return rows
