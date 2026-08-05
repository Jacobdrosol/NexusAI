#!/usr/bin/env python3
"""Validate dry-run chat-history import manifests before any database import."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ALLOWED_SCOPES = {"global", "project", "bridged"}
ALLOWED_ROLES = {"user", "assistant", "system", "tool"}
UNSUPPORTED_ATTACHMENT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".js",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".vbs",
}
UNSUPPORTED_ATTACHMENT_MIME_PREFIXES = ("application/x-msdownload", "application/x-sh", "application/x-msdos-program")
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{16,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
]


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    code: str
    detail: str


@dataclass
class ValidationResult:
    conversations: int = 0
    messages: int = 0
    attachments: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocker_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "blocker")

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "warning")

    def add(self, severity: str, file: Path, line: int, code: str, detail: str) -> None:
        self.findings.append(Finding(severity=severity, file=str(file), line=line, code=code, detail=detail))


def _load_jsonl(path: Path, result: ValidationResult) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    if not path.exists():
        result.add("blocker", path, 0, "missing_file", f"required manifest file is missing: {path.name}")
        return rows
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            result.add("blocker", path, line_no, "invalid_json", f"invalid JSONL row: {exc.msg}")
            continue
        if not isinstance(item, dict):
            result.add("blocker", path, line_no, "invalid_record", "JSONL row must be an object")
            continue
        rows.append((line_no, item))
    return rows


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _text_values(nested)


def _scan_for_secrets(path: Path, line_no: int, record: dict[str, Any], result: ValidationResult) -> None:
    for text in _text_values(record):
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                result.add("blocker", path, line_no, "secret_like_value", "record contains a secret-like value")
                return


def _string(record: dict[str, Any], key: str) -> str:
    return str(record.get(key) or "").strip()


def _bool(record: dict[str, Any], key: str) -> bool:
    return bool(record.get(key))


def _parse_timestamp(value: str) -> bool:
    if not value:
        return False
    candidate = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


def _validate_conversation(
    path: Path,
    line_no: int,
    record: dict[str, Any],
    result: ValidationResult,
    project_ids: set[str],
) -> None:
    result.conversations += 1
    for key in ("source_platform", "source_conversation_id", "title", "owner_user_id"):
        if not _string(record, key):
            result.add("blocker", path, line_no, f"missing_{key}", f"conversation is missing {key}")
    scope = _string(record, "scope") or "global"
    if scope not in ALLOWED_SCOPES:
        result.add("blocker", path, line_no, "invalid_scope", f"scope must be one of: {', '.join(sorted(ALLOWED_SCOPES))}")
    project_id = _string(record, "project_id")
    if scope in {"project", "bridged"} and not project_id:
        result.add("blocker", path, line_no, "missing_project", "project or bridged conversations require project_id")
    if project_id and project_ids and project_id not in project_ids:
        result.add("blocker", path, line_no, "unknown_project", f"project_id is not in approved projects: {project_id}")
    if _bool(record, "memory_profiles_enabled"):
        result.add("blocker", path, line_no, "memory_enabled", "bulk imports must not enable memory by default")
    for key in ("tool_access_enabled", "tool_access_filesystem", "tool_access_repo_search"):
        if _bool(record, key):
            result.add("blocker", path, line_no, "tools_enabled", f"bulk imports must not enable {key}")


def _validate_message(path: Path, line_no: int, record: dict[str, Any], result: ValidationResult) -> None:
    result.messages += 1
    for key in ("source_platform", "source_conversation_id", "source_message_id"):
        if not _string(record, key):
            result.add("blocker", path, line_no, f"missing_{key}", f"message is missing {key}")
    role = _string(record, "role")
    if role not in ALLOWED_ROLES:
        result.add("blocker", path, line_no, "invalid_role", f"role must be one of: {', '.join(sorted(ALLOWED_ROLES))}")
    if not isinstance(record.get("content"), str):
        result.add("blocker", path, line_no, "invalid_content", "message content must be a string")
    created_at = _string(record, "created_at")
    if not _parse_timestamp(created_at):
        result.add("blocker", path, line_no, "invalid_timestamp", "created_at must be an ISO-8601 timestamp")


def _validate_attachment(path: Path, line_no: int, record: dict[str, Any], result: ValidationResult) -> None:
    result.attachments += 1
    for key in ("source_platform", "source_conversation_id", "source_message_id", "name"):
        if not _string(record, key):
            result.add("blocker", path, line_no, f"missing_{key}", f"attachment is missing {key}")
    name = _string(record, "name")
    mime_type = _string(record, "mime_type").lower()
    suffix = Path(name).suffix.lower()
    if suffix in UNSUPPORTED_ATTACHMENT_EXTENSIONS or mime_type.startswith(UNSUPPORTED_ATTACHMENT_MIME_PREFIXES):
        result.add("blocker", path, line_no, "unsupported_attachment", f"unsupported attachment type: {name}")
    try:
        size_bytes = int(record.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = -1
    if size_bytes < 0:
        result.add("blocker", path, line_no, "invalid_attachment_size", "size_bytes must be zero or greater")
    staged_path = _string(record, "staged_path")
    if staged_path and Path(staged_path).is_relative_to(Path.cwd()):
        result.add("warning", path, line_no, "repo_staged_attachment", "attachment appears staged inside the current repository")


def _load_project_ids(path: Path | None) -> set[str]:
    if path is None:
        return set()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}
    if isinstance(raw, dict):
        values = raw.get("project_ids") or raw.get("projects") or []
        return {str(item).strip() for item in values if str(item).strip()}
    return set()


def validate_manifest(manifest_dir: Path, *, projects_file: Path | None = None) -> ValidationResult:
    result = ValidationResult()
    project_ids = _load_project_ids(projects_file)
    file_specs = [
        ("conversations.jsonl", _validate_conversation),
        ("messages.jsonl", _validate_message),
        ("attachments.jsonl", _validate_attachment),
    ]
    for filename, validator in file_specs:
        path = manifest_dir / filename
        for line_no, record in _load_jsonl(path, result):
            _scan_for_secrets(path, line_no, record, result)
            if validator is _validate_conversation:
                validator(path, line_no, record, result, project_ids)
            else:
                validator(path, line_no, record, result)
    return result


def _print_report(result: ValidationResult) -> None:
    print("Chat import manifest validation")
    print(f"- conversations: {result.conversations}")
    print(f"- messages: {result.messages}")
    print(f"- attachments: {result.attachments}")
    print(f"- blockers: {result.blocker_count}")
    print(f"- warnings: {result.warning_count}")
    if result.findings:
        print("\nFindings")
        for item in result.findings:
            location = f"{item.file}:{item.line}" if item.line else item.file
            print(f"- {item.severity.upper()} {item.code} at {location}: {item.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate dry-run NexusAI chat history import manifests.")
    parser.add_argument("manifest_dir", type=Path, help="Directory containing conversations/messages/attachments JSONL files.")
    parser.add_argument("--projects-file", type=Path, help="Optional JSON list of approved NexusAI project IDs.")
    args = parser.parse_args(argv)

    result = validate_manifest(args.manifest_dir, projects_file=args.projects_file)
    _print_report(result)
    return 1 if result.blocker_count else 0


if __name__ == "__main__":
    sys.exit(main())
