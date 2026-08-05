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
- Batch 4 in progress: added active and paused schedule counts to bot dispatch mode rows so operator activation state is visible from the main bot list.
- Batch 5 in progress: added a selected-bot capability summary on the chat page for profile, backend model, image support, memory state, and chat tool gates.
- Batch 6 in progress: centralized dashboard chat profile normalization so `/bots` and `/chat` use the same profile, autonomy, tool gate, and capability calculations.
- Batch 7 in progress: added a schedules overview summary for active, paused, recent failed, and active unattested automations.
- Batch 8 in progress: added project-detail AI workspace readiness, summarizing assigned bots, memory gate, chat workspace tools, repo workspace, vault context, database connections, and GitHub context in one operator-facing panel.
- Batch 9 in progress: added chat effective-context visibility so the active conversation shows whether personal memory and workspace tools will actually be used after combining chat, bot, project, and per-message gates.
- Batch 10 in progress: added bot-detail operating summary for dispatch state, readiness, active/paused schedules, chat mode, chat tools, memory, and next operator action.
- Batch 11 in progress: added provider/model token usage visibility to the work dashboard so usage can be traced by backend model as well as project, manager, and bot.
- Batch 12 in progress: added worker-list runtime tool evidence so browser, CLI, and provider credential blockers are visible from the fleet table without opening each worker or using SSH.

## Validation

- `pytest tests/test_bot_tooling_status.py tests/test_bot_readiness.py tests/test_bot_tool_policy.py -q` passed for Batch 1.
- `pytest tests/test_dashboard_phase4_pages.py::test_chat_page_surfaces_assistant_bot_and_model_provenance tests/test_dashboard_phase4_pages.py::test_chat_page_unscoped_filter_limits_conversation_list tests/test_dashboard_phase4_pages.py::test_chat_page_project_filter_limits_conversation_list tests/test_chat_api.py::test_create_conversation_and_post_message -q` passed for Batch 2 chat provenance.
- `pytest tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes tests/test_bot_tooling_status.py -q` passed for Batch 3 bot profile/tool gate visibility.
- `pytest tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles -q` passed for Batch 4 dispatch schedule counts.
- `pytest tests/test_dashboard_phase4_pages.py::test_chat_page_limits_normal_bot_selectors_to_chat_bots tests/test_dashboard_phase4_pages.py::test_chat_page_surfaces_assistant_bot_and_model_provenance tests/test_dashboard_phase4_pages.py::test_chat_page_unscoped_filter_limits_conversation_list -q` passed for Batch 5 chat bot capability summary.
- `pytest tests/test_bot_chat_profiles.py tests/test_bot_tooling_status.py tests/test_dashboard_phase4_pages.py::test_chat_page_limits_normal_bot_selectors_to_chat_bots tests/test_dashboard_phase4_pages.py::test_chat_page_surfaces_assistant_bot_and_model_provenance tests/test_dashboard_phase4_pages.py::test_chat_page_unscoped_filter_limits_conversation_list tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes -q` passed for Batch 6 shared chat profile normalization.
- `pytest tests/test_dashboard_phase4_pages.py::test_schedules_page_and_proxy_support_operational_schedule_management tests/test_bot_chat_profiles.py tests/test_dashboard_phase4_pages.py::test_chat_page_limits_normal_bot_selectors_to_chat_bots -q` passed for Batch 7 schedules overview summary.
- `pytest tests/test_dashboard_phase4_pages.py::test_project_detail_page_renders_with_partial_github_status tests/test_dashboard_phase4_pages.py::test_project_detail_page_surfaces_ai_workspace_readiness tests/test_dashboard_phase4_pages.py::test_project_repo_workspace_api_proxies_control_plane -q` passed for Batch 8 project readiness.
- `pytest tests/test_dashboard_phase4_pages.py::test_chat_page_limits_normal_bot_selectors_to_chat_bots tests/test_dashboard_phase4_pages.py::test_chat_page_embeds_effective_context_gate_inputs tests/test_dashboard_phase4_pages.py::test_chat_page_surfaces_assistant_bot_and_model_provenance tests/test_dashboard_phase4_pages.py::test_chat_page_unscoped_filter_limits_conversation_list tests/test_dashboard_phase4_pages.py::test_chat_page_project_filter_limits_conversation_list tests/test_bot_chat_profiles.py tests/test_chat_api.py::test_create_conversation_and_post_message -q` passed for Batch 9 effective chat context visibility.
- `pytest tests/test_dashboard_phase4_pages.py::test_bot_detail_page_renders_chat_profile_controls tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes tests/test_bot_chat_profiles.py tests/test_bot_tooling_status.py tests/test_bot_readiness.py::test_bot_readiness_list_returns_each_registered_bot -q` passed for Batch 10 bot operating summary.
- `pytest tests/test_dashboard_phase4_pages.py::test_work_page_surfaces_provider_model_usage tests/test_dashboard_phase4_pages.py::test_schedules_page_and_proxy_support_operational_schedule_management -q` passed for Batch 11 provider/model usage visibility.
- `pytest tests/test_dashboard_phase4_pages.py::test_workers_page_surfaces_runtime_tool_evidence tests/test_dashboard_phase4_pages.py::test_worker_probe_view_exposes_attested_runtime_tool_evidence tests/test_dashboard_phase4_pages.py::test_worker_probe_view_marks_unavailable_browser_session_degraded tests/test_dashboard_phase4_pages.py::test_worker_detail_page_loads_when_logged_in -q` passed for Batch 12 worker-list runtime evidence.
