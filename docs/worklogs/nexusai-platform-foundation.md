# NexusAI Platform Foundation Worklog

## Objective

Improve NexusAI one scoped platform foundation at a time so it can become the primary AI workspace for chats, project work, site operations, repo work, and autonomous worker management.

## Current Scope

First item: build a Work Overview surface that makes active work operationally visible by project and manager.

## Completion Criteria For This Item

- Operators can see active, queued, blocked, failed, QC, and completed work grouped by project.
- Each project group is further split by manager, with enough detail to identify the manager bot, queue depth, active work, and recent problem tasks.
- Worker queue depth and active worker load are visible alongside project/manager work.
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

## Validation Plan

- Added unit-level tests for work grouping with realistic task metadata.
- Added dashboard route test proving the page renders project and manager groupings.
- Run focused pytest coverage for new route, task summaries, and dashboard smoke where applicable.
- After deployment, measure route render time and verify no fresh 500 or slow-request logs.

## Risks And Limitations

- Some legacy tasks may not have project or manager metadata. They will be grouped under explicit fallback buckets rather than hidden.
- “Manager” may be inferred until all manager-created tasks consistently stamp `root_pm_bot_id`.
- This item does not implement hard budget enforcement; that is the next foundation item after the work overview is stable.
