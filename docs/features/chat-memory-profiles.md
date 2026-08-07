# Chat Memory Profiles

## Purpose

Memory profiles provide user-scoped continuity across chats without mixing personal context into every project, bot, or team workflow. They are separate from project files, vault items, repo context, connections, and ordinary conversation history.

## Current Implementation

This slice supports one default profile per user: `default`.

Eligible chat turns store user and assistant messages into `memory_profile_items`. Later eligible turns retrieve semantically similar profile items and inject them as a bounded system context block named `Personal Memory Profile`.

The profile is scoped by `user_id`. Two users sharing the same NexusAI instance, projects, and bots do not share or train each other's memory profile.

Users can manage their own default profile from the dashboard Memory page. The page supports listing recent memory items, searching by semantic query, manually adding memory items, editing item content and role, and deleting items. Dashboard memory routes always derive `user_id` from the signed-in account and do not accept a caller-provided user id.

Bot and project memory gates are configurable from their dashboard screens. New bots and projects default to memory off. New chats default to memory on.

## Eligibility Gate

Memory may be used and updated only when all required gates are enabled for the current message turn:

- Conversation gate: `conversation.memory_profiles_enabled` must be true. New conversations default to true.
- Bot gate: `bot.memory_profiles_enabled` must be true. New bots default to false.
- Project gate: if the conversation is project-scoped, `project.memory_profiles_enabled` must be true. New projects default to false.
- User gate: the request must have a user id, either from the conversation owner or the message request.

Unscoped conversations do not require the project gate because there is no scoped project.

If any required gate is disabled, the turn does not retrieve profile memories and the sent/received messages are not added to the profile. Ordinary chat history and explicit project context still work normally.

## Project Chat Retrieval

Project chat retrieval is separate from personal memory. Every eligible user or assistant message in a project-scoped or bridged conversation is automatically upserted as an independent vault item in `project:<project_id>:chat`. This makes relevant prior chat evidence available to later chats in the same project without requiring a manual ingest action.

Unscoped conversations are not added to a project chat vault. They remain ordinary conversation history.

To intentionally include another conversation, paste its reference in the form `conversation:<UUID>` into a new message. NexusAI loads a bounded transcript directly from that conversation, whether it is scoped or unscoped. When both conversations have an owner, the owners must match; a reference does not bypass team-user boundaries. The conversation settings menu provides a copy action for this reference.

Project chat retrieval is bounded and labeled as retrieved chat context. It does not enable, train, or retrieve a personal memory profile, and it does not replace repository or file evidence for coding claims.

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
