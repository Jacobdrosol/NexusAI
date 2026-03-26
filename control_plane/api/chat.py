import asyncio
from collections import Counter
import base64
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
from shared.chat_attachments import (
    CHAT_ATTACHMENT_MAX_FILES,
    CHAT_ATTACHMENT_MAX_TEXT_BYTES,
    CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
)
from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage, Task, TaskMetadata
from shared.settings_manager import get_context_limits_for_model


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
    r"\b(read|search|scan|inspect|review|audit|analy[sz]e|open|look\s+through|walk\s+through)\b",
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
_IMAGE_CAPABILITY_MARKERS = {"image", "images", "vision", "multimodal"}


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
        return any(token in lowered_model for token in ("vision", "-vl", "qwen2.5-vl", "qwen-vl", "llava"))
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
_UNCITED_MAX_LINES = 28
_UNCITED_MAX_CHARS = 1800
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
            ("read ", "search ", "scan ", "inspect ", "review ", "analyze ", "analyse ", "open ", "look through ", "walk through ")
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
        if _GROUNDING_NOTE_LINE_RE.search(line):
            continue
        if _PLANNING_PREAMBLE_LINE_RE.search(line):
            continue
        if _TOOL_ARG_LINE_RE.search(line) and any(token in line for token in ("*", "/", "\\", ".cs", ".razor", ".py", ".ts")):
            continue
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
    if mode in {"assign_request", "assign_pending", "pm_run_report", "assign_summary", "assign_error"}:
        return False
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

    kept: List[str] = []
    for item in rendered_entries[:head_messages]:
        kept.append(item)
    omitted_count = max(0, len(rendered_entries) - len(kept))
    tail: List[str] = []
    used_chars = sum(len(item) + 1 for item in kept)
    for item in reversed(rendered_entries[head_messages:]):
        item_cost = len(item) + 1
        if len(kept) + len(tail) >= max_messages or used_chars + item_cost > max_chars:
            break
        tail.append(item)
        used_chars += item_cost
    tail.reverse()
    omitted_count = max(0, len(rendered_entries) - len(kept) - len(tail))
    if omitted_count > 0:
        kept.append(f"... ({omitted_count} earlier chat message(s) omitted for size) ...")
    kept.extend(tail)
    return {
        "conversation_transcript": "\n".join(kept),
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
        if str(metadata.get("mode") or "").strip() not in {"pm_run_report", "assign_summary", "assign_pending"}:
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
    pm_orchestrator = request.app.state.pm_orchestrator
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
                metadata={
                    "mode": "assign_pending",
                    "orchestration_id": assignment.get("orchestration_id"),
                    "task_count": len(assignment.get("tasks", [])),
                    "assigned_pm_bot_id": str(assignment.get("pm_bot_id") or assign_bot_id or ""),
                    **context_meta,
                },
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
                        metadata={
                            "mode": "assign_error",
                            "orchestration_id": assignment.get("orchestration_id"),
                        },
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
        ns_item_limit, ns_source_limit = 30, 12  # defaults
        if ns_bot_registry:
            try:
                ns_bot = await ns_bot_registry.get(target_bot_id)
                ns_item_limit, ns_source_limit = _get_context_limits_for_bot(ns_bot)
            except Exception:
                pass

        require_repo_evidence = _repo_evidence_requested(body)
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
            item_limit, source_limit = 30, 12  # defaults
            if bot_registry:
                try:
                    bot = await bot_registry.get(target_bot_id)
                    item_limit, source_limit = _get_context_limits_for_bot(bot)
                except Exception:
                    pass  # Keep defaults on error

            require_repo_evidence = _repo_evidence_requested(body)
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
