# NexusAI Platform Foundation Worklog

## Objective

Improve NexusAI one scoped platform foundation at a time so it can become the primary AI workspace for chats, project work, site operations, repo work, and autonomous worker management.

## Current Scope

Current item: add operator-facing token-governor controls so project/manager safety limits can be tuned from the platform.

## Completion Criteria For This Item

- Operators can see active, queued, blocked, failed, QC, and completed work grouped by project.
- Each project group is further split by manager, with enough detail to identify the manager bot, queue depth, active work, and recent problem tasks.
- Worker queue depth and active worker load are visible alongside project/manager work.
- Token usage is visible by project, manager, and provider/model for the current operational window.
- Metered LLM work can be capped by project and manager at admission time.
- Metered LLM dispatch reserves project and manager budgets so queued work cannot burst past configured limits.
- Admins can view and update token-governor limits from a dedicated Settings tab without editing env values.
- The token-governor controls expose live one-hour governor status when the control plane is reachable.
- Page render uses bounded control-plane calls and does not reintroduce slow navigation.
- Focused tests cover grouping behavior and dashboard rendering.
- Documentation describes the purpose, data flow, and limitations.

## Decisions

- Work Overview is distinct from the existing generic Overview page. It is focused on command/control of active work, not broad setup status.
- Tasks are grouped using task metadata first: `project_id`, then manager-like metadata such as `root_pm_bot_id`, `pipeline_entry_bot_id`, or `parent_task_id`. If task metadata does not identify a manager, the task bot is used as the manager bucket.
- Queue depth is derived from task summaries and worker metrics where available. It is operational evidence, not a billing or quality score.

## Current State

- Existing `/tasks` page already has token usage and task tables.
- Existing task summary API is fast after commit `550266a`.
- Added a dedicated `/work` dashboard surface and `/api/work/overview` JSON endpoint.
- Added `dashboard.work_overview.build_work_overview` to group task summaries by project and manager.
- Added sidebar navigation entry under Operations.
- Added focused tests for grouping behavior and page rendering.
- Extended control-plane token usage summaries with `by_project`, `by_manager`, and `by_provider_model`.
- Added Work page usage panels for project/manager usage and provider/model usage.
- Added token governor settings for project/manager hourly token caps and queued metered-task caps.
- Extended token governor status to expose project/manager limits.
- Added project and manager budget checks during task admission.
- Added scheduler-side project and manager token reservation so eligible queued tasks are selected within configured limits.
- Added a dedicated Settings tab for token-governor controls.
- Added `/api/settings/token-governor` GET/PUT endpoints that whitelist governor keys, normalize valid values, reject invalid values, and include live status when available.

## Validation Plan

- Added unit-level tests for work grouping with realistic task metadata.
- Added dashboard route test proving the page renders project and manager groupings.
- Added task-manager test proving usage grouping by project, manager, and provider/model.
- Added task-manager tests proving project queued-task rejection, manager hourly rejection after recorded usage, and scheduler project-budget reservation.
- Added dashboard tests proving the token-governor Settings tab renders, the API reports settings/live status, valid updates are normalized, and invalid updates are rejected.
- Run focused pytest coverage for new route, task summaries, and dashboard smoke where applicable.
- After deployment, measure route render time and verify no fresh 500 or slow-request logs.

## Risks And Limitations

- Some legacy tasks may not have project or manager metadata. They will be grouped under explicit fallback buckets rather than hidden.
- “Manager” may be inferred until all manager-created tasks consistently stamp `root_pm_bot_id`.
- Project and manager caps only apply to metered LLM providers. Tool/browser-only workers remain governed by their existing concurrency controls.
- Token caps use measured usage plus configured per-task estimates, so estimates must be tuned per bot for the best balance between throughput and safety.
- Environment variables still override setting values in the task manager. The Settings tab is the normal runtime control, but deployment environment overrides must be checked when a saved value appears ineffective.
