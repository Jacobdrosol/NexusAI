# Chat History Migration Staging

This document defines the staged path for importing history from ChatGPT, Codex, Claude, Gemini, OpenWebUI, and similar chat systems into NexusAI. It is a migration plan, not an importer implementation.

## Objectives

- Preserve useful historical context without turning imported history into noisy active memory.
- Keep imported chats organized by project, source platform, and privacy scope.
- Make imports auditable and reversible before they affect normal chat, memory profiles, or project context.
- Avoid granting workspace tools or worker permissions to imported conversations by default.

## Scope Rules

Imported conversations should be assigned one of these scopes during staging:

| Scope | Use When | Default Tool Access | Default Memory Training |
| --- | --- | --- | --- |
| `unscoped` | One-off questions, personal brainstorming, or source history that does not clearly belong to a project. | Off | Off until reviewed |
| `personal` | User-specific preferences, recurring personal workflows, homework, writing, or planning. | Off | Optional after review |
| `project` | Work tied to a NexusAI project, repo, site, customer, or research area. | Off | Follows project, bot, and chat memory gates |
| `archive` | Old history retained for search only. | Off | Off |

Do not infer tool permissions from the source platform. Imported chats must start read-only.

## Import Shape

Each imported conversation should be normalized into:

- `source_platform`: `chatgpt`, `codex`, `claude`, `gemini`, `openwebui`, or another explicit source.
- `source_conversation_id`: stable source identifier when available.
- `source_exported_at`: timestamp from the export bundle or import run.
- `title`: source title or generated title.
- `owner_user_id`: NexusAI user who owns the imported history.
- `scope`: `global`, `project`, or `bridged` once mapped into the existing conversation model.
- `project_id`: only when the user or explicit mapping chooses a project.
- `memory_profiles_enabled`: false by default for bulk imports.
- `tool_access_enabled`, `tool_access_filesystem`, `tool_access_repo_search`: false by default.
- `metadata.import`: source metadata, importer version, source counts, skipped item counts, and validation status.

Each imported message should preserve:

- `role`
- `content`
- `created_at` when available
- `metadata.import.source_message_id`
- `metadata.import.source_platform`
- attachment metadata when available, without blindly inlining unsafe or oversized files

## Staging Flow

1. Export the source history into an operator-controlled staging folder outside the repository.
2. Parse source files into a normalized manifest without writing to NexusAI.
3. Generate a review report:
   - conversation count
   - message count
   - attachment count and total size
   - candidate project mappings
   - conversations with missing timestamps
   - conversations with unsupported attachment types
   - conversations that may include secrets or credentials
4. Let the owner approve project mappings and privacy scopes.
5. Import into NexusAI with memory disabled and tools disabled.
6. Run validation:
   - sample imported conversations render on the Chat page
   - message ordering is stable
   - attachments render only when supported
   - `/v1/chat/usage` does not count imported assistant messages as fresh usage unless usage metadata is explicitly imported
   - memory profile pages do not include imported messages until the owner enables memory training for selected chats
7. Enable memory selectively only after manual review or a dedicated memory-extraction pass.

## Operator Staging Layout

Keep raw exports and generated import plans outside this repository. Recommended layout:

```text
<operator-staging-root>/
  raw/
    chatgpt/
    codex/
    claude/
    gemini/
    openwebui/
  normalized/
    conversations.jsonl
    messages.jsonl
    attachments.jsonl
  reports/
    dry-run-summary.md
    project-mapping-review.csv
    secret-scan-findings.json
    unsupported-items.json
  approved/
    import-plan.json
```

The repository should contain importer code and documentation only. It must not contain raw source exports, normalized personal history, private attachments, source cookies, account identifiers beyond reviewed NexusAI user IDs, or generated import plans for a real user.

## Source Export Notes

| Source | Expected Inputs | Important Handling |
| --- | --- | --- |
| ChatGPT | Data export archive with conversations JSON and optional attachments. | Preserve conversation tree order when present. Mark generated image/file artifacts as attachments, not memory. |
| Codex | Task/thread export or local task archive when available. | Preserve repository path, branch, commit, and task ID in `metadata.import`; do not infer repo write permissions. |
| Claude | Exported conversations or copied project transcripts. | Preserve project/source labels when available, but keep project mapping pending owner review. |
| Gemini | Exported conversations from Takeout or copied transcripts. | Normalize role names and timestamps; mark missing timestamps in the dry-run report. |
| OpenWebUI | Database/API export or conversation JSON. | Preserve local model names as source metadata; map models to NexusAI routes only after import review. |

If a source only supports copied transcripts, import them as `archive` or `unscoped` unless the owner supplies an explicit project mapping.

## Dry-Run Manifest Contract

The first importer pass should emit JSONL files with one object per line and no database writes.

Validate the dry-run files before approving any import:

```bash
python scripts/validate_chat_import_manifest.py <operator-staging-root>/normalized --projects-file <approved-projects.json>
```

The optional projects file may be a JSON list such as `["globeiq", "nexusai"]` or an object with `project_ids`/`projects`. The validator exits non-zero when import blockers are present.

Dry-run validation also enforces source-link integrity before import:

- Duplicate `source_platform` plus `source_conversation_id` conversation keys are blockers.
- Duplicate `source_platform` plus `source_conversation_id` plus `source_message_id` message keys are blockers.
- Messages must reference a conversation present in `conversations.jsonl`.
- Attachments must reference a message present in `messages.jsonl`.
- Bridged project IDs must exist in the approved project list when one is supplied.
- Attachment `import_action` must be `import`, `metadata_only`, `review`, or `skip`.

Conversation record:

```json
{
  "source_platform": "chatgpt",
  "source_conversation_id": "source-stable-id",
  "title": "Conversation title",
  "owner_user_id": "user@example.com",
  "scope": "global",
  "project_id": null,
  "bridge_project_ids": [],
  "memory_profiles_enabled": false,
  "tool_access_enabled": false,
  "tool_access_filesystem": false,
  "tool_access_repo_search": false,
  "metadata": {"import": {"source_platform": "chatgpt", "validation_status": "pending"}}
}
```

Message record:

```json
{
  "source_platform": "chatgpt",
  "source_conversation_id": "source-stable-id",
  "source_message_id": "source-message-id",
  "role": "user",
  "content": "message text",
  "created_at": "2026-08-05T12:00:00+00:00",
  "metadata": {"import": {"source_platform": "chatgpt", "source_message_id": "source-message-id"}}
}
```

Attachment record:

```json
{
  "source_platform": "chatgpt",
  "source_conversation_id": "source-stable-id",
  "source_message_id": "source-message-id",
  "name": "file.png",
  "mime_type": "image/png",
  "size_bytes": 12345,
  "staged_path": "<operator-staging-root>/raw/chatgpt/file.png",
  "import_action": "review"
}
```

## Import Blockers

The dry-run report must block import until these are resolved:

- Any secret-like value in message text, file names, attachment metadata, or source metadata.
- Any unsupported attachment type that cannot be safely skipped or retained as metadata-only.
- Any conversation mapped to a project that does not exist in NexusAI.
- Any imported conversation requesting workspace tools.
- Any imported conversation requesting memory training before owner review.
- Any source export item with ambiguous ownership.
- Any malformed timestamp that would break stable message ordering.

## Memory Policy

Bulk imported history must not automatically train memory profiles. The safe default is:

- Imported chats: memory off until reviewed.
- Imported messages: never create memory items directly.
- Memory extraction: separate reviewed pass that proposes memory items with `source=imported`.
- Project chats: still require all three gates before future messages use or update memory: chat enabled, bot enabled, project enabled.

## Safety Constraints

- Do not import API keys, passwords, private SSH keys, session cookies, or access tokens into chat content.
- Do not enable workspace tools on imported conversations during import.
- Do not run workers from imported conversations automatically.
- Do not merge source identities across users.
- Do not treat imported source titles as reliable project mappings without owner review.
- Do not commit raw source exports to this repository.

## Future Implementation Notes

The first importer should be a dry-run-first CLI or admin-only dashboard flow:

1. `parse` source exports into normalized JSONL.
2. `plan` project and scope mappings.
3. `validate` unsupported items and risky content.
4. `import` approved conversations with tools and memory disabled.
5. `verify` counts, sample rendering, and searchability.

After the importer is stable, add source-specific adapters for ChatGPT, Codex, Claude, Gemini, and OpenWebUI exports.
