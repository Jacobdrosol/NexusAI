import asyncio
from collections import Counter
import base64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Tuple
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from control_plane.chat.workspace_tools import (
    build_focus_query,
    extract_path_hints,
    list_workspace_tree,
    normalize_workspace_root,
    read_workspace_file_snippet,
    search_workspace_snippets,
)
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.chat_attachments import (
    CHAT_ATTACHMENT_MAX_FILES,
    CHAT_ATTACHMENT_MAX_TEXT_BYTES,
    CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
)
from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage, Task, TaskMetadata
from shared.settings_manager import get_context_limits_for_model

logger = logging.getLogger(__name__)


def _get_bot_model(bot) -> str:
    """Extract model name from bot's first backend config.
    
    Returns empty string if no backends or model not found.
    """
    if not bot or not bot.backends:
        return ""
    backend = bot.backends[0]
    return str(backend.model or "")


def _get_context_limits_for_bot(bot) -> tuple[int, int]:
    """Return (item_limit, source_limit) based on bot's model context window."""
    model = _get_bot_model(bot)
    if not model:
        return 30, 12  # Default limits
    return get_context_limits_for_model(model)


def _first_backend_snapshot(bot: Any) -> Dict[str, Any]:
    if not bot or not getattr(bot, "backends", None):
        return {}
    backend = bot.backends[0]
    return {
        "backend_type": str(getattr(backend, "type", "") or "") or None,
        "provider": str(getattr(backend, "provider", "") or "") or None,
        "model": str(getattr(backend, "model", "") or "") or None,
        "worker_id": str(getattr(backend, "worker_id", "") or "") or None,
    }


def _assistant_bot_metadata(
    bot: Any,
    *,
    bot_id: str,
    execution_provenance: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    backend = dict(_first_backend_snapshot(bot))
    if execution_provenance:
        backend.update(
            {
                "backend_type": execution_provenance.get("backend_type") or backend.get("backend_type"),
                "provider": execution_provenance.get("provider") or backend.get("provider"),
                "model": execution_provenance.get("model") or backend.get("model"),
                "worker_id": execution_provenance.get("worker_id") or backend.get("worker_id"),
            }
        )
        captured_at = str(execution_provenance.get("captured_at") or "").strip()
        if captured_at:
            backend["captured_at"] = captured_at
        backend["source"] = "scheduler"
    else:
        backend["source"] = "bot_config"
    metadata: Dict[str, Any] = {
        "bot": {
            "id": str(getattr(bot, "id", "") or bot_id),
            "name": str(getattr(bot, "name", "") or "") or None,
            "role": str(getattr(bot, "role", "") or "") or None,
            "project_id": str(getattr(bot, "project_id", "") or "") or None,
            "updated_at": str(getattr(bot, "updated_at", "") or "") or None,
        },
        "model": backend,
    }
    if extra:
        metadata.update(extra)
    return metadata


def _assistant_model_provider(
    bot: Any,
    execution_provenance: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    backend = dict(_first_backend_snapshot(bot))
    if execution_provenance:
        backend.update(
            {
                "provider": execution_provenance.get("provider") or backend.get("provider"),
                "model": execution_provenance.get("model") or backend.get("model"),
            }
        )
    return (
        str(backend.get("model") or "") or None,
        str(backend.get("provider") or "") or None,
    )

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
    inline_coding_enabled: bool = False
    attachments: List["ChatAttachmentInput"] = Field(default_factory=list)


class ChatAttachmentInput(BaseModel):
    name: str
    mime_type: str
    kind: Literal["image", "text", "binary"]
    size_bytes: int = 0
    data_url: Optional[str] = None
    text_content: Optional[str] = None


class UpdateConversationToolAccessRequest(BaseModel):
    enabled: bool = False
    filesystem: bool = False
    repo_search: bool = False


_REPO_ACTION_RE = re.compile(
    r"\b(read|search|scan|inspect|review|audit|analy[sz]e|open|look\s+through|walk\s+through|go\s+through)\b",
    re.IGNORECASE,
)
_REPO_TARGET_RE = re.compile(
    r"\b(repo(?:sitory)?|codebase|source\s+code|workspace|project\s+files?|file\s+tree|files?|folders?|directories?)\b",
    re.IGNORECASE,
)
_REPO_REQUEST_CUE_RE = re.compile(
    r"\b((?:can|could|would|will)\s+you|please|help(?:\s+me)?|i\s+need\s+you\s+to|let(?:'|â€™)?s)\b",
    re.IGNORECASE,
)
_REPO_TRANSCRIPT_MARKER_RE = re.compile(
    r"^\s*(files inspected \(verified context\)|source-of-truth|supporting context|\[S\d+\]|assistant|response|copy|re-run|send to vault)\b",
    re.IGNORECASE,
)
_REPO_NEGATION_RE = re.compile(
    r"\b(don['â€™]?t|do\s+not|doesn['â€™]?t|does\s+not|stop|avoid|without|instead)\b[^.\n]{0,80}\b(repo(?:sitory)?|repo\s+search|workspace\s+tools?|project\s+context)\b",
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
_ACCESS_DENIAL_LINE_RE = re.compile(
    r"^\s*(i\s+appreciate\s+you\s+setting\s+that\s+up|"
    r"i\s+need\s+to\s+be\s+transparent|"
    r"i\s+(?:cannot|can['’]?t)\s+(?:directly\s+)?(?:access|browse|read)|"
    r"i\s+do\s+not\s+currently\s+see\s+any\s+workspace\s+tools|"
    r"i\s+do\s+not\s+currently\s+have\s+(?:workspace|file\s*system|repo)\s+access|"
    r"they\s+aren['’]?t\s+always\s+passed\s+through\s+to\s+the\s+model\s+instance|"
    r"this\s+sometimes\s+happens\s+depending\s+on\s+the\s+configuration\b|"
    r"depending\s+on\s+the\s+configuration\b|"
    r"however,\s+given\s+your\b)",
    re.IGNORECASE,
)
_IMAGE_CAPABILITY_MARKERS = {"image", "images", "vision", "multimodal"}
_INLINE_CODE_TRIGGER_RE = re.compile(
    r"(?:^|\b)(?:please\s+code\s+this|code\s+this|can\s+you\s+code\s+this|"
    r"please\s+implement\s+this|implement\s+this|can\s+you\s+implement\s+this)\b",
    re.IGNORECASE,
)
_INLINE_INTEGRATION_EXPECTED_RE = re.compile(
    r"\b(add(?:\s+(?:a|an|the))?\s+feature|extend|modify|update|integrat(?:e|ion)|wire(?:\s+up)?|"
    r"hook(?:\s+up)?|existing|we(?:'ve|\s+have)\s+built|in\s+the\s+\w+\s+view|programs?\s+view)\b",
    re.IGNORECASE,
)
_INLINE_NEW_FILES_ONLY_OK_RE = re.compile(
    r"\b(new\s+file|new\s+module|from\s+scratch|scaffold|skeleton|prototype\s+only)\b",
    re.IGNORECASE,
)
_INLINE_SERVER_KEYWORD_RE = re.compile(r"\b(server|backend)\b", re.IGNORECASE)
_INLINE_WEBAPP_KEYWORD_RE = re.compile(r"\b(web\s*app|webapp|frontend|front[-\s]?end|ui)\b", re.IGNORECASE)
_INLINE_SERVER_PATH_RE = re.compile(r"(?:^|/)[^/]*server[^/]*/", re.IGNORECASE)
_INLINE_WEBAPP_PATH_RE = re.compile(
    r"(?:^|/)(?:[^/]*webapp[^/]*|[^/]*frontend[^/]*|[^/]*client[^/]*|[^/]*ui[^/]*)/",
    re.IGNORECASE,
)
_INLINE_LOW_SIGNAL_OUTPUT_LINE_RE = re.compile(
    r"^\s*(?:now\s+)?(?:let\s+me|i\s+need\s+to)\s+(?:check|look|find|inspect|review|read|scan|explore)\b|"
    r"^\s*i(?:\s*['’]?m|\s+am)\s+ready\s+to\s+help\b|"
    r"^\s*could\s+you\s+(?:share|provide|specify)\b|"
    r"^\s*please\s+(?:let\s+me\s+know|specify|share)\b",
    re.IGNORECASE,
)
_INLINE_LOW_SIGNAL_FIRST_LINE_RE = re.compile(
    r"^\s*(?:now\s+)?(?:let\s+me|first,\s*let\s+me|next,\s*let\s+me|i\s+need\s+to)\b",
    re.IGNORECASE,
)
_INLINE_OUTPUT_CHANGE_MARKER_RE = re.compile(
    r"\b(updated?|modified|created|deleted|added|implemented|wired|integrated|refactored|patched|fixed)\b",
    re.IGNORECASE,
)
_INLINE_DELIVERABLE_SCHEDULE_RE = re.compile(
    r"\b(schedule|scheduler|frequency|end\s+of\s+day|end\s+of\s+week|end\s+of\s+month|end\s+of\s+quarter|end\s+of\s+year)\b",
    re.IGNORECASE,
)
_INLINE_DELIVERABLE_REPORT_RE = re.compile(
    r"\b(report|reporting|accounting|financial|month[-\s]?end|month\s+end)\b",
    re.IGNORECASE,
)
_INLINE_DELIVERABLE_PDF_RE = re.compile(r"\b(pdf|export)\b", re.IGNORECASE)
_INLINE_FEATURE_REQUEST_RE = re.compile(
    r"\b(add|build|implement|create|introduce|feature|enhancement|code\s+this|make\s+it\s+happen)\b",
    re.IGNORECASE,
)
_INLINE_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:test|tests|spec|specs|__tests__)(?:/|$)|"
    r"(?:^|/)[^/]*\.(?:test|spec)\.[^/]+$|"
    r"(?:^|/)[^/]*tests?\.[^/]+$",
    re.IGNORECASE,
)
_INLINE_CODE_FILE_EXTENSIONS: set[str] = {
    "c",
    "cc",
    "cpp",
    "cs",
    "css",
    "go",
    "h",
    "hpp",
    "html",
    "java",
    "js",
    "jsx",
    "kt",
    "lua",
    "php",
    "py",
    "razor",
    "rb",
    "rs",
    "scala",
    "sh",
    "sql",
    "swift",
    "ts",
    "tsx",
    "vue",
}
_INLINE_NON_CODE_FILE_EXTENSIONS: set[str] = {
    "cfg",
    "config",
    "csproj",
    "csv",
    "env",
    "gif",
    "ico",
    "jpeg",
    "jpg",
    "json",
    "lock",
    "map",
    "md",
    "pdf",
    "png",
    "props",
    "resx",
    "settings",
    "sln",
    "svg",
    "toml",
    "txt",
    "xml",
    "yaml",
    "yml",
}
_INLINE_NON_CODE_FILENAMES: set[str] = {
    "dockerfile",
    "makefile",
    "readme",
    "license",
    "changelog",
}


def _attachment_size_bytes(item: ChatAttachmentInput) -> int:
    if item.size_bytes and item.size_bytes > 0:
        return int(item.size_bytes)
    kind = str(item.kind or "").strip().lower()
    if kind == "text":
        return len(str(item.text_content or "").encode("utf-8"))
    if kind == "image":
        data_url = str(item.data_url or "").strip()
        if "," not in data_url:
            return 0
        _, encoded = data_url.split(",", 1)
        try:
            return len(base64.b64decode(encoded, validate=False))
        except Exception:
            return 0
    return 0


def _validate_attachment_limits(attachments: List[ChatAttachmentInput]) -> None:
    if len(attachments or []) > CHAT_ATTACHMENT_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many attachments. Maximum is {CHAT_ATTACHMENT_MAX_FILES} files per message.",
        )
    total_bytes = sum(max(0, _attachment_size_bytes(item)) for item in (attachments or []))
    if total_bytes > CHAT_ATTACHMENT_MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Attachment limit exceeded. Maximum total attachment size is 1 GB per message.",
        )


def _attachment_payload_dicts(attachments: List[ChatAttachmentInput]) -> List[Dict[str, Any]]:
    _validate_attachment_limits(attachments)
    normalized: List[Dict[str, Any]] = []
    for item in attachments or []:
        kind = str(item.kind or "").strip().lower()
        name = str(item.name or "").strip() or "attachment"
        mime_type = str(item.mime_type or "").strip().lower() or "application/octet-stream"
        size_bytes = max(0, _attachment_size_bytes(item))
        if kind == "image":
            data_url = str(item.data_url or "").strip()
            if not data_url.startswith("data:image/"):
                raise HTTPException(status_code=400, detail=f"Attachment '{name}' must provide an image data URL.")
            normalized.append(
                {
                    "name": name,
                    "mime_type": mime_type,
                    "kind": "image",
                    "data_url": data_url,
                    "size_bytes": size_bytes,
                }
            )
            continue
        if kind == "text":
            text_content = str(item.text_content or "")
            if not text_content.strip():
                raise HTTPException(status_code=400, detail=f"Attachment '{name}' must include text content.")
            normalized.append(
                {
                    "name": name,
                    "mime_type": mime_type,
                    "kind": "text",
                    "text_content": text_content[:CHAT_ATTACHMENT_MAX_TEXT_BYTES],
                    "size_bytes": size_bytes,
                    "truncated": size_bytes > CHAT_ATTACHMENT_MAX_TEXT_BYTES,
                }
            )
            continue
        normalized.append(
            {
                "name": name,
                "mime_type": mime_type,
                "kind": "binary",
                "size_bytes": size_bytes,
            }
        )
    return normalized


def _message_attachment_parts(metadata: Any) -> List[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return []
    parts: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        name = str(item.get("name") or "").strip() or "attachment"
        mime_type = str(item.get("mime_type") or "").strip() or "application/octet-stream"
        if kind == "image":
            data_url = str(item.get("data_url") or "").strip()
            if data_url.startswith("data:image/"):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                        "name": name,
                        "mime_type": mime_type,
                    }
                )
            continue
        text_content = str(item.get("text_content") or "")
        if text_content.strip():
            suffix = ""
            if bool(item.get("truncated")):
                suffix = "\n[Attachment content was truncated before model delivery.]"
            parts.append(
                {
                    "type": "text",
                    "text": f"[Attached file: {name} ({mime_type})]\n{text_content}{suffix}",
                    "name": name,
                    "mime_type": mime_type,
                }
            )
            continue
        if kind == "binary":
            size_bytes = int(item.get("size_bytes") or 0)
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached file: {name} ({mime_type}, {size_bytes} bytes)]\n"
                        "Binary attachment was included with the message but its raw contents were not inlined."
                    ),
                    "name": name,
                    "mime_type": mime_type,
                }
            )
    return parts


async def _target_supports_image_attachments(request: Request, *, target_bot_id: str) -> bool:
    bot_registry = getattr(request.app.state, "bot_registry", None)
    model_registry = getattr(request.app.state, "model_registry", None)
    if bot_registry is None:
        return False
    try:
        bot = await bot_registry.get(target_bot_id)
    except Exception:
        return False
    backends = getattr(bot, "backends", None) or []
    if not backends:
        return False
    backend = backends[0]
    provider = str(getattr(backend, "provider", "") or "").strip().lower()
    model_name = str(getattr(backend, "model", "") or "").strip()
    if model_registry is not None:
        try:
            for catalog_model in await model_registry.list():
                if not bool(getattr(catalog_model, "enabled", True)):
                    continue
                if str(getattr(catalog_model, "provider", "") or "").strip().lower() != provider:
                    continue
                if str(getattr(catalog_model, "name", "") or "").strip() != model_name:
                    continue
                caps = {str(item or "").strip().lower() for item in (getattr(catalog_model, "capabilities", None) or [])}
                return bool(caps & _IMAGE_CAPABILITY_MARKERS)
        except Exception:
            pass
    lowered_model = model_name.lower()
    if provider == "gemini":
        return True
    if provider == "openai":
        return any(token in lowered_model for token in ("gpt-4o", "gpt-4.1", "gpt-5"))
    if provider == "claude":
        return any(token in lowered_model for token in ("claude-3", "claude-4"))
    if provider in {"ollama_cloud", "ollama"}:
        return any(
            token in lowered_model
            for token in ("vision", "-vl", "qwen2.5-vl", "qwen-vl", "qwen3-vl", "qwen3.5:", "llava", "gemma3")
        )
    return False
_GROUNDING_NOTE_LINE_RE = re.compile(r"^\s*grounding\s+note\s*:\s*", re.IGNORECASE)
_PLANNING_PREAMBLE_LINE_RE = re.compile(
    r"^\s*(i(?:\s*['’]ll|\s+will)\s+help\s+you\b|"
    r"let\s+me\s+start\s+by\s+(?:reading|reviewing|checking)\b|"
    r"i(?:\s*['’]m|\s+am)\s+going\s+to\s+(?:read|review|check|scan)\b)",
    re.IGNORECASE,
)
_TOOL_ECHO_LINE_RE = re.compile(
    r"^\s*(read_file|search_file|open_file|list_files|scan_repo|inspect_file|analyze_file)\b",
    re.IGNORECASE,
)
_TOOL_ARG_LINE_RE = re.compile(
    r"(^|\b)(pattern|path|file|query|glob|limit|max_results|recursive)\s*:\s*",
    re.IGNORECASE,
)
_CODE_FENCE_LINE_RE = re.compile(r"^\s*```[\w-]*\s*$")
_CITATION_TAIL_RATIO = 0.75
_CITATION_DENSITY_WINDOW = 900
_UNCITED_MAX_LINES = 60
_UNCITED_MAX_CHARS = 6000
_UNCITED_MAX_LINE_CHARS = 1800
_DEFAULT_GROUNDED_FALLBACK = (
    "Actionable next steps from verified context:\n"
    "1. Build a gap list: current controllers/schemas vs required lesson-block capabilities.\n"
    "2. Expand context to models + services + UI renderer files, then prioritize missing contracts.\n"
    "3. Implement in phases: schema/contracts, backend services/controllers, UI block components, tests.\n"
    "4. Run one end-to-end validation pass and capture follow-up fixes."
)


def _repo_intent_requested(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    candidate_lines: List[str] = []
    total_chars = 0
    for raw in text.splitlines():
        line = str(raw or "").strip()
        if not line and candidate_lines:
            break
        if _REPO_TRANSCRIPT_MARKER_RE.match(line):
            break
        if not line:
            continue
        candidate_lines.append(line)
        total_chars += len(line) + 1
        if total_chars >= 420:
            break
    candidate = " ".join(candidate_lines).strip() if candidate_lines else text[:420].strip()
    if not candidate:
        return False
    if _REPO_NEGATION_RE.search(candidate):
        return False
    lowered_candidate = candidate.lower()
    if "code review" in lowered_candidate and (
        bool(_REPO_REQUEST_CUE_RE.search(candidate))
        or candidate.endswith("?")
        or lowered_candidate.startswith(("code review", "do a code review"))
    ):
        return True
    if not _REPO_ACTION_RE.search(candidate):
        return False
    if not _REPO_TARGET_RE.search(candidate):
        return False
    return bool(
        _REPO_REQUEST_CUE_RE.search(candidate)
        or candidate.endswith("?")
        or lowered_candidate.startswith(
            ("read ", "search ", "scan ", "inspect ", "review ", "analyze ", "analyse ", "open ", "look through ", "walk through ", "go through ")
        )
    )


def _context_resolution_requested(body: PostMessageRequest) -> bool:
    return bool(
        body.context_items
        or body.context_item_ids
        or body.include_project_context
        or body.use_workspace_tools
        or _repo_intent_requested(body.content)
    )


def _repo_evidence_requested(body: PostMessageRequest) -> bool:
    return bool(
        body.context_items
        or body.context_item_ids
        or body.include_project_context
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
    payload: List[dict] = []
    for message in messages:
        attachment_parts = _message_attachment_parts(message.metadata)
        if attachment_parts:
            content_parts: List[Dict[str, Any]] = []
            if str(message.content or "").strip():
                content_parts.append({"type": "text", "text": str(message.content)})
            content_parts.extend(attachment_parts)
            payload.append({"role": message.role, "content": content_parts})
        else:
            payload.append({"role": message.role, "content": message.content})
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
        return f"{prefix}\n{_condense_uncited_grounded_output('')}"
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
    return f"{prefix}\n{uncited_summary}"


def _sanitize_repo_grounded_output(output: str) -> str:
    def _is_tool_artifact_line(line: str) -> bool:
        stripped_line = str(line or "").strip()
        if not stripped_line:
            return False
        if not _TOOL_ARG_LINE_RE.search(stripped_line):
            return False
        lowered_line = stripped_line.lower()
        # Keep natural-language "file:" references, strip CLI-like argument lines.
        if lowered_line.startswith("files inspected"):
            return False
        return any(token in stripped_line for token in ("*", "/", "\\", ".cs", ".razor", ".py", ".ts"))

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
    previous_was_tool_echo = False
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
        if _CODE_FENCE_LINE_RE.match(stripped):
            continue
        if _TOOL_ECHO_LINE_RE.search(stripped):
            previous_was_tool_echo = True
            continue
        if previous_was_tool_echo and (_TOOL_ARG_LINE_RE.search(stripped) or not stripped):
            continue
        previous_was_tool_echo = False
        if _is_tool_artifact_line(stripped):
            continue
        if _UNVERIFIABLE_ACTION_LINE_RE.search(stripped):
            continue
        if _REQUEST_PERMISSION_LINE_RE.search(stripped):
            continue
        if _ACCESS_DENIAL_LINE_RE.search(stripped):
            continue
        if _GROUNDING_NOTE_LINE_RE.search(stripped):
            continue
        if _PLANNING_PREAMBLE_LINE_RE.search(stripped):
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
        return _DEFAULT_GROUNDED_FALLBACK
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
        if _ACCESS_DENIAL_LINE_RE.search(line):
            continue
        if _GROUNDING_NOTE_LINE_RE.search(line):
            continue
        if _PLANNING_PREAMBLE_LINE_RE.search(line):
            continue
        if _TOOL_ARG_LINE_RE.search(line) and any(token in line for token in ("*", "/", "\\", ".cs", ".razor", ".py", ".ts")):
            continue
        if len(line) > _UNCITED_MAX_LINE_CHARS:
            line = line[:_UNCITED_MAX_LINE_CHARS].rstrip() + "..."
        kept.append(line)
        if len(kept) >= _UNCITED_MAX_LINES:
            break
    compacted = "\n".join(kept).strip()
    if not compacted:
        return _DEFAULT_GROUNDED_FALLBACK
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


_REPO_PROFILE_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "bin",
    "obj",
    "dist",
    "build",
    "out",
    ".idea",
    ".vs",
}
_REPO_PROFILE_MARKER_FILES = {
    ".sln",
    ".csproj",
    ".fsproj",
    ".vbproj",
    "package.json",
    "tsconfig.json",
    "vite.config.ts",
    "vite.config.js",
    "next.config.js",
    "next.config.mjs",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "meson.build",
    "Makefile",
}
_REPO_PROFILE_MARKER_FILES_LOWER = {name.lower() for name in _REPO_PROFILE_MARKER_FILES}


def _scan_repo_profile(root: Path, *, max_files: int = 2000) -> Dict[str, Any]:
    ext_counts: Counter[str] = Counter()
    sample_paths: Dict[str, List[str]] = {}
    marker_paths: List[str] = []
    scanned_files = 0

    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _REPO_PROFILE_SKIP_DIRS]
        for filename in filenames:
            scanned_files += 1
            if scanned_files > max_files:
                break
            full_path = Path(current_root) / filename
            try:
                rel_path = full_path.relative_to(root).as_posix()
            except Exception:
                continue
            lowered_name = filename.lower()
            suffix = full_path.suffix.lower()
            if lowered_name in _REPO_PROFILE_MARKER_FILES_LOWER or suffix in {
                ".sln",
                ".csproj",
                ".fsproj",
                ".vbproj",
                ".razor",
            }:
                marker_paths.append(rel_path)
            ext_key = suffix if suffix else f"[{lowered_name}]"
            ext_counts[ext_key] += 1
            if suffix and len(sample_paths.get(suffix, [])) < 3:
                sample_paths.setdefault(suffix, []).append(rel_path)
        if scanned_files > max_files:
            break

    top_exts = ext_counts.most_common(6)
    lower_markers = {path.lower() for path in marker_paths}

    has_dotnet = any(path.endswith((".sln", ".csproj", ".fsproj", ".vbproj")) for path in lower_markers)
    has_razor = any(path.endswith(".razor") for path in lower_markers) or ext_counts.get(".razor", 0) > 0
    has_typescript = "package.json" in lower_markers or "tsconfig.json" in lower_markers or ext_counts.get(".ts", 0) > 0 or ext_counts.get(".tsx", 0) > 0
    has_javascript = "package.json" in lower_markers or ext_counts.get(".js", 0) > 0 or ext_counts.get(".jsx", 0) > 0
    has_python = (
        "pyproject.toml" in lower_markers
        or "requirements.txt" in lower_markers
        or "poetry.lock" in lower_markers
        or ext_counts.get(".py", 0) > 0
    )
    has_go = "go.mod" in lower_markers or ext_counts.get(".go", 0) > 0
    has_rust = "cargo.toml" in lower_markers or ext_counts.get(".rs", 0) > 0
    has_cpp = "cmakelists.txt" in lower_markers or ext_counts.get(".cpp", 0) > 0 or ext_counts.get(".hpp", 0) > 0 or ext_counts.get(".h", 0) > 0

    stack_signals: List[str] = []
    guidance: List[str] = ["Match nearby existing files and project structure before introducing a new language."]
    if has_dotnet and has_razor:
        stack_signals.append(".NET / ASP.NET Razor")
        guidance.append("Pages and UI components should prefer `.razor` files alongside existing Razor files.")
        guidance.append("Backend and service changes should prefer `.cs` files inside the existing project/solution structure.")
    elif has_dotnet:
        stack_signals.append(".NET")
        guidance.append("Prefer `.cs` files inside the existing `.csproj` / solution structure for implementation work.")
    if has_typescript:
        stack_signals.append("TypeScript / Node")
        guidance.append("Web UI and frontend logic should prefer `.ts` / `.tsx` and existing package-managed conventions.")
    elif has_javascript:
        stack_signals.append("JavaScript / Node")
        guidance.append("Use the existing JavaScript project structure instead of introducing a new runtime unless required.")
    if has_python:
        stack_signals.append("Python")
        guidance.append("Only choose Python for modules that already live in Python or when the repo context clearly points there.")
    if has_go:
        stack_signals.append("Go")
        guidance.append("Service or CLI work in Go repos should stay in the existing module and package layout.")
    if has_rust:
        stack_signals.append("Rust")
        guidance.append("Rust changes should prefer the existing crate structure and Cargo-managed workflows.")
    if has_cpp:
        stack_signals.append("C/C++")
        guidance.append("Native or desktop/runtime components should stay in the existing C/C++ build system and file layout.")
    if not stack_signals and top_exts:
        stack_signals.append("Mixed or unclear stack")
        guidance.append("Infer file type from adjacent files in the touched area instead of defaulting to Python.")

    return {
        "marker_paths": marker_paths[:8],
        "top_exts": top_exts,
        "sample_paths": sample_paths,
        "stack_signals": stack_signals,
        "guidance": guidance,
        "scanned_files": scanned_files,
    }


def _format_repo_profile_context_item(root: Path) -> str:
    if not root.exists() or not root.is_dir():
        return ""
    try:
        profile = _scan_repo_profile(root)
    except Exception:
        return ""
    lines = ["[repo-profile] Workspace stack summary"]
    stack_signals = profile.get("stack_signals") or []
    if stack_signals:
        lines.append("Likely primary stack: " + ", ".join(str(item) for item in stack_signals))
    marker_paths = profile.get("marker_paths") or []
    if marker_paths:
        lines.append("Key repo markers: " + "; ".join(str(item) for item in marker_paths))
    top_exts = profile.get("top_exts") or []
    if top_exts:
        ext_text = ", ".join(f"{ext} ({count})" for ext, count in top_exts)
        lines.append("Dominant file types: " + ext_text)
    sample_paths = profile.get("sample_paths") or {}
    for ext in (".razor", ".cs", ".ts", ".tsx", ".py", ".cpp"):
        samples = sample_paths.get(ext) or []
        if samples:
            lines.append(f"Example {ext} files: " + "; ".join(samples[:3]))
    guidance = profile.get("guidance") or []
    if guidance:
        lines.append("Implementation guidance:")
        lines.extend(f"- {item}" for item in guidance[:5])
    lines.append("Use this repo profile as the source of truth for language, framework, and file extension choices.")
    return "\n".join(lines)


async def _resolve_repo_profile_context_item(*, workspace_root: str | None) -> List[str]:
    root = normalize_workspace_root(workspace_root)
    if root is None:
        return []
    item = await asyncio.to_thread(_format_repo_profile_context_item, root)
    return [item] if item else []


async def _effective_tool_access(
    request: Request,
    *,
    conversation: ChatConversation,
    target_bot_id: str | None,
) -> Dict[str, Any]:
    if not target_bot_id:
        return {
            "enabled": False,
            "filesystem": False,
            "repo_search": False,
            "workspace_root": None,
        }

    bot_registry = getattr(request.app.state, "bot_registry", None)
    if bot_registry is None:
        return {
            "enabled": False,
            "filesystem": False,
            "repo_search": False,
            "workspace_root": None,
        }
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
    workspace_root = project_cfg.get("workspace_root")
    if not workspace_root and project_id:
        project_registry = getattr(request.app.state, "project_registry", None)
        if project_registry is not None:
            try:
                project = await project_registry.get(project_id)
                workspace_root = _project_repo_workspace_root(project)
            except Exception:
                workspace_root = None
    if not all_enabled:
        return {
            "enabled": False,
            "filesystem": False,
            "repo_search": False,
            "workspace_root": workspace_root,
        }
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


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


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
    item_limit: int = 30,
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

    repo_profile_context: List[str] = []
    if workspace_root and (filesystem_allowed or force_workspace_context or force_project_context):
        repo_profile_context = await _resolve_repo_profile_context_item(workspace_root=workspace_root)

    repo_context: List[str] = []
    if (body.include_project_context or body.use_workspace_tools or force_project_context) and repo_search_allowed and conversation is not None:
        repo_context = await _resolve_project_repo_context_items(
            request,
            conversation=conversation,
            query=body.content,
        )

    # Source-of-truth ordering: workspace -> ingested repo -> explicitly-selected vault -> manual context
    resolved.extend(repo_profile_context)
    resolved.extend(workspace_context)
    resolved.extend(repo_context)
    resolved.extend(vault_items)
    resolved.extend(manual_context)
    return _order_context_items(resolved, limit=item_limit)


def _extract_assign_instruction(content: str) -> Optional[str]:
    text = content.strip()
    if not text.lower().startswith("@assign"):
        return None
    instruction = text[len("@assign"):].strip()
    return instruction or None


def _inline_code_requested(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if _extract_assign_instruction(text) is not None:
        return False
    return bool(_INLINE_CODE_TRIGGER_RE.search(text))


def _bot_inline_coding_default_enabled(bot: Any) -> bool:
    if bot is None:
        return False
    exec_policy = getattr(bot, "execution_policy", None)
    if isinstance(exec_policy, dict):
        return bool(exec_policy.get("inline_coding_default", False))
    return bool(getattr(exec_policy, "inline_coding_default", False))


def _inline_code_mode_requested(body: PostMessageRequest, *, bot: Any = None) -> bool:
    requested = _inline_code_requested(body.content)
    if not requested:
        return False
    require_flag_raw = str(os.environ.get("NEXUSAI_INLINE_CODE_REQUIRE_FLAG", "1") or "1").strip().lower()
    require_flag = require_flag_raw not in {"0", "false", "no", "off"}
    if not require_flag:
        return True
    if bool(getattr(body, "inline_coding_enabled", False)):
        return True
    return _bot_inline_coding_default_enabled(bot)


def _inline_code_unavailable_message() -> str:
    return (
        "Inline coding mode was requested, but workspace editing tools are not available for this chat turn.\n\n"
        "Enable all three switches and retry the same message:\n"
        "1. Project -> Chat Workspace Tools -> filesystem\n"
        "2. Bot -> Chat Tool Access -> filesystem\n"
        "3. Conversation -> Chat Tool Access -> enabled + filesystem"
    )


def _inline_code_existing_edits_expected(requested_task: str) -> bool:
    text = str(requested_task or "").strip()
    if not text:
        return False
    if _INLINE_NEW_FILES_ONLY_OK_RE.search(text):
        return False
    return bool(_INLINE_INTEGRATION_EXPECTED_RE.search(text))


def _inline_code_change_breakdown(artifacts: List[Dict[str, Any]], deleted_paths: List[str]) -> Dict[str, Any]:
    created_paths: List[str] = []
    updated_paths: List[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status == "created":
            created_paths.append(path)
        else:
            updated_paths.append(path)
    deleted = [str(path or "").strip().replace("\\", "/") for path in (deleted_paths or []) if str(path or "").strip()]
    return {
        "created_count": len(created_paths),
        "updated_count": len(updated_paths),
        "deleted_count": len(deleted),
        "created_paths": created_paths[:24],
        "updated_paths": updated_paths[:24],
        "deleted_paths": deleted[:24],
    }


def _inline_code_require_existing_code_surface_edits() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_REQUIRE_EXISTING_CODE_SURFACE_EDITS", True)


def _inline_code_require_deliverable_contract() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_REQUIRE_DELIVERABLE_CONTRACT", True)


def _inline_code_require_feature_test_edits() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_REQUIRE_FEATURE_TEST_EDITS", True)


def _inline_code_force_deterministic_completion_summary() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_FORCE_DETERMINISTIC_SUMMARY", True)


def _inline_code_is_code_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    filename = normalized.rsplit("/", 1)[-1].strip().lower()
    if not filename:
        return False
    basename = filename.split(".", 1)[0]
    if basename in _INLINE_NON_CODE_FILENAMES:
        return False
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].strip().lower()
    if not ext:
        return False
    if ext in _INLINE_CODE_FILE_EXTENSIONS:
        return True
    if ext in _INLINE_NON_CODE_FILE_EXTENSIONS:
        return False
    return False


def _inline_code_existing_code_surface_coverage(
    breakdown: Dict[str, Any] | None,
    required_surfaces: List[str],
) -> Dict[str, Any]:
    updated_paths = list((breakdown or {}).get("updated_paths") or [])
    deleted_paths = list((breakdown or {}).get("deleted_paths") or [])
    existing_paths = _inline_code_merge_paths(updated_paths, deleted_paths)
    existing_code_paths = [path for path in existing_paths if _inline_code_is_code_path(path)]
    coverage = _inline_code_surface_coverage(existing_code_paths, required_surfaces)
    coverage["existing_code_paths"] = existing_code_paths[:24]
    return coverage


def _inline_code_required_deliverables(requested_task: str) -> List[str]:
    text = str(requested_task or "").strip().lower()
    required: List[str] = []
    if not text:
        return required
    if _INLINE_DELIVERABLE_SCHEDULE_RE.search(text):
        required.append("scheduling")
    if _INLINE_DELIVERABLE_REPORT_RE.search(text):
        required.append("reporting")
    if _INLINE_DELIVERABLE_PDF_RE.search(text):
        required.append("pdf_export")
    return required


def _inline_code_deliverable_contract_coverage(
    *,
    requested_task: str,
    files_touched: List[str],
    artifacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    required = _inline_code_required_deliverables(requested_task)
    evidence_chunks: List[str] = []
    for path in list(files_touched or [])[:40]:
        normalized = str(path or "").strip().replace("\\", "/")
        if normalized:
            evidence_chunks.append(normalized)
    for item in artifacts[:24]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if path:
            evidence_chunks.append(path)
        content = str(item.get("content") or "")
        if content:
            evidence_chunks.append(content[:4_000])
    evidence_text = "\n".join(evidence_chunks)
    checks = {
        "scheduling": bool(_INLINE_DELIVERABLE_SCHEDULE_RE.search(evidence_text)),
        "reporting": bool(_INLINE_DELIVERABLE_REPORT_RE.search(evidence_text)),
        "pdf_export": bool(_INLINE_DELIVERABLE_PDF_RE.search(evidence_text)),
    }
    missing = [name for name in required if not checks.get(name, False)]
    touched: List[str] = [name for name in required if checks.get(name, False)]
    return {
        "required_deliverables": required,
        "touched_deliverables": touched,
        "missing_deliverables": missing,
        "passed": not bool(missing),
    }


def _inline_code_deliverable_contract_gate_failure_message(contract: Dict[str, Any]) -> str:
    required = ", ".join(contract.get("required_deliverables") or []) or "(none)"
    missing = ", ".join(contract.get("missing_deliverables") or []) or "(none)"
    return (
        "Quality gate failed: explicit request deliverables were not fully implemented.\n"
        f"Required deliverables: {required}\n"
        f"Missing deliverables: {missing}"
    )


def _inline_code_feature_request_expected(requested_task: str, *, integration_required: bool) -> bool:
    if not integration_required:
        return False
    text = str(requested_task or "").strip()
    if not text:
        return False
    return bool(_INLINE_FEATURE_REQUEST_RE.search(text))


def _inline_code_test_paths(paths: List[str]) -> List[str]:
    test_paths: List[str] = []
    seen: set[str] = set()
    for raw in list(paths or []):
        normalized = str(raw or "").strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        if _INLINE_TEST_PATH_RE.search(normalized):
            seen.add(normalized)
            test_paths.append(normalized)
    return test_paths


def _inline_code_test_coverage(
    *,
    requested_task: str,
    integration_required: bool,
    files_touched: List[str],
    deleted_paths: List[str],
) -> Dict[str, Any]:
    tests_required = bool(
        _inline_code_require_feature_test_edits()
        and _inline_code_feature_request_expected(requested_task, integration_required=integration_required)
    )
    all_paths = _inline_code_merge_paths(list(files_touched or []), list(deleted_paths or []))
    test_paths = _inline_code_test_paths(all_paths)
    return {
        "tests_required": tests_required,
        "test_paths": test_paths,
        "passed": (not tests_required) or bool(test_paths),
    }


def _inline_code_test_coverage_gate_failure_message(test_coverage: Dict[str, Any]) -> str:
    touched = ", ".join(test_coverage.get("test_paths") or []) or "(none)"
    return (
        "Quality gate failed: feature work did not include test file edits.\n"
        "Expected at least one test update to validate behavior.\n"
        f"Test files touched: {touched}"
    )


def _inline_code_test_coverage_warning_message(test_coverage: Dict[str, Any]) -> str:
    touched = ", ".join(test_coverage.get("test_paths") or []) or "(none)"
    return (
        "Quality warning: feature work did not include test file edits.\n"
        "A test-remediation pass was attempted when enabled, but no test files were updated.\n"
        f"Test files touched: {touched}"
    )


def _inline_code_new_files_only_warning_message(task: Task, breakdown: Dict[str, Any]) -> str:
    created_paths = list(breakdown.get("created_paths") or [])
    created_preview = ", ".join(created_paths[:8]) if created_paths else "(none)"
    output = _extract_task_output(task.result).strip()
    if len(output) > 1800:
        output = f"{output[:1800].rstrip()}..."
    base = (
        "Quality warning: this run created new files but did not modify existing tracked files.\n\n"
        "For feature work in an existing repository, quality is usually better when at least one existing file is integrated "
        "(for example wiring DI/service registration, route/controller binding, startup config, or existing UI integration)."
    )
    details = f"Created files: {created_preview}"
    if not output:
        return f"{base}\n\n{details}"
    return f"{base}\n\n{details}\n\nModel output:\n{output}"


def _inline_code_write_call_has_material_change(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    name = str(item.get("name") or "").strip().lower()
    if name not in {"write_file", "edit_file"}:
        return False
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        return False
    path = str(arguments.get("path") or "").strip()
    if not path:
        return False
    if name == "write_file":
        return arguments.get("content") is not None
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")
    if old_text is not None and new_text is not None:
        return str(old_text) != str(new_text)
    if any(key in arguments for key in ("patch", "diff", "replace_text", "find_text", "content")):
        return True
    return True


def _inline_code_has_write_tool_evidence(result: Any) -> bool:
    payload = result if isinstance(result, dict) else {}
    diagnostics = payload.get("agent_loop_diagnostics") if isinstance(payload, dict) else None
    has_diagnostics = isinstance(diagnostics, dict)
    executed = payload.get("tool_calls_executed") if isinstance(payload, dict) else None
    has_executed = isinstance(executed, list)
    if isinstance(executed, list):
        saw_any_write_call = False
        for item in executed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if name in {"write_file", "edit_file"}:
                saw_any_write_call = True
                if _inline_code_write_call_has_material_change(item):
                    return True
        if saw_any_write_call:
            return False
    if has_diagnostics and bool(diagnostics.get("observed_write_tool_call")):
        return True
    # Backward-compatible fallback: older runs/tests may not include tool telemetry.
    # In that case, treat evidence as indeterminate (pass) instead of hard-failing.
    if not has_diagnostics and not has_executed:
        return True
    return False


def _inline_code_output_quality_assessment(output: str) -> Dict[str, Any]:
    lines = [str(line or "").strip() for line in str(output or "").splitlines() if str(line or "").strip()]
    normalized = "\n".join(lines).strip()
    first_line = lines[0] if lines else ""
    has_change_markers = bool(_INLINE_OUTPUT_CHANGE_MARKER_RE.search(normalized))
    has_path_markers = bool(_PATH_LIKE_TOKEN_RE.search(normalized))
    discovery_only = bool(first_line and _INLINE_LOW_SIGNAL_OUTPUT_LINE_RE.search(first_line))
    starts_with_action_plan = bool(first_line and _INLINE_LOW_SIGNAL_FIRST_LINE_RE.search(first_line))
    has_discovery_fragments = bool(_UNVERIFIABLE_ACTION_FRAGMENT_RE.search(normalized))
    requests_clarification = bool(_REQUEST_PERMISSION_LINE_RE.search(first_line))
    access_denial = bool(_ACCESS_DENIAL_LINE_RE.search(first_line))
    low_signal = bool(
        discovery_only
        or (starts_with_action_plan and not has_change_markers and not has_path_markers)
        or (has_discovery_fragments and len(lines) <= 3 and not has_change_markers and not has_path_markers)
    )
    usable = bool(normalized) and not requests_clarification and not access_denial and not (
        low_signal
    )
    return {
        "usable": usable,
        "first_line": first_line,
        "discovery_only": discovery_only,
        "starts_with_action_plan": starts_with_action_plan,
        "has_discovery_fragments": has_discovery_fragments,
        "low_signal": low_signal,
        "requests_clarification": requests_clarification,
        "access_denial": access_denial,
        "has_change_markers": has_change_markers,
        "has_path_markers": has_path_markers,
        "normalized_length": len(normalized),
    }


def _inline_code_synthesized_completion_summary(
    *,
    files_touched: List[str],
    change_breakdown: Dict[str, Any] | None,
    required_surfaces: List[str],
    surface_coverage: Dict[str, Any] | None,
    existing_code_surface_required: bool,
    existing_code_surface_coverage: Dict[str, Any] | None,
    deliverable_contract: Dict[str, Any] | None = None,
    test_coverage: Dict[str, Any] | None = None,
) -> str:
    created_count = int((change_breakdown or {}).get("created_count") or 0)
    updated_count = int((change_breakdown or {}).get("updated_count") or 0)
    deleted_count = int((change_breakdown or {}).get("deleted_count") or 0)
    lines: List[str] = [
        "Inline coding task completed with concrete repository edits.",
        f"Change summary: {updated_count} updated, {created_count} created, {deleted_count} deleted.",
    ]
    required = ", ".join(required_surfaces or [])
    if required:
        missing = ", ".join((surface_coverage or {}).get("missing_surfaces") or []) or "none"
        lines.append(f"Requested surfaces: {required}. Missing surfaces after remediation: {missing}.")
    if existing_code_surface_required:
        code_missing = ", ".join((existing_code_surface_coverage or {}).get("missing_surfaces") or []) or "none"
        lines.append(f"Existing code-file edits across requested surfaces: missing {code_missing}.")
    required_deliverables = ", ".join((deliverable_contract or {}).get("required_deliverables") or [])
    if required_deliverables:
        deliverable_missing = ", ".join((deliverable_contract or {}).get("missing_deliverables") or []) or "none"
        lines.append(
            f"Requested deliverables: {required_deliverables}. Missing deliverables after remediation: {deliverable_missing}."
        )
    if bool((test_coverage or {}).get("tests_required")):
        test_paths = list((test_coverage or {}).get("test_paths") or [])
        test_preview = ", ".join(test_paths[:6]) if test_paths else "none"
        lines.append(f"Test coverage edits included: {test_preview}.")
    preview_paths = list(files_touched or [])[:8]
    if preview_paths:
        lines.append("Files touched in temp workspace:")
        lines.extend(f"- {path}" for path in preview_paths)
    return "\n".join(lines).strip()


def _inline_code_required_surfaces(requested_task: str) -> List[str]:
    text = str(requested_task or "").strip().lower()
    if not text:
        return []
    server_requested = bool(_INLINE_SERVER_KEYWORD_RE.search(text))
    webapp_requested = bool(_INLINE_WEBAPP_KEYWORD_RE.search(text))
    if server_requested and webapp_requested:
        return ["server", "webapp"]
    return []


def _inline_code_surfaces_for_path(path: str) -> List[str]:
    normalized = str(path or "").strip().replace("\\", "/")
    lowered = normalized.lower()
    surfaces: List[str] = []
    if _INLINE_SERVER_PATH_RE.search(normalized):
        surfaces.append("server")
    if _INLINE_WEBAPP_PATH_RE.search(normalized) or lowered.endswith(".razor"):
        surfaces.append("webapp")
    return surfaces


def _inline_code_surface_coverage(paths: List[str], required_surfaces: List[str]) -> Dict[str, Any]:
    normalized_paths = [str(path or "").strip().replace("\\", "/") for path in (paths or []) if str(path or "").strip()]
    by_surface: Dict[str, List[str]] = {key: [] for key in {"server", "webapp"}}
    touched_surfaces: set[str] = set()
    for path in normalized_paths:
        for surface in _inline_code_surfaces_for_path(path):
            touched_surfaces.add(surface)
            by_surface.setdefault(surface, []).append(path)
    required_unique: List[str] = []
    for surface in required_surfaces or []:
        token = str(surface or "").strip().lower()
        if token in {"server", "webapp"} and token not in required_unique:
            required_unique.append(token)
    missing = [surface for surface in required_unique if surface not in touched_surfaces]
    return {
        "required_surfaces": required_unique,
        "touched_surfaces": sorted(touched_surfaces),
        "missing_surfaces": missing,
        "paths_by_surface": {key: value[:24] for key, value in by_surface.items() if value},
        "passed": not missing,
    }


def _inline_code_missing_write_evidence_message() -> str:
    return (
        "Quality gate failed: no write-tool evidence was observed.\n"
        "The run must include at least one write operation via write_file or edit_file."
    )


def _inline_code_surface_gate_failure_message(surface_coverage: Dict[str, Any]) -> str:
    required = ", ".join(surface_coverage.get("required_surfaces") or []) or "(none)"
    missing = ", ".join(surface_coverage.get("missing_surfaces") or []) or "(none)"
    return (
        "Quality gate failed: required code surfaces were not all edited.\n"
        f"Required surfaces: {required}\n"
        f"Missing surfaces: {missing}"
    )


def _inline_code_surface_existing_code_gate_failure_message(surface_coverage: Dict[str, Any]) -> str:
    required = ", ".join(surface_coverage.get("required_surfaces") or []) or "(none)"
    missing = ", ".join(surface_coverage.get("missing_surfaces") or []) or "(none)"
    return (
        "Quality gate failed: required surfaces are missing edits to existing code files.\n"
        f"Required surfaces: {required}\n"
        f"Missing surfaces for existing code edits: {missing}"
    )


def _inline_code_quality_gate_failure_message(task: Task, failures: List[str], files_touched: List[str]) -> str:
    details = "\n".join(f"- {item}" for item in failures if str(item or "").strip())
    touched_preview = ", ".join((files_touched or [])[:8]) if files_touched else "(none)"
    output = _extract_task_output(task.result).strip()
    if len(output) > 1800:
        output = f"{output[:1800].rstrip()}..."
    base = (
        "Inline coding run failed quality gates.\n\n"
        f"{details}\n\n"
        f"Files touched in temp workspace: {touched_preview}"
    )
    if not output:
        return base
    return f"{base}\n\nModel output:\n{output}"


def _inline_code_payload_stats(payload: List[dict]) -> Dict[str, int]:
    total_chars = 0
    user_messages = 0
    assistant_messages = 0
    system_messages = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        total_chars += len(content)
        if role == "user":
            user_messages += 1
        elif role == "assistant":
            assistant_messages += 1
        elif role == "system":
            system_messages += 1
    return {
        "message_count": len(payload),
        "total_chars": total_chars,
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "system_messages": system_messages,
    }


def _inline_code_integration_repair_attempt_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_INTEGRATION_REPAIR_ATTEMPTS", 2, minimum=0, maximum=4)


def _inline_code_surface_repair_attempt_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_SURFACE_REPAIR_ATTEMPTS", 2, minimum=0, maximum=4)


def _inline_code_integration_repair_prompt(created_paths: List[str]) -> str:
    preview = ", ".join((created_paths or [])[:8]) if created_paths else "(none)"
    return (
        "Quality remediation pass: the previous coding pass created only new files and did not integrate with existing code.\n"
        f"Created files from prior pass: {preview}\n\n"
        "Now make concrete integration edits by modifying at least one existing tracked file to wire this feature in.\n"
        "Use write_file/edit_file tooling to make real repository edits directly.\n"
        "Do not ask the user to share files.\n"
        "Typical integration targets include Program.cs (DI registration), ApplicationDbContext.cs (DbSet/config), "
        "existing controllers/services, and existing scheduler registration.\n"
        "Do not ask for clarification. Implement directly and keep previous new files intact."
    )


def _inline_code_surface_repair_candidate_map(
    *,
    workspace_root: str,
    missing_surfaces: List[str],
    touched_paths: List[str],
) -> Dict[str, List[str]]:
    root = normalize_workspace_root(workspace_root)
    if root is None:
        return {}
    requested_surfaces = {
        str(surface or "").strip().lower()
        for surface in (missing_surfaces or [])
        if str(surface or "").strip().lower() in {"server", "webapp"}
    }
    if not requested_surfaces:
        return {}
    touched = {
        str(path or "").strip().replace("\\", "/").lower()
        for path in (touched_paths or [])
        if str(path or "").strip()
    }
    per_surface_limit = _env_int(
        "NEXUSAI_INLINE_CODE_SURFACE_CANDIDATE_LIMIT_PER_SURFACE",
        8,
        minimum=2,
        maximum=20,
    )
    scan_file_limit = _env_int(
        "NEXUSAI_INLINE_CODE_SURFACE_CANDIDATE_SCAN_FILE_LIMIT",
        5_000,
        minimum=500,
        maximum=20_000,
    )
    skip_dirs = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "bin",
        "obj",
        "dist",
        "build",
    }
    scored_paths: Dict[str, List[Tuple[int, str]]] = {surface: [] for surface in requested_surfaces}
    scanned_files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs]
        for filename in filenames:
            scanned_files += 1
            if scanned_files > scan_file_limit:
                break
            full_path = Path(dirpath) / filename
            try:
                rel_path = full_path.relative_to(root).as_posix()
            except Exception:
                continue
            lowered = rel_path.lower()
            if lowered in touched:
                continue
            if not _inline_code_is_code_path(rel_path):
                continue
            surfaces = _inline_code_surfaces_for_path(rel_path)
            if not surfaces:
                continue
            for surface in surfaces:
                if surface not in requested_surfaces:
                    continue
                score = 0
                if "/admin/" in lowered:
                    score += 4
                if any(token in lowered for token in ("program", "schedule", "report", "account", "financial")):
                    score += 3
                if lowered.endswith(".razor") or lowered.endswith(".tsx") or lowered.endswith(".jsx"):
                    score += 2
                if surface == "server" and lowered.endswith(".cs"):
                    score += 1
                scored_paths.setdefault(surface, []).append((score, rel_path))
        if scanned_files > scan_file_limit:
            break
    output: Dict[str, List[str]] = {}
    for surface in requested_surfaces:
        unique: List[str] = []
        seen: set[str] = set()
        ranked = sorted(scored_paths.get(surface) or [], key=lambda item: (-item[0], item[1]))
        for _score, path in ranked:
            normalized = str(path or "").strip().replace("\\", "/")
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            unique.append(normalized)
            if len(unique) >= per_surface_limit:
                break
        if unique:
            output[surface] = unique
    return output


def _inline_code_surface_repair_prompt(
    missing_surfaces: List[str],
    touched_paths: List[str],
    candidate_map: Dict[str, List[str]] | None = None,
) -> str:
    missing = ", ".join(str(item).strip().lower() for item in (missing_surfaces or []) if str(item).strip()) or "(none)"
    touched = ", ".join((touched_paths or [])[:10]) if touched_paths else "(none)"
    candidate_lines: List[str] = []
    for surface in ("webapp", "server"):
        paths = list((candidate_map or {}).get(surface) or [])
        if not paths:
            continue
        preview = ", ".join(paths[:6])
        candidate_lines.append(f"- Candidate existing {surface} files to edit now: {preview}")
    candidates_block = "\n".join(candidate_lines).strip()
    candidates_section = f"{candidates_block}\n" if candidates_block else ""
    return (
        "Quality remediation pass: required code surfaces are missing.\n"
        f"Missing surfaces: {missing}\n"
        f"Current touched paths: {touched}\n\n"
        "Now update existing files in each missing surface and keep prior good edits intact.\n"
        "- For missing 'webapp': edit existing UI files (for example .razor pages/components).\n"
        "- For missing 'server': edit existing backend files (for example controllers/services).\n"
        f"{candidates_section}"
        "- Use write_file or edit_file now; do not stop at discovery.\n"
        "- Do not spend this pass on directory browsing; make at least one concrete file edit first.\n"
        "- If needed, apply a minimal integration/wiring update in an existing file, then continue.\n"
        "- Do not ask the user for clarification."
    )


def _inline_code_no_change_repair_attempt_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_NO_CHANGE_REPAIR_ATTEMPTS", 2, minimum=0, maximum=3)


def _inline_code_skip_downstream_repairs_without_writes() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_SKIP_DOWNSTREAM_REPAIRS_WITHOUT_WRITES", True)


def _inline_code_test_repair_attempt_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_TEST_REPAIR_ATTEMPTS", 1, minimum=0, maximum=3)


def _inline_code_fail_on_missing_tests() -> bool:
    return _env_bool("NEXUSAI_INLINE_CODE_FAIL_ON_MISSING_TESTS", False)


def _inline_code_payload_message_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_PAYLOAD_MAX_MESSAGES", 8, minimum=4, maximum=14)


def _inline_code_payload_char_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_PAYLOAD_MAX_CHARS", 14_000, minimum=6_000, maximum=60_000)


def _inline_code_retry_payload_char_limit() -> int:
    return _env_int("NEXUSAI_INLINE_CODE_RETRY_PAYLOAD_MAX_CHARS", 16_000, minimum=6_000, maximum=60_000)


def _inline_code_no_change_repair_prompt(requested_task: str = "") -> str:
    base = (
        "No file edits were produced in the previous run.\n\n"
        "Now execute the coding task by making concrete repository edits immediately.\n"
        "- Perform discovery using workspace tools now (search_files/read_file) and continue directly to edits.\n"
        "- Do not ask the user for files or clarification.\n"
        "- Make at least one write operation (write_file or edit_file) before finishing.\n"
        "- If this is existing-repo feature work, integrate through existing files (not only brand-new files).\n"
        "- A response that only says you need to inspect directories/files is invalid for this turn."
    )
    required_surfaces = _inline_code_required_surfaces(requested_task)
    if "server" in required_surfaces and "webapp" in required_surfaces:
        return (
            f"{base}\n"
            "- This task explicitly requests both backend and UI work; edit at least one existing server/backend file and one existing webapp/frontend file."
        )
    return base


def _inline_code_test_repair_prompt(requested_task: str, touched_paths: List[str]) -> str:
    touched = ", ".join((touched_paths or [])[:12]) if touched_paths else "(none)"
    return (
        "Quality remediation pass: add or update tests for the implemented feature.\n"
        f"Files currently touched: {touched}\n\n"
        "Now create or modify at least one relevant test file that validates the new behavior.\n"
        "- Prefer existing test projects/folders and match current test conventions.\n"
        "- Keep prior feature edits intact.\n"
        "- Use write_file/edit_file and make concrete test changes now.\n"
        "- Do not ask the user for clarification.\n\n"
        f"Original request:\n{str(requested_task or '').strip()}"
    )


async def _inline_code_attempt_no_change_repair(
    *,
    task_manager: Any,
    target_bot_id: str,
    conversation_id: str,
    project_id: str,
    orchestration_id: str,
    temp_root: str,
    requested_task: str,
    workspace_tree_preview: str = "",
) -> Task | None:
    remediation_payload: List[dict] = [
        {"role": "system", "content": _inline_code_no_change_repair_prompt(requested_task)},
        {"role": "user", "content": str(requested_task or "").strip()},
    ]
    remediation_payload = _inject_inline_workspace_marker(
        remediation_payload,
        workspace_root=temp_root,
        requested_task=requested_task,
        workspace_tree_preview=workspace_tree_preview,
    )
    remediation_payload = _inline_code_compact_payload(
        remediation_payload,
        max_messages=_inline_code_payload_message_limit(),
        max_chars=_inline_code_retry_payload_char_limit(),
    )
    remediation_task = await task_manager.create_task(
        bot_id=target_bot_id,
        payload=remediation_payload,
        metadata=TaskMetadata(
            source="chat_assign",
            project_id=project_id or None,
            conversation_id=conversation_id or None,
            orchestration_id=orchestration_id,
        ),
    )
    return await _inline_code_wait_for_task(task_manager, task_id=remediation_task.id)


async def _inline_code_attempt_integration_repair(
    *,
    task_manager: Any,
    target_bot_id: str,
    conversation_id: str,
    project_id: str,
    orchestration_id: str,
    temp_root: str,
    requested_task: str,
    created_paths: List[str],
    workspace_tree_preview: str = "",
) -> Task | None:
    remediation_payload: List[dict] = [
        {"role": "system", "content": _inline_code_integration_repair_prompt(created_paths)},
        {"role": "user", "content": str(requested_task or "").strip()},
    ]
    remediation_payload = _inject_inline_workspace_marker(
        remediation_payload,
        workspace_root=temp_root,
        requested_task=requested_task,
        workspace_tree_preview=workspace_tree_preview,
    )
    remediation_payload = _inline_code_compact_payload(
        remediation_payload,
        max_messages=_inline_code_payload_message_limit(),
        max_chars=_inline_code_retry_payload_char_limit(),
    )
    remediation_task = await task_manager.create_task(
        bot_id=target_bot_id,
        payload=remediation_payload,
        metadata=TaskMetadata(
            source="chat_assign",
            project_id=project_id or None,
            conversation_id=conversation_id or None,
            orchestration_id=orchestration_id,
        ),
    )
    return await _inline_code_wait_for_task(task_manager, task_id=remediation_task.id)


async def _inline_code_attempt_surface_repair(
    *,
    task_manager: Any,
    target_bot_id: str,
    conversation_id: str,
    project_id: str,
    orchestration_id: str,
    temp_root: str,
    requested_task: str,
    missing_surfaces: List[str],
    touched_paths: List[str],
    workspace_tree_preview: str = "",
) -> Task | None:
    candidate_map = _inline_code_surface_repair_candidate_map(
        workspace_root=temp_root,
        missing_surfaces=missing_surfaces,
        touched_paths=touched_paths,
    )
    prompt = _inline_code_surface_repair_prompt(
        missing_surfaces,
        touched_paths,
        candidate_map=candidate_map,
    )
    remediation_payload: List[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": str(requested_task or "").strip()},
    ]
    remediation_payload = _inject_inline_workspace_marker(
        remediation_payload,
        workspace_root=temp_root,
        requested_task=requested_task,
        workspace_tree_preview=workspace_tree_preview,
    )
    remediation_payload = _inline_code_compact_payload(
        remediation_payload,
        max_messages=_inline_code_payload_message_limit(),
        max_chars=_inline_code_retry_payload_char_limit(),
    )
    remediation_task = await task_manager.create_task(
        bot_id=target_bot_id,
        payload=remediation_payload,
        metadata=TaskMetadata(
            source="chat_assign",
            project_id=project_id or None,
            conversation_id=conversation_id or None,
            orchestration_id=orchestration_id,
        ),
    )
    return await _inline_code_wait_for_task(task_manager, task_id=remediation_task.id)


async def _inline_code_attempt_test_repair(
    *,
    task_manager: Any,
    target_bot_id: str,
    conversation_id: str,
    project_id: str,
    orchestration_id: str,
    temp_root: str,
    requested_task: str,
    touched_paths: List[str],
    workspace_tree_preview: str = "",
) -> Task | None:
    remediation_payload: List[dict] = [
        {"role": "system", "content": _inline_code_test_repair_prompt(requested_task, touched_paths)},
        {"role": "user", "content": str(requested_task or "").strip()},
    ]
    remediation_payload = _inject_inline_workspace_marker(
        remediation_payload,
        workspace_root=temp_root,
        requested_task=requested_task,
        workspace_tree_preview=workspace_tree_preview,
    )
    remediation_payload = _inline_code_compact_payload(
        remediation_payload,
        max_messages=_inline_code_payload_message_limit(),
        max_chars=_inline_code_retry_payload_char_limit(),
    )
    remediation_task = await task_manager.create_task(
        bot_id=target_bot_id,
        payload=remediation_payload,
        metadata=TaskMetadata(
            source="chat_assign",
            project_id=project_id or None,
            conversation_id=conversation_id or None,
            orchestration_id=orchestration_id,
        ),
    )
    return await _inline_code_wait_for_task(task_manager, task_id=remediation_task.id)


def _inline_code_compact_payload(
    payload: List[dict],
    *,
    max_messages: int = 14,
    max_chars: int = 55_000,
) -> List[dict]:
    if not isinstance(payload, list) or not payload:
        return payload

    def _is_inline_retry_noise(content: str) -> bool:
        text = str(content or "").strip().lower()
        if not text:
            return False
        markers = (
            "inline coding mode could not start",
            "inline coding run completed but produced no file edits",
            "pm pipeline failed",
            "quality warning: this run created new files but did not modify existing tracked files",
            "inline coding task completed.",
        )
        return any(marker in text for marker in markers)

    context_system: dict | None = None
    remaining: List[dict] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if index == 0 and role == "system" and str(item.get("content") or "").strip().startswith("Context:"):
            context_system = dict(item)
            continue
        if role in {"system", "user", "assistant"}:
            candidate = dict(item)
            if role == "assistant" and _is_inline_retry_noise(str(candidate.get("content") or "")):
                continue
            remaining.append(candidate)

    # Keep newest unique user/system messages and at most one recent assistant message.
    compacted_tail: List[dict] = []
    seen_user_contents: set[str] = set()
    seen_system_contents: set[str] = set()
    kept_assistant = False
    for item in reversed(remaining):
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role == "assistant":
            if kept_assistant:
                continue
            kept_assistant = True
        elif role == "user":
            if content and content in seen_user_contents:
                continue
            if content:
                seen_user_contents.add(content)
        elif role == "system":
            if content and content in seen_system_contents:
                continue
            if content:
                seen_system_contents.add(content)
        compacted_tail.append(item)
    remaining = list(reversed(compacted_tail))

    max_chars = max(2_000, int(max_chars))
    if context_system is not None:
        context_content = context_system.get("content")
        if isinstance(context_content, str):
            max_context_chars = max(1_500, int(max_chars * 0.6))
            if len(context_content) > max_context_chars:
                context_system["content"] = (
                    f"{context_content[:max_context_chars].rstrip()}\n... (inline context truncated for prompt budget)"
                )

    keep_slots = max(1, max_messages - (1 if context_system is not None else 0))
    selected = remaining[-keep_slots:]
    total_chars = sum(len(str(item.get("content") or "")) for item in selected)
    context_chars = len(str((context_system or {}).get("content") or "")) if context_system is not None else 0
    while selected and (total_chars + context_chars) > max_chars:
        removed = selected.pop(0)
        total_chars -= len(str(removed.get("content") or ""))

    if not selected and remaining:
        fallback = dict(remaining[-1])
        fallback_content = fallback.get("content")
        if isinstance(fallback_content, str) and len(fallback_content) > max_chars:
            fallback["content"] = f"...{fallback_content[-max_chars:]}"
        selected = [fallback]
        total_chars = len(str(fallback.get("content") or ""))

    compacted: List[dict] = []
    if context_system is not None:
        compacted.append(context_system)
    compacted.extend(selected)
    if compacted:
        return compacted
    return payload[-max_messages:]


async def _inline_code_workspace_tree_preview(workspace_root: str | None) -> str:
    root = normalize_workspace_root(workspace_root)
    if root is None:
        return ""
    try:
        tree = await asyncio.to_thread(
            list_workspace_tree,
            root,
            max_depth=3,
            max_entries=140,
        )
    except Exception:
        return ""
    text = str(tree or "").strip()
    if not text:
        return ""
    if len(text) > 4_500:
        text = f"{text[:4_500].rstrip()}\n... (workspace tree preview truncated)"
    return text


def _inject_inline_workspace_marker(
    payload: List[dict],
    *,
    workspace_root: str,
    requested_task: str = "",
    workspace_tree_preview: str = "",
) -> List[dict]:
    marker_root = str(workspace_root or "").strip()
    if not marker_root:
        return payload
    marked: List[dict] = list(payload)
    normalized_task = str(requested_task or "").strip()
    if normalized_task:
        if len(normalized_task) > 4000:
            normalized_task = f"{normalized_task[:4000].rstrip()}..."
        marked.append(
            {
                "role": "system",
                "content": (
                    "Coding task for this turn (execute now):\n"
                    f"{normalized_task}\n\n"
                    "This task is already specific enough to begin implementation. "
                    "Choose a minimal first slice if scope is broad, then implement concrete file edits."
                ),
            }
        )
    required_surfaces = _inline_code_required_surfaces(normalized_task)
    surface_execution_hint = ""
    if "server" in required_surfaces and "webapp" in required_surfaces:
        surface_execution_hint = (
            "\n- This request explicitly asks for both server/backend and webapp/frontend updates; "
            "edit at least one existing tracked file in each surface."
        )
    marked.append(
        {
            "role": "system",
            "content": (
                "Inline coding mode is enabled for this turn. Use available workspace tools to inspect and edit files "
                "directly in the connected project workspace, then summarize exactly what you changed.\n\n"
                "Because this turn explicitly requested coding, you must make best-effort repository edits now. "
                "Do not ask the user to re-specify what to build unless you are blocked by missing permissions or "
                "missing repository context. If scope is broad, implement a minimal first slice and state assumptions.\n\n"
                "Do not ask the user to send files or point to file paths; discover and read relevant files via workspace tools.\n\n"
                "For feature requests in an existing repo, include integration edits to existing files (not only newly-created files) "
                "unless the repo is genuinely empty.\n"
                "You may read files, infer architecture, make inline edits, add files, and delete files via workspace tools.\n\n"
                "Execution requirement for coding turns:\n"
                "- Perform concrete discovery via search_files/read_file (not only workspace_tree/list_directory), then implement edits now.\n"
                "- After discovery, call write_file or edit_file to make concrete repository edits in this turn.\n"
                f"{surface_execution_hint}\n"
                "- Do not stop at planning text.\n"
                "- End the turn with concrete file changes unless blocked by a hard tool/runtime error."
            ),
        }
    )
    if str(workspace_tree_preview or "").strip():
        marked.append(
            {
                "role": "system",
                "content": (
                    "Workspace tree snapshot (source-of-truth for existing structure):\n"
                    f"{workspace_tree_preview}"
                ),
            }
        )
    # Hidden scheduler marker for agentic workspace tool loop.
    marked.append({"role": "system", "content": "", "_workspace_root": marker_root})
    return marked


def _inline_code_requires_project_message() -> str:
    return (
        "Inline coding mode requires a project-scoped conversation with repo workspace enabled.\n\n"
        "Create this chat under a project, then retry the same message."
    )


def _inline_code_bot_policy_message() -> str:
    return (
        "Inline coding mode requires bot execution policy support for writable workspace tools.\n\n"
        "Set both values on the selected bot and retry:\n"
        "1. execution_policy.workspace_context_injection = true\n"
        "2. execution_policy.repo_output_mode = allow"
    )


def _inline_code_terminal_error_message(task: Task) -> str:
    error_text = ""
    if task.error is not None:
        error_text = str(task.error.message or "").strip()
    if not error_text:
        error_text = "Inline coding task failed before a result was produced."
    return f"Inline coding run failed.\n\n{error_text}"


def _inline_code_no_changes_message(task: Task) -> str:
    output = _extract_task_output(task.result).strip()
    if len(output) > 2400:
        output = f"{output[:2400].rstrip()}..."
    base = (
        "Inline coding run completed but produced no file edits in the temp workspace.\n\n"
        "This turn requested coding, so at least one concrete file change is required."
    )
    if not output:
        return base
    return f"{base}\n\nModel output:\n{output}"


def _inline_code_merge_paths(*path_sets: List[str]) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for group in path_sets:
        for raw_path in group:
            normalized = str(raw_path or "").strip().replace("\\", "/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return merged


def _inline_code_read_file_text(path: Path, *, max_bytes: int = 500_000, max_chars: int = 160_000) -> Tuple[str, bool] | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if b"\x00" in data:
        return None
    if len(data) > max_bytes:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return text, truncated


async def _inline_code_collect_workspace_artifacts(temp_root: Path) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    from control_plane.api.projects import _decode_git_porcelain_path, _run_repo_command

    status = await _run_repo_command(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=temp_root,
        timeout_seconds=20,
    )
    if not bool(status.get("ok")):
        return [], [], []

    lines = [str(line).rstrip("\n\r") for line in str(status.get("stdout") or "").splitlines() if str(line).strip()]
    changed_codes: Dict[str, str] = {}
    deleted_paths: List[str] = []
    for line in lines:
        if line.startswith("##") or len(line) < 4:
            continue
        code = line[:2]
        raw_path = line[3:].strip()
        if not raw_path:
            continue
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1].strip()
        normalized = _decode_git_porcelain_path(raw_path).strip().replace("\\", "/").lstrip("./")
        if not normalized or normalized.startswith(".git/"):
            continue
        if "D" in code and not code.startswith("??"):
            deleted_paths.append(normalized)
            continue
        changed_codes[normalized] = code

    artifacts: List[Dict[str, Any]] = []
    files_touched: List[str] = []
    for relative_path, code in sorted(changed_codes.items()):
        target = (temp_root / relative_path).resolve(strict=False)
        if not target.exists() or not target.is_file():
            continue
        read_result = _inline_code_read_file_text(target)
        if read_result is None:
            continue
        content, truncated = read_result
        if not content and target.stat().st_size > 0:
            continue
        status_label = "changed"
        if code.startswith("??") or "A" in code:
            status_label = "created"
        elif any(flag in code for flag in ("M", "R", "C")):
            status_label = "updated"
        artifacts.append(
            {
                "kind": "file",
                "label": relative_path,
                "path": relative_path,
                "content": content,
                "status": status_label,
                "source": "inline_temp_workspace",
                "truncated": truncated,
            }
        )
        files_touched.append(relative_path)
    return artifacts, files_touched, _inline_code_merge_paths(deleted_paths)


def _inline_code_normalize_task_result(
    *,
    result: Any,
    artifacts: List[Dict[str, Any]],
    files_touched: List[str],
    deleted_paths: List[str],
    workspace_entry: Dict[str, Any] | None,
    change_breakdown: Dict[str, Any] | None = None,
    integration_required: bool | None = None,
    integration_passed: bool | None = None,
    write_tool_evidence: bool | None = None,
    required_surfaces: List[str] | None = None,
    surface_coverage: Dict[str, Any] | None = None,
    existing_code_surface_required: bool | None = None,
    existing_code_surface_coverage: Dict[str, Any] | None = None,
    deliverable_contract: Dict[str, Any] | None = None,
    test_coverage: Dict[str, Any] | None = None,
    context_sources: List[str] | None = None,
    context_item_count: int | None = None,
    tool_access: Dict[str, Any] | None = None,
    payload_stats: Dict[str, int] | None = None,
    quality_warnings: List[str] | None = None,
    quality_gate_failures: List[str] | None = None,
    output_override: str | None = None,
    output_quality: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if isinstance(result, dict):
        normalized: Dict[str, Any] = dict(result)
    else:
        normalized = {"output": _extract_task_output(result)}

    existing_artifacts = normalized.get("artifacts")
    merged_artifacts: List[Dict[str, Any]] = [item for item in existing_artifacts if isinstance(item, dict)] if isinstance(existing_artifacts, list) else []
    existing_paths = {str(item.get("path") or "").strip().replace("\\", "/") for item in merged_artifacts}
    for item in artifacts:
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path or path in existing_paths:
            continue
        existing_paths.add(path)
        merged_artifacts.append(item)
    if merged_artifacts:
        normalized["artifacts"] = merged_artifacts

    merged_paths = _inline_code_merge_paths(
        [str(path) for path in (normalized.get("files_touched") or []) if str(path).strip()] if isinstance(normalized.get("files_touched"), list) else [],
        files_touched,
    )
    if merged_paths:
        normalized["files_touched"] = merged_paths
    if merged_paths and not normalized.get("change_summary"):
        normalized["change_summary"] = [f"Updated {len(merged_paths)} file(s) in temp workspace: {', '.join(merged_paths[:8])}"]
    if deleted_paths:
        deleted_note = (
            "Deleted files in temp workspace are not auto-applied by artifact replay: "
            + ", ".join(deleted_paths[:8])
            + "."
        )
        handoff = str(normalized.get("handoff_notes") or "").strip()
        normalized["handoff_notes"] = f"{handoff}\n{deleted_note}".strip() if handoff else deleted_note
    if workspace_entry:
        normalized["assignment_workspace"] = workspace_entry
    if isinstance(change_breakdown, dict) and change_breakdown:
        normalized["change_breakdown"] = dict(change_breakdown)
    if integration_required is not None:
        normalized["integration_quality_gate"] = {
            "existing_file_edits_required": bool(integration_required),
            "passed": bool(integration_passed) if integration_passed is not None else None,
        }
    if (
        write_tool_evidence is not None
        or required_surfaces is not None
        or isinstance(surface_coverage, dict)
        or existing_code_surface_required is not None
        or isinstance(existing_code_surface_coverage, dict)
        or isinstance(deliverable_contract, dict)
        or isinstance(test_coverage, dict)
        or quality_gate_failures
    ):
        normalized["inline_quality_gate"] = {
            "write_tool_evidence_required": True,
            "write_tool_evidence": bool(write_tool_evidence) if write_tool_evidence is not None else None,
            "required_surfaces": list(required_surfaces or []),
            "surface_coverage": dict(surface_coverage or {}),
            "existing_code_surface_edits_required": bool(existing_code_surface_required)
            if existing_code_surface_required is not None
            else None,
            "existing_code_surface_coverage": dict(existing_code_surface_coverage or {}),
            "deliverable_contract": dict(deliverable_contract or {}),
            "test_coverage": dict(test_coverage or {}),
            "failures": [str(item).strip() for item in (quality_gate_failures or []) if str(item).strip()],
        }
    if context_sources is not None or context_item_count is not None or isinstance(tool_access, dict):
        normalized["inline_context"] = {
            "context_item_count": int(context_item_count or 0),
            "context_sources": list(context_sources or []),
            "tool_access": {
                "enabled": bool((tool_access or {}).get("enabled", False)),
                "filesystem": bool((tool_access or {}).get("filesystem", False)),
                "repo_search": bool((tool_access or {}).get("repo_search", False)),
                "workspace_root": str((tool_access or {}).get("workspace_root") or "").strip() or None,
            },
        }
    if isinstance(payload_stats, dict) and payload_stats:
        normalized["inline_payload"] = {
            "message_count": int(payload_stats.get("message_count") or 0),
            "total_chars": int(payload_stats.get("total_chars") or 0),
            "user_messages": int(payload_stats.get("user_messages") or 0),
            "assistant_messages": int(payload_stats.get("assistant_messages") or 0),
            "system_messages": int(payload_stats.get("system_messages") or 0),
        }
    warnings = [str(item).strip() for item in (quality_warnings or []) if str(item).strip()]
    if warnings:
        normalized["quality_warnings"] = warnings
    if output_override is not None:
        normalized["output"] = str(output_override)
    if isinstance(output_quality, dict) and output_quality:
        normalized["inline_output_quality_gate"] = dict(output_quality)
    return normalized


async def _inline_code_wait_for_task(task_manager: Any, *, task_id: str, max_wait_seconds: float = 1800.0) -> Task:
    deadline = asyncio.get_event_loop().time() + max(1.0, float(max_wait_seconds))
    while True:
        task = await task_manager.get_task(task_id)
        if str(task.status or "").strip().lower() in {"completed", "failed", "cancelled", "retried"}:
            return task
        if asyncio.get_event_loop().time() >= deadline:
            raise HTTPException(status_code=504, detail="inline coding task timed out")
        await asyncio.sleep(0.25)


async def _inline_code_persist_result_without_trigger_dispatch(task_manager: Any, *, task: Task, result: Dict[str, Any]) -> Task:
    now = datetime.now(timezone.utc).isoformat()
    updated = task.model_copy(update={"result": result, "updated_at": now})
    lock = getattr(task_manager, "_lock", None)
    tasks_map = getattr(task_manager, "_tasks", None)
    if lock is not None and isinstance(tasks_map, dict):
        async with lock:
            if task.id in tasks_map:
                tasks_map[task.id] = updated
    await task_manager._persist_task(updated)
    await task_manager._upsert_bot_run(updated)
    await task_manager._record_artifacts_for_task(updated)
    return updated


async def _inline_code_prepare_temp_workspace(
    *,
    request: Request,
    project_id: str,
    orchestration_id: str,
) -> Dict[str, Any]:
    from control_plane.api.projects import _ensure_orchestration_temp_workspace

    project_registry = getattr(request.app.state, "project_registry", None)
    if project_registry is None:
        raise HTTPException(status_code=500, detail="project registry unavailable for inline coding")
    workspace_entry = await _ensure_orchestration_temp_workspace(
        project_id=project_id,
        orchestration_id=orchestration_id,
        project_registry=project_registry,
        workspace_store=getattr(request.app.state, "orchestration_workspace_store", None),
        strict=True,
        key_vault=getattr(request.app.state, "key_vault", None),
    )
    if workspace_entry is None:
        raise HTTPException(status_code=409, detail="inline coding temp workspace is unavailable")
    temp_root = Path(str(workspace_entry.get("temp_root") or "").strip())
    if not str(temp_root).strip() or not temp_root.exists():
        raise HTTPException(status_code=409, detail="inline coding temp workspace path does not exist")
    return workspace_entry


def _inline_code_assistant_metadata(
    *,
    orchestration_id: str,
    task: Task,
    run_status: str,
    files_touched: List[str],
) -> Dict[str, Any]:
    passed = str(run_status).strip().lower() == "passed"
    return {
        "mode": "pm_run_report",
        "orchestration_id": orchestration_id,
        "task_count": 1,
        "completed": 1 if passed else 0,
        "failed": 0 if passed else 1,
        "run_status": "passed" if passed else "failed",
        "ingest_allowed": passed,
        "workflow_complete": passed,
        "final_qc_required": False,
        "final_qc_completed": passed,
        "deliverables_complete": bool(files_touched),
        "missing_deliverables": [],
        "missing_stages": [],
        "skipped_stages": [],
        "intentionally_excluded_stages": [],
        "intentionally_skipped_stages": [],
        "workflow_policy_codes": [],
        "failed_task_summaries": [] if passed else [_inline_code_terminal_error_message(task)],
        "inline_code": True,
        "files_touched": files_touched,
    }


def _build_assignment_conversation_brief(
    messages: List[ChatMessage],
    *,
    current_assign_message_id: Optional[str] = None,
    max_messages: int = 6,
    max_chars: int = 2400,
) -> str:
    selected: List[str] = []
    total_chars = 0
    for message in reversed(messages):
        if current_assign_message_id and str(message.id) == str(current_assign_message_id):
            continue
        if str(message.role or "").strip().lower() != "user":
            continue
        content = str(message.content or "").strip()
        if not content:
            continue
        if _extract_assign_instruction(content) is not None:
            continue
        lowered = content.lower()
        if len(content) < 140 and any(
            marker in lowered
            for marker in (
                "don't truncate",
                "do not truncate",
                "you truncated",
                "stop truncating",
            )
        ):
            continue
        normalized = re.sub(r"\s+", " ", content).strip()
        if not normalized:
            continue
        snippet = normalized[:600] + ("..." if len(normalized) > 600 else "")
        selected.append(snippet)
        total_chars += len(snippet)
        if len(selected) >= max_messages or total_chars >= max_chars:
            break
    if not selected:
        return ""
    selected.reverse()
    lines = [f"Prior user intent {idx + 1}: {item}" for idx, item in enumerate(selected)]
    return "\n".join(lines)


def _assignment_context_message_is_eligible(
    message: ChatMessage,
    *,
    current_assign_message_id: Optional[str] = None,
) -> bool:
    if current_assign_message_id and str(message.id) == str(current_assign_message_id):
        return False
    role = str(message.role or "").strip().lower()
    if role not in {"user", "assistant"}:
        return False
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    mode = str(metadata.get("mode") or "").strip().lower()
    # assign_error messages are never useful context
    if mode == "assign_error":
        return False
    # For PM-run messages: include only if the run was accepted for ingestion
    # (ingest_allowed=True means the run passed and its context is valuable)
    if mode in {"assign_request", "assign_pending", "pm_run_report", "assign_summary"}:
        return metadata.get("ingest_allowed") is True
    content = str(message.content or "").strip()
    if not content:
        return False
    return True


def _filter_assignment_context_messages(
    messages: List[ChatMessage],
    *,
    current_assign_message_id: Optional[str] = None,
) -> List[ChatMessage]:
    return [
        message
        for message in messages
        if _assignment_context_message_is_eligible(
            message,
            current_assign_message_id=current_assign_message_id,
        )
    ]


def _assignment_transcript_middle_priority(role: str, content: str) -> int:
    lowered = str(content or "").lower()
    score = 0
    if role == "user":
        score += 5
    elif role == "assistant":
        score += 2
    if any(marker in lowered for marker in ("must", "should", "need", "avoid", "do not", "don't", "prefer", "required", "deliver")):
        score += 2
    if any(marker in lowered for marker in ("docs/", ".md", "desmos", "api", "in house", "in-house", "scope", "constraint", "roadmap", "plan")):
        score += 2
    if any(marker in lowered for marker in ("because", "risk", "evidence", "repo", "workspace", "research")):
        score += 1
    return score


def _select_assignment_transcript_excerpt(
    rendered_entries: List[str],
    transcript_entries: List[tuple[str, str]],
    *,
    max_messages: int,
    max_chars: int,
    head_messages: int,
) -> List[str]:
    if not rendered_entries:
        return []

    tail_messages = min(6, max(2, max_messages // 5))
    kept_indices: List[int] = []
    kept_index_set: set[int] = set()

    def _keep(index: int) -> None:
        if index < 0 or index >= len(rendered_entries) or index in kept_index_set:
            return
        kept_index_set.add(index)
        kept_indices.append(index)

    for index in range(min(head_messages, len(rendered_entries))):
        _keep(index)
    for index in range(max(0, len(rendered_entries) - tail_messages), len(rendered_entries)):
        _keep(index)

    ranked: List[tuple[int, int]] = []
    for index, (role, content) in enumerate(transcript_entries):
        if index in kept_index_set:
            continue
        score = _assignment_transcript_middle_priority(role, content)
        if score > 0:
            ranked.append((score, index))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    for _, index in ranked:
        if len(kept_indices) >= max_messages:
            break
        candidate_indices = sorted([*kept_indices, index])
        candidate_lines = [rendered_entries[item] for item in candidate_indices]
        if len("\n".join(candidate_lines)) > max_chars:
            continue
        _keep(index)

    return [rendered_entries[index] for index in sorted(kept_indices)]


def _build_assignment_conversation_transcript(
    messages: List[ChatMessage],
    *,
    current_assign_message_id: Optional[str] = None,
    max_messages: int = 120,
    max_chars: int = 24000,
    head_messages: int = 8,
) -> Dict[str, Any]:
    transcript_entries: List[tuple[str, str]] = []
    eligible_messages = _filter_assignment_context_messages(
        messages,
        current_assign_message_id=current_assign_message_id,
    )
    for message in eligible_messages:
        role = str(message.role or "").strip().lower()
        content = str(message.content or "").strip()
        normalized = re.sub(r"\s+", " ", content).strip()
        if not normalized:
            continue
        transcript_entries.append((role, normalized))

    if not transcript_entries:
        return {
            "conversation_transcript": "",
            "conversation_message_count": 0,
            "conversation_transcript_strategy": "empty",
        }

    rendered_entries = [f"{role}: {content}" for role, content in transcript_entries]
    full_transcript = "\n".join(rendered_entries)
    if len(transcript_entries) <= max_messages and len(full_transcript) <= max_chars:
        return {
            "conversation_transcript": full_transcript,
            "conversation_message_count": len(transcript_entries),
            "conversation_transcript_strategy": "full",
        }

    kept = _select_assignment_transcript_excerpt(
        rendered_entries,
        transcript_entries,
        max_messages=max_messages,
        max_chars=max_chars,
        head_messages=head_messages,
    )
    omitted_count = max(0, len(rendered_entries) - len(kept))
    if omitted_count > 0:
        insert_at = min(head_messages, len(kept))
        kept.insert(insert_at, f"... ({omitted_count} earlier chat message(s) omitted for size) ...")
    compacted = "\n".join(kept)
    if len(compacted) > max_chars:
        compacted = compacted[: max(0, max_chars - 32)].rstrip() + "\n... [TRUNCATED]"
    return {
        "conversation_transcript": compacted,
        "conversation_message_count": len(transcript_entries),
        "conversation_transcript_strategy": "excerpt",
    }


def _clip_assignment_memory_snippet(text: str, *, limit: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip()).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _build_assignment_memory_hits(
    semantic_hits: List[Dict[str, Any]],
    semantic_messages_by_id: Dict[str, ChatMessage],
    *,
    max_hits: int = 8,
) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for hit in semantic_hits:
        message_id = str(hit.get("message_id") or "").strip()
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        message = semantic_messages_by_id.get(message_id)
        snippet = _clip_assignment_memory_snippet(
            str(hit.get("content") or (message.content if message else "") or "")
        )
        if not snippet:
            continue
        hits.append(
            {
                "message_id": message_id,
                "role": str(hit.get("role") or (message.role if message else "") or "").strip().lower(),
                "created_at": str(hit.get("created_at") or (message.created_at if message else "") or ""),
                "score": round(float(hit.get("score") or 0.0), 4),
                "weighted_score": round(float(hit.get("weighted_score") or hit.get("score") or 0.0), 4),
                "snippet": snippet,
            }
        )
        if len(hits) >= max_hits:
            break
    return hits


async def _build_assignment_context_snapshot(
    chat_manager: Any,
    *,
    conversation_id: str,
    assign_instruction: str,
    current_assign_message_id: Optional[str],
) -> Dict[str, Any]:
    try:
        return await _build_assignment_context_snapshot_inner(
            chat_manager,
            conversation_id=conversation_id,
            assign_instruction=assign_instruction,
            current_assign_message_id=current_assign_message_id,
        )
    except Exception as _ctx_exc:
        logger.warning(
            "[ASSIGN] _build_assignment_context_snapshot failed for conversation %s: %s",
            conversation_id,
            _ctx_exc,
        )
        return {
            "conversation_brief": "",
            "conversation_transcript": "",
            "conversation_message_count": 0,
            "conversation_transcript_strategy": "unavailable",
            "assignment_memory_hits": [],
            "assignment_memory_hit_count": 0,
        }


async def _build_assignment_context_snapshot_inner(
    chat_manager: Any,
    *,
    conversation_id: str,
    assign_instruction: str,
    current_assign_message_id: Optional[str],
) -> Dict[str, Any]:
    eligible_message_count = await chat_manager.count_indexable_messages(conversation_id)
    if eligible_message_count <= 120:
        all_messages = await chat_manager.list_messages(conversation_id)
        eligible_messages = _filter_assignment_context_messages(
            all_messages,
            current_assign_message_id=current_assign_message_id,
        )
        brief = _build_assignment_conversation_brief(
            eligible_messages,
            current_assign_message_id=current_assign_message_id,
            max_messages=8,
            max_chars=3200,
        )
        transcript = _build_assignment_conversation_transcript(
            eligible_messages,
            current_assign_message_id=current_assign_message_id,
            max_messages=140,
            max_chars=24000,
            head_messages=10,
        )
        return {
            "conversation_brief": brief,
            "conversation_transcript": str(transcript.get("conversation_transcript") or ""),
            "conversation_message_count": int(transcript.get("conversation_message_count") or 0),
            "conversation_transcript_strategy": str(transcript.get("conversation_transcript_strategy") or ""),
            "assignment_memory_hits": [],
            "assignment_memory_hit_count": 0,
        }

    head_messages = await chat_manager.list_message_slice(conversation_id, limit=10, newest=False)
    tail_messages = await chat_manager.list_message_slice(conversation_id, limit=12, newest=True)
    semantic_hits = await chat_manager.search_message_memory(
        conversation_id,
        assign_instruction,
        limit=16,
        roles=["user", "assistant"],
    )
    semantic_message_ids = [str(item.get("message_id") or "").strip() for item in semantic_hits if str(item.get("message_id") or "").strip()]
    semantic_messages_by_id: Dict[str, ChatMessage] = {}
    if semantic_message_ids:
        semantic_messages = await chat_manager.get_messages_by_ids(conversation_id, semantic_message_ids)
        semantic_messages_by_id = {
            message.id: message
            for message in semantic_messages
            if _assignment_context_message_is_eligible(
                message,
                current_assign_message_id=current_assign_message_id,
            )
        }
        semantic_hits = [
            hit
            for hit in semantic_hits
            if str(hit.get("message_id") or "").strip() in semantic_messages_by_id
        ]

    combined: List[ChatMessage] = []
    seen: set[str] = set()
    filtered_head = _filter_assignment_context_messages(
        head_messages,
        current_assign_message_id=current_assign_message_id,
    )
    filtered_tail = _filter_assignment_context_messages(
        tail_messages,
        current_assign_message_id=current_assign_message_id,
    )
    for message in filtered_head + list(semantic_messages_by_id.values()) + filtered_tail:
        if message.id in seen:
            continue
        seen.add(message.id)
        combined.append(message)
    combined.sort(key=lambda item: item.created_at)

    brief = _build_assignment_conversation_brief(
        combined,
        current_assign_message_id=current_assign_message_id,
        max_messages=10,
        max_chars=3600,
    )
    transcript = _build_assignment_conversation_transcript(
        combined,
        current_assign_message_id=current_assign_message_id,
        max_messages=80,
        max_chars=18000,
        head_messages=12,
    )
    memory_hits = _build_assignment_memory_hits(semantic_hits, semantic_messages_by_id)
    return {
        "conversation_brief": brief,
        "conversation_transcript": str(transcript.get("conversation_transcript") or ""),
        "conversation_message_count": eligible_message_count,
        "conversation_transcript_strategy": "semantic_excerpt",
        "assignment_memory_hits": memory_hits,
        "assignment_memory_hit_count": len(memory_hits),
    }


def _assignment_context_message_metadata(context_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "assignment_context_strategy": str(context_snapshot.get("conversation_transcript_strategy") or "").strip(),
        "assignment_context_message_count": int(context_snapshot.get("conversation_message_count") or 0),
        "assignment_memory_hit_count": int(context_snapshot.get("assignment_memory_hit_count") or 0),
        "assignment_memory_hits": list(context_snapshot.get("assignment_memory_hits") or []),
    }


def _extract_task_output(result: Any) -> str:
    if isinstance(result, dict):
        output = result.get("output")
        if output is not None:
            return str(output)
        return json.dumps(result)
    if result is None:
        return ""
    return str(result)


def _render_pm_run_report_content(
    *,
    pm_bot_id: str,
    orchestration_id: str,
    task_count: int,
    completed: int,
    failed: int,
    run_status: str,
    operator_marked_failed: bool = False,
) -> str:
    first_line = f"PM run {run_status}."
    if operator_marked_failed:
        first_line = "PM run failed (operator-marked)."
    return "\n".join(
        [
            first_line,
            f"Assigned Bot: {pm_bot_id}",
            f"Orchestration ID: {orchestration_id}",
            f"Tasks: {task_count} total, {completed} completed, {failed} failed.",
            "Open View DAG or Full Recap for full task-by-task details.",
        ]
    )


def _is_failed_pm_message_metadata(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    mode = str(metadata.get("mode") or "").strip()
    if mode not in {"pm_run_report", "assign_summary", "assign_pending"}:
        return False
    run_status = str(metadata.get("run_status") or "").strip().lower()
    ingest_allowed = metadata.get("ingest_allowed")
    return run_status == "failed" or ingest_allowed is False


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
async def list_messages(
    conversation_id: str,
    request: Request,
    limit: Optional[int] = Query(default=None, ge=1),
) -> List[ChatMessage]:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.list_messages(conversation_id, limit=limit)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/conversations/{conversation_id}/orchestrations/{orchestration_id}/mark-failed", response_model=ChatMessage)
async def mark_pm_run_failed(conversation_id: str, orchestration_id: str, request: Request) -> ChatMessage:
    chat_manager = request.app.state.chat_manager
    task_manager = request.app.state.task_manager
    try:
        messages = await chat_manager.list_messages(conversation_id, limit=500)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    target: Optional[ChatMessage] = None
    related_messages: list[ChatMessage] = []
    for message in reversed(messages):
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        if str(metadata.get("orchestration_id") or "").strip() != str(orchestration_id or "").strip():
            continue
        if str(metadata.get("mode") or "").strip() not in {"pm_run_report", "assign_summary", "assign_pending", "assign_request"}:
            continue
        related_messages.append(message)
        if target is None and str(metadata.get("mode") or "").strip() in {"pm_run_report", "assign_summary"}:
            target = message
    if target is None and related_messages:
        target = related_messages[0]
    if target is None:
        raise HTTPException(status_code=404, detail="PM run report message not found for this orchestration")

    existing_metadata = target.metadata if isinstance(target.metadata, dict) else {}
    task_count = int(existing_metadata.get("task_count") or 0)
    completed = int(existing_metadata.get("completed") or 0)
    failed = max(1, int(existing_metadata.get("failed") or 0))
    pm_bot_id = str(target.bot_id or "")
    updated_metadata = dict(existing_metadata)
    updated_metadata.update(
        {
            "mode": "pm_run_report",
            "run_status": "failed",
            "ingest_allowed": False,
            "operator_marked_failed": True,
        }
    )
    content = _render_pm_run_report_content(
        pm_bot_id=pm_bot_id,
        orchestration_id=str(orchestration_id or "").strip(),
        task_count=task_count,
        completed=completed,
        failed=failed,
        run_status="failed",
        operator_marked_failed=True,
    )
    updated_target: Optional[ChatMessage] = None
    for message in related_messages:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        mode = str(metadata.get("mode") or "").strip()
        next_metadata = dict(metadata)
        next_metadata.update(
            {
                "run_status": "failed",
                "ingest_allowed": False,
                "operator_marked_failed": True,
            }
        )
        next_content = None
        # Only rewrite content/mode for the run report messages, not the user assign_request
        if mode in {"pm_run_report", "assign_summary"} or message.id == target.id:
            next_metadata["mode"] = "pm_run_report"
            next_content = content
        updated = await chat_manager.update_message(
            message.id,
            content=next_content,
            metadata=next_metadata,
        )
        if message.id == target.id:
            updated_target = updated
    try:
        tasks = await task_manager.list_tasks(orchestration_id=str(orchestration_id or "").strip(), limit=500)
    except Exception:
        tasks = []
    project_id = next(
        (
            str(task.metadata.project_id or "").strip()
            for task in tasks
            if task.metadata and str(task.metadata.project_id or "").strip()
        ),
        "",
    )
    if project_id:
        from control_plane.api.projects import _cleanup_orchestration_temp_workspace

        await _cleanup_orchestration_temp_workspace(
            project_id=project_id,
            orchestration_id=str(orchestration_id or "").strip(),
            workspace_store=getattr(request.app.state, "orchestration_workspace_store", None),
            reason="operator_marked_failed",
        )
    return updated_target or target


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, request: Request, body: PostMessageRequest) -> dict:
    await enforce_body_size(request, route_name="chat_messages", default_max_bytes=1_500_000_000)
    await enforce_rate_limit(
        request,
        route_name="chat_messages",
        default_limit=120,
        default_window_seconds=60,
    )
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    task_manager = request.app.state.task_manager
    pm_orchestrator = request.app.state.pm_orchestrator
    assignment_service = getattr(request.app.state, "assignment_service", None)
    try:
        conversation = await chat_manager.get_conversation(conversation_id)
        target_bot_id = body.bot_id or conversation.default_bot_id
        attachments = _attachment_payload_dicts(body.attachments)
        if any(str(item.get("kind") or "") == "image" for item in attachments):
            if not target_bot_id:
                raise HTTPException(status_code=400, detail="Image attachments require an explicit bot or conversation bot.")
            if not await _target_supports_image_attachments(request, target_bot_id=target_bot_id):
                raise HTTPException(status_code=400, detail="The selected bot model does not support image attachments.")
        if not str(body.content or "").strip() and not attachments:
            raise HTTPException(status_code=400, detail="content or attachments are required")
        assign_instruction = _extract_assign_instruction(body.content)
        user_message_metadata = None
        if assign_instruction is not None:
            requested_pm_bot_id = str(body.bot_id or "").strip()
            user_message_metadata = {
                "mode": "assign_request",
                "requested_pm_bot_id": requested_pm_bot_id,
            }
        if attachments:
            base_meta = dict(user_message_metadata or {})
            base_meta["attachments"] = attachments
            user_message_metadata = base_meta
        user_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
            metadata=user_message_metadata,
        )
        if assign_instruction is not None:
            assign_bot_id = str(body.bot_id or "").strip()
            if not assign_bot_id:
                raise HTTPException(status_code=400, detail="PM assignment requires an explicit PM bot selection")
            tool_access = await _effective_tool_access(
                request,
                conversation=conversation,
                target_bot_id=assign_bot_id,
            )
            # Get model-aware context limits for assign
            assign_bot_registry = getattr(request.app.state, "bot_registry", None)
            assign_bot = None
            assign_item_limit, _ = 30, 12  # defaults
            if assign_bot_registry:
                try:
                    assign_bot = await assign_bot_registry.get(assign_bot_id)
                    assign_item_limit, _ = _get_context_limits_for_bot(assign_bot)
                except Exception:
                    pass
            resolved_context = await _resolve_context_items(
                request,
                body,
                conversation=conversation,
                tool_access=tool_access,
                force_project_context=True,
                force_workspace_context=_repo_intent_requested(assign_instruction),
                item_limit=assign_item_limit,
            )
            context_snapshot = await _build_assignment_context_snapshot(
                chat_manager,
                conversation_id=conversation_id,
                assign_instruction=assign_instruction,
                current_assign_message_id=user_message.id,
            )
            if assignment_service is not None:
                assignment = await assignment_service.create_assignment(
                    conversation_id=conversation_id,
                    instruction=assign_instruction,
                    pm_bot_id=assign_bot_id,
                    context_items=resolved_context,
                    node_overrides={},
                    conversation_brief=str(context_snapshot.get("conversation_brief") or ""),
                    conversation_transcript=str(context_snapshot.get("conversation_transcript") or ""),
                    conversation_message_count=int(context_snapshot.get("conversation_message_count") or 0),
                    conversation_transcript_strategy=str(context_snapshot.get("conversation_transcript_strategy") or ""),
                    assignment_memory_hits=list(context_snapshot.get("assignment_memory_hits") or []),
                    assignment_memory_hit_count=int(context_snapshot.get("assignment_memory_hit_count") or 0),
                )
            else:
                assignment = await pm_orchestrator.orchestrate_assignment(
                    conversation_id=conversation_id,
                    instruction=assign_instruction,
                    requested_pm_bot_id=body.bot_id,
                    context_items=resolved_context,
                    conversation_brief=str(context_snapshot.get("conversation_brief") or ""),
                    conversation_transcript=str(context_snapshot.get("conversation_transcript") or ""),
                    conversation_message_count=int(context_snapshot.get("conversation_message_count") or 0),
                    conversation_transcript_strategy=str(context_snapshot.get("conversation_transcript_strategy") or ""),
                    assignment_memory_hits=list(context_snapshot.get("assignment_memory_hits") or []),
                    assignment_memory_hit_count=int(context_snapshot.get("assignment_memory_hit_count") or 0),
                    project_id=conversation.project_id,
                )
            context_meta = _assignment_context_message_metadata(context_snapshot)
            user_message = await chat_manager.update_message(
                user_message.id,
                metadata={
                    "mode": "assign_request",
                    "requested_pm_bot_id": assign_bot_id,
                    "assigned_pm_bot_id": str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                    "orchestration_id": assignment.get("orchestration_id"),
                    "assignment_id": assignment.get("assignment_id"),
                    "run_id": assignment.get("run_id") or assignment.get("orchestration_run_id"),
                    **context_meta,
                },
            )
            assistant_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=(
                    f"Assignment queued ({len(assignment.get('tasks', []))} tasks).\n"
                    f"Assigned Bot: {assignment.get('pm_bot_id') or assign_bot_id or ''}\n"
                    f"Orchestration ID: {assignment.get('orchestration_id')}\n"
                    "A full assignment summary will be posted when the workflow finishes."
                ),
                bot_id=str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                metadata=_assistant_bot_metadata(
                    assign_bot,
                    bot_id=str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                    extra={
                        "mode": "assign_pending",
                        "orchestration_id": assignment.get("orchestration_id"),
                        "assignment_id": assignment.get("assignment_id"),
                        "run_id": assignment.get("run_id") or assignment.get("orchestration_run_id"),
                        "task_count": len(assignment.get("tasks", [])),
                        "assigned_pm_bot_id": str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                        **context_meta,
                    },
                ),
            )

            async def _persist_assignment_summary() -> None:
                try:
                    completion = await pm_orchestrator.wait_for_completion(assignment)
                    await pm_orchestrator.persist_summary_message(
                        conversation_id=conversation_id,
                        assignment=assignment,
                        completion=completion,
                    )
                except Exception as exc:
                    await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=(
                            f"Assignment orchestration {assignment.get('orchestration_id')} "
                            f"failed while summarizing: {exc}"
                        ),
                        bot_id=str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                        metadata=_assistant_bot_metadata(
                            assign_bot,
                            bot_id=str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                            extra={
                                "mode": "assign_error",
                                "orchestration_id": assignment.get("orchestration_id"),
                            },
                        ),
                    )

            asyncio.create_task(_persist_assignment_summary())
            return {
                "mode": "assign",
                "user_message": user_message,
                "assistant_message": assistant_message,
                "assignment": assignment,
                "completion": None,
            }

        messages = await chat_manager.list_messages(conversation_id)
        if not target_bot_id:
            return {"user_message": user_message, "assistant_message": None}

        # Get bot to determine model-aware context limits
        ns_bot_registry = getattr(request.app.state, "bot_registry", None)
        ns_bot = None
        ns_item_limit, ns_source_limit = 30, 12  # defaults
        if ns_bot_registry:
            try:
                ns_bot = await ns_bot_registry.get(target_bot_id)
                ns_item_limit, ns_source_limit = _get_context_limits_for_bot(ns_bot)
            except Exception:
                pass

        require_repo_evidence = _repo_evidence_requested(body)
        repo_intent = _repo_intent_requested(body.content)
        inline_code_mode = _inline_code_mode_requested(body, bot=ns_bot)
        force_project_context = repo_intent or inline_code_mode
        force_workspace_context = repo_intent or inline_code_mode
        tool_access = await _effective_tool_access(
            request,
            conversation=conversation,
            target_bot_id=target_bot_id,
        )
        if inline_code_mode:
            inline_coding_allowed = bool(
                tool_access.get("enabled")
                and tool_access.get("filesystem")
                and str(tool_access.get("workspace_root") or "").strip()
            )
            if not inline_coding_allowed:
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=_inline_code_unavailable_message(),
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
            if not str(conversation.project_id or "").strip():
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=_inline_code_requires_project_message(),
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
            exec_policy = getattr(ns_bot, "execution_policy", None) if ns_bot is not None else None
            if isinstance(exec_policy, dict):
                ws_injection = bool(exec_policy.get("workspace_context_injection", False))
                repo_output_mode = str(exec_policy.get("repo_output_mode", "deny") or "deny").strip().lower()
            else:
                ws_injection = bool(getattr(exec_policy, "workspace_context_injection", False))
                repo_output_mode = str(getattr(exec_policy, "repo_output_mode", "deny") or "deny").strip().lower()
            if not ws_injection or repo_output_mode != "allow":
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=_inline_code_bot_policy_message(),
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
        resolved_context = await _resolve_context_items(
            request,
            body,
            conversation=conversation,
            tool_access=tool_access,
            force_project_context=force_project_context,
            force_workspace_context=force_workspace_context,
            item_limit=ns_item_limit,
        )
        context_sources = _context_source_labels(resolved_context, limit=ns_source_limit)
        if not context_sources and resolved_context:
            context_sources = ["context snippets (unlabeled)"]
        if require_repo_evidence and not resolved_context:
            assistant_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=_repo_context_unavailable_message(),
                bot_id=target_bot_id,
                metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id),
            )
            return {"user_message": user_message, "assistant_message": assistant_message}
        payload = _messages_to_payload(
            messages,
            context_items=resolved_context,
            require_repo_evidence=require_repo_evidence,
        )
        if inline_code_mode:
            integration_required = _inline_code_existing_edits_expected(body.content)
            payload = _inline_code_compact_payload(
                payload,
                max_messages=_inline_code_payload_message_limit(),
                max_chars=_inline_code_payload_char_limit(),
            )
            payload_stats = _inline_code_payload_stats(payload)
            orchestration_id = str(uuid.uuid4())
            try:
                workspace_entry = await _inline_code_prepare_temp_workspace(
                    request=request,
                    project_id=str(conversation.project_id or "").strip(),
                    orchestration_id=orchestration_id,
                )
                temp_root = str(workspace_entry.get("temp_root") or "").strip()
                workspace_tree_preview = await _inline_code_workspace_tree_preview(temp_root)
                payload = _inject_inline_workspace_marker(
                    payload,
                    workspace_root=temp_root,
                    requested_task=body.content,
                    workspace_tree_preview=workspace_tree_preview,
                )
                inline_task = await task_manager.create_task(
                    bot_id=target_bot_id,
                    payload=payload,
                    metadata=TaskMetadata(
                        source="chat_assign",
                        project_id=conversation.project_id,
                        conversation_id=conversation_id,
                        orchestration_id=orchestration_id,
                    ),
                )
                terminal_task = await _inline_code_wait_for_task(task_manager, task_id=inline_task.id)
                files_touched: List[str] = []
                if str(terminal_task.status or "").strip().lower() == "completed":
                    artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                    change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                    quality_warnings: List[str] = []
                    write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                    cumulative_write_tool_evidence = bool(write_tool_evidence)
                    no_change_repair_attempts = _inline_code_no_change_repair_attempt_limit()
                    if (not files_touched or not write_tool_evidence) and no_change_repair_attempts > 0:
                        for _ in range(no_change_repair_attempts):
                            try:
                                repaired_task = await _inline_code_attempt_no_change_repair(
                                    task_manager=task_manager,
                                    target_bot_id=target_bot_id,
                                    conversation_id=conversation_id,
                                    project_id=str(conversation.project_id or "").strip(),
                                    orchestration_id=orchestration_id,
                                    temp_root=temp_root,
                                    requested_task=body.content,
                                    workspace_tree_preview=workspace_tree_preview,
                                )
                            except Exception:
                                logger.exception("Inline no-change remediation attempt failed before task completion")
                                break
                            if repaired_task is None:
                                break
                            if str(repaired_task.status or "").strip().lower() != "completed":
                                quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                break
                            terminal_task = repaired_task
                            artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                            change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                            write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                            cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                            if files_touched and cumulative_write_tool_evidence:
                                quality_warnings.append("No-change remediation pass executed and produced concrete file edits with write-tool evidence.")
                                break
                    integration_passed = bool(
                        not integration_required
                        or int(change_breakdown.get("updated_count") or 0) > 0
                        or int(change_breakdown.get("deleted_count") or 0) > 0
                    )
                    skip_downstream_repairs = bool(
                        _inline_code_skip_downstream_repairs_without_writes()
                        and not cumulative_write_tool_evidence
                    )
                    if skip_downstream_repairs:
                        quality_warnings.append(
                            "Skipping integration/surface remediation passes because no write-tool evidence was observed after no-change remediation."
                        )
                    repair_attempts = _inline_code_integration_repair_attempt_limit()
                    if integration_required and not integration_passed and repair_attempts > 0 and not skip_downstream_repairs:
                        for _ in range(repair_attempts):
                            try:
                                repaired_task = await _inline_code_attempt_integration_repair(
                                    task_manager=task_manager,
                                    target_bot_id=target_bot_id,
                                    conversation_id=conversation_id,
                                    project_id=str(conversation.project_id or "").strip(),
                                    orchestration_id=orchestration_id,
                                    temp_root=temp_root,
                                    requested_task=body.content,
                                    created_paths=list(change_breakdown.get("created_paths") or []),
                                    workspace_tree_preview=workspace_tree_preview,
                                )
                            except Exception:
                                logger.exception("Inline integration remediation attempt failed before task completion")
                                break
                            if repaired_task is None:
                                break
                            if str(repaired_task.status or "").strip().lower() != "completed":
                                quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                break
                            terminal_task = repaired_task
                            artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                            change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                            integration_passed = bool(
                                not integration_required
                                or int(change_breakdown.get("updated_count") or 0) > 0
                                or int(change_breakdown.get("deleted_count") or 0) > 0
                            )
                            if integration_passed:
                                quality_warnings.append(
                                    "Integration remediation pass executed and added edits to existing tracked files."
                                )
                                break
                        if integration_required and not integration_passed:
                            quality_warnings.append(_inline_code_new_files_only_warning_message(terminal_task, change_breakdown))
                    required_surfaces = _inline_code_required_surfaces(body.content)
                    surface_paths = _inline_code_merge_paths(
                        files_touched,
                        list(change_breakdown.get("created_paths") or []),
                        list(change_breakdown.get("updated_paths") or []),
                        list(change_breakdown.get("deleted_paths") or []),
                    )
                    surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                    existing_code_surface_required = bool(
                        integration_required and required_surfaces and _inline_code_require_existing_code_surface_edits()
                    )
                    existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                        change_breakdown,
                        required_surfaces,
                    )
                    missing_surface_union = _inline_code_merge_paths(
                        list(surface_coverage.get("missing_surfaces") or []),
                        list(existing_code_surface_coverage.get("missing_surfaces") or [])
                        if existing_code_surface_required
                        else [],
                    )
                    surface_repair_attempts = _inline_code_surface_repair_attempt_limit()
                    if required_surfaces and missing_surface_union and surface_repair_attempts > 0 and not skip_downstream_repairs:
                        for _ in range(surface_repair_attempts):
                            missing_surfaces = list(missing_surface_union)
                            if not missing_surfaces:
                                break
                            try:
                                repaired_task = await _inline_code_attempt_surface_repair(
                                    task_manager=task_manager,
                                    target_bot_id=target_bot_id,
                                    conversation_id=conversation_id,
                                    project_id=str(conversation.project_id or "").strip(),
                                    orchestration_id=orchestration_id,
                                    temp_root=temp_root,
                                    requested_task=body.content,
                                    missing_surfaces=missing_surfaces,
                                    touched_paths=surface_paths,
                                    workspace_tree_preview=workspace_tree_preview,
                                )
                            except Exception:
                                logger.exception("Inline surface remediation attempt failed before task completion")
                                break
                            if repaired_task is None:
                                break
                            if str(repaired_task.status or "").strip().lower() != "completed":
                                quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                break
                            terminal_task = repaired_task
                            artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                            change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                            write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                            cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                            integration_passed = bool(
                                not integration_required
                                or int(change_breakdown.get("updated_count") or 0) > 0
                                or int(change_breakdown.get("deleted_count") or 0) > 0
                            )
                            surface_paths = _inline_code_merge_paths(
                                files_touched,
                                list(change_breakdown.get("created_paths") or []),
                                list(change_breakdown.get("updated_paths") or []),
                                list(change_breakdown.get("deleted_paths") or []),
                            )
                            surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                            existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                                change_breakdown,
                                required_surfaces,
                            )
                            missing_surface_union = _inline_code_merge_paths(
                                list(surface_coverage.get("missing_surfaces") or []),
                                list(existing_code_surface_coverage.get("missing_surfaces") or [])
                                if existing_code_surface_required
                                else [],
                            )
                            if not missing_surface_union:
                                quality_warnings.append(
                                    "Surface remediation pass executed and satisfied required code surface coverage."
                                )
                                break
                    quality_gate_failures: List[str] = []
                    if not cumulative_write_tool_evidence:
                        quality_gate_failures.append(_inline_code_missing_write_evidence_message())
                    if integration_required and not integration_passed:
                        quality_gate_failures.append(_inline_code_new_files_only_warning_message(terminal_task, change_breakdown))
                    if required_surfaces and not bool(surface_coverage.get("passed")):
                        quality_gate_failures.append(_inline_code_surface_gate_failure_message(surface_coverage))
                    if existing_code_surface_required and not bool(existing_code_surface_coverage.get("passed")):
                        quality_gate_failures.append(
                            _inline_code_surface_existing_code_gate_failure_message(existing_code_surface_coverage)
                        )
                    deliverable_contract = _inline_code_deliverable_contract_coverage(
                        requested_task=body.content,
                        files_touched=files_touched,
                        artifacts=artifacts,
                    )
                    if _inline_code_require_deliverable_contract() and not bool(deliverable_contract.get("passed")):
                        quality_gate_failures.append(
                            _inline_code_deliverable_contract_gate_failure_message(deliverable_contract)
                        )
                    test_coverage = _inline_code_test_coverage(
                        requested_task=body.content,
                        integration_required=integration_required,
                        files_touched=files_touched,
                        deleted_paths=deleted_paths,
                    )
                    test_repair_attempts = _inline_code_test_repair_attempt_limit()
                    if bool(test_coverage.get("tests_required")) and not bool(test_coverage.get("passed")) and test_repair_attempts > 0:
                        for _ in range(test_repair_attempts):
                            try:
                                repaired_task = await _inline_code_attempt_test_repair(
                                    task_manager=task_manager,
                                    target_bot_id=target_bot_id,
                                    conversation_id=conversation_id,
                                    project_id=str(conversation.project_id or "").strip(),
                                    orchestration_id=orchestration_id,
                                    temp_root=temp_root,
                                    requested_task=body.content,
                                    touched_paths=files_touched,
                                    workspace_tree_preview=workspace_tree_preview,
                                )
                            except Exception:
                                logger.exception("Inline test remediation attempt failed before task completion")
                                break
                            if repaired_task is None:
                                break
                            if str(repaired_task.status or "").strip().lower() != "completed":
                                quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                break
                            terminal_task = repaired_task
                            artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                            change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                            write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                            cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                            integration_passed = bool(
                                not integration_required
                                or int(change_breakdown.get("updated_count") or 0) > 0
                                or int(change_breakdown.get("deleted_count") or 0) > 0
                            )
                            surface_paths = _inline_code_merge_paths(
                                files_touched,
                                list(change_breakdown.get("created_paths") or []),
                                list(change_breakdown.get("updated_paths") or []),
                                list(change_breakdown.get("deleted_paths") or []),
                            )
                            surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                            existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                                change_breakdown,
                                required_surfaces,
                            )
                            deliverable_contract = _inline_code_deliverable_contract_coverage(
                                requested_task=body.content,
                                files_touched=files_touched,
                                artifacts=artifacts,
                            )
                            test_coverage = _inline_code_test_coverage(
                                requested_task=body.content,
                                integration_required=integration_required,
                                files_touched=files_touched,
                                deleted_paths=deleted_paths,
                            )
                            if bool(test_coverage.get("passed")):
                                quality_warnings.append("Test remediation pass executed and added test file edits.")
                                break
                    if not bool(test_coverage.get("passed")):
                        if _inline_code_fail_on_missing_tests():
                            quality_gate_failures.append(_inline_code_test_coverage_gate_failure_message(test_coverage))
                        else:
                            quality_warnings.append(_inline_code_test_coverage_warning_message(test_coverage))
                    raw_output = _extract_task_output(terminal_task.result)
                    sanitized_output = _sanitize_repo_grounded_output(raw_output)
                    output_quality = _inline_code_output_quality_assessment(sanitized_output or raw_output)
                    output_override: str | None = sanitized_output if sanitized_output and sanitized_output != raw_output else None
                    if files_touched and (
                        not bool(output_quality.get("usable")) or _inline_code_force_deterministic_completion_summary()
                    ):
                        output_override = _inline_code_synthesized_completion_summary(
                            files_touched=files_touched,
                            change_breakdown=change_breakdown,
                            required_surfaces=required_surfaces,
                            surface_coverage=surface_coverage,
                            existing_code_surface_required=existing_code_surface_required,
                            existing_code_surface_coverage=existing_code_surface_coverage,
                            deliverable_contract=deliverable_contract,
                            test_coverage=test_coverage,
                        )
                        if not bool(output_quality.get("usable")):
                            quality_warnings.append(
                                "Low-signal model output was replaced with a deterministic change summary."
                            )
                        else:
                            quality_warnings.append(
                                "Deterministic completion summary generated from actual repository diff."
                            )
                    normalized_result = _inline_code_normalize_task_result(
                        result=terminal_task.result,
                        artifacts=artifacts,
                        files_touched=files_touched,
                        deleted_paths=deleted_paths,
                        workspace_entry=workspace_entry,
                        change_breakdown=change_breakdown,
                        integration_required=integration_required,
                        integration_passed=integration_passed,
                        write_tool_evidence=cumulative_write_tool_evidence,
                        required_surfaces=required_surfaces,
                        surface_coverage=surface_coverage,
                        existing_code_surface_required=existing_code_surface_required,
                        existing_code_surface_coverage=existing_code_surface_coverage,
                        deliverable_contract=deliverable_contract,
                        test_coverage=test_coverage,
                        context_sources=context_sources,
                        context_item_count=len(resolved_context),
                        tool_access=tool_access,
                        payload_stats=payload_stats,
                        quality_warnings=quality_warnings,
                        quality_gate_failures=quality_gate_failures,
                        output_override=output_override,
                        output_quality=output_quality,
                    )
                    if normalized_result != terminal_task.result:
                        try:
                            terminal_task = await _inline_code_persist_result_without_trigger_dispatch(
                                task_manager,
                                task=terminal_task,
                                result=normalized_result,
                            )
                        except Exception:
                            logger.exception("Failed to persist normalized inline coding result for task %s", terminal_task.id)
                    if not files_touched:
                        assistant_message = await chat_manager.add_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=_inline_code_no_changes_message(terminal_task),
                            bot_id=target_bot_id,
                            metadata=_assistant_bot_metadata(
                                ns_bot,
                                bot_id=target_bot_id,
                                extra=_inline_code_assistant_metadata(
                                    orchestration_id=orchestration_id,
                                    task=terminal_task,
                                    run_status="failed",
                                    files_touched=[],
                                ),
                            ),
                        )
                        return {"user_message": user_message, "assistant_message": assistant_message}
                    if quality_gate_failures:
                        assistant_message = await chat_manager.add_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=_inline_code_quality_gate_failure_message(terminal_task, quality_gate_failures, files_touched),
                            bot_id=target_bot_id,
                            metadata=_assistant_bot_metadata(
                                ns_bot,
                                bot_id=target_bot_id,
                                extra=_inline_code_assistant_metadata(
                                    orchestration_id=orchestration_id,
                                    task=terminal_task,
                                    run_status="failed",
                                    files_touched=files_touched,
                                ),
                            ),
                        )
                        return {"user_message": user_message, "assistant_message": assistant_message}
                    assistant_output = _extract_task_output(terminal_task.result)
                    assistant_output = _apply_repo_evidence_envelope(
                        assistant_output,
                        require_repo_evidence=require_repo_evidence,
                        context_sources=context_sources,
                    )
                    if files_touched and "Files touched in temp workspace:" not in assistant_output:
                        preview = "\n".join(f"- {path}" for path in files_touched[:8])
                        assistant_output = (
                            f"{assistant_output}\n\nFiles touched in temp workspace:\n{preview}".strip()
                            if assistant_output.strip()
                            else f"Files touched in temp workspace:\n{preview}"
                        )
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=assistant_output,
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(
                            ns_bot,
                            bot_id=target_bot_id,
                            extra=_inline_code_assistant_metadata(
                                orchestration_id=orchestration_id,
                                task=terminal_task,
                                run_status="passed",
                                files_touched=files_touched,
                            ),
                        ),
                    )
                    return {"user_message": user_message, "assistant_message": assistant_message}

                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=_inline_code_terminal_error_message(terminal_task),
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(
                        ns_bot,
                        bot_id=target_bot_id,
                        extra=_inline_code_assistant_metadata(
                            orchestration_id=orchestration_id,
                            task=terminal_task,
                            run_status="failed",
                            files_touched=[],
                        ),
                    ),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
            except HTTPException as exc:
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=f"Inline coding mode could not start: {exc.detail}",
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
            except Exception as exc:
                assistant_message = await chat_manager.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=f"Inline coding run failed before completion: {exc}",
                    bot_id=target_bot_id,
                    metadata=_assistant_bot_metadata(
                        ns_bot,
                        bot_id=target_bot_id,
                        extra={"mode": "assign_error", "orchestration_id": orchestration_id},
                    ),
                )
                return {"user_message": user_message, "assistant_message": assistant_message}
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
        execution_provenance = None
        if hasattr(scheduler, "consume_task_execution_provenance"):
            execution_provenance = scheduler.consume_task_execution_provenance(task.id)
        assistant_output = _extract_task_output(result)
        assistant_output = _apply_repo_evidence_envelope(
            assistant_output,
            require_repo_evidence=require_repo_evidence,
            context_sources=context_sources,
        )
        assistant_model, assistant_provider = _assistant_model_provider(ns_bot, execution_provenance)
        assistant_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_output,
            bot_id=target_bot_id,
            model=assistant_model,
            provider=assistant_provider,
            metadata=_assistant_bot_metadata(ns_bot, bot_id=target_bot_id, execution_provenance=execution_provenance),
        )
        return {"user_message": user_message, "assistant_message": assistant_message}
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversations/{conversation_id}/stream")
async def stream_message(conversation_id: str, request: Request, body: PostMessageRequest) -> StreamingResponse:
    await enforce_body_size(request, route_name="chat_stream", default_max_bytes=1_500_000_000)
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
    assignment_service = getattr(request.app.state, "assignment_service", None)

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            conversation = await chat_manager.get_conversation(conversation_id)
            target_bot_id = body.bot_id or conversation.default_bot_id
            attachments = _attachment_payload_dicts(body.attachments)
            if any(str(item.get("kind") or "") == "image" for item in attachments):
                if not target_bot_id:
                    raise HTTPException(status_code=400, detail="Image attachments require an explicit bot or conversation bot.")
                if not await _target_supports_image_attachments(request, target_bot_id=target_bot_id):
                    raise HTTPException(status_code=400, detail="The selected bot model does not support image attachments.")
            if not str(body.content or "").strip() and not attachments:
                raise HTTPException(status_code=400, detail="content or attachments are required")
            assign_instruction = _extract_assign_instruction(body.content)
            user_message_metadata = None
            if assign_instruction is not None:
                requested_pm_bot_id = str(body.bot_id or "").strip()
                user_message_metadata = {
                    "mode": "assign_request",
                    "requested_pm_bot_id": requested_pm_bot_id,
                }
            if attachments:
                base_meta = dict(user_message_metadata or {})
                base_meta["attachments"] = attachments
                user_message_metadata = base_meta
            user_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=body.content,
                metadata=user_message_metadata,
            )
            yield f"event: user_message\ndata: {user_message.model_dump_json()}\n\n"
            if assign_instruction is not None:
                yield 'event: status\ndata: {"phase":"planning","label":"Planning task graph..."}\n\n'
                assign_bot_id = str(body.bot_id or "").strip()
                if not assign_bot_id:
                    raise HTTPException(status_code=400, detail="PM assignment requires an explicit PM bot selection")
                tool_access = await _effective_tool_access(
                    request,
                    conversation=conversation,
                    target_bot_id=assign_bot_id,
                )
                # Get model-aware context limits for PM assign
                pm_bot_registry = getattr(request.app.state, "bot_registry", None)
                pm_item_limit, pm_source_limit = 30, 8  # defaults
                if pm_bot_registry:
                    try:
                        pm_bot = await pm_bot_registry.get(assign_bot_id)
                        pm_item_limit, pm_source_limit = _get_context_limits_for_bot(pm_bot)
                    except Exception:
                        pass
                if _context_resolution_requested(body):
                    yield 'event: status\ndata: {"phase":"context","label":"Collecting repository context..."}\n\n'
                resolved_context = await _resolve_context_items(
                    request,
                    body,
                    conversation=conversation,
                    tool_access=tool_access,
                    force_project_context=True,
                    force_workspace_context=_repo_intent_requested(assign_instruction),
                    item_limit=pm_item_limit,
                )
                context_sources = _context_source_labels(resolved_context, limit=pm_source_limit)
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
                context_snapshot = await _build_assignment_context_snapshot(
                    chat_manager,
                    conversation_id=conversation_id,
                    assign_instruction=assign_instruction,
                    current_assign_message_id=user_message.id,
                )
                if assignment_service is not None:
                    assignment = await assignment_service.create_assignment(
                        conversation_id=conversation_id,
                        instruction=assign_instruction,
                        pm_bot_id=assign_bot_id,
                        context_items=resolved_context,
                        node_overrides={},
                        conversation_brief=str(context_snapshot.get("conversation_brief") or ""),
                        conversation_transcript=str(context_snapshot.get("conversation_transcript") or ""),
                        conversation_message_count=int(context_snapshot.get("conversation_message_count") or 0),
                        conversation_transcript_strategy=str(context_snapshot.get("conversation_transcript_strategy") or ""),
                        assignment_memory_hits=list(context_snapshot.get("assignment_memory_hits") or []),
                        assignment_memory_hit_count=int(context_snapshot.get("assignment_memory_hit_count") or 0),
                    )
                else:
                    assignment = await pm_orchestrator.orchestrate_assignment(
                        conversation_id=conversation_id,
                        instruction=assign_instruction,
                        requested_pm_bot_id=body.bot_id,
                        context_items=resolved_context,
                        conversation_brief=str(context_snapshot.get("conversation_brief") or ""),
                        conversation_transcript=str(context_snapshot.get("conversation_transcript") or ""),
                        conversation_message_count=int(context_snapshot.get("conversation_message_count") or 0),
                        conversation_transcript_strategy=str(context_snapshot.get("conversation_transcript_strategy") or ""),
                        assignment_memory_hits=list(context_snapshot.get("assignment_memory_hits") or []),
                        assignment_memory_hit_count=int(context_snapshot.get("assignment_memory_hit_count") or 0),
                        project_id=conversation.project_id,
                    )
                context_meta = _assignment_context_message_metadata(context_snapshot)
                user_message = await chat_manager.update_message(
                    user_message.id,
                    metadata={
                        "mode": "assign_request",
                        "requested_pm_bot_id": assign_bot_id,
                        "assigned_pm_bot_id": str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                        "orchestration_id": assignment.get("orchestration_id"),
                        "assignment_id": assignment.get("assignment_id"),
                        "run_id": assignment.get("run_id") or assignment.get("orchestration_run_id"),
                        **context_meta,
                    },
                )
                yield f"event: user_message\ndata: {user_message.model_dump_json()}\n\n"
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
            if not target_bot_id:
                yield "event: done\ndata: {}\n\n"
                return

            # Get bot to determine model-aware context limits
            bot_registry = getattr(request.app.state, "bot_registry", None)
            bot = None
            item_limit, source_limit = 30, 12  # defaults
            if bot_registry:
                try:
                    bot = await bot_registry.get(target_bot_id)
                    item_limit, source_limit = _get_context_limits_for_bot(bot)
                except Exception:
                    pass  # Keep defaults on error

            require_repo_evidence = _repo_evidence_requested(body)
            repo_intent = _repo_intent_requested(body.content)
            inline_code_mode = _inline_code_mode_requested(body, bot=bot)
            force_project_context = repo_intent or inline_code_mode
            force_workspace_context = repo_intent or inline_code_mode
            tool_access = await _effective_tool_access(
                request,
                conversation=conversation,
                target_bot_id=target_bot_id,
            )
            if inline_code_mode:
                inline_coding_allowed = bool(
                    tool_access.get("enabled")
                    and tool_access.get("filesystem")
                    and str(tool_access.get("workspace_root") or "").strip()
                )
                if not inline_coding_allowed:
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=_inline_code_unavailable_message(),
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(bot, bot_id=target_bot_id),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return
                if not str(conversation.project_id or "").strip():
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=_inline_code_requires_project_message(),
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(bot, bot_id=target_bot_id),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return
                exec_policy = getattr(bot, "execution_policy", None) if bot is not None else None
                if isinstance(exec_policy, dict):
                    ws_injection = bool(exec_policy.get("workspace_context_injection", False))
                    repo_output_mode = str(exec_policy.get("repo_output_mode", "deny") or "deny").strip().lower()
                else:
                    ws_injection = bool(getattr(exec_policy, "workspace_context_injection", False))
                    repo_output_mode = str(getattr(exec_policy, "repo_output_mode", "deny") or "deny").strip().lower()
                if not ws_injection or repo_output_mode != "allow":
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=_inline_code_bot_policy_message(),
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(bot, bot_id=target_bot_id),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return
            if require_repo_evidence:
                yield 'event: status\ndata: {"phase":"context","label":"Collecting repository context..."}\n\n'
            resolved_context = await _resolve_context_items(
                request,
                body,
                conversation=conversation,
                tool_access=tool_access,
                force_project_context=force_project_context,
                force_workspace_context=force_workspace_context,
                item_limit=item_limit,
            )
            context_sources = _context_source_labels(resolved_context, limit=source_limit)
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
                    metadata=_assistant_bot_metadata(bot, bot_id=target_bot_id),
                )
                yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                yield "event: done\ndata: {}\n\n"
                return
            payload = _messages_to_payload(
                messages,
                context_items=resolved_context,
                require_repo_evidence=require_repo_evidence,
            )
            if inline_code_mode:
                integration_required = _inline_code_existing_edits_expected(body.content)
                payload = _inline_code_compact_payload(
                    payload,
                    max_messages=_inline_code_payload_message_limit(),
                    max_chars=_inline_code_payload_char_limit(),
                )
                payload_stats = _inline_code_payload_stats(payload)
                orchestration_id = str(uuid.uuid4())
                try:
                    yield (
                        'event: status\ndata: '
                        '{"phase":"workspace","label":"Preparing temp workspace for inline coding..."}\n\n'
                    )
                    workspace_entry = await _inline_code_prepare_temp_workspace(
                        request=request,
                        project_id=str(conversation.project_id or "").strip(),
                        orchestration_id=orchestration_id,
                    )
                    temp_root = str(workspace_entry.get("temp_root") or "").strip()
                    workspace_tree_preview = await _inline_code_workspace_tree_preview(temp_root)
                    payload = _inject_inline_workspace_marker(
                        payload,
                        workspace_root=temp_root,
                        requested_task=body.content,
                        workspace_tree_preview=workspace_tree_preview,
                    )
                    inline_task = await task_manager.create_task(
                        bot_id=target_bot_id,
                        payload=payload,
                        metadata=TaskMetadata(
                            source="chat_assign",
                            project_id=conversation.project_id,
                            conversation_id=conversation_id,
                            orchestration_id=orchestration_id,
                        ),
                    )
                    yield (
                        'event: status\ndata: '
                        f'{json.dumps({"phase":"queued","label":"Inline coding task queued.","task_id":inline_task.id})}\n\n'
                    )

                    last_status: Optional[str] = None
                    deadline = asyncio.get_event_loop().time() + 1800.0
                    last_heartbeat_at = asyncio.get_event_loop().time()
                    heartbeat_interval_seconds = 10.0
                    run_started_at = asyncio.get_event_loop().time()
                    terminal_task: Optional[Task] = None
                    while True:
                        now = asyncio.get_event_loop().time()
                        current = await task_manager.get_task(inline_task.id)
                        status_value = str(current.status or "").strip().lower()
                        if status_value != last_status:
                            if status_value == "queued":
                                label = "Waiting for execution slot..."
                                phase = "queued"
                            elif status_value in {"running", "blocked"}:
                                label = "Executing inline coding task..."
                                phase = "running"
                            elif status_value == "completed":
                                label = "Primary coding pass completed. Running quality checks..."
                                phase = "finalizing"
                            elif status_value in {"failed", "cancelled", "retried"}:
                                label = f"Inline coding task ended with status: {status_value}."
                                phase = "failed"
                            else:
                                label = f"Inline coding task status: {status_value or 'unknown'}."
                                phase = "running"
                            yield f'event: status\ndata: {json.dumps({"phase": phase, "label": label, "task_id": current.id})}\n\n'
                            last_status = status_value
                            last_heartbeat_at = now
                        elif status_value in {"queued", "running", "blocked"} and (now - last_heartbeat_at) >= heartbeat_interval_seconds:
                            elapsed = int(max(0.0, now - run_started_at))
                            if status_value == "queued":
                                heartbeat_label = f"Still queued... ({elapsed}s elapsed)"
                                heartbeat_phase = "queued"
                            elif status_value == "blocked":
                                heartbeat_label = f"Still running remediation/coordination... ({elapsed}s elapsed)"
                                heartbeat_phase = "running"
                            else:
                                heartbeat_label = f"Inline coding still running... ({elapsed}s elapsed)"
                                heartbeat_phase = "running"
                            yield (
                                'event: status\ndata: '
                                f'{json.dumps({"phase": heartbeat_phase, "label": heartbeat_label, "task_id": current.id})}\n\n'
                            )
                            last_heartbeat_at = now
                        if status_value in {"completed", "failed", "cancelled", "retried"}:
                            terminal_task = current
                            break
                        if now >= deadline:
                            raise HTTPException(status_code=504, detail="inline coding task timed out")
                        await asyncio.sleep(0.35)

                    if terminal_task is None:
                        raise HTTPException(status_code=504, detail="inline coding task did not reach terminal state")

                    files_touched: List[str] = []
                    if str(terminal_task.status or "").strip().lower() == "completed":
                        yield (
                            'event: status\ndata: '
                            f'{json.dumps({"phase":"finalizing","label":"Collecting workspace artifacts and quality signals...","task_id":terminal_task.id})}\n\n'
                        )
                        artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                        change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                        quality_warnings: List[str] = []
                        write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                        cumulative_write_tool_evidence = bool(write_tool_evidence)
                        no_change_repair_attempts = _inline_code_no_change_repair_attempt_limit()
                        if (not files_touched or not write_tool_evidence) and no_change_repair_attempts > 0:
                            for attempt_idx in range(no_change_repair_attempts):
                                yield (
                                    'event: status\ndata: '
                                    f'{json.dumps({"phase":"finalizing","label":f"No-change remediation pass {attempt_idx + 1}/{no_change_repair_attempts}: enforcing concrete edits...","task_id":terminal_task.id})}\n\n'
                                )
                                try:
                                    repaired_task = await _inline_code_attempt_no_change_repair(
                                        task_manager=task_manager,
                                        target_bot_id=target_bot_id,
                                        conversation_id=conversation_id,
                                        project_id=str(conversation.project_id or "").strip(),
                                        orchestration_id=orchestration_id,
                                        temp_root=temp_root,
                                        requested_task=body.content,
                                        workspace_tree_preview=workspace_tree_preview,
                                    )
                                except Exception:
                                    logger.exception("Inline no-change remediation attempt failed before task completion")
                                    break
                                if repaired_task is None:
                                    break
                                if str(repaired_task.status or "").strip().lower() != "completed":
                                    quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                    break
                                terminal_task = repaired_task
                                artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                                change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                                write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                                cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                                if files_touched and cumulative_write_tool_evidence:
                                    quality_warnings.append("No-change remediation pass executed and produced concrete file edits with write-tool evidence.")
                                    break
                        integration_passed = bool(
                            not integration_required
                            or int(change_breakdown.get("updated_count") or 0) > 0
                            or int(change_breakdown.get("deleted_count") or 0) > 0
                        )
                        skip_downstream_repairs = bool(
                            _inline_code_skip_downstream_repairs_without_writes()
                            and not cumulative_write_tool_evidence
                        )
                        if skip_downstream_repairs:
                            quality_warnings.append(
                                "Skipping integration/surface remediation passes because no write-tool evidence was observed after no-change remediation."
                            )
                        repair_attempts = _inline_code_integration_repair_attempt_limit()
                        if integration_required and not integration_passed and repair_attempts > 0 and not skip_downstream_repairs:
                            for attempt_idx in range(repair_attempts):
                                yield (
                                    'event: status\ndata: '
                                    f'{json.dumps({"phase":"finalizing","label":f"Integration remediation pass {attempt_idx + 1}/{repair_attempts}: adding edits to existing files...","task_id":terminal_task.id})}\n\n'
                                )
                                try:
                                    repaired_task = await _inline_code_attempt_integration_repair(
                                        task_manager=task_manager,
                                        target_bot_id=target_bot_id,
                                        conversation_id=conversation_id,
                                        project_id=str(conversation.project_id or "").strip(),
                                        orchestration_id=orchestration_id,
                                        temp_root=temp_root,
                                        requested_task=body.content,
                                        created_paths=list(change_breakdown.get("created_paths") or []),
                                        workspace_tree_preview=workspace_tree_preview,
                                    )
                                except Exception:
                                    logger.exception("Inline integration remediation attempt failed before task completion")
                                    break
                                if repaired_task is None:
                                    break
                                if str(repaired_task.status or "").strip().lower() != "completed":
                                    quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                    break
                                terminal_task = repaired_task
                                artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                                change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                                integration_passed = bool(
                                    not integration_required
                                    or int(change_breakdown.get("updated_count") or 0) > 0
                                    or int(change_breakdown.get("deleted_count") or 0) > 0
                                )
                                if integration_passed:
                                    quality_warnings.append(
                                        "Integration remediation pass executed and added edits to existing tracked files."
                                    )
                                    break
                            if integration_required and not integration_passed:
                                quality_warnings.append(_inline_code_new_files_only_warning_message(terminal_task, change_breakdown))
                        required_surfaces = _inline_code_required_surfaces(body.content)
                        surface_paths = _inline_code_merge_paths(
                            files_touched,
                            list(change_breakdown.get("created_paths") or []),
                            list(change_breakdown.get("updated_paths") or []),
                            list(change_breakdown.get("deleted_paths") or []),
                        )
                        surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                        existing_code_surface_required = bool(
                            integration_required and required_surfaces and _inline_code_require_existing_code_surface_edits()
                        )
                        existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                            change_breakdown,
                            required_surfaces,
                        )
                        missing_surface_union = _inline_code_merge_paths(
                            list(surface_coverage.get("missing_surfaces") or []),
                            list(existing_code_surface_coverage.get("missing_surfaces") or [])
                            if existing_code_surface_required
                            else [],
                        )
                        surface_repair_attempts = _inline_code_surface_repair_attempt_limit()
                        if required_surfaces and missing_surface_union and surface_repair_attempts > 0 and not skip_downstream_repairs:
                            for attempt_idx in range(surface_repair_attempts):
                                missing_surfaces = list(missing_surface_union)
                                if not missing_surfaces:
                                    break
                                missing_label = ", ".join(missing_surfaces)
                                yield (
                                    'event: status\ndata: '
                                    f'{json.dumps({"phase":"finalizing","label":f"Surface remediation pass {attempt_idx + 1}/{surface_repair_attempts}: covering {missing_label}...","task_id":terminal_task.id})}\n\n'
                                )
                                try:
                                    repaired_task = await _inline_code_attempt_surface_repair(
                                        task_manager=task_manager,
                                        target_bot_id=target_bot_id,
                                        conversation_id=conversation_id,
                                        project_id=str(conversation.project_id or "").strip(),
                                        orchestration_id=orchestration_id,
                                        temp_root=temp_root,
                                        requested_task=body.content,
                                        missing_surfaces=missing_surfaces,
                                        touched_paths=surface_paths,
                                        workspace_tree_preview=workspace_tree_preview,
                                    )
                                except Exception:
                                    logger.exception("Inline surface remediation attempt failed before task completion")
                                    break
                                if repaired_task is None:
                                    break
                                if str(repaired_task.status or "").strip().lower() != "completed":
                                    quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                    break
                                terminal_task = repaired_task
                                artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                                change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                                write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                                cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                                integration_passed = bool(
                                    not integration_required
                                    or int(change_breakdown.get("updated_count") or 0) > 0
                                    or int(change_breakdown.get("deleted_count") or 0) > 0
                                )
                                surface_paths = _inline_code_merge_paths(
                                    files_touched,
                                    list(change_breakdown.get("created_paths") or []),
                                    list(change_breakdown.get("updated_paths") or []),
                                    list(change_breakdown.get("deleted_paths") or []),
                                )
                                surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                                existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                                    change_breakdown,
                                    required_surfaces,
                                )
                                missing_surface_union = _inline_code_merge_paths(
                                    list(surface_coverage.get("missing_surfaces") or []),
                                    list(existing_code_surface_coverage.get("missing_surfaces") or [])
                                    if existing_code_surface_required
                                    else [],
                                )
                                if not missing_surface_union:
                                    quality_warnings.append(
                                        "Surface remediation pass executed and satisfied required code surface coverage."
                                    )
                                    break
                        quality_gate_failures: List[str] = []
                        if not cumulative_write_tool_evidence:
                            quality_gate_failures.append(_inline_code_missing_write_evidence_message())
                        if integration_required and not integration_passed:
                            quality_gate_failures.append(_inline_code_new_files_only_warning_message(terminal_task, change_breakdown))
                        if required_surfaces and not bool(surface_coverage.get("passed")):
                            quality_gate_failures.append(_inline_code_surface_gate_failure_message(surface_coverage))
                        if existing_code_surface_required and not bool(existing_code_surface_coverage.get("passed")):
                            quality_gate_failures.append(
                                _inline_code_surface_existing_code_gate_failure_message(existing_code_surface_coverage)
                            )
                        deliverable_contract = _inline_code_deliverable_contract_coverage(
                            requested_task=body.content,
                            files_touched=files_touched,
                            artifacts=artifacts,
                        )
                        if _inline_code_require_deliverable_contract() and not bool(deliverable_contract.get("passed")):
                            quality_gate_failures.append(
                                _inline_code_deliverable_contract_gate_failure_message(deliverable_contract)
                            )
                        test_coverage = _inline_code_test_coverage(
                            requested_task=body.content,
                            integration_required=integration_required,
                            files_touched=files_touched,
                            deleted_paths=deleted_paths,
                        )
                        test_repair_attempts = _inline_code_test_repair_attempt_limit()
                        if bool(test_coverage.get("tests_required")) and not bool(test_coverage.get("passed")) and test_repair_attempts > 0:
                            for attempt_idx in range(test_repair_attempts):
                                yield (
                                    'event: status\ndata: '
                                    f'{json.dumps({"phase":"finalizing","label":f"Test remediation pass {attempt_idx + 1}/{test_repair_attempts}: adding validation coverage...","task_id":terminal_task.id})}\n\n'
                                )
                                try:
                                    repaired_task = await _inline_code_attempt_test_repair(
                                        task_manager=task_manager,
                                        target_bot_id=target_bot_id,
                                        conversation_id=conversation_id,
                                        project_id=str(conversation.project_id or "").strip(),
                                        orchestration_id=orchestration_id,
                                        temp_root=temp_root,
                                        requested_task=body.content,
                                        touched_paths=files_touched,
                                        workspace_tree_preview=workspace_tree_preview,
                                    )
                                except Exception:
                                    logger.exception("Inline test remediation attempt failed before task completion")
                                    break
                                if repaired_task is None:
                                    break
                                if str(repaired_task.status or "").strip().lower() != "completed":
                                    quality_warnings.append(_inline_code_terminal_error_message(repaired_task))
                                    break
                                terminal_task = repaired_task
                                artifacts, files_touched, deleted_paths = await _inline_code_collect_workspace_artifacts(Path(temp_root))
                                change_breakdown = _inline_code_change_breakdown(artifacts, deleted_paths)
                                write_tool_evidence = _inline_code_has_write_tool_evidence(terminal_task.result)
                                cumulative_write_tool_evidence = bool(cumulative_write_tool_evidence or write_tool_evidence)
                                integration_passed = bool(
                                    not integration_required
                                    or int(change_breakdown.get("updated_count") or 0) > 0
                                    or int(change_breakdown.get("deleted_count") or 0) > 0
                                )
                                surface_paths = _inline_code_merge_paths(
                                    files_touched,
                                    list(change_breakdown.get("created_paths") or []),
                                    list(change_breakdown.get("updated_paths") or []),
                                    list(change_breakdown.get("deleted_paths") or []),
                                )
                                surface_coverage = _inline_code_surface_coverage(surface_paths, required_surfaces)
                                existing_code_surface_coverage = _inline_code_existing_code_surface_coverage(
                                    change_breakdown,
                                    required_surfaces,
                                )
                                deliverable_contract = _inline_code_deliverable_contract_coverage(
                                    requested_task=body.content,
                                    files_touched=files_touched,
                                    artifacts=artifacts,
                                )
                                test_coverage = _inline_code_test_coverage(
                                    requested_task=body.content,
                                    integration_required=integration_required,
                                    files_touched=files_touched,
                                    deleted_paths=deleted_paths,
                                )
                                if bool(test_coverage.get("passed")):
                                    quality_warnings.append("Test remediation pass executed and added test file edits.")
                                    break
                        if not bool(test_coverage.get("passed")):
                            if _inline_code_fail_on_missing_tests():
                                quality_gate_failures.append(_inline_code_test_coverage_gate_failure_message(test_coverage))
                            else:
                                quality_warnings.append(_inline_code_test_coverage_warning_message(test_coverage))
                        raw_output = _extract_task_output(terminal_task.result)
                        sanitized_output = _sanitize_repo_grounded_output(raw_output)
                        output_quality = _inline_code_output_quality_assessment(sanitized_output or raw_output)
                        output_override: str | None = sanitized_output if sanitized_output and sanitized_output != raw_output else None
                        if files_touched and (
                            not bool(output_quality.get("usable")) or _inline_code_force_deterministic_completion_summary()
                        ):
                            output_override = _inline_code_synthesized_completion_summary(
                                files_touched=files_touched,
                                change_breakdown=change_breakdown,
                                required_surfaces=required_surfaces,
                                surface_coverage=surface_coverage,
                                existing_code_surface_required=existing_code_surface_required,
                                existing_code_surface_coverage=existing_code_surface_coverage,
                                deliverable_contract=deliverable_contract,
                                test_coverage=test_coverage,
                            )
                            if not bool(output_quality.get("usable")):
                                quality_warnings.append(
                                    "Low-signal model output was replaced with a deterministic change summary."
                                )
                            else:
                                quality_warnings.append(
                                    "Deterministic completion summary generated from actual repository diff."
                                )
                        normalized_result = _inline_code_normalize_task_result(
                            result=terminal_task.result,
                            artifacts=artifacts,
                            files_touched=files_touched,
                            deleted_paths=deleted_paths,
                            workspace_entry=workspace_entry,
                            change_breakdown=change_breakdown,
                            integration_required=integration_required,
                            integration_passed=integration_passed,
                            write_tool_evidence=cumulative_write_tool_evidence,
                            required_surfaces=required_surfaces,
                            surface_coverage=surface_coverage,
                            existing_code_surface_required=existing_code_surface_required,
                            existing_code_surface_coverage=existing_code_surface_coverage,
                            deliverable_contract=deliverable_contract,
                            test_coverage=test_coverage,
                            context_sources=context_sources,
                            context_item_count=len(resolved_context),
                            tool_access=tool_access,
                            payload_stats=payload_stats,
                            quality_warnings=quality_warnings,
                            quality_gate_failures=quality_gate_failures,
                            output_override=output_override,
                            output_quality=output_quality,
                        )
                        if normalized_result != terminal_task.result:
                            try:
                                terminal_task = await _inline_code_persist_result_without_trigger_dispatch(
                                    task_manager,
                                    task=terminal_task,
                                    result=normalized_result,
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to persist normalized inline coding result for task %s",
                                    terminal_task.id,
                                )
                        if not files_touched:
                            assistant_message = await chat_manager.add_message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=_inline_code_no_changes_message(terminal_task),
                                bot_id=target_bot_id,
                                metadata=_assistant_bot_metadata(
                                    bot,
                                    bot_id=target_bot_id,
                                    extra=_inline_code_assistant_metadata(
                                        orchestration_id=orchestration_id,
                                        task=terminal_task,
                                        run_status="failed",
                                        files_touched=[],
                                    ),
                                ),
                            )
                            yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                            yield "event: done\ndata: {}\n\n"
                            return
                        if quality_gate_failures:
                            assistant_message = await chat_manager.add_message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=_inline_code_quality_gate_failure_message(terminal_task, quality_gate_failures, files_touched),
                                bot_id=target_bot_id,
                                metadata=_assistant_bot_metadata(
                                    bot,
                                    bot_id=target_bot_id,
                                    extra=_inline_code_assistant_metadata(
                                        orchestration_id=orchestration_id,
                                        task=terminal_task,
                                        run_status="failed",
                                        files_touched=files_touched,
                                    ),
                                ),
                            )
                            yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                            yield "event: done\ndata: {}\n\n"
                            return

                        assistant_output = _extract_task_output(terminal_task.result)
                        assistant_output = _apply_repo_evidence_envelope(
                            assistant_output,
                            require_repo_evidence=require_repo_evidence,
                            context_sources=context_sources,
                        )
                        if files_touched and "Files touched in temp workspace:" not in assistant_output:
                            preview = "\n".join(f"- {path}" for path in files_touched[:8])
                            assistant_output = (
                                f"{assistant_output}\n\nFiles touched in temp workspace:\n{preview}".strip()
                                if assistant_output.strip()
                                else f"Files touched in temp workspace:\n{preview}"
                            )
                        yield 'event: status\ndata: {"phase":"persisting","label":"Saving inline coding recap..."}\n\n'
                        assistant_message = await chat_manager.add_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=assistant_output,
                            bot_id=target_bot_id,
                            metadata=_assistant_bot_metadata(
                                bot,
                                bot_id=target_bot_id,
                                extra=_inline_code_assistant_metadata(
                                    orchestration_id=orchestration_id,
                                    task=terminal_task,
                                    run_status="passed",
                                    files_touched=files_touched,
                                ),
                            ),
                        )
                        yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                        yield "event: done\ndata: {}\n\n"
                        return

                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=_inline_code_terminal_error_message(terminal_task),
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(
                            bot,
                            bot_id=target_bot_id,
                            extra=_inline_code_assistant_metadata(
                                orchestration_id=orchestration_id,
                                task=terminal_task,
                                run_status="failed",
                                files_touched=[],
                            ),
                        ),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return
                except HTTPException as exc:
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"Inline coding mode could not start: {exc.detail}",
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(bot, bot_id=target_bot_id),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return
                except Exception as exc:
                    assistant_message = await chat_manager.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"Inline coding run failed before completion: {exc}",
                        bot_id=target_bot_id,
                        metadata=_assistant_bot_metadata(
                            bot,
                            bot_id=target_bot_id,
                            extra={"mode": "assign_error", "orchestration_id": orchestration_id},
                        ),
                    )
                    yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return

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
            stream_worker_id: Optional[str] = None
            token_counter = 0
            async for event in scheduler.stream(task):
                event_name = str(event.get("event") or "")
                if event_name == "backend_selected":
                    provider = str(event.get("provider") or "unknown")
                    model = str(event.get("model") or "unknown")
                    stream_provider = provider
                    stream_model = model
                    worker_id = str(event.get("worker_id") or "").strip()
                    stream_worker_id = worker_id or None
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
                        stream_execution_provenance = (
                            {
                                "provider": stream_provider,
                                "model": stream_model,
                                "worker_id": stream_worker_id,
                            }
                            if stream_provider or stream_model or stream_worker_id
                            else None
                        )
                        partial_metadata = _assistant_bot_metadata(
                            bot,
                            bot_id=target_bot_id,
                            execution_provenance=stream_execution_provenance,
                            extra={"streaming": True},
                        )
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
            stream_execution_provenance = (
                {
                    "provider": stream_provider,
                    "model": stream_model,
                    "worker_id": stream_worker_id,
                }
                if stream_provider or stream_model or stream_worker_id
                else None
            )
            metadata = _assistant_bot_metadata(
                bot,
                bot_id=target_bot_id,
                execution_provenance=stream_execution_provenance,
                extra={"usage": (result or {}).get("usage", {})} if isinstance(result, dict) else {},
            )
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
