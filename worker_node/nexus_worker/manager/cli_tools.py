"""CLI tool discovery helpers for nexus_worker."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict, List


_KNOWN_CLI_TOOLS: list[dict[str, Any]] = [
    {
        "name": "codex",
        "version_args": ["--version"],
        "requires_approval": True,
        "approval_hints": [
            "Run `codex login` on the worker node before enabling write-capable tasks.",
            "Review repository trust and sandbox settings before routing coding tasks here.",
        ],
    },
    {
        "name": "gh",
        "version_args": ["--version"],
        "requires_approval": True,
        "approval_hints": [
            "Run `gh auth login` or configure a GitHub token before using repository workflows.",
        ],
    },
    {
        "name": "git",
        "version_args": ["--version"],
        "requires_approval": False,
        "approval_hints": [],
    },
    {
        "name": "python",
        "version_args": ["--version"],
        "requires_approval": False,
        "approval_hints": [],
    },
    {
        "name": "node",
        "version_args": ["--version"],
        "requires_approval": False,
        "approval_hints": [],
    },
    {
        "name": "npm",
        "version_args": ["--version"],
        "requires_approval": False,
        "approval_hints": [],
    },
    {
        "name": "docker",
        "version_args": ["--version"],
        "requires_approval": True,
        "approval_hints": [
            "Ensure the worker service account is allowed to access the Docker daemon before enabling Docker-backed tasks.",
        ],
    },
    {
        "name": "ollama",
        "version_args": ["--version"],
        "requires_approval": False,
        "approval_hints": [],
    },
]


def _read_version(command: str, version_args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            [command, *version_args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    first_line = out.splitlines()[0].strip()
    return first_line[:200]


def discover_cli_tools() -> list[dict[str, Any]]:
    """Return detected CLI tools and their approval metadata."""
    discovered: list[dict[str, Any]] = []
    for tool in _KNOWN_CLI_TOOLS:
        path = shutil.which(str(tool["name"]))
        if not path:
            continue
        discovered.append(
            {
                "name": tool["name"],
                "path": path,
                "version": _read_version(str(tool["name"]), list(tool.get("version_args") or ["--version"])),
                "requires_approval": bool(tool.get("requires_approval", False)),
                "approval_hints": list(tool.get("approval_hints") or []),
            }
        )
    return discovered

