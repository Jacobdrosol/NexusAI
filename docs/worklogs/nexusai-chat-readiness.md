# NexusAI Chat Readiness Worklog

## Objective

Make the dashboard chat page safe to begin using as the daily NexusAI workspace while preserving the completed Work Overview foundation.

## Scope

- Keep the Work Overview scope closed and documented as complete.
- Fix project-scoped chat organization so selected projects show only matching primary or bridged conversations.
- Move dashboard navigation to a top-only layout on desktop.
- Collapse navigation into an explicit menu on small screens.
- Verify chat layout, composer visibility, responsive behavior, and console health in a browser.
- Expose bot-scoped chat profiles so chat-only, tutor, vision/math, coding, and automation bots can be distinguished before giving them higher-risk tools.

## Completed Work

- Updated `docs/worklogs/nexusai-platform-foundation.md` so the previous Work Overview item no longer appears active.
- Reworked `dashboard/templates/base.html` to use one top navigation structure with a mobile menu toggle.
- Updated `dashboard/static/style.css` so the navigation is top-only on desktop and collapses under the small-screen breakpoint.
- Updated small-screen chat layout so the active conversation appears before the conversation list and the composer stays reachable on mobile.
- Fixed `dashboard/templates/chat.html` project filtering so client-side row matching honors `data-project-id` and `data-bridge-project-ids`.
- Hardened the chat composer for daily use:
  - prevents duplicate sends while a request is active
  - keeps drafts and attachments intact when a send is rejected
  - clears drafts only after the backend accepts the request
  - shows send and stream failures inline instead of relying on blocking alerts
  - resolves streams that finish without a saved assistant message instead of leaving an indefinite pending bubble
  - avoids forcing the message pane to the bottom while the user is reading older messages
- Added dashboard regression assertions for the mobile nav toggle and chat filter script behavior.
- Added bot-scoped chat profile metadata on the bot list and bot detail page.
- Added editable bot detail controls for chat profile mode, label, notes, attachments, image understanding, and diagrams.
- Persisted `chat_profile` inside bot `routing_rules` alongside the existing bot-level `chat_tool_access` gate.
- Preserved the existing three-switch workspace tool model: bot, project, and conversation must all allow workspace tools before chat can use repo/file access.
- Kept long-running coding automation out of this scope; coding-capable bots are visible and configurable, but high-risk repo work remains case-by-case.
- Fixed bot detail error rendering so missing or degraded history endpoints no longer turn the page into a 500.
- Updated dashboard event-stream counts to use ID-only aggregate queries so stale local task columns do not break every page's live status connection.
- Contained wide bot tables inside scrollable regions on small screens.
- Changed chat context activation so ordinary references to a repository, workspace, files, or planned work do not silently request repo evidence or produce a tool-access refusal. Repository context now requires an explicit workspace-tools toggle, selected context item, or inline-coding mode.
- Reworked assistant message re-run into response regeneration: it reuses the original user turn, excludes the original response from the model payload, persists alternate responses, and allows the active response to be switched from the message actions menu.

## Validation

- `python -m pytest tests/test_dashboard_phase4_pages.py::test_chat_page_loads_when_logged_in tests/test_dashboard_phase4_pages.py::test_chat_page_renders_project_filter_metadata_on_conversations tests/test_dashboard_phase4_pages.py::test_chat_page_project_filter_limits_conversation_list -q`
- `python -m pytest tests/test_chat_manager.py tests/test_chat_api.py -q`
- `python -m pytest tests/test_work_overview.py -q`
- `python -m pytest tests/test_dashboard_phase4_pages.py::test_chat_page_loads_when_logged_in tests/test_dashboard_phase4_pages.py::test_chat_page_renders_project_filter_metadata_on_conversations tests/test_dashboard_phase4_pages.py::test_chat_page_project_filter_limits_conversation_list tests/test_dashboard_phase4_pages.py::test_chat_page_supports_attachment_picker -q`
- `python -m pytest tests/test_dashboard_phase4_pages.py::test_bot_detail_page_loads_when_logged_in tests/test_dashboard_phase4_pages.py::test_bot_detail_page_renders_chat_profile_controls tests/test_dashboard_phase4_pages.py::test_bot_detail_page_still_loads_when_history_endpoints_fail tests/test_dashboard_phase4_pages.py::test_bots_page_identifies_scheduled_and_manual_dispatch_modes tests/test_dashboard_phase4_pages.py::test_bots_page_surfaces_bot_scoped_chat_profiles -q`
- `python -m pytest tests/test_dashboard_phase4_pages.py::test_events_stream_emits_dashboard_counts -q`
- `python -m py_compile dashboard\routes\chat.py`
- `python -m py_compile dashboard\routes\bots.py`
- `git diff --check`
- `python -m pytest tests/test_chat_api.py -q -k "stream_message_regeneration or stream_message_endpoint"`
- `python -m compileall -q control_plane\api\chat.py control_plane\chat\chat_manager.py dashboard\routes\chat.py dashboard\cp_client.py`
- Browser verification against local dashboard and control plane:
  - desktop 1366x820: top nav only, no horizontal overflow
  - project filter `ui-globeiq`: primary and bridged conversations visible, unrelated conversation hidden
  - selected chat: messages area and composer present, composer remains within viewport
  - mobile 390x844: menu collapsed by default, opens on tap, links visible, no horizontal overflow
  - console warnings/errors: none observed

## Risks And Limitations

- The broad `tests/test_dashboard_phase4_pages.py` file is slow as a full-file run and previously exceeded a 3-minute local timeout. Affected tests were run directly.
- Browser validation used local seeded test data, not production data.
- This does not add new site-work automation features; it prepares the chat surface and navigation for daily use.
