from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable

import requests

from dashboard.project_data import ensure_project_data_layout

_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".tgz", ".7z", ".exe", ".dll", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".mov", ".avi", ".class", ".jar", ".pyc",
}


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest project data files into the NexusAI vault.")
    parser.add_argument("--project-id", required=True, help="Project ID to ingest for.")
    parser.add_argument("--namespace", default=None, help="Vault namespace. Defaults to project:<id>:data")
    parser.add_argument("--control-plane-url", default=os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000"))
    parser.add_argument("--api-token", default=os.environ.get("CONTROL_PLANE_API_TOKEN"))
    parser.add_argument("--max-bytes", type=int, default=200_000, help="Skip files larger than this size.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ingested without sending requests.")
    args = parser.parse_args()

    project_root = ensure_project_data_layout(args.project_id)
    namespace = args.namespace or f"project:{args.project_id}:data"
    base_url = args.control_plane_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if args.api_token:
        headers["X-Nexus-API-Key"] = args.api_token

    ingested = 0
    skipped = 0
    failed = 0

    print(f"Project data root: {project_root}")
    print(f"Namespace: {namespace}")
    print(f"Control plane: {base_url}")

    for path in _iter_files(project_root):
        relative_path = path.relative_to(project_root).as_posix()
        size = path.stat().st_size
        if size > args.max_bytes:
            skipped += 1
            print(f"SKIP  {relative_path} ({size} bytes exceeds limit)")
            continue

        text = _read_text(path)
        if not text or not text.strip():
            skipped += 1
            print(f"SKIP  {relative_path} (empty or unreadable)")
            continue

        payload = {
            "title": f"{args.project_id}:{relative_path}",
            "content": text,
            "namespace": namespace,
            "project_id": args.project_id,
            "source_type": "file",
            "source_ref": f"project-data://{args.project_id}/{relative_path}",
            "metadata": {
                "ingest_kind": "project_data_file",
                "relative_path": relative_path,
                "size": size,
            },
        }

        if args.dry_run:
            ingested += 1
            print(f"DRY   {relative_path}")
            continue

        response = requests.post(f"{base_url}/v1/vault/items", headers=headers, data=json.dumps(payload), timeout=60)
        if 200 <= response.status_code < 300:
            ingested += 1
            print(f"OK    {relative_path}")
        else:
            failed += 1
            detail = response.text.strip()
            print(f"FAIL  {relative_path} ({response.status_code}) {detail[:300]}")

    print(f"Done. ingested={ingested} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
