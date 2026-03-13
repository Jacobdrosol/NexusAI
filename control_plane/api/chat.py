import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from control_plane.chat.workspace_tools import (
    build_focus_query,
    extract_path_hints,
    normalize_workspace_root,
    read_workspace_file_snippet,
    search_workspace_snippets,
)
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage, Task, TaskMetadata

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class CreateConversationRequest(BaseModel):
    title: str
    project_id: Optional[str] = None
    bridge_project_ids: List[str] = Field(default_factory=list)
    scope: str = "global"
    default_bot_id: Optional[str] = None
    default_model_id: Optional[str] = None
    tool_access_enabled: bool = False
    tool_access_filesystem: bool = False
    tool_access_repo_search: bool = False


class PostMessageRequest(BaseModel):
    content: str
    bot_id: Optional[str] = None
    context_items: Optional[List[str]] = None
    context_item_ids: Optional[List[str]] = None
    include_project_context: bool = False
    use_workspace_tools: bool = False


class UpdateConversationToolAccessRequest(BaseModel):
    enabled: bool = False
    filesystem: bool = False
    repo_search: bool = False


_REPO_INTENT_RE = re.compile(
    r"\b(repo|repository|codebase|source code|files?|read|search|scan|audit|analy[sz]e|inspect)\b",
    re.IGNORECASE,
)
_SOURCE_SCORE_SUFFIX_RE = re.compile(r"\s*\(score=[^)]+\)\s*$", re.IGNORECASE)
_UNVERIFIABLE_ACTION_LINE_RE = re.compile(
    r"^\s*((?:now\s+)?let\s+me\s+|searching\b|i\s+searched\b|"
    r"i(?:\s+will|\s+am\s+going\s+to|\s*['’]ll)\s+(?:search|read|scan|review|look|check)\b|"
    r"now\s+i\s+have\s+the\s+actual\s+file\s+contents\b|"
    r"after\s+reviewing\s+your\s+actual\s+codebase\b|"
    r"i\s+can\s+read\s+and\s+search\b|\*\*/)",
    re.IGNORECASE,
)
_UNVERIFIABLE_ACTION_FRAGMENT_RE = re.compile(
    r"\b(let\s+me\s+(?:search|read|scan|review|look|check)|"
    r"searching\s+for|i\s+searched|"
    r"i(?:\s+will|\s+am\s+going\s+to|\s*['’]ll)\s+(?:search|read|scan|review|look|check))\b",
    re.IGNORECASE,
)
_SOURCE_CITATION_RE = re.compile(r"\[S\d+\]")
_PATH_LIKE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+")
_QUOTED_TERM_LIST_RE = re.compile(r'^(?:"[^"]+"\s*){2,}$')
_SOURCE_HEADER_LINE_RE = re.compile(
    r"^\s*(files inspected \(verified context\)|source-of-truth \(workspace repo\)|supporting context \(ingested repo/docs/history\))\s*$",
    re.IGNORECASE,
)
_SOURCE_BULLET_LINE_RE = re.compile(r"^\s*-\s*\[S\d+\]\s+.+$")
_REQUEST_PERMISSION_LINE_RE = re.compile(
    r"^\s*(please\s+confirm\s+which\s+files|should\s+i\s+start\s+with|"
    r"let\s+me\s+know\s+which\s+files|which\s+files\s+would\s+you\s+like\s+me\s+to\s+read)\b",
    re.IGNORECASE,
)
_GROUNDING_NOTE_LINE_RE = re.compile(r"^\s*grounding\s+note\s*:\s*", re.IGNORECASE)
_CITATION_TAIL_RATIO = 0.75
_CITATION_DENSITY_WINDOW = 900
_UNCITED_MAX_LINES = 28
_UNCITED_MAX_CHARS = 1800


def _repo_intent_requested(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    return bool(_REPO_INTENT_RE.search(text))


def _context_resolution_requested(body: PostMessageRequest) -> bool:
    return bool(
        body.context_items
        or body.context_item_ids
        or body.include_project_context
        or body.use_workspace_tools
        or _repo_intent_requested(body.content)
    )


def _context_source_labels(context_items: Optional[List[str]], *, limit: int = 12) -> List[str]:
    labels: List[str] = []
    seen: set[str] = set()
    for entry in context_items or []:
        line = str(entry or "").splitlines()[0].strip()
        if not line.startswith("["):
            continue
        close = line.find("]")
        if close <= 1:
            continue
        marker = line[1:close].strip()
        detail = line[close + 1 :].strip()
        if not marker:
            continue
        cleaned_detail = _SOURCE_SCORE_SUFFIX_RE.sub("", detail).strip()
        label = f"{marker} {cleaned_detail}".strip()
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _source_tier(label: str) -> int:
    lowered = str(label or "").lower()
    if lowered.startswith("workspace:file") or lowered.startswith("workspace:search"):
        return 0
    if lowered.startswith("repo:"):
        return 1
    if lowered.startswith("vault:"):
        return 2
    return 3


def _split_sources_by_tier(labels: List[str]) -> tuple[List[str], List[str], List[str], List[str]]:
    workspace: List[str] = []
    repo: List[str] = []
    vault: List[str] = []
    other: List[str] = []
    for label in labels:
        tier = _source_tier(label)
        if tier == 0:
            workspace.append(label)
        elif tier == 1:
            repo.append(label)
        elif tier == 2:
            vault.append(label)
        else:
            other.append(label)
    return workspace, repo, vault, other


def _order_context_items(entries: List[str], *, limit: int = 30) -> List[str]:
    parsed: List[tuple[int, int, str]] = []
    for index, entry in enumerate(entries):
        first_line = str(entry or "").splitlines()[0].strip()
        parsed.append((_source_tier(first_line), index, entry))
    parsed.sort(key=lambda row: (row[0], row[1]))
    ordered: List[str] = []
    seen: set[str] = set()
    for _, _, entry in parsed:
        normalized = str(entry or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(entry)
        if len(ordered) >= limit:
            break
    return ordered


def _messages_to_payload(
    messages: List[ChatMessage],
    *,
    context_items: Optional[List[str]] = None,
    require_repo_evidence: bool = False,
) -> List[dict]:
    payload = [{"role": m.role, "content": m.content} for m in messages]
    resolved_context = list(context_items or [])
    sources = _context_source_labels(resolved_context, limit=12)
    if resolved_context:
        joined = "\n".join(resolved_context)
        payload.insert(0, {"role": "system", "content": f"Context:\n{joined}"})
    if require_repo_evidence:
        indexed_sources = [(f"S{idx + 1}", source) for idx, source in enumerate(sources)]
        if sources:
            source_lines = "\n".join(f"- [{sid}] {source}" for sid, source in indexed_sources)
            policy = (
                "Repository Evidence Policy:\n"
                "- Treat workspace snippets as source of truth for current code state.\n"
                "- Use ingested repo/docs/PR/commit context only as supporting context.\n"
                "- Use only the provided context snippets as verified repository evidence for this turn.\n"
                "- Do not claim you searched/read/scanned files unless those files appear in verified sources.\n"
                "- Do not simulate tool execution logs (for example: 'Let me search...', glob patterns, or pseudo command traces).\n"
                "- Prefer source citations like [S1] for concrete claims when practical.\n"
                "- Keep responses concise and evidence-first (summary + key findings + concrete next steps).\n"
                "- Do not output reconstructed full class/interface definitions unless directly shown in verified snippets.\n"
                "- Do not ask permission to read files already in verified sources; answer directly from current context.\n"
                "- If evidence is incomplete, explicitly state what you could not verify.\n"
                "- For repository/code/security analysis, include a 'Files inspected' section with exact paths/markers.\n"
                "Verified sources:\n"
                f"{source_lines}"
            )
        else:
            policy = (
                "Repository Evidence Policy:\n"
                "- No repository snippets were retrieved for this turn.\n"
                "- Do not claim you searched/read/scanned repository files.\n"
                "- Explain that evidence is unavailable and request project context/workspace-tool access or specific files."
            )
        insert_at = 1 if resolved_context else 0
        payload.insert(insert_at, {"role": "system", "content": policy})
    return payload


def _repo_context_unavailable_message() -> str:
    return (
        "I could not retrieve repository context for this turn, so I cannot verify the current code state.\n\n"
        "I am intentionally not claiming file reads/search results without evidence. "
        "Enable project repo context/workspace tools for this chat and try again."
    )


def _apply_repo_evidence_envelope(output: str, *, require_repo_evidence: bool, context_sources: List[str]) -> str:
    if not require_repo_evidence:
        return output
    if not context_sources:
        return _repo_context_unavailable_message()
    normalized = _sanitize_repo_grounded_output(output)
    indexed_sources = [(f"S{idx + 1}", source) for idx, source in enumerate(context_sources[:12])]
    workspace, repo, vault, other = _split_sources_by_tier([source for _, source in indexed_sources])
    sections: List[str] = ["Files inspected (verified context)"]
    sections.append("Source-of-truth (workspace repo)")
    if workspace:
        for sid, source in indexed_sources:
            if source in workspace:
                sections.append(f"- [{sid}] {source}")
    else:
        sections.append("- unavailable in this turn (workspace context not resolved)")
    if repo or vault or other:
        sections.append("Supporting context (ingested repo/docs/history)")
        for sid, source in indexed_sources:
            if source in repo or source in vault or source in other:
                sections.append(f"- [{sid}] {source}")
    prefix = "\n".join(sections) + "\n"
    if not normalized:
        return (
            f"{prefix}\n{_condense_uncited_grounded_output('')}\n\n"
            "Grounding note: inline [S#] citations were not generated; response kept concise."
        )
    citation_matches = list(_SOURCE_CITATION_RE.finditer(normalized))
    has_inline_citation = bool(citation_matches)
    if has_inline_citation and len(normalized) > 1200:
        last_citation_end = citation_matches[-1].end()
        tail_cited = last_citation_end >= int(len(normalized) * _CITATION_TAIL_RATIO)
        density_ok = (len(citation_matches) * _CITATION_DENSITY_WINDOW) >= len(normalized)
        if not tail_cited or not density_ok:
            has_inline_citation = False
    if has_inline_citation:
        return f"{prefix}\n{normalized}"
    uncited_summary = _condense_uncited_grounded_output(normalized)
    return (
        f"{prefix}\n{uncited_summary}\n\n"
        "Grounding note: inline [S#] citations were not generated; response kept concise."
    )


def _sanitize_repo_grounded_output(output: str) -> str:
    def _is_unverified_path_list_line(line: str) -> bool:
        stripped_line = str(line or "").strip()
        if not stripped_line:
            return False
        if _SOURCE_CITATION_RE.search(stripped_line):
            return False
        lowered_line = stripped_line.lower()
        if lowered_line.startswith(
            (
                "files inspected",
                "source-of-truth",
                "supporting context",
                "grounding note",
                "- [s",
            )
        ):
            return False
        if _QUOTED_TERM_LIST_RE.match(stripped_line):
            return True
        tokens = _PATH_LIKE_TOKEN_RE.findall(stripped_line)
        if not tokens:
            return False
        if len(tokens) >= 2:
            return True
        token = tokens[0]
        if stripped_line == token:
            return True
        token_coverage = len(token) / max(len(stripped_line), 1)
        return token_coverage >= 0.75

    text = str(output or "")
    lines = text.splitlines()
    kept: List[str] = []
    dropping_model_source_block = False
    for raw in lines:
        line = str(raw or "")
        stripped = line.strip()
        if _SOURCE_HEADER_LINE_RE.match(stripped):
            dropping_model_source_block = True
            continue
        if dropping_model_source_block:
            if _SOURCE_HEADER_LINE_RE.match(stripped) or _SOURCE_BULLET_LINE_RE.match(stripped) or not stripped:
                continue
            dropping_model_source_block = False
        if _UNVERIFIABLE_ACTION_LINE_RE.search(stripped):
            continue
        if _REQUEST_PERMISSION_LINE_RE.search(stripped):
            continue
        if _GROUNDING_NOTE_LINE_RE.search(stripped):
            continue
        if stripped.startswith('"') and stripped.endswith('"') and _UNVERIFIABLE_ACTION_FRAGMENT_RE.search(stripped):
            continue
        if _is_unverified_path_list_line(stripped):
            continue
        kept.append(line)
    compacted: List[str] = []
    previous_blank = False
    for line in kept:
        is_blank = not str(line).strip()
        if is_blank and previous_blank:
            continue
        compacted.append(line)
        previous_blank = is_blank
    return "\n".join(compacted).strip()


def _condense_uncited_grounded_output(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return (
            "I reviewed the verified context listed above and can answer directly from it. "
            "Request a focused summary (for example: architecture, gaps, or next steps)."
        )
    lines = normalized.splitlines()
    kept: List[str] = []
    for raw in lines:
        line = str(raw or "").strip()
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        lowered = line.lower()
        if lowered.startswith(
            (
                "code review:",
                "executive summary",
                "phase ",
                "data models -",
                "service layer -",
                "controller layer -",
                "frontend components -",
                "database schema -",
                "expansion plan -",
            )
        ):
            continue
        if _REQUEST_PERMISSION_LINE_RE.search(line):
            continue
        if _GROUNDING_NOTE_LINE_RE.search(line):
            continue
        kept.append(line)
        if len(kept) >= _UNCITED_MAX_LINES:
            break
    compacted = "\n".join(kept).strip()
    if not compacted:
        return (
            "I reviewed the verified context listed above and can answer directly from it. "
            "Request a focused summary (for example: architecture, gaps, or next steps)."
        )
    if len(compacted) > _UNCITED_MAX_CHARS:
        compacted = compacted[:_UNCITED_MAX_CHARS].rstrip() + "..."
    return compacted


def _project_repo_namespace(project_id: str, project: Any) -> str:
    default_namespace = f"project:{project_id}:repo"
    settings = getattr(project, "settings_overrides", None)
    if not isinstance(settings, dict):
        return default_namespace
    github_cfg = settings.get("github")
    if not isinstance(github_cfg, dict):
        return default_namespace
    sync_cfg = github_cfg.get("context_sync")
    if not isinstance(sync_cfg, dict):
        return default_namespace
    namespace = str(sync_cfg.get("namespace") or "").strip()
    return namespace or default_namespace


def _conversation_project_ids(conversation: Optional[ChatConversation]) -> List[str]:
    if conversation is None:
        return []
    ids: List[str] = []
    if conversation.project_id:
        ids.append(str(conversation.project_id).strip())
    ids.extend(str(pid).strip() for pid in conversation.bridge_project_ids if str(pid).strip())
    return list(dict.fromkeys([pid for pid in ids if pid]))


def _parse_tool_access_config(raw: Any) -> Dict[str, Any]:
    cfg = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "filesystem": bool(cfg.get("filesystem", False)),
        "repo_search": bool(cfg.get("repo_search", False)),
        "workspace_root": str(cfg.get("workspace_root") or "").strip() or None,
    }


def _conversation_tool_access(conversation: ChatConversation) -> Dict[str, Any]:
    return {
        "enabled": bool(getattr(conversation, "tool_access_enabled", False)),
        "filesystem": bool(getattr(conversation, "tool_access_filesystem", False)),
        "repo_search": bool(getattr(conversation, "tool_access_repo_search", False)),
    }


def _bot_tool_access(bot: Any) -> Dict[str, Any]:
    routing = getattr(bot, "routing_rules", None)
    routing_rules = routing if isinstance(routing, dict) else {}
    raw = routing_rules.get("chat_tool_access")
    if not isinstance(raw, dict):
        raw = routing_rules.get("tool_access")
    return _parse_tool_access_config(raw)


def _project_tool_access(project: Any) -> Dict[str, Any]:
    settings = getattr(project, "settings_overrides", None)
    if not isinstance(settings, dict):
        return _parse_tool_access_config(None)
    return _parse_tool_access_config(settings.get("chat_tool_access"))


def _project_workspace_slug(project_id: str) -> str:
    token = str(project_id or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", token).strip("-")
    return slug or "project"


def _repo_workspace_base_root() -> Path:
    raw = str(os.environ.get("NEXUSAI_REPO_WORKSPACE_ROOT", "") or "").strip()
    candidate = Path(raw).expanduser() if raw else (Path("data") / "repo_workspaces")
    try:
        if candidate.is_absolute():
            return candidate.resolve(strict=False)
        return (Path.cwd() / candidate).resolve(strict=False)
    except Exception:
        return (Path.cwd() / "data" / "repo_workspaces").resolve(strict=False)


def _managed_repo_workspace_root(project_id: str) -> str:
    return str((_repo_workspace_base_root() / _project_workspace_slug(project_id) / "repo").resolve(strict=False))


def _project_repo_workspace_root(project: Any) -> str | None:
    settings = getattr(project, "settings_overrides", None)
    if not isinstance(settings, dict):
        return None
    raw = settings.get("repo_workspace")
    cfg = raw if isinstance(raw, dict) else {}
    if not bool(cfg.get("enabled", False)):
        return None
    managed = bool(cfg.get("managed_path_mode", True))
    if managed:
        project_id = str(getattr(project, "id", "") or "").strip()
        return _managed_repo_workspace_root(project_id) if project_id else None
    root = normalize_workspace_root(str(cfg.get("root_path") or "").strip() or None)
    return str(root) if root is not None else None


async def _effective_tool_access(
    request: Request,
    *,
    conversation: ChatConversation,
    target_bot_id: str | None,
) -> Dict[str, Any]:
    disabled = {
        "enabled": False,
        "filesystem": False,
        "repo_search": False,
        "workspace_root": None,
    }
    if not target_bot_id:
        return disabled

    bot_registry = getattr(request.app.state, "bot_registry", None)
    if bot_registry is None:
        return disabled
    bot = await bot_registry.get(target_bot_id)
    bot_cfg = _bot_tool_access(bot)
    chat_cfg = _conversation_tool_access(conversation)

    project_cfg = _parse_tool_access_config(None)
    project_id = str(conversation.project_id or "").strip()
    if project_id:
        project_registry = getattr(request.app.state, "project_registry", None)
        if project_registry is not None:
            try:
                project = await project_registry.get(project_id)
                project_cfg = _project_tool_access(project)
            except Exception:
                project_cfg = _parse_tool_access_config(None)

    all_enabled = bool(chat_cfg["enabled"] and bot_cfg["enabled"] and project_cfg["enabled"])
    if not all_enabled:
        return disabled
    workspace_root = project_cfg.get("workspace_root")
    if not workspace_root and bool(project_cfg.get("filesystem", False)) and project_id:
        project_registry = getattr(request.app.state, "project_registry", None)
        if project_registry is not None:
            try:
                project = await project_registry.get(project_id)
                workspace_root = _project_repo_workspace_root(project)
            except Exception:
                workspace_root = None
    return {
        "enabled": True,
        "filesystem": bool(chat_cfg["filesystem"] and bot_cfg["filesystem"] and project_cfg["filesystem"]),
        "repo_search": bool(chat_cfg["repo_search"] and bot_cfg["repo_search"] and project_cfg["repo_search"]),
        "workspace_root": workspace_root,
    }


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int((os.environ.get(name, "") or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


async def _resolve_workspace_context_items(
    *,
    query: str,
    workspace_root: str | None,
) -> List[str]:
    root = normalize_workspace_root(workspace_root)
    if root is None:
        return []

    max_total_items = _env_int("NEXUSAI_CHAT_WORKSPACE_MAX_ITEMS", 8, minimum=1, maximum=20)
    max_total_chars = _env_int("NEXUSAI_CHAT_WORKSPACE_MAX_TOTAL_CHARS", 12_000, minimum=1200, maximum=60_000)
    max_file_bytes = _env_int("NEXUSAI_CHAT_WORKSPACE_MAX_FILE_BYTES", 200_000, minimum=4_000, maximum=2_000_000)
    max_read_chars = _env_int("NEXUSAI_CHAT_WORKSPACE_READ_MAX_CHARS", 3_200, minimum=200, maximum=20_000)
    search_max_files = _env_int("NEXUSAI_CHAT_WORKSPACE_SEARCH_MAX_FILES", 400, minimum=40, maximum=5_000)
    search_max_hits = _env_int("NEXUSAI_CHAT_WORKSPACE_SEARCH_MAX_HITS", 6, minimum=1, maximum=20)

    resolved: List[str] = []
    seen_paths: set[str] = set()
    used_chars = 0

    hints = extract_path_hints(query, limit=max_total_items)
    for hint in hints:
        file_row = await asyncio.to_thread(
            read_workspace_file_snippet,
            root,
            hint,
            max_file_bytes=max_file_bytes,
            max_chars=max_read_chars,
        )
        if not isinstance(file_row, dict):
            continue
        path = str(file_row.get("path") or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        snippet = str(file_row.get("snippet") or "").strip()
        if not snippet:
            continue
        text = f"[workspace:file] {path}\n{snippet}"
        if used_chars + len(text) > max_total_chars:
            return resolved
        resolved.append(text)
        used_chars += len(text)
        if len(resolved) >= max_total_items:
            return resolved

    remaining = max(1, max_total_items - len(resolved))
    hits = await asyncio.to_thread(
        search_workspace_snippets,
        root,
        query,
        limit=min(search_max_hits, remaining),
        max_files=search_max_files,
        max_file_bytes=max_file_bytes,
        max_chars_per_snippet=320,
    )
    for hit in hits:
        path = str(hit.get("path") or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        snippet = str(hit.get("snippet") or "").strip()
        if not snippet:
            continue
        score = hit.get("score")
        text = f"[workspace:search] {path} (score={score})\n{snippet}"
        if used_chars + len(text) > max_total_chars:
            break
        resolved.append(text)
        used_chars += len(text)
        if len(resolved) >= max_total_items:
            break
    return resolved


def _repo_row_priority(row: Any) -> tuple[int, float]:
    if not isinstance(row, dict):
        return (-100, 0.0)
    title = str(row.get("title") or "").strip()
    lowered = title.lower()
    priority = 0
    if ":pr:" in lowered or ":issue" in lowered or ":discussion" in lowered:
        priority -= 6
    if ":commit:" in lowered:
        priority -= 2
    if "temp issue files/" in lowered or "temp_issue_files/" in lowered:
        priority -= 12
    if lowered.endswith(".designer.cs"):
        priority -= 6
    if "/migrations/" in lowered or ":migrations/" in lowered:
        priority -= 10
    if re.search(r"[\\/][^\\/]+\.[a-z0-9]{1,8}$", title, flags=re.IGNORECASE):
        priority += 6
    if any(token in lowered for token in ("/src/", "/backend/", "/server/", "/api/", "/controllers/", "/services/", "/models/")):
        priority += 3
    score_value = 0.0
    try:
        score_value = float(row.get("score") or 0.0)
    except Exception:
        score_value = 0.0
    return priority, score_value


def _repo_row_match_boost(row: Any, query_terms: set[str]) -> int:
    if not isinstance(row, dict) or not query_terms:
        return 0
    title = str(row.get("title") or "").lower()
    content = str(row.get("content") or "").lower()
    title_hits = sum(1 for term in query_terms if term in title)
    content_hits = sum(1 for term in query_terms if term in content)
    return (title_hits * 5) + min(content_hits, 3)


async def _resolve_project_repo_context_items(
    request: Request,
    *,
    conversation: ChatConversation,
    query: str,
) -> List[str]:
    vault_manager = getattr(request.app.state, "vault_manager", None)
    if vault_manager is None:
        return []

    project_registry = getattr(request.app.state, "project_registry", None)
    project_ids = _conversation_project_ids(conversation)
    if not project_ids:
        return []

    max_total_items = 10
    max_chars_per_item = 1800
    max_total_chars = 12_000
    project_count = max(len(project_ids), 1)
    per_project_limit = max(8, min(24, (max_total_items * 2) // project_count + 2))
    focused_query = build_focus_query(query, max_terms=12)
    raw_query = str(query or "").strip()[:800]
    search_queries: List[str] = []
    if focused_query:
        search_queries.append(focused_query)
    if raw_query and raw_query not in search_queries:
        search_queries.append(raw_query)
    query_terms = set(focused_query.split())

    resolved: List[str] = []
    seen_chunks: set[str] = set()
    used_chars = 0
    for project_id in project_ids:
        namespace = f"project:{project_id}:repo"
        if project_registry is not None:
            try:
                project = await project_registry.get(project_id)
                namespace = _project_repo_namespace(project_id, project)
            except Exception:
                namespace = f"project:{project_id}:repo"

        rows: List[dict] = []
        for search_query in search_queries:
            if not search_query:
                continue
            try:
                rows = await vault_manager.search(
                    query=search_query,
                    namespace=namespace,
                    project_id=project_id,
                    limit=per_project_limit,
                )
            except Exception:
                rows = []
            if rows:
                break

        ranked_rows = sorted(
            list(rows or []),
            key=lambda r: (
                _repo_row_priority(r)[0] + _repo_row_match_boost(r, query_terms),
                _repo_row_priority(r)[1],
            ),
            reverse=True,
        )
        high_quality = [row for row in ranked_rows if _repo_row_priority(row)[0] >= 2]
        candidate_rows = high_quality if high_quality else ranked_rows

        for row in candidate_rows:
            chunk_id = str(row.get("chunk_id") or "").strip()
            if not chunk_id or chunk_id in seen_chunks:
                continue
            seen_chunks.add(chunk_id)
            snippet = str(row.get("content") or "").strip()
            if not snippet:
                continue
            snippet = snippet[:max_chars_per_item]
            title = str(row.get("title") or "repo-context").strip() or "repo-context"
            score_raw = row.get("score")
            try:
                score_text = f"{float(score_raw):.3f}"
            except Exception:
                score_text = "n/a"
            text = f"[repo:{project_id}] {title} (namespace={namespace}, score={score_text})\n{snippet}"
            if used_chars + len(text) > max_total_chars:
                return resolved
            resolved.append(text)
            used_chars += len(text)
            if len(resolved) >= max_total_items:
                return resolved

    return resolved


async def _resolve_context_items(
    request: Request,
    body: PostMessageRequest,
    *,
    conversation: Optional[ChatConversation] = None,
    tool_access: Optional[Dict[str, Any]] = None,
    force_project_context: bool = False,
    force_workspace_context: bool = False,
) -> List[str]:
    # Backward compatible direct context usage.
    manual_context: List[str] = list(body.context_items or [])
    resolved: List[str] = []
    item_ids = [str(i).strip() for i in (body.context_item_ids or []) if str(i).strip()]
    tool_cfg = tool_access if isinstance(tool_access, dict) else {}
    repo_search_allowed = bool(tool_cfg.get("repo_search", False))
    filesystem_allowed = bool(tool_cfg.get("filesystem", False))
    workspace_root = str(tool_cfg.get("workspace_root") or "").strip() or None
    vault_manager = getattr(request.app.state, "vault_manager", None)
    vault_items: List[str] = []
    if item_ids and vault_manager is not None:
        for item_id in item_ids[:20]:
            try:
                item = await vault_manager.get_item(item_id)
                text = (item.content or "").strip()
                if not text:
                    continue
                # Bound payload size to reduce latency and accidental leakage.
                snippet = text[:4000]
                vault_items.append(f"[vault:{item.id}] {item.title}\n{snippet}")
            except Exception:
                continue

    workspace_context: List[str] = []
    if (body.use_workspace_tools or force_workspace_context) and filesystem_allowed and workspace_root:
        workspace_context = await _resolve_workspace_context_items(
            query=body.content,
            workspace_root=workspace_root,
        )

    repo_context: List[str] = []
    if (body.include_project_context or body.use_workspace_tools or force_project_context) and repo_search_allowed and conversation is not None:
        repo_context = await _resolve_project_repo_context_items(
            request,
            conversation=conversation,
            query=body.content,
        )

    # Source-of-truth ordering: workspace -> ingested repo -> explicitly-selected vault -> manual context
    resolved.extend(workspace_context)
    resolved.extend(repo_context)
    resolved.extend(vault_items)
    resolved.extend(manual_context)
    return _order_context_items(resolved, limit=30)


def _extract_assign_instruction(content: str) -> Optional[str]:
    text = content.strip()
    if not text.lower().startswith("@assign"):
        return None
    instruction = text[len("@assign"):].strip()
    return instruction or None


def _extract_task_output(result: Any) -> str:
    if isinstance(result, dict):
        output = result.get("output")
        if output is not None:
            return str(output)
        return json.dumps(result)
    if result is None:
        return ""
    return str(result)


@router.post("/conversations", response_model=ChatConversation)
async def create_conversation(request: Request, body: CreateConversationRequest) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    project_id = (body.project_id or "").strip() or None
    bridge_project_ids = [str(pid).strip() for pid in body.bridge_project_ids if str(pid).strip()]
    bridge_project_ids = list(dict.fromkeys(bridge_project_ids))

    if body.scope == "project" and not project_id:
        raise HTTPException(status_code=400, detail="project_id is required for project-scoped conversations")
    if body.scope == "bridged":
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required for bridged conversations")
        bridge_project_ids = [pid for pid in bridge_project_ids if pid != project_id]

    return await chat_manager.create_conversation(
        title=body.title,
        project_id=project_id,
        bridge_project_ids=bridge_project_ids,
        scope=body.scope,
        default_bot_id=body.default_bot_id,
        default_model_id=body.default_model_id,
        tool_access_enabled=body.tool_access_enabled,
        tool_access_filesystem=body.tool_access_filesystem,
        tool_access_repo_search=body.tool_access_repo_search,
    )


@router.get("/conversations", response_model=List[ChatConversation])
async def list_conversations(
    request: Request,
    project_id: Optional[str] = Query(default=None),
    archived: Literal["active", "archived", "all"] = Query(default="active"),
) -> List[ChatConversation]:
    chat_manager = request.app.state.chat_manager
    return await chat_manager.list_conversations(project_id=project_id, archived=archived)


@router.get("/conversations/{conversation_id}", response_model=ChatConversation)
async def get_conversation(conversation_id: str, request: Request) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.get_conversation(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/conversations/{conversation_id}/tool-access", response_model=ChatConversation)
async def update_conversation_tool_access(
    conversation_id: str,
    request: Request,
    body: UpdateConversationToolAccessRequest,
) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.update_conversation_tool_access(
            conversation_id,
            tool_access_enabled=body.enabled,
            tool_access_filesystem=body.filesystem,
            tool_access_repo_search=body.repo_search,
        )
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, request: Request) -> None:
    chat_manager = request.app.state.chat_manager
    try:
        await chat_manager.delete_conversation(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/conversations/{conversation_id}/archive", response_model=ChatConversation)
async def archive_conversation(conversation_id: str, request: Request) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.archive_conversation(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/conversations/{conversation_id}/restore", response_model=ChatConversation)
async def restore_conversation(conversation_id: str, request: Request) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.restore_conversation(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/conversations/{conversation_id}/messages", response_model=List[ChatMessage])
async def list_messages(conversation_id: str, request: Request) -> List[ChatMessage]:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.list_messages(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, request: Request, body: PostMessageRequest) -> dict:
    await enforce_body_size(request, route_name="chat_messages", default_max_bytes=200_000)
    await enforce_rate_limit(
        request,
        route_name="chat_messages",
        default_limit=120,
        default_window_seconds=60,
    )
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    pm_orchestrator = request.app.state.pm_orchestrator
    try:
        conversation = await chat_manager.get_conversation(conversation_id)
        user_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
        )
        assign_instruction = _extract_assign_instruction(body.content)
        if assign_instruction is not None:
            assign_bot_id = body.bot_id or conversation.default_bot_id
            tool_access = await _effective_tool_access(
                request,
                conversation=conversation,
                target_bot_id=assign_bot_id,
            )
            resolved_context = await _resolve_context_items(
                request,
                body,
                conversation=conversation,
                tool_access=tool_access,
            )
            assignment = await pm_orchestrator.orchestrate_assignment(
                conversation_id=conversation_id,
                instruction=assign_instruction,
                requested_pm_bot_id=body.bot_id,
                context_items=resolved_context,
                project_id=conversation.project_id,
            )
            completion = await pm_orchestrator.wait_for_completion(assignment)
            assistant_message = await pm_orchestrator.persist_summary_message(
                conversation_id=conversation_id,
                assignment=assignment,
                completion=completion,
            )
            return {
                "mode": "assign",
                "user_message": user_message,
                "assistant_message": assistant_message,
                "assignment": assignment,
                "completion": completion,
            }

        messages = await chat_manager.list_messages(conversation_id)
        target_bot_id = body.bot_id or conversation.default_bot_id
        if not target_bot_id:
            return {"user_message": user_message, "assistant_message": None}

        require_repo_evidence = _context_resolution_requested(body)
        repo_intent = _repo_intent_requested(body.content)
        force_project_context = repo_intent
        force_workspace_context = repo_intent
        tool_access = await _effective_tool_access(
            request,
            conversation=conversation,
            target_bot_id=target_bot_id,
        )
        resolved_context = await _resolve_context_items(
            request,
            body,
            conversation=conversation,
            tool_access=tool_access,
            force_project_context=force_project_context,
            force_workspace_context=force_workspace_context,
        )
        context_sources = _context_source_labels(resolved_context, limit=12)
        if not context_sources and resolved_context:
            context_sources = ["context snippets (unlabeled)"]
        if require_repo_evidence and not resolved_context:
            assistant_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=_repo_context_unavailable_message(),
                bot_id=target_bot_id,
            )
            return {"user_message": user_message, "assistant_message": assistant_message}
        payload = _messages_to_payload(
            messages,
            context_items=resolved_context,
            require_repo_evidence=require_repo_evidence,
        )
        task = Task(
            id=f"chat-{user_message.id}",
            bot_id=target_bot_id,
            payload=payload,
            metadata=TaskMetadata(
                source="chat",
                project_id=conversation.project_id,
                conversation_id=conversation_id,
            ),
            status="running",
            created_at=user_message.created_at,
            updated_at=user_message.created_at,
        )
        result = await scheduler.schedule(task)
        assistant_output = _extract_task_output(result)
        assistant_output = _apply_repo_evidence_envelope(
            assistant_output,
            require_repo_evidence=require_repo_evidence,
            context_sources=context_sources,
        )
        assistant_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_output,
            bot_id=target_bot_id,
        )
        return {"user_message": user_message, "assistant_message": assistant_message}
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversations/{conversation_id}/stream")
async def stream_message(conversation_id: str, request: Request, body: PostMessageRequest) -> StreamingResponse:
    await enforce_body_size(request, route_name="chat_stream", default_max_bytes=200_000)
    await enforce_rate_limit(
        request,
        route_name="chat_stream",
        default_limit=60,
        default_window_seconds=60,
    )
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    task_manager = request.app.state.task_manager
    pm_orchestrator = request.app.state.pm_orchestrator

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            conversation = await chat_manager.get_conversation(conversation_id)
            user_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=body.content,
            )
            yield f"event: user_message\ndata: {user_message.model_dump_json()}\n\n"
            assign_instruction = _extract_assign_instruction(body.content)
            if assign_instruction is not None:
                yield 'event: status\ndata: {"phase":"planning","label":"Planning task graph..."}\n\n'
                assign_bot_id = body.bot_id or conversation.default_bot_id
                tool_access = await _effective_tool_access(
                    request,
                    conversation=conversation,
                    target_bot_id=assign_bot_id,
                )
                if _context_resolution_requested(body):
                    yield 'event: status\ndata: {"phase":"context","label":"Collecting repository context..."}\n\n'
                resolved_context = await _resolve_context_items(
                    request,
                    body,
                    conversation=conversation,
                    tool_access=tool_access,
                    force_workspace_context=_repo_intent_requested(assign_instruction),
                )
                context_sources = _context_source_labels(resolved_context, limit=8)
                if context_sources:
                    context_payload = {
                        "snippet_count": len(resolved_context),
                        "source_count": len(context_sources),
                        "sources": context_sources,
                    }
                    yield f"event: context_summary\ndata: {json.dumps(context_payload)}\n\n"
                    yield (
                        'event: status\ndata: '
                        f'{json.dumps({"phase":"context","label":f"Loaded {len(resolved_context)} context snippets from {len(context_sources)} sources."})}\n\n'
                    )
                elif _context_resolution_requested(body):
                    yield (
                        'event: status\ndata: '
                        '{"phase":"context","label":"No repository context retrieved. The response will avoid unverifiable file claims."}\n\n'
                    )
                assignment = await pm_orchestrator.orchestrate_assignment(
                    conversation_id=conversation_id,
                    instruction=assign_instruction,
                    requested_pm_bot_id=body.bot_id,
                    context_items=resolved_context,
                    project_id=conversation.project_id,
                )
                graph_payload = {
                    "orchestration_id": assignment.get("orchestration_id"),
                    "tasks": assignment.get("tasks", []),
                    "plan": assignment.get("plan", {}),
                }
                yield f"event: task_graph\ndata: {json.dumps(graph_payload)}\n\n"

                tracked_ids = [
                    str(t.get("id"))
                    for t in assignment.get("tasks", [])
                    if isinstance(t, dict) and t.get("id")
                ]
                last_status: Dict[str, str] = {}

                while True:
                    all_terminal = True
                    for task_id in tracked_ids:
                        task = await task_manager.get_task(task_id)
                        previous = last_status.get(task_id)
                        if previous != task.status:
                            title = ""
                            if isinstance(task.payload, dict):
                                title = str(task.payload.get("title") or "")
                            payload = {
                                "task_id": task.id,
                                "status": task.status,
                                "bot_id": task.bot_id,
                                "title": title,
                                "result": task.result if task.status == "completed" else None,
                                "error": (
                                    task.error.model_dump()
                                    if task.status == "failed" and task.error
                                    else None
                                ),
                            }
                            yield f"event: task_status\ndata: {json.dumps(payload)}\n\n"
                            last_status[task_id] = task.status
                        if task.status not in {"completed", "failed", "retried"}:
                            all_terminal = False
                    if all_terminal:
                        break
                    await asyncio.sleep(0.4)

                yield 'event: status\ndata: {"phase":"summarizing","label":"Summarizing results..."}\n\n'
                completion = await pm_orchestrator.wait_for_completion(assignment, max_wait_seconds=1.0)
                assistant_message = await pm_orchestrator.persist_summary_message(
                    conversation_id=conversation_id,
                    assignment=assignment,
                    completion=completion,
                )
                yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                yield "event: done\ndata: {}\n\n"
                return

            messages = await chat_manager.list_messages(conversation_id)
            target_bot_id = body.bot_id or conversation.default_bot_id
            if not target_bot_id:
                yield "event: done\ndata: {}\n\n"
                return

            require_repo_evidence = _context_resolution_requested(body)
            repo_intent = _repo_intent_requested(body.content)
            force_project_context = repo_intent
            force_workspace_context = repo_intent
            tool_access = await _effective_tool_access(
                request,
                conversation=conversation,
                target_bot_id=target_bot_id,
            )
            if require_repo_evidence:
                yield 'event: status\ndata: {"phase":"context","label":"Collecting repository context..."}\n\n'
            resolved_context = await _resolve_context_items(
                request,
                body,
                conversation=conversation,
                tool_access=tool_access,
                force_project_context=force_project_context,
                force_workspace_context=force_workspace_context,
            )
            context_sources = _context_source_labels(resolved_context, limit=8)
            if not context_sources and resolved_context:
                context_sources = ["context snippets (unlabeled)"]
            if context_sources:
                context_payload = {
                    "snippet_count": len(resolved_context),
                    "source_count": len(context_sources),
                    "sources": context_sources,
                }
                yield f"event: context_summary\ndata: {json.dumps(context_payload)}\n\n"
                yield (
                    'event: status\ndata: '
                    f'{json.dumps({"phase":"context","label":f"Loaded {len(resolved_context)} context snippets from {len(context_sources)} sources."})}\n\n'
                )
            elif require_repo_evidence:
                yield (
                    'event: status\ndata: '
                    '{"phase":"context","label":"No repository context retrieved. The response will avoid unverifiable file claims."}\n\n'
                )
            if require_repo_evidence and not resolved_context:
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=_repo_context_unavailable_message(),
                    bot_id=target_bot_id,
                )
                yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                yield "event: done\ndata: {}\n\n"
                return
            payload = _messages_to_payload(
                messages,
                context_items=resolved_context,
                require_repo_evidence=require_repo_evidence,
            )
            yield f'event: status\ndata: {json.dumps({"phase":"queued","label":"Queued on selected backend...","conversation_id":conversation_id,"message_count":len(messages)})}\n\n'
            task = Task(
                id=f"chat-{user_message.id}",
                bot_id=target_bot_id,
                payload=payload,
                metadata=TaskMetadata(
                    source="chat",
                    project_id=conversation.project_id,
                    conversation_id=conversation_id,
                ),
                status="running",
                created_at=user_message.created_at,
                updated_at=user_message.created_at,
            )
            result = None
            streamed_chunks: list[str] = []
            assistant_message: Optional[ChatMessage] = None
            stream_provider: Optional[str] = None
            stream_model: Optional[str] = None
            token_counter = 0
            async for event in scheduler.stream(task):
                event_name = str(event.get("event") or "")
                if event_name == "backend_selected":
                    provider = str(event.get("provider") or "unknown")
                    model = str(event.get("model") or "unknown")
                    stream_provider = provider
                    stream_model = model
                    worker_id = str(event.get("worker_id") or "").strip()
                    label = f"Using {provider}/{model}"
                    if worker_id:
                        label += f" on {worker_id}"
                    yield f'event: status\ndata: {json.dumps({"phase": "running", "label": label})}\n\n'
                elif event_name == "dispatch_started":
                    worker_id = str(event.get("worker_id") or "").strip()
                    host = str(event.get("host") or "").strip()
                    port = event.get("port")
                    label = f"Worker {worker_id} accepted request"
                    if host and port:
                        label += f" ({host}:{port})"
                    yield f'event: status\ndata: {json.dumps({"phase": "dispatching", "label": label})}\n\n'
                elif event_name == "token":
                    chunk = str(event.get("text") or "")
                    if chunk:
                        streamed_chunks.append(chunk)
                        if require_repo_evidence:
                            token_counter += 1
                            if token_counter % 32 == 0:
                                yield (
                                    'event: status\ndata: '
                                    '{"phase":"analysis","label":"Analyzing verified repository context..."}\n\n'
                                )
                            continue
                        partial_content = "".join(streamed_chunks)
                        partial_metadata = {"streaming": True}
                        if assistant_message is None:
                            assistant_message = await chat_manager.add_message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=partial_content,
                                bot_id=target_bot_id,
                                model=stream_model,
                                provider=stream_provider,
                                metadata=partial_metadata,
                            )
                        else:
                            assistant_message = await chat_manager.update_message(
                                assistant_message.id,
                                content=partial_content,
                                metadata=partial_metadata,
                                model=stream_model,
                                provider=stream_provider,
                            )
                    if not require_repo_evidence:
                        yield f'event: token\ndata: {json.dumps({"text": chunk})}\n\n'
                elif event_name == "final":
                    result = dict(event)
                elif event_name == "error":
                    payload = json.dumps({"error": event.get("error") or "stream_error"})
                    yield f"event: error\ndata: {payload}\n\n"
                    return
            if result is None and streamed_chunks:
                result = {
                    "output": "".join(streamed_chunks),
                    "usage": {},
                    "partial": True,
                }
            if result is None:
                payload = json.dumps({"error": "stream ended before final response"})
                yield f"event: error\ndata: {payload}\n\n"
                return
            assistant_output = _extract_task_output(result)
            assistant_output = _apply_repo_evidence_envelope(
                assistant_output,
                require_repo_evidence=require_repo_evidence,
                context_sources=context_sources,
            )
            yield 'event: status\ndata: {"phase":"persisting","label":"Saving response..."}\n\n'
            metadata = {"usage": (result or {}).get("usage", {})} if isinstance(result, dict) else {}
            metadata["streaming"] = False
            if isinstance(result, dict) and result.get("partial"):
                metadata["partial"] = True
            if assistant_message is None:
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=assistant_output,
                    bot_id=target_bot_id,
                    model=stream_model,
                    provider=stream_provider,
                    metadata=metadata or None,
                )
            else:
                assistant_message = await chat_manager.update_message(
                    assistant_message.id,
                    content=assistant_output,
                    metadata=metadata or None,
                    model=stream_model,
                    provider=stream_provider,
                )
            yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
            yield "event: done\ndata: {}\n\n"
        except ConversationNotFoundError:
            payload = json.dumps({"error": "conversation_not_found"})
            yield f"event: error\ndata: {payload}\n\n"
        except BotNotFoundError:
            payload = json.dumps({"error": "bot_not_found"})
            yield f"event: error\ndata: {payload}\n\n"
        except Exception as e:
            payload = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
