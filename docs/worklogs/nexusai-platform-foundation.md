# NexusAI Platform Foundation Worklog

## Objective

Improve NexusAI one scoped platform foundation at a time so it can become the primary AI workspace for chats, project work, site operations, repo work, and autonomous worker management.

## Current Scope

Current item: surface worker health issues in Work Overview alongside queue depth.

## Completion Criteria For This Item

- Operators can see active, queued, blocked, failed, QC, and completed work grouped by project.
- Work Overview loads active/waiting/problem summaries separately from recent history so active work is not hidden behind a high volume of recently completed tasks.
- Each project group is further split by manager, with enough detail to identify the manager bot, queue depth, active work, and recent problem tasks.
- Operators can drill into one project-manager lane from Work Overview and inspect bounded active/waiting/problem task details without loading payload content.
- Worker queue depth and active worker load are visible alongside project/manager work.
- Work Overview flags enabled-offline, disabled, high-load, and queued workers so operators can identify capacity problems quickly.
- Token usage is visible by project, manager, and provider/model for the current operational window.
- Metered LLM work can be capped by project and manager at admission time.
- Metered LLM dispatch reserves project and manager budgets so queued work cannot burst past configured limits.
- Admins can view and update token-governor limits from a dedicated Settings tab without editing env values.
- The token-governor controls expose live one-hour governor status when the control plane is reachable.
- Admins can stop active/waiting work for one project or one project-manager lane from Work Overview.
- Stop actions cancel only non-terminal running, queued, or blocked tasks and preserve an explicit cancellation reason.
- Stop actions preview the matching and selected task counts before cancellation so destructive work controls are explicit.
- Orchestration stop actions preview loaded run task counts and active/waiting cancellable counts before cancellation.
- Admins can place or release a dispatch hold for one project or one project-manager lane from Work Overview.
- Work Overview page and read APIs are admin-only because they expose operational task, worker, queue, token, and control-plane routing data.
- Dispatch holds prevent queued matching tasks from starting while leaving task rows intact for later release, inspection, or cancellation.
- Control-plane shutdown closes the TaskManager and cancels its watchdog/runner/retry background tasks cleanly.
- Control-plane API tests tear down TaskManager background tasks after each fixture instance.
- Work Overview shows stale active/waiting counts and oldest active/waiting ages by project and manager.
- Recent work rows include age labels so operators can see whether active, waiting, or problem tasks are fresh.
- Work Overview identifies task summaries that are missing project metadata or are grouped under inferred managers instead of explicit manager metadata.
- Work Overview classifies failed/retried work by bounded error label, source, and bot so operators can identify repeated failure modes quickly.
- Work Overview summarizes loaded orchestration IDs with project, manager, active/waiting/problem/stale counts, latest task status, and total task count.
- Operators can inspect bounded task details for one orchestration without invoking cancellation preview or loading task content.
- Lane details show bounded error type, code, and message from non-content task summaries.
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
- Added `/api/work/stop` for admin-only scoped cancellation by project and optional manager.
- Added Work Overview project and manager stop buttons for lanes that have active or waiting work.
- Extended task cancellation API/client calls to pass cancellation reasons through to the task manager.
- Added TaskManager shutdown to the production FastAPI lifespan teardown.
- Added control-plane test fixture cleanup for TaskManager background tasks.
- Added a TaskManager lifecycle test proving `close()` clears a running watchdog task.
- Added Work Overview freshness metrics for stale running tasks, stale queued/blocked tasks, oldest active age, and oldest waiting age.
- Added Work page stale-work card, project stale badges, manager stale counts, manager oldest-age columns, and per-task age labels.
- Added `work_dispatch_holds` runtime setting and control-plane endpoints to list, set, and release project/manager dispatch holds.
- Added task-manager dispatch gating so held project/manager queued tasks remain queued until the hold is released.
- Added Work Overview held-lane visibility plus Hold/Release Project and Hold/Release Lane controls.
- Updated Work Overview stop controls to call the existing dry-run endpoint first and include matched/selected task counts in the confirmation.
- Added `/api/work/lane` to return bounded project/manager lane details from task summaries only.
- Added a Work Overview lane-details panel with task IDs, bot IDs, status, step/source, updated time, error summary, counts, and hold state.
- Split Work Overview loading into active/problem and recent-history task-summary windows, merged by task ID, with snapshot window metadata shown on the page.
- Updated scoped stop and lane-detail APIs to request only relevant task statuses from the control plane.
- Added degraded-data tracking to Work Overview so failed active/recent task, project, bot, worker, hold, or usage loads are surfaced in both HTML and JSON output.
- Added active/recent unavailable snapshot flags so a missing task window is visible next to the loaded row counts.
- Added metadata-health summaries to Work Overview with missing-project counts, inferred-manager counts, missing-manager counts, and bounded sample task rows.
- Added Work page metadata-gap cards and a side-panel sample list for routing follow-up.
- Added bounded `error_summary` fields to task summaries so dashboards can classify failures while keeping payload/result content excluded.
- Added Work Overview problem-source aggregation by error code/type, task source, and bot.
- Added a Work page Problem Sources panel.
- Added Work Overview orchestration rollups from loaded task summaries and a Work page Orchestrations panel.
- Added `/api/work/orchestration/stop` for admin-only orchestration stop previews and control-plane cancellation proxying.
- Added Work page Stop Run controls for orchestration rows that have active or waiting work.
- Updated lane-detail compaction to use bounded `error_summary` fields from task-summary rows, with legacy full-error fallback.
- Updated lane-detail rendering to show error code, type, and bounded message together when available.
- Added `/api/work/orchestration` for read-only, summary-only orchestration task drilldown with status counts and stoppable counts.
- Added Work page View Run controls that load one orchestration into the existing detail panel without using the stop endpoint.
- Restricted `/work`, `/api/work/overview`, `/api/work/lane`, and `/api/work/orchestration` to admin users.
- Tightened the Work Overview test login helper so tests can safely seed learner and admin accounts in any order.
- Added worker health counters for enabled-offline workers, disabled workers, high-load workers, queued workers, and total worker issues.
- Added a Worker Issues card and Worker Load summary text that expose capacity problems on the Work page.

## Validation Plan

- Added unit-level tests for work grouping with realistic task metadata.
- Added dashboard route test proving the page renders project and manager groupings.
- Added task-manager test proving usage grouping by project, manager, and provider/model.
- Added task-manager tests proving project queued-task rejection, manager hourly rejection after recorded usage, and scheduler project-budget reservation.
- Added dashboard tests proving the token-governor Settings tab renders, the API reports settings/live status, valid updates are normalized, and invalid updates are rejected.
- Added Work Overview tests proving stop controls render, dry-run filtering targets only stoppable project-manager tasks, actual cancellation ignores terminal/out-of-scope tasks, and missing project IDs are rejected.
- Added control-plane API test proving single-task cancellation preserves the provided reason.
- Added lifecycle test proving TaskManager watchdog shutdown is explicit and leaves no stored watchdog task.
- Added Work Overview assertions with a fixed clock proving stale active/waiting counts and age labels are deterministic.
- Added task-manager test proving a held project-manager lane does not dispatch until released.
- Added control-plane task API test covering dispatch hold set/list/release.
- Added dashboard tests covering Work Overview hold rendering and dashboard hold/release API proxy behavior.
- Added dashboard rendering assertion that the Work page includes stop dry-run preview behavior.
- Added dashboard API tests covering lane-detail filtering, bounded results, hold state, and validation errors.
- Added dashboard tests proving Work Overview requests active/problem statuses separately, merges duplicate summary rows, and status-filters stop/lane API queries.
- Added dashboard test proving a failed active/problem summary load renders a partial-data warning and is returned by `/api/work/overview`.
- Added Work Overview tests proving clean tasks have no metadata gaps and fallback-routed tasks are counted with source-specific samples.
- Added TaskManager test proving non-content task summaries include bounded error labels without returning full error content.
- Added Work Overview test proving failed/retried work is grouped by error label, source, and bot.
- Added Work Overview test proving orchestration rollups include active, waiting, problem, stale, latest-task, project, and manager details.
- Added Work Overview API tests proving orchestration stop dry-runs count only matching run tasks, actual stop proxies to the control plane, and missing orchestration IDs are rejected.
- Added dashboard rendering assertions that Work Overview exposes Stop Run controls.
- Added lane-detail API assertions proving summary-only error labels are returned without loading full task content.
- Added Work Overview API tests proving orchestration drilldown filters to one run, returns bounded task details, sorts newest first, and rejects missing IDs or invalid limits.
- Added dashboard rendering assertions that Work Overview exposes View Run controls.
- Added dashboard access test proving non-admin users receive 403 for Work Overview and its read APIs.
- Added Work Overview assertions proving worker health issue counts are computed and rendered.
- Run focused pytest coverage for new route, task summaries, and dashboard smoke where applicable.
- After deployment, measure route render time and verify no fresh 500 or slow-request logs.

## Risks And Limitations

- Some legacy tasks may not have project or manager metadata. They will be grouped under explicit fallback buckets rather than hidden.
- “Manager” may be inferred until all manager-created tasks consistently stamp `root_pm_bot_id`.
- Project and manager caps only apply to metered LLM providers. Tool/browser-only workers remain governed by their existing concurrency controls.
- Token caps use measured usage plus configured per-task estimates, so estimates must be tuned per bot for the best balance between throughput and safety.
- Environment variables still override setting values in the task manager. The Settings tab is the normal runtime control, but deployment environment overrides must be checked when a saved value appears ineffective.
- Work Overview stop controls are scoped cancellation, not pause/resume state. Cancelled orchestrations still need orchestration-level cancellation when future fan-out must be blocked.
- Stale active work currently means a running task has not updated in at least 60 minutes. Stale waiting work means a queued or blocked task has been waiting at least 30 minutes.
- Dispatch holds block task start only. They do not cancel running tasks, block task creation, pause schedules, or prevent future orchestration fan-out from adding more queued work.
- Lane drilldown intentionally uses `include_content=false`; it is for operational routing and triage, not full payload/result review.
- Work Overview task counts are based on bounded loaded windows. The page flags when an active/problem or recent-history window reaches its configured cap.
- Degraded-data warnings identify failed control-plane calls, but they do not retry or repair the control plane. They are operator visibility for safe decisions.
- Metadata-gap counts are based only on the bounded loaded task-summary windows, so they are routing indicators rather than a full historical metadata audit.
- Problem-source counts are based only on loaded failed/retried task summaries and use bounded error labels, not full task payloads or results.
- Orchestration rollups are based only on the bounded loaded task-summary windows. They are an operator snapshot, not a complete historical run graph.
- Orchestration stop previews use bounded loaded summaries for operator confirmation; the control-plane cancellation remains authoritative and blocks later fan-out for the orchestration ID.
- Lane-detail error labels are intentionally bounded summaries for routing and triage, not complete failure payloads.
- Orchestration drilldown is read-only and summary-only. It does not replace the full pipeline/detail graph views for artifact review or result inspection.
- Work Overview is an operator/admin surface. Learner or non-admin project users need separate, narrower project status views if they should see any work progress.
- Worker high-load status currently uses a fixed 0.85 load threshold from reported worker metrics. It depends on workers reporting comparable load values.
