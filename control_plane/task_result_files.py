from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

_FENCE_OPEN_RE = re.compile(r"^```([^\s`]*)?(?:\s+.*)?$")
_LABEL_PREFIX_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:[-*]\s*)?(?:(?:deliverable|file|path|output|artifact)(?:\s+\d+)?\s*:)\s*",
    re.IGNORECASE,
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/@+ -]+$")
_KNOWN_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "README",
    "README.md",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "tsconfig.base.json",
    "vite.config.ts",
    "vitest.config.ts",
    "jest.config.js",
    "jest.config.ts",
}


def extract_result_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("output", "content", "text", "result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(result, indent=2, sort_keys=True)
    if result is None:
        return ""
    return str(result)


def extract_file_candidates(result: Any) -> List[Dict[str, Any]]:
    candidates: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    text = extract_result_text(result)
    for item in _extract_markdown_file_candidates(text):
        candidates[item["path"]] = item

    if isinstance(result, dict):
        explicit = result.get("artifacts")
        if isinstance(explicit, list):
            for idx, item in enumerate(explicit):
                parsed = _explicit_artifact_candidate(item, index=idx)
                if parsed is not None:
                    candidates[parsed["path"]] = parsed

    return list(candidates.values())


def _explicit_artifact_candidate(item: Any, *, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    normalized_path = _normalize_candidate_path(item.get("path"))
    if not normalized_path:
        return None
    content_value = item.get("content")
    if content_value is None:
        return None
    if isinstance(content_value, str):
        content = content_value
    else:
        content = json.dumps(content_value, indent=2, sort_keys=True)
    return {
        "path": normalized_path,
        "content": content,
        "source": "explicit_artifact",
        "label": str(item.get("label") or item.get("name") or f"Artifact {index + 1}"),
        "language": None,
    }


def _extract_markdown_file_candidates(text: str) -> List[Dict[str, Any]]:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    extracted: List[Dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        match = _FENCE_OPEN_RE.match(lines[idx].strip())
        if not match:
            idx += 1
            continue

        language = (match.group(1) or "").strip() or None
        path_hint = _find_nearest_path_hint(lines, idx - 1)
        idx += 1
        code_lines: List[str] = []
        while idx < len(lines) and not lines[idx].strip().startswith("```"):
            code_lines.append(lines[idx])
            idx += 1
        if idx < len(lines):
            idx += 1
        content = "\n".join(code_lines).strip("\n")
        if path_hint and content.strip():
            extracted.append(
                {
                    "path": path_hint,
                    "content": content,
                    "source": "markdown_code_fence",
                    "label": path_hint,
                    "language": language,
                }
            )
    return extracted


def _find_nearest_path_hint(lines: List[str], start_index: int) -> Optional[str]:
    inspected = 0
    idx = start_index
    while idx >= 0 and inspected < 6:
        raw = str(lines[idx] or "").strip()
        idx -= 1
        if not raw:
            continue
        inspected += 1
        candidate = _normalize_candidate_line(raw, require_path_style=True)
        if candidate:
            return candidate
    return None


def _normalize_candidate_line(raw: str, *, require_path_style: bool = False) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None

    original = text
    had_label_prefix = bool(_LABEL_PREFIX_RE.match(original))
    text = text.replace("`", "").strip()
    text = _LABEL_PREFIX_RE.sub("", text).strip()
    if ":" in text:
        head, tail = text.split(":", 1)
        if "/" in tail or "\\" in tail or "." in tail:
            text = tail.strip()
        elif "/" in head or "\\" in head:
            text = head.strip()
    candidate = _normalize_candidate_path(text)
    if not candidate:
        return None
    if not require_path_style:
        return candidate
    stripped_original = original.replace("`", "").strip()
    if had_label_prefix:
        return candidate
    if "/" not in candidate and candidate not in _KNOWN_FILENAMES:
        return None
    if stripped_original == candidate:
        return candidate
    if stripped_original.endswith(candidate) and ("/" in candidate or "\\" in candidate):
        return candidate
    return None


def _normalize_candidate_path(raw: Any) -> Optional[str]:
    text = str(raw or "").strip().strip("'\"").replace("\\", "/")
    if not text:
        return None
    if text.startswith("./"):
        text = text[2:]
    while text.startswith("/"):
        return None
    if "://" in text or text.lower().startswith("data:"):
        return None
    if _WINDOWS_DRIVE_RE.match(text):
        return None
    if not _PATH_CHARS_RE.match(text):
        return None

    candidate = PurePosixPath(text)
    parts = list(candidate.parts)
    if not parts:
        return None
    if any(part in {"..", "."} or not str(part).strip() for part in parts):
        return None
    normalized = "/".join(parts)
    leaf = parts[-1]
    if len(normalized) > 240:
        return None
    if "." not in leaf and leaf not in _KNOWN_FILENAMES:
        return None
    return normalized
