# Chat Memory Profiles

## Purpose

Memory profiles provide user-scoped continuity across chats without mixing personal context into every project, bot, or team workflow. They are separate from project files, vault items, repo context, connections, and ordinary conversation history.

## Current Implementation

This slice supports one default profile per user: `default`.

Eligible chat turns store user and assistant messages into `memory_profile_items`. Later eligible turns retrieve semantically similar profile items and inject them as a bounded system context block named `Personal Memory Profile`.

The profile is scoped by `user_id`. Two users sharing the same NexusAI instance, projects, and bots do not share or train each other's memory profile.

## Eligibility Gate

Memory may be used and updated only when all required gates are enabled for the current message turn:

- Conversation gate: `conversation.memory_profiles_enabled` must be true. New conversations default to true.
- Bot gate: `bot.memory_profiles_enabled` must be true. New bots default to false.
- Project gate: if the conversation is project-scoped, `project.memory_profiles_enabled` must be true. New projects default to false.
- User gate: the request must have a user id, either from the conversation owner or the message request.

Unscoped conversations do not require the project gate because there is no scoped project.

If any required gate is disabled, the turn does not retrieve profile memories and the sent/received messages are not added to the profile. Ordinary chat history and explicit project context still work normally.

## Stored Metadata

Messages record compact memory decision metadata:

- `eligible`
- `profile_id`
- `user_id`
- `hit_count`
- gate booleans for chat, bot, and project

The metadata is diagnostic. It is not a copy of the bot configuration or project configuration.

## Design Constraints

- Personal memory is never treated as verified project evidence.
- Personal memory does not override the current user message, selected project context, retrieved files, or tool evidence.
- Worker, automation, customer-service, repair, and scoped operational bots should keep `memory_profiles_enabled` false unless there is a specific reason to use personal memory.
- Imported memory must remain user-scoped and should be reviewable before activation.

## Future Multi-Profile Design

Future work should add first-class `memory_profiles` records with:

- `id`
- `user_id`
- display name
- source: `manual`, `chatgpt_import`, `claude_import`, `auto`
- enabled state
- import metadata
- retention policy
- created/updated timestamps

Conversations should select one memory profile at a time by default, with a later option to allow explicit multi-profile retrieval. Project and bot gates should remain booleans, while conversations decide which user profile is active.

Imported ChatGPT, Claude, Gemini, or Codex history should be normalized into profile items only after parsing, deduplication, and user review. Importers must not ingest secrets, credentials, or private third-party data without an explicit review step.
