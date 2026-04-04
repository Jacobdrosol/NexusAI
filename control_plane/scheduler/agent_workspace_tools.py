"""Interactive workspace tools for agentic bot execution.

Provides tool definitions (OpenAI function-calling format) and a safe executor
so that pipeline bots can interactively read, search, and write files during
their LLM inference loop, rather than relying solely on pre-injected context.

All file operations are sandboxed to the workspace root; path traversal is
blocked.  Write operations are only permitted when ``allow_writes=True``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from control_plane.chat.workspace_tools import (
    _safe_resolve_under_root,
    _is_probably_text_file,
    list_workspace_tree,
    normalize_workspace_root,
    search_workspace_snippets,
)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI / Ollama function-calling format)
# ---------------------------------------------------------------------------

_READ_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the complete content of a file in the project workspace. "
                "Use this to understand existing code before making changes. "
                "Returns the file content as a string, or an error if the file does not exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file from the workspace root (e.g. 'GlobeIQ.Server/Models/Assignment.cs').",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and subdirectories at a given path in the workspace. "
                "Use '.' or '' for the root directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path (e.g. 'GlobeIQ.Server/Models' or '.').",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search the workspace for files containing a text pattern. "
                "Returns the top matching file paths with a short snippet showing where the match was found. "
                "Useful for finding which files reference a class, method, or variable name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text or pattern to search for (keywords, class names, method names, etc.).",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "Optional glob pattern to restrict which files are searched (e.g. '*.cs', '*.py').",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_tree",
            "description": (
                "Return the full directory tree of the workspace up to 4 levels deep. "
                "Use this to understand the overall project layout before deciding where to place new files."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

_WRITE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a file in the workspace with the given content. "
                "Use this to create new files or apply your changes to existing files. "
                "Parent directories are created automatically. "
                "IMPORTANT: Always read_file first for existing files so you preserve all existing content correctly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path for the file (e.g. 'GlobeIQ.Server/Models/Assignment.cs').",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]


def get_tool_definitions(*, allow_writes: bool = False) -> list[dict]:
    """Return the list of tool definitions appropriate for the execution context."""
    tools = list(_READ_TOOLS)
    if allow_writes:
        tools.extend(_WRITE_TOOLS)
    return tools


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

_MAX_READ_CHARS = 24_000
_MAX_SEARCH_SNIPPET = 2_000
_MAX_SEARCH_RESULTS = 10


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    workspace_root: Path | None,
    *,
    allow_writes: bool = False,
) -> str:
    """Execute a workspace tool call and return its result as a string."""
    if workspace_root is None:
        return "ERROR: No workspace root available. Cannot execute tool."

    try:
        if name == "read_file":
            return _tool_read_file(workspace_root, arguments)
        elif name == "list_directory":
            return _tool_list_directory(workspace_root, arguments)
        elif name == "search_files":
            return _tool_search_files(workspace_root, arguments)
        elif name == "workspace_tree":
            return _tool_workspace_tree(workspace_root)
        elif name == "write_file":
            if not allow_writes:
                return "ERROR: write_file is not permitted for this bot (repo_output_mode is not 'allow')."
            return _tool_write_file(workspace_root, arguments)
        else:
            return f"ERROR: Unknown tool '{name}'."
    except Exception as exc:
        return f"ERROR executing tool '{name}': {exc}"


def _tool_read_file(root: Path, args: dict) -> str:
    path_hint = str(args.get("path") or "").strip()
    if not path_hint:
        return "ERROR: 'path' argument is required."
    resolved = _safe_resolve_under_root(root, path_hint)
    if resolved is None:
        return f"ERROR: Path '{path_hint}' is outside the workspace root or invalid."
    if not resolved.exists():
        return f"File not found: {path_hint}"
    if not resolved.is_file():
        return f"'{path_hint}' is a directory, not a file. Use list_directory instead."
    if not _is_probably_text_file(resolved, max_file_bytes=500_000):
        return f"'{path_hint}' appears to be a binary file and cannot be read as text."
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"ERROR reading file: {exc}"
    if len(content) > _MAX_READ_CHARS:
        return content[:_MAX_READ_CHARS] + f"\n...[TRUNCATED — file has {len(content)} chars total]"
    return content


def _tool_list_directory(root: Path, args: dict) -> str:
    path_hint = str(args.get("path") or ".").strip() or "."
    resolved = _safe_resolve_under_root(root, path_hint)
    if resolved is None:
        return f"ERROR: Path '{path_hint}' is outside the workspace root or invalid."
    if not resolved.exists():
        return f"Directory not found: {path_hint}"
    if not resolved.is_dir():
        return f"'{path_hint}' is a file, not a directory. Use read_file instead."
    entries = []
    try:
        for entry in sorted(resolved.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                rel = str(entry.relative_to(root)).replace("\\", "/")
            except Exception:
                rel = entry.name
            kind = "DIR" if entry.is_dir() else "FILE"
            entries.append(f"{kind}  {rel}")
    except Exception as exc:
        return f"ERROR listing directory: {exc}"
    if not entries:
        return f"(empty directory: {path_hint})"
    return "\n".join(entries[:500])  # cap at 500 entries


def _tool_search_files(root: Path, args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: 'query' argument is required."
    file_glob = str(args.get("file_glob") or "").strip()

    hits = search_workspace_snippets(
        root,
        query,
        limit=_MAX_SEARCH_RESULTS,
        max_files=800,
        max_file_bytes=300_000,
        max_chars_per_snippet=_MAX_SEARCH_SNIPPET,
    )

    if file_glob:
        import fnmatch
        hits = [h for h in hits if fnmatch.fnmatch(str(h.get("path") or ""), file_glob)]

    if not hits:
        return f"No files found matching query: {query}"

    lines = []
    for hit in hits:
        path = hit.get("path", "")
        snippet = (hit.get("snippet") or "").strip()
        lines.append(f"--- {path} ---\n{snippet}")
    return "\n\n".join(lines)


def _tool_workspace_tree(root: Path) -> str:
    tree = list_workspace_tree(root, max_depth=4, max_entries=400)
    return tree or "(empty workspace)"


def _tool_write_file(root: Path, args: dict) -> str:
    path_hint = str(args.get("path") or "").strip()
    content = args.get("content")
    if not path_hint:
        return "ERROR: 'path' argument is required."
    if content is None:
        return "ERROR: 'content' argument is required."
    content_str = str(content)
    resolved = _safe_resolve_under_root(root, path_hint)
    if resolved is None:
        return f"ERROR: Path '{path_hint}' is outside the workspace root or invalid."
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content_str, encoding="utf-8")
        try:
            rel = str(resolved.relative_to(root)).replace("\\", "/")
        except Exception:
            rel = path_hint
        return f"OK: wrote {len(content_str)} characters to {rel}"
    except Exception as exc:
        return f"ERROR writing file: {exc}"


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

def parse_tool_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    """Parse tool call arguments that may arrive as a JSON string or dict."""
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        raw_arguments = raw_arguments.strip()
        if not raw_arguments:
            return {}
        try:
            parsed = json.loads(raw_arguments)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}
