import json

from scripts.validate_chat_import_manifest import validate_manifest


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_chat_import_manifest_validator_accepts_safe_dry_run(tmp_path):
    manifest_dir = tmp_path / "normalized"
    manifest_dir.mkdir()
    _write_jsonl(
        manifest_dir / "conversations.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "title": "Physics planning",
                "owner_user_id": "jacob@example.com",
                "scope": "global",
                "project_id": None,
                "memory_profiles_enabled": False,
                "tool_access_enabled": False,
                "tool_access_filesystem": False,
                "tool_access_repo_search": False,
            }
        ],
    )
    _write_jsonl(
        manifest_dir / "messages.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "role": "user",
                "content": "Help me plan a derivation.",
                "created_at": "2026-08-05T12:00:00+00:00",
            }
        ],
    )
    _write_jsonl(
        manifest_dir / "attachments.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "name": "diagram.png",
                "mime_type": "image/png",
                "size_bytes": 1234,
                "staged_path": str(tmp_path / "raw" / "diagram.png"),
                "import_action": "review",
            }
        ],
    )

    result = validate_manifest(manifest_dir)

    assert result.conversations == 1
    assert result.messages == 1
    assert result.attachments == 1
    assert result.blocker_count == 0


def test_chat_import_manifest_validator_blocks_tools_memory_secrets_and_unsafe_attachments(tmp_path):
    manifest_dir = tmp_path / "normalized"
    manifest_dir.mkdir()
    _write_jsonl(
        manifest_dir / "conversations.jsonl",
        [
            {
                "source_platform": "codex",
                "source_conversation_id": "source-conv-1",
                "title": "Repo task",
                "owner_user_id": "jacob@example.com",
                "scope": "project",
                "project_id": "missing-project",
                "memory_profiles_enabled": True,
                "tool_access_enabled": True,
                "tool_access_filesystem": True,
                "tool_access_repo_search": True,
            }
        ],
    )
    _write_jsonl(
        manifest_dir / "messages.jsonl",
        [
            {
                "source_platform": "codex",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "role": "assistant",
                "content": "token=sk-1234567890abcdef1234567890",
                "created_at": "not-a-date",
            }
        ],
    )
    _write_jsonl(
        manifest_dir / "attachments.jsonl",
        [
            {
                "source_platform": "codex",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "name": "run.ps1",
                "mime_type": "text/plain",
                "size_bytes": 42,
                "staged_path": str(tmp_path / "raw" / "run.ps1"),
            }
        ],
    )
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps(["globeiq"]), encoding="utf-8")

    result = validate_manifest(manifest_dir, projects_file=projects_file)
    codes = {item.code for item in result.findings}

    assert result.blocker_count >= 6
    assert "unknown_project" in codes
    assert "memory_enabled" in codes
    assert "tools_enabled" in codes
    assert "secret_like_value" in codes
    assert "invalid_timestamp" in codes
    assert "unsupported_attachment" in codes


def test_chat_import_manifest_validator_blocks_unlinked_and_duplicate_source_records(tmp_path):
    manifest_dir = tmp_path / "normalized"
    manifest_dir.mkdir()
    _write_jsonl(
        manifest_dir / "conversations.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "title": "Project planning",
                "owner_user_id": "jacob@example.com",
                "scope": "bridged",
                "project_id": "globeiq",
                "bridge_project_ids": ["unknown-project"],
                "memory_profiles_enabled": False,
                "tool_access_enabled": False,
                "tool_access_filesystem": False,
                "tool_access_repo_search": False,
            },
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "title": "Duplicate planning",
                "owner_user_id": "jacob@example.com",
                "scope": "global",
                "project_id": None,
                "memory_profiles_enabled": False,
                "tool_access_enabled": False,
                "tool_access_filesystem": False,
                "tool_access_repo_search": False,
            },
        ],
    )
    _write_jsonl(
        manifest_dir / "messages.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "role": "user",
                "content": "Project note.",
                "created_at": "2026-08-05T12:00:00+00:00",
            },
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "msg-1",
                "role": "assistant",
                "content": "Duplicate note.",
                "created_at": "2026-08-05T12:01:00+00:00",
            },
            {
                "source_platform": "claude",
                "source_conversation_id": "missing-conv",
                "source_message_id": "msg-2",
                "role": "user",
                "content": "No matching conversation.",
                "created_at": "2026-08-05T12:02:00+00:00",
            },
        ],
    )
    _write_jsonl(
        manifest_dir / "attachments.jsonl",
        [
            {
                "source_platform": "chatgpt",
                "source_conversation_id": "source-conv-1",
                "source_message_id": "missing-message",
                "name": "diagram.png",
                "mime_type": "image/png",
                "size_bytes": 1234,
                "staged_path": str(tmp_path / "raw" / "diagram.png"),
                "import_action": "attach",
            }
        ],
    )
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps(["globeiq"]), encoding="utf-8")

    result = validate_manifest(manifest_dir, projects_file=projects_file)
    codes = {item.code for item in result.findings}

    assert result.blocker_count >= 5
    assert "duplicate_conversation" in codes
    assert "unknown_bridge_project" in codes
    assert "duplicate_message" in codes
    assert "unknown_conversation" in codes
    assert "unknown_message" in codes
    assert "invalid_import_action" in codes
