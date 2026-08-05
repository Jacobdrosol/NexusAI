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
