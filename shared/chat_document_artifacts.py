"""Bounded DOCX artifact generation for explicitly enabled chat bots."""
from __future__ import annotations

import base64
import io
import re
from typing import Any

from docx import Document

from shared.chat_attachments import CHAT_ATTACHMENT_MAX_INLINE_BYTES, CHAT_ATTACHMENT_MAX_TEXT_BYTES

_DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_REQUEST_RE = re.compile(r"(?:\.docx\b|\bword\s+document\b|\bmicrosoft\s+word\b)", re.IGNORECASE)
_DOCX_FILENAME_RE = re.compile(r"(?:named|called|as)\s+[\"']?([^\"'\n]{1,120}?\.docx)\b", re.IGNORECASE)
_INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def document_generation_enabled(bot: Any) -> bool:
    """Return whether this bot is explicitly allowed to create chat DOCX artifacts."""
    routing_rules = getattr(bot, "routing_rules", None)
    if not isinstance(routing_rules, dict):
        return False
    profile = routing_rules.get("chat_profile")
    return bool(isinstance(profile, dict) and profile.get("document_generation") is True)


def requested_docx_artifact(prompt: str, *, enabled: bool) -> str | None:
    """Return the requested DOCX filename only for explicit, bot-authorized requests."""
    if not enabled or not _DOCX_REQUEST_RE.search(str(prompt or "")):
        return None
    match = _DOCX_FILENAME_RE.search(str(prompt or ""))
    candidate = match.group(1) if match else "NexusAI_Document.docx"
    return _safe_docx_filename(candidate)


def document_generation_instruction(filename: str) -> str:
    return (
        "The user explicitly requested a downloadable DOCX named "
        f"'{filename}'. Provide the complete document body in Markdown now. "
        "Do not say that you cannot create files, do not include implementation notes, and do not wrap the "
        "document in a code fence. NexusAI will convert your final response into the DOCX attachment."
    )


def build_docx_attachment(*, filename: str, content: str) -> dict[str, Any] | None:
    """Convert bounded Markdown-like assistant output into a downloadable DOCX attachment."""
    text = str(content or "").strip()
    if not text:
        return None
    document = Document()
    in_code_block = False
    for raw_line in text.splitlines():
        line = str(raw_line or "").rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if not stripped:
            document.add_paragraph("")
            continue
        if in_code_block:
            paragraph = document.add_paragraph()
            paragraph.style = document.styles["No Spacing"]
            run = paragraph.add_run(line)
            run.font.name = "Courier New"
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            document.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
            continue
        if re.match(r"^[-*+]\s+", stripped):
            document.add_paragraph(re.sub(r"^[-*+]\s+", "", stripped), style="List Bullet")
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            document.add_paragraph(re.sub(r"^\d+[.)]\s+", "", stripped), style="List Number")
            continue
        document.add_paragraph(stripped)

    buffer = io.BytesIO()
    document.save(buffer)
    raw = buffer.getvalue()
    if len(raw) > CHAT_ATTACHMENT_MAX_INLINE_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "name": _safe_docx_filename(filename),
        "mime_type": _DOCX_MIME_TYPE,
        "kind": "document",
        "data_url": f"data:{_DOCX_MIME_TYPE};base64,{encoded}",
        "text_content": text[:CHAT_ATTACHMENT_MAX_TEXT_BYTES],
        "size_bytes": len(raw),
        "truncated": len(text) > CHAT_ATTACHMENT_MAX_TEXT_BYTES,
        "extraction_status": "generated",
        "generated_by": "chat_document_artifact",
    }


def _safe_docx_filename(value: str) -> str:
    name = _INVALID_FILENAME_CHARS.sub("", str(value or "").strip()).strip(". ")
    if not name:
        name = "NexusAI_Document.docx"
    if not name.lower().endswith(".docx"):
        name = f"{name}.docx"
    stem = name[:-5].strip()[:100].rstrip(". ") or "NexusAI_Document"
    return f"{stem}.docx"
