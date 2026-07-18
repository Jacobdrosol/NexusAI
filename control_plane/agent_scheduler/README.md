# Agent Scheduler

The `agent_scheduler` module provides time-based (cron) scheduling for autonomous agent execution. It allows platform operators to define recurring schedules that automatically trigger orchestration runs on a bot at specified intervals.

> **Status: Operational for a single control-plane instance**
> The scheduler persists definitions and run history, dispatches through the normal task manager, and is managed through the API and dashboard. Retry policy, distributed coordination, and retention still need hardening before multi-instance or high-volume use.

---

## Purpose

- Define named schedules with cron expressions and target bots
- Automatically dispatch orchestration runs when a schedule fires
- Prevent duplicate dispatches using a per-window dedup key
- Track run history per schedule

---

## Files

### `engine.py` (557 lines)

**Class: `AgentScheduleEngine`**

| Method | Description |
|--------|-------------|
| `create_schedule(...)` | Create a schedule with `active` or `paused` status |
| `get_schedule(id)` | Fetch one schedule by ID |
| `list_schedules(...)` | List schedules, optionally filtered by status or target bot |
| `update_schedule(id, patch)` | Update a schedule and recalculate its next run |
| `trigger_schedule(id)` | Manually dispatch one run without enabling recurring dispatch |
| `tick_once()` | Reconcile linked task results and dispatch schedules that are due |
| `list_runs(schedule_id, ...)` | List historical runs for a schedule |

**Cron parser:**

Full 5-field cron support: `minute hour day month weekday`

- Supports `*`, `/step`, `-range`, `,list` in all fields
- Weekday: 0 = Sunday, 7 = Sunday (both accepted)
- Timezone-aware: schedules fire at the correct local time regardless of server timezone
- Next-run calculation uses `ZoneInfo` (Python 3.9+)

---

## Database Schema

Table: `agent_schedules`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Schedule UUID |
| `name` | TEXT | Human-readable schedule name |
| `cron_expression` | TEXT | 5-field cron expression |
| `timezone` | TEXT | IANA timezone name (e.g., `America/Chicago`) |
| `prompt` | TEXT | Goal/prompt sent to the bot when schedule fires |
| `target_bot_id` | TEXT | Bot that receives the task |
| `assignment_pm_bot_id` | TEXT | Optional PM bot to root the orchestration in |
| `status` | TEXT | `active` or `paused` |
| `retry_max` | INT | Max retry attempts *(stored, not implemented)* |
| `retry_backoff_seconds` | INT | Backoff between retries *(stored, not implemented)* |
| `metadata_json` | TEXT | Additional key/value metadata |

Table: `agent_schedule_runs`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID |
| `schedule_id` | TEXT FK | Parent schedule |
| `dedupe_key` | TEXT | Prevents duplicate dispatches within the same time window |
| `status` | TEXT | `queued`, `running`, `completed`, or `failed` |
| `orchestration_id` | TEXT | Bound orchestration if dispatched |
| `task_id` | TEXT | Bound task if dispatched |
| `error` | TEXT | Error message if failed |
| `created_at` | TEXT | ISO 8601 |

---

## Usage

```python
schedule = await engine.create_schedule({
    "name": "nightly-qc",
    "status": "paused",
    "cron_expression": "0 2 * * *",
    "timezone": "America/Chicago",
    "prompt": "Run final QC on all open projects",
    "target_bot_id": "pm-final-qc",
})

# Manual testing does not require recurring dispatch to be active.
await engine.trigger_schedule(schedule["id"])
```

---

## How Dispatch Works

When `tick_once()` runs:
1. Load all enabled schedules
2. For each schedule, compute the last expected fire time
3. Check `agent_schedule_runs` for a recent run with a matching `dedupe_key`
4. If no dedup hit → create a run record → dispatch task via `TaskManager`
5. Bind returned `task_id` and `orchestration_id` to the run record

The `dedupe_key` is `{schedule_id}:{cron_window}` where `cron_window` is the ISO timestamp of the computed fire time (minute-level precision).

---

## Known Issues

| # | Severity | Issue |
|---|----------|-------|
| 1 | High | No distributed lock; multiple control plane instances can dispatch the same schedule simultaneously |
| 2 | High | `retry_max` and `retry_backoff_seconds` are stored but never used; retries are not implemented |
| 3 | Medium | Scheduled runs use a schedule/timestamp key; cross-process coordination still needs a distributed lock |
| 4 | Low | No pruning of old run records; the table grows unbounded |

---

## Refactor Notes

- Should use a distributed advisory lock (Redis or SQLite advisory lock) to prevent multi-instance dispatch.
- Retry logic should re-check failed runs and attempt re-dispatch up to `retry_max`.
- The REST API supports create, list, get, update, manual trigger, and run history. The dashboard supports creation, pause/resume, manual trigger, and run-history inspection.
- Consider storing next_run_at as a computed column and indexing it for efficient tick queries.
