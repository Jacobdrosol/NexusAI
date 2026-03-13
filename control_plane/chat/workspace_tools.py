from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_IGNORE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".next",
}

_BLOCKED_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
    ".pyc",
    ".pyd",
    ".class",
    ".jar",
    ".sqlite",
    ".db",
}
_CODE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".cs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".rs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
}
_DOC_SUFFIXES = {
    ".md",
    ".rst",
    ".txt",
    ".adoc",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
}
_STOP_TERMS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "through",
    "able",
    "ability",
    "using",
    "within",
    "application",
    "project",
    "projects",
    "repo",
    "repository",
    "search",
    "read",
    "files",
    "context",
    "connection",
    "ensure",
    "testing",
    "test",
    "feature",
    "goal",
    "forward",
    "would",
    "like",
    "want",
    "looking",
    "gather",
    "information",
    "move",
}

_WINDOWS_PATH_RE = re.compile(r"([A-Za-z]:\\[^\s\"']+)")
_POSIX_PATH_RE = re.compile(r"((?:\./|\../|/)[^\s\"']+)")
_GENERIC_PATH_RE = re.compile(r"([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+)")


def normalize_workspace_root(raw: str | None) -> Path | None:
    candidate = str(raw or "").strip()
    if not candidate:
        return None
    try:
        path = Path(candidate).expanduser().resolve()
    except Exception:
        return None
    if not path.exists() or not path.is_dir():
        return None
    return path


def _is_probably_text_file(path: Path, max_file_bytes: int) -> bool:
    if path.suffix.lower() in _BLOCKED_SUFFIXES:
        return False
    try:
        size = int(path.stat().st_size)
    except Exception:
        return False
    if size <= 0 or size > max_file_bytes:
        return False
    try:
        with path.open("rb") as handle:
            sample = handle.read(2048)
    except Exception:
        return False
    if b"\x00" in sample:
        return False
    return True


def _clean_path_hint(text: str) -> str:
    value = str(text or "").strip()
    while value and value[-1] in {".", ",", ";", ")", "]", "}", "\"", "'"}:
        value = value[:-1]
    while value and value[0] in {"(", "[", "{", "\"", "'"}:
        value = value[1:]
    return value.strip()


def extract_path_hints(query: str, *, limit: int = 8) -> list[str]:
    raw = str(query or "")
    candidates: list[str] = []
    for regex in (_WINDOWS_PATH_RE, _POSIX_PATH_RE, _GENERIC_PATH_RE):
        for match in regex.findall(raw):
            cleaned = _clean_path_hint(match)
            if cleaned:
                candidates.append(cleaned)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _safe_resolve_under_root(root: Path, path_hint: str) -> Path | None:
    try:
        candidate = Path(path_hint)
    except Exception:
        return None
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except Exception:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def read_workspace_file_snippet(
    root: Path,
    path_hint: str,
    *,
    max_file_bytes: int = 200_000,
    max_chars: int = 4_000,
) -> dict[str, Any] | None:
    resolved = _safe_resolve_under_root(root, path_hint)
    if resolved is None or not resolved.is_file():
        return None
    if not _is_probably_text_file(resolved, max_file_bytes=max_file_bytes):
        return None
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    snippet = content[:max_chars]
    if len(content) > max_chars:
        snippet += "\n...[TRUNCATED]"
    try:
        relative_path = str(resolved.relative_to(root)).replace("\\", "/")
    except Exception:
        relative_path = resolved.name
    return {"path": relative_path, "snippet": snippet}


def _query_terms(query: str, *, max_terms: int = 8) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_./-]+", str(query or "").lower())
    terms: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = token.strip("._-/")
        if len(cleaned) < 3:
            continue
        if cleaned in _STOP_TERMS:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        terms.append(cleaned)
        if len(terms) >= max_terms:
            break
    return terms


def _best_matching_snippet(text: str, terms: list[str], *, max_chars: int) -> str:
    lines = text.splitlines()
    for line in lines:
        lowered = line.lower()
        if any(term in lowered for term in terms):
            snippet = line.strip()
            if len(snippet) > max_chars:
                snippet = snippet[:max_chars].rstrip() + "..."
            return snippet
    fallback = text.strip().splitlines()
    if not fallback:
        return ""
    snippet = fallback[0]
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip() + "..."
    return snippet


def _path_priority(path: str) -> int:
    lowered = str(path or "").lower()
    suffix = Path(lowered).suffix
    priority = 0
    if suffix in _CODE_SUFFIXES:
        priority += 6
    elif suffix in _DOC_SUFFIXES:
        priority -= 2
    if lowered.startswith(("src/", "backend/", "server/", "api/", "app/", "services/", "controllers/", "models/")):
        priority += 4
    if any(token in lowered for token in ("/src/", "/backend/", "/server/", "/api/", "/controllers/", "/services/", "/models/")):
        priority += 2
    if lowered.startswith(("docs/timeline/", "temp_issue_files/")):
        priority -= 5
    if "/tests/" in lowered or lowered.startswith(("tests/", "test/")):
        priority -= 1
    return priority


def search_workspace_snippets(
    root: Path,
    query: str,
    *,
    limit: int = 4,
    max_files: int = 400,
    max_file_bytes: int = 200_000,
    max_chars_per_snippet: int = 300,
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []

    matches: list[dict[str, Any]] = []
    scanned = 0
    stop = False
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in _IGNORE_DIR_NAMES]
        for file_name in file_names:
            scanned += 1
            if scanned > max_files:
                stop = True
                break
            full_path = Path(current_root) / file_name
            if not _is_probably_text_file(full_path, max_file_bytes=max_file_bytes):
                continue

            try:
                rel_path = str(full_path.relative_to(root)).replace("\\", "/")
            except Exception:
                rel_path = file_name
            rel_lower = rel_path.lower()
            path_hits = sum(1 for term in terms if term in rel_lower)
            text = ""
            content_hits = 0
            if path_hits == 0:
                try:
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                lowered = text.lower()
                content_hits = sum(1 for term in terms if term in lowered)
                if content_hits == 0:
                    continue
            else:
                try:
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    text = ""
                lowered = text.lower() if text else ""
                content_hits = sum(1 for term in terms if term in lowered)

            score = (path_hits * 4) + min(content_hits, 8) + _path_priority(rel_path)
            snippet = _best_matching_snippet(text, terms, max_chars=max_chars_per_snippet)
            matches.append(
                {
                    "path": rel_path,
                    "score": score,
                    "snippet": snippet,
                }
            )
        if stop:
            break

    matches.sort(key=lambda row: (-int(row.get("score") or 0), str(row.get("path") or "")))
    return matches[: max(1, limit)]
