# Agent Scheduler

The `agent_scheduler` module provides time-based (cron) scheduling for autonomous agent execution. It allows platform operators to define recurring schedules that automatically trigger orchestration runs on a bot at specified intervals.

> **⚠️ Status: Incomplete**
> The agent scheduler is wired into the platform but several key features (retry policy, distributed dedup, multi-instance safety) are stored in the schema but not yet implemented. See Known Issues below.

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
| `create_schedule(...)` | Create or update a named schedule |
| `get_schedule(name)` | Fetch a schedule by name |
| `list_schedules()` | List all schedules |
| `delete_schedule(name)` | Remove a schedule |
| `tick()` | Find schedules due now and dispatch runs (call this on a regular interval) |
| `list_runs(schedule_name, ...)` | List historical runs for a schedule |

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
| `name` | TEXT PK | Human-readable schedule name |
| `cron_expression` | TEXT | 5-field cron expression |
| `timezone` | TEXT | IANA timezone name (e.g., `America/Chicago`) |
| `prompt` | TEXT | Goal/prompt sent to the bot when schedule fires |
| `target_bot_id` | TEXT | Bot that receives the task |
| `assignment_pm_bot_id` | TEXT | Optional PM bot to root the orchestration in |
| `enabled` | BOOL | Whether the schedule is active |
| `retry_max` | INT | Max retry attempts *(stored, not implemented)* |
| `retry_backoff_seconds` | INT | Backoff between retries *(stored, not implemented)* |
| `metadata_json` | TEXT | Additional key/value metadata |

Table: `agent_schedule_runs`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID |
| `schedule_name` | TEXT FK | Parent schedule |
| `dedupe_key` | TEXT | Prevents duplicate dispatches within the same time window |
| `status` | TEXT | `pending`, `dispatched`, `failed` |
| `orchestration_id` | TEXT | Bound orchestration if dispatched |
| `task_id` | TEXT | Bound task if dispatched |
| `error` | TEXT | Error message if failed |
| `created_at` | TEXT | ISO 8601 |

---

## Usage

```python
engine = AgentScheduleEngine()

# Create a nightly schedule
await engine.create_schedule(
    name="nightly-qc",
    cron_expression="0 2 * * *",
    timezone="America/Chicago",
    prompt="Run final QC on all open projects",
    target_bot_id="pm-final-qc",
    enabled=True,
)

# Call tick() from a background task (e.g., every 60s)
await engine.tick()
```

---

## How Dispatch Works

When `tick()` runs:
1. Load all enabled schedules
2. For each schedule, compute the last expected fire time
3. Check `agent_schedule_runs` for a recent run with a matching `dedupe_key`
4. If no dedup hit → create a run record → dispatch task via `TaskManager`
5. Bind returned `task_id` and `orchestration_id` to the run record

The `dedupe_key` is `{schedule_name}:{cron_window}` where `cron_window` is the ISO timestamp of the computed fire time (minute-level precision).

---

## Known Issues

| # | Severity | Issue |
|---|----------|-------|
| 1 | 🔴 High | No distributed lock — multiple control plane instances will dispatch the same schedule simultaneously |
| 2 | 🔴 High | `retry_max` and `retry_backoff_seconds` are stored but never used — retries not implemented |
| 3 | 🟠 Medium | `tick()` swallows all exceptions silently — failures are not surfaced or alerted |
| 4 | 🟠 Medium | Dedup key uses timestamp + schedule_id — two runs in the same millisecond could collide |
| 5 | 🟠 Medium | Orphaned `agent_schedule_runs` rows if task creation fails after run record is written |
| 6 | 🟡 Low | No API or dashboard UI for schedules yet — schedules must be created programmatically |
| 7 | 🟡 Low | No pruning of old run records — table grows unbounded |

---

## Refactor Notes

- Should use a distributed advisory lock (Redis or SQLite advisory lock) to prevent multi-instance dispatch.
- Retry logic should be implemented in `tick()`: re-check failed runs and attempt re-dispatch up to `retry_max`.
- A REST API and dashboard UI for schedule management has not yet been built.
- Consider storing next_run_at as a computed column and indexing it for efficient tick queries.
