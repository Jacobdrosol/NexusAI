# Chat

The chat module handles persistent conversations, streaming message delivery, workspace tool access, and PM orchestration entry point.

---

## Files

| File | Class | Purpose |
|------|-------|---------|
| `chat_manager.py` | `ChatManager` | Conversation and message CRUD, semantic memory |
| `pm_orchestrator.py` | `PMOrchestrator` | `@assign` orchestration bootstrap |
| `workspace_tools.py` | Functions | File and repo snippet helpers for tool-access chat |

---

## ChatManager (`chat_manager.py`)

### Database Tables (SQLite)

| Table | Purpose |
|-------|---------|
| `conversations` | Conversation metadata: title, scope, project_id, bot defaults, tool access flags |
| `messages` | Individual messages: role (system/user/assistant/tool), content, bot_id, model |
| `chat_message_memory` | Chunked embeddings for in-conversation semantic search |

### Key Methods

| Method | Description |
|--------|-------------|
| `create_conversation(title, ...)` | Creates a new conversation |
| `get_conversation(id)` | Raises `ConversationNotFoundError` if not found |
| `list_conversations(project_id, scope, archived, limit)` | Filtered listing |
| `update_conversation(id, ...)` | Update title, bot defaults, tool access |
| `archive_conversation(id)` | Sets `archived_at` timestamp |
| `add_message(conversation_id, role, content, ...)` | Stores message, triggers memory indexing |
| `list_messages(conversation_id, limit)` | Ordered by `created_at` |
| `search_memory(conversation_id, query, limit)` | Cosine similarity search over `chat_message_memory` |

### Conversation Scopes

| Scope | Description |
|-------|-------------|
| `global` | Accessible across all projects |
| `project` | Scoped to one project |
| `bridged` | Scoped to a project + its bridge projects |

### Tool Access Flags

Three independent boolean flags control workspace tool access:
- `tool_access_enabled` — master switch
- `tool_access_filesystem` — allows reading local workspace files
- `tool_access_repo_search` — allows repo snippet search

All three must be enabled (bot policy, project policy, and chat policy) for workspace tools to activate.

### Memory Indexing

Messages with `role=user` or `role=assistant` (excluding assignment metadata modes) are chunked at 800 chars with 120 overlap and indexed using a 64-dimensional SHA-256 hash embedding. Cosine similarity is used for retrieval. **Note: this is a bag-of-words approximation — semantic quality is limited.**

---

## PMOrchestrator (`pm_orchestrator.py`)

### Entry Point: `orchestrate_assignment()`

```python
await pm_orchestrator.orchestrate_assignment(
    conversation_id=...,
    instruction=...,
    requested_pm_bot_id=...,   # required — raises BotNotFoundError if missing
    context_items=[...],
    conversation_brief=...,
    conversation_transcript=...,
    conversation_message_count=...,
    assignment_memory_hits=[...],
    project_id=...,
)
```

Returns: `{orchestration_id, pm_bot_id, instruction, plan, tasks, allowed_bot_ids, workflow_graph_id, pipeline_name}`

### What It Does

1. Validates the requested PM bot exists and has an explicit workflow.
2. Calls `_extract_assignment_scope()` to build a comprehensive scope dict including:
   - `scope_lock` (domains, allowed_artifacts, forbidden_keywords)
   - `docs_only`, `ui_test_mode`, `explicit_stage_exclusions`
   - Constraint hints, focus topics, artifact hints, transcript
3. Optionally creates an orchestration temp workspace (if `project_id` and `orchestration_workspace_store` are set).
4. Creates a root task via `task_manager.create_task(bot_id=pm_bot.id, payload={...scope...})`.
5. Returns immediately — the full DAG unfolds via `BotWorkflowTrigger` as stages complete.

### Scope Lock

`_extract_scope_lock(instruction)` performs keyword extraction to populate:
- `domains`: detected from instruction (math, geometry, programming)
- `allowed_artifacts`: e.g., `["*.md"]` for docs-only
- `forbidden_keywords`: e.g., `[".py", "test"]` for docs-only math blocks

**Known issue**: the domain catalog is narrow and hardcoded. Most assignments produce `"general"` domain, making the lock a no-op.

### Docs-Only Detection

`_instruction_requests_docs_only_outputs(instruction)` checks for a docs signal AND a docs-only signal simultaneously. Both must be present. See [PM_WORKFLOW.md](../../docs/PM_WORKFLOW.md) for full behavior.

---

## Workspace Tools (`workspace_tools.py`)

Provides functions used by `api/chat.py` when `use_workspace_tools=True`:

| Function | Description |
|----------|-------------|
| `normalize_workspace_root(path)` | Resolves and validates the workspace root directory |
| `extract_path_hints(query)` | Extracts file path hints from a natural language query |
| `build_focus_query(message, hints)` | Builds an optimised search query from message + path hints |
| `search_workspace_snippets(root, query, max_results)` | Grep-style file search returning snippets |
| `read_workspace_file_snippet(root, path, max_chars)` | Reads a file within the workspace root, bounded by `max_chars` |

Access control: workspace tools are gated at three levels — bot `execution_policy.repo_output_mode == "allow"`, project policy, and conversation `tool_access_enabled`. All three must be set.

---

## Known Issues

- Chat memory embeddings are SHA-256 hash based — no semantic retrieval quality.
- `_embed` function is duplicated between `ChatManager` and `VaultManager`.
- Workspace tool `search_workspace_snippets` uses subprocess grep; may not respect `.gitignore`.
- Conversation memory is per-conversation only; cross-conversation context requires vault ingestion.
