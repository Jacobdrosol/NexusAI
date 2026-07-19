"""Validate that tracked files are safe to publish in the NexusAI repository."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable


_ALLOWED_RUNTIME_PATHS = {".env.example", "data/.gitkeep"}
_PUBLIC_CONFIG_EXAMPLES = {
    "config/bots/README.md",
    "config/bots/assistant_bot.yaml",
    "config/bots/example_bot.yaml",
    "config/workers/example_worker.yaml",
    "config/workers/local_worker.yaml",
}
_TRACKED_PATH_MARKERS = (
    "/.env",
    ".env",
    "/runtime/",
    "/data/",
    "/data.backup.",
)
_DATABASE_SUFFIXES = (".db", ".db-shm", ".db-wal", ".db-journal")
_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{30,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{20,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
)


def tracked_files(repo_root: Path) -> list[Path]:
    """Return repository-tracked files without reading ignored runtime state."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo_root}", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=False,
    )
    return [repo_root / Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def _has_runtime_path_marker(relative_path: str) -> bool:
    normalized_path = relative_path.replace("\\", "/").lstrip("/")
    normalized = f"/{normalized_path}"
    return any(marker in normalized for marker in _TRACKED_PATH_MARKERS)


def _is_temporary_probe_path(relative_path: str) -> bool:
    return any(part.startswith("tmp_") for part in Path(relative_path).parts)


def _is_database_path(relative_path: str) -> bool:
    return relative_path.casefold().endswith(_DATABASE_SUFFIXES)


def _is_private_config_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    if normalized in _PUBLIC_CONFIG_EXAMPLES:
        return False
    return normalized.startswith("config/bots/") or normalized.startswith("config/workers/")


def find_public_release_violations(repo_root: Path) -> list[str]:
    """Return publish-safety violations without including any matched secret text."""
    violations: list[str] = []
    for path in tracked_files(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        if relative not in _ALLOWED_RUNTIME_PATHS and _has_runtime_path_marker(relative):
            violations.append(f"tracked runtime or environment file: {relative}")
            continue
        if _is_temporary_probe_path(relative):
            violations.append(f"tracked temporary probe file: {relative}")
            continue
        if _is_database_path(relative):
            violations.append(f"tracked database file: {relative}")
            continue
        if _is_private_config_path(relative):
            violations.append(f"tracked private bot or worker config: {relative}")
            continue
        if path.is_dir() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            violations.append(f"credential-shaped value in tracked file: {relative}")
    return violations


def format_violations(violations: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in violations)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    violations = find_public_release_violations(repo_root)
    if not violations:
        print("Public release hygiene check passed.")
        return 0
    print("Public release hygiene check failed:")
    print(format_violations(violations))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
