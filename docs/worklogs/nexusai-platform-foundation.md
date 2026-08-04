# NexusAI Platform Foundation Worklog

## Objective

Improve NexusAI one scoped platform foundation at a time so it can become the primary AI workspace for chats, project work, site operations, repo work, and autonomous worker management.

## Current Scope

Current item: show bot-level token usage in Work Overview.

## Completion Criteria For This Item

- Operators can see active, queued, blocked, failed, QC, and completed work grouped by project.
- Work Overview loads active/waiting/problem summaries separately from recent history so active work is not hidden behind a high volume of recently completed tasks.
- Each project group is further split by manager, with enough detail to identify the manager bot, queue depth, active work, and recent problem tasks.
- Operators can drill into one project-manager lane from Work Overview and inspect bounded active/waiting/problem task details without loading payload content.
- Worker queue depth and active worker load are visible alongside project/manager work.
- Work Overview flags enabled-offline, disabled, high-load, and queued workers so operators can identify capacity problems quickly.
- Token usage is visible by project, manager, and provider/model for the current operational window.
- Work Overview flags tasks missing token usage records so operators can distinguish low usage from missing telemetry.
- Work Overview shows token usage by bot so runaway usage can be traced to the worker identity causing it.
- Work Overview provides a single attention rollup across problem tasks, stale work, metadata gaps, worker issues, and usage gaps.
- Work Overview lists the project-manager lanes that need attention first, with bounded reason labels and active/waiting counts.
- Attention Lanes expose direct bounded Details, Hold/Release, and Stop controls for the affected project-manager lane.
- Work Overview shows prioritized queue-pressure lanes so operators can identify project-manager queues building active, waiting, blocked, problem, stale, or held pressure.
- Work Overview shows aggregate capacity pressure from active work, waiting work, online workers, and worker queue depth.
- Work Overview shows snapshot health so operators know when task counts come from complete, capped, or partially unavailable task-summary windows.
- Metered LLM work can be capped by project and manager at admission time.
- Metered LLM dispatch reserves project and manager budgets so queued work cannot burst past configured limits.
- Admins can view and update token-governor limits from a dedicated Settings tab without editing env values.
- The token-governor controls expose live one-hour governor status when the control plane is reachable.
- Admins can stop active/waiting work for one project or one project-manager lane from Work Overview.
- Stop actions cancel only non-terminal running, queued, or blocked tasks and preserve an explicit cancellation reason.
- Stop actions preview the matching and selected task counts before cancellation so destructive work controls are explicit.
- Orchestration stop actions preview loaded run task counts and active/waiting cancellable counts before cancellation.
- Admins can place or release a dispatch hold for one project or one project-manager lane from Work Overview.
- Dispatch holds show affected queued task counts, bot counts, creator, and creation time where the control plane provides those fields.
- Project-manager lanes show bounded worker route evidence from task execution provenance and count active/problem rows with unknown worker attribution.
- Work Overview distinguishes active/problem tasks missing worker attribution from waiting tasks that may not have a worker yet.
- Work Overview page and read APIs are admin-only because they expose operational task, worker, queue, token, and control-plane routing data.
- Dispatch holds prevent queued matching tasks from starting while leaving task rows intact for later release, inspection, or cancellation.
- Control-plane shutdown closes the TaskManager and cancels its watchdog/runner/retry background tasks cleanly.
- Control-plane API tests tear down TaskManager background tasks after each fixture instance.
- Work Overview shows stale active/waiting counts and oldest active/waiting ages by project and manager.
- Recent work rows include age labels so operators can see whether active, waiting, or problem tasks are fresh.
- Work Overview identifies task summaries that are missing project metadata or are grouped under inferred managers instead of explicit manager metadata.
- Work Overview classifies failed/retried work by bounded error label, source, and bot so operators can identify repeated failure modes quickly.
- Recent problem rows show bounded failure labels and task source or step metadata without loading full task content.
- Work Overview summarizes loaded orchestration IDs with project, manager, active/waiting/problem/stale counts, latest task status, and total task count.
- Operators can inspect bounded task details for one orchestration without invoking cancellation preview or loading task content.
- Lane details show bounded error type, code, and message from non-content task summaries.
- Lane and orchestration drilldowns show bounded worker/backend route evidence from task execution provenance without loading payload or result content.
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
- Added a stable empty usage summary shape when token usage is unavailable.
- Added a Usage Gaps card and usage coverage text for measured tasks, missing-usage tasks, and total tokens.
- Added an attention summary to the Work Overview API after task, worker, metadata, and usage data are loaded.
- Added a Needs Attention card and Attention Breakdown panel on the Work page.
- Added step/source metadata to latest manager task summaries.
- Added bounded problem labels to recent problem task summaries and rendered them in the Recent Problems panel.
- Added Dispatch Holds side-panel rows with project/manager scope, reason, queued task count, bot count, creator, and creation time.
- Added hold impact details to held project badges and manager hold cells.
- Added per-lane worker route evidence with attributed task counts, unknown-worker counts, top worker IDs, and latest task worker labels when task metadata includes execution provenance.
- Added top-level Route Gaps and Route Coverage signals so operators can distinguish missing execution provenance on active/problem work from queued or blocked work that has not started.
- Added worker/backend/provider/model fields to lane and orchestration drilldown task rows when execution provenance is present.
- Added a prioritized Attention Lanes panel from loaded project-manager summaries, including problem, stale, route-gap, and held reasons.
- Added scoped action controls to Attention Lanes so operators can review details, hold/release eligible lanes, or stop active/waiting work without hunting through the full project table.
- Attention Lanes distinguish manager-lane holds from inherited project holds so a project-level hold is not incorrectly released as a manager-lane hold.
- Added a Queue Pressure panel derived from the loaded task snapshot with per-lane active, waiting, queued, blocked, problem, stale-waiting, and hold signals.
- Queue Pressure rows reuse lane review, hold/release, and stop actions while preserving project-hold scope.
- Added a Capacity Pressure card and Capacity Snapshot panel showing active/waiting work, online workers, worker queue depth, total pressure, and per-online-worker ratios.
- Capacity pressure flags work with no online workers as critical instead of making operators infer that from separate worker and queue tables.
- Added snapshot health to Work Overview API and page output, including ready, warning, and critical levels for loaded, capped, or unavailable task-summary windows.
- Snapshot Health shows active/problem row counts, recent row counts, merged rows, capped windows, and unavailable windows.
- Added bot-level token usage to the Work page using the control-plane `by_bot` summary.
- Added `by_bot` to the Work Overview stable usage fallback shape so unavailable usage data has a consistent contract.

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
- Added Work Overview assertions proving token usage gaps are rendered and missing usage data has a stable API fallback shape.
- Added Work Overview assertions proving the attention rollup totals and severity level are calculated and rendered.
- Added Work Overview assertions proving recent problem rows expose bounded failure labels and sources.
- Added Work Overview assertions proving hold impact counts, creator, and creation time are preserved and rendered.
- Added Work Overview assertions proving worker route evidence is preserved, rendered, and missing worker attribution is shown explicitly.
- Added Work Overview assertions proving route-gap attention counts only active/problem worker-attribution gaps while waiting unknown rows remain visible separately.
- Added Work Overview API assertions proving lane and orchestration drilldowns expose execution-provenance worker/backend fields without changing their bounded summary behavior.
- Added Work Overview assertions proving attention lanes identify the affected project-manager lane and bounded reason labels.
- Added Work Overview assertions proving Attention Lanes render direct review/stop actions and preserve project-hold scope.
- Added Work Overview assertions proving queue-pressure lanes compute queue state and render direct review/stop actions.
- Added Work Overview assertions proving capacity pressure ratios are computed and work-without-online-workers is classified as critical.
- Added Work Overview route assertions proving healthy snapshots render as ready, unavailable active/problem windows render as critical, and capped windows render as warning.
- Added TaskManager and Work Overview assertions proving bot-level usage totals are emitted, rendered, and stable when usage is unavailable.
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
- Usage-gap counts depend on the control plane returning `tasks_without_usage`. A zero count means either no gap was reported or the usage endpoint was unavailable and the stable fallback was used with a degraded-data warning if the call failed.
- The attention rollup is an operator triage signal. Counts can overlap because one underlying task may contribute to multiple signals, such as stale work and missing metadata.
- Recent problem labels use the same bounded error-summary classifier as the aggregate Problem Sources panel. They are triage labels, not full diagnostic payloads.
- Dispatch-hold impact counts are control-plane reported values. They are visibility for operator decisions and do not independently enforce hold behavior.
- Worker route evidence depends on `metadata.execution_provenance`. Running or queued tasks may not have worker attribution yet, so the page reports unknown worker counts instead of guessing.
- Route Gaps count only missing worker attribution on active/problem rows. Waiting unknown counts are shown for visibility but are not treated as an attention issue because queued work may not have an assigned worker yet.
- Attention Lanes are based on the bounded loaded Work Overview task windows. They prioritize operator triage for the visible snapshot and are not a full historical audit.
- Attention Lane controls reuse the existing scoped Work Overview lane APIs. Stop previews and control-plane cancellation remain authoritative for destructive actions.
- Queue Pressure is calculated from the bounded loaded Work Overview task windows. It is an operator pressure snapshot, not a full historical backlog inventory.
- Capacity pressure is a coarse operational signal from bounded task and worker snapshots. It does not replace provider-level concurrency, host resource, or token-governor controls.
- Snapshot health only describes the bounded dashboard task-summary windows. The control plane remains authoritative for cancellation, dispatch, and complete historical state.
- Bot-level usage is based on measured usage in completed task results. Tasks without usage are counted as gaps rather than estimated spend.
