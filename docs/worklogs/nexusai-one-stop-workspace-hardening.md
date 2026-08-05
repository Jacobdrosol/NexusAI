# NexusAI One-Stop Workspace Hardening

## Objective

Make NexusAI usable as the primary workspace for chat, project context, worker operations, tooling, quality control, and eventually scoped coding/research workflows.

## Scope

- Improve operator visibility into bots, workers, tools, readiness, and active work.
- Keep current agentic workers safe and scoped before adding higher-risk coding workers.
- Improve chat/project ergonomics so the platform is usable day to day.
- Commit stable implementation batches only; temporary scratch lists stay uncommitted.

## Completion Criteria

- Chat can be used reliably for normal assistant-style work.
- Bot and worker readiness is visible without SSH.
- Blocked tools show actionable causes.
- Project and manager work lanes show task pressure, usage, and holds.
- New tooling changes have focused tests.
- Remaining external blockers are documented separately from NexusAI code defects.

## Current State

- Public NexusAI has pushed hardening commits after deployed commit `3808ea1`.
- Live readiness before this hardening pass: 105 ready, 2 enabled blocked, 23 disabled.
- The only enabled blockers are GlobeIQ browser-session attestation failures for the browser inspector lane.
- Work overview already tracks project/manager lanes, token usage, queue pressure, holds, route evidence, and task freshness.

## Batch Plan

- Batch 1: Bot tooling visibility and readiness triage.
- Batch 2: Chat usability and live-message verification fixes.
- Batch 3: Bot profile/tooling config checks for chat/research/tutor assistants.
- Batch 4: Safer operator controls for scoped worker activation and proof runs.

## Progress

- Batch 1 complete and pushed as `0fb7a5c`: added reusable bot tooling status builder, `/bots` readiness overview, `/api/bots/tooling-status`, grouped blocker causes, required tool summaries, and focused tests.
- Batch 2 in progress: added chat assistant provenance rendering so normal messages show the answering bot, provider, model, and bot update timestamp when stored in message metadata.
- Batch 3 in progress: added bot-list safety details for effective chat autonomy and chat tool gates so manual chat bots and tool-enabled assistants are distinguishable without opening each bot.

## Validation

- `pytest tests/test_bot_tooling_status.py tests/test_bot_readiness.py tests/test_bot_tool_policy.py -q` passed for Batch 1.
- `pytest tests/test_dashboard_phase4_pages.py::test_chat_page_surfaces_assistant_bot_and_model_provenance tests/test_dashboard_phase4_pages.py::test_chat_page_unscoped_filter_limits_conversation_list tests/test_dashboard_phase4_pages.py::test_chat_page_project_filter_limits_conversation_list tests/test_chat_api.py::test_create_conversation_and_post_message -q` passed for Batch 2 chat provenance.
- `pytest tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes tests/test_bot_tooling_status.py -q` passed for Batch 3 bot profile/tool gate visibility.
