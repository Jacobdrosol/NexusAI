#!/usr/bin/env python3
"""Guard against private use-case identifiers leaking into the public repo.

Scans tracked files for a configurable list of private identifiers
(project names, domains, product names, etc.) and exits non-zero if any
are found. Run as a pre-commit hook and in CI so private details can
never be committed to the public framework repo.

Configure the identifier list in one of:
  - .private-identifiers (one per line, plain text)
  - .private-identifiers.json ({"identifiers": [...]})
  - the PRIVATE_IDENTIFIERS env var (comma-separated)

If no list is configured, the guard is a no-op (exit 0) so the repo
remains usable by anyone without their own private terms.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Defaults that are safe to ship (generic placeholders). Operators should
# add their own private identifiers to .private-identifiers.
_DEFAULT_IDENTIFIERS: list[str] = []


def _load_identifiers() -> list[str]:
    env = os.environ.get("PRIVATE_IDENTIFIERS", "").strip()
    if env:
        return [part.strip() for part in env.split(",") if part.strip()]

    json_path = ROOT / ".private-identifiers.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            raw = data.get("identifiers", []) if isinstance(data, dict) else []
            return [str(item).strip() for item in raw if str(item).strip()]
        except Exception:
            pass

    txt_path = ROOT / ".private-identifiers"
    if txt_path.exists():
        return [
            line.strip()
            for line in txt_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    return list(_DEFAULT_IDENTIFIERS)


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return []
    return [f for f in out.stdout.split("\0") if f]


def _scan_file(path: Path, identifiers: list[str]) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lowered = content.lower()
    hits: list[str] = []
    for ident in identifiers:
        needle = ident.lower()
        if not needle:
            continue
        if needle in lowered:
            hits.append(ident)
    return hits


def main() -> int:
    identifiers = _load_identifiers()
    if not identifiers:
        print("[private-identifiers] no identifiers configured; skipping")
        return 0

    files = _tracked_files()
    # The config files themselves legitimately contain the identifiers.
    skip = {".private-identifiers", ".private-identifiers.json"}
    failures: list[tuple[str, list[str]]] = []
    for rel in files:
        if rel in skip:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        hits = _scan_file(path, identifiers)
        if hits:
            failures.append((rel, hits))

    if failures:
        print("[private-identifiers] BLOCKED: private identifiers found in tracked files:")
        for rel, hits in failures:
            print(f"  {rel}: {', '.join(sorted(set(hits)))}")
        print(
            "\nRemove these references before committing. If a match is a false "
            "positive, remove the identifier from .private-identifiers."
        )
        return 1

    print(f"[private-identifiers] clean ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
