# Chat History Migration Staging

## Purpose

This document defines the safe staging path for importing historical chats from outside NexusAI into NexusAI conversations, projects, vault context, and memory. It is a planning and operating contract only; it does not start an import.

The goal is to make NexusAI the primary chat and work surface without mixing unrelated history, leaking private context into project work, or training personal memory from messages that were not intentionally allowed.

## Import Sources

Supported source classes should be treated separately:

- ChatGPT export conversations.
- Codex task and coding-session history.
- Claude, Gemini, OpenWebUI, or other assistant chats.
- Manual notes or copied transcripts.

Each source import must record:

- Source system.
- Source export timestamp.
- Import operator.
- Import run ID.
- Original conversation ID or stable source identifier when available.
- Whether the source contained attachments, code files, terminal output, or images.

## Destination Choices

Each imported conversation must be explicitly routed to one destination class:

- `unscoped`: one-off personal or general chats that do not belong to a project.
- `project`: chats tied to one project, repo, site, course platform, or research area.
- `bridged`: chats intentionally shared across a small set of projects.
- `archive_only`: preserved for search/reference but not used as active chat history.

Do not infer project scope from keywords alone. If confidence is low, import as `archive_only` or hold for operator review.

## Memory Rules

Historical imports must not automatically update personal memory.

Imported messages can become memory candidates only after a separate review step confirms:

- The importing user owns the source conversation.
- The destination chat has memory enabled.
- The selected bot has memory enabled.
- The scoped project has memory enabled, unless the chat is unscoped.
- The message contains durable preference, identity, project style, or workflow context rather than transient task detail.

Memory candidates should be created as editable memory items with metadata:

```json
{
  "source": "history_import_candidate",
  "source_system": "chatgpt",
  "import_run_id": "import-YYYYMMDD-N",
  "source_conversation_id": "source-id",
  "review_status": "pending"
}
```

## Project Vault Rules

Project-relevant historical content should be staged into the project vault only when it is useful as reusable context.

Good vault candidates:

- Project requirements.
- Architecture decisions.
- Known credentials references without raw secret values.
- Deployment procedures.
- Runbooks.
- Design notes.
- Prior bug investigations.
- Accepted content standards.

Poor vault candidates:

- Long free-form back-and-forth with no durable outcome.
- Failed attempts with no retained decision.
- Raw secrets, passwords, tokens, or private keys.
- Personal memory that is not project context.
- Duplicate content already represented in a current document.

Vault import metadata should include `source_system`, `import_run_id`, `source_conversation_id`, and `review_status`.

## Attachment Handling

Attachments must be classified before import:

- `text`: safe to store as message attachment or vault text if size limits allow.
- `image`: safe for message history, but only used by vision-capable bots.
- `code`: route to project vault or repo context only after project ownership is clear.
- `binary`: preserve metadata first; import file content only after explicit review.

Raw terminal logs should be treated as code/work evidence, not personal memory.

## Import Pipeline

1. Load source export into a staging area.
2. Normalize conversations, messages, timestamps, roles, and attachments.
3. Generate a routing proposal for each conversation.
4. Hold low-confidence routes for operator review.
5. Create NexusAI conversations with imported-message metadata.
6. Stage project vault candidates separately from chat history.
7. Stage memory candidates separately from chat history.
8. Run import validation reports.
9. Approve memory and vault candidates in small reviewed batches.

## Validation Report

Every import run must produce a report with:

- Total source conversations.
- Imported conversations.
- Archive-only conversations.
- Held conversations.
- Imported messages.
- Skipped messages with reasons.
- Imported attachments by kind.
- Vault candidates.
- Memory candidates.
- Raw-secret detection hits.
- Project routing confidence summary.
- Errors and retryable failures.

## Safety Invariants

- Imports must be idempotent by source system, source conversation ID, and import run ID.
- Imported history must preserve original timestamps separately from import timestamps.
- Imported history must not dispatch bots, spawn workers, run tools, or mutate repos.
- Imported history must not train memory by default.
- Imported history must not grant workspace tools to unscoped chats.
- Imported raw secrets must be blocked or redacted before storage.

## Future Work

- Multi-profile memory import and review queues.
- Per-source import adapters.
- Operator UI for routing proposals.
- Attachment deduplication.
- Full-text and vector reindex progress dashboard.
- Import rollback by import run ID.
