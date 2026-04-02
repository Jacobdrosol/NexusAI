# Orchestration

The `orchestration` module manages the lifecycle of workflow assignment runs — creating, tracking, splicing, and relaunching PM orchestration pipelines. It sits between the chat/Platform AI layer and the task manager, providing a persistent store of run history and graph snapshots.

---

## Components

### `assignment_service.py` — `AssignmentService`

The primary interface for creating and managing orchestration runs. Used by the Platform AI tuner, the chat PM flow, and the pipelines dashboard.

| Method | Description |
|--------|-------------|
| `preview(...)` | Dry-run: generate graph and validate connections without executing |
| `create_assignment(...)` | Full execution: launch PM orchestration, store run record, bind orchestration ID |
| `get_graph(run_id)` | Fetch live execution graph from current orchestration state |
| `splice_and_rerun(run_id, from_node_id)` | Create a child run, skip upstream nodes, rerun from a target node |
| `rerun_node(run_id, node_id)` | Retry a specific failed task within the existing orchestration |
| `list_lineage(run_id)` | Return all runs in the same assignment chain (parent + children) |

**Assignment flow:**

```
create_assignment()
  → conversation → pm_orchestrator bot → structured output (graph + workstreams)
  → _validate_project_bindings() (check connection slots)
  → pm_orchestrator.orchestrate_assignment() (launch via task_manager)
  → bind orchestration_id in run_store
  → return run record
```

**Splice flow (for Platform AI tuner and dashboard):**

```
splice_and_rerun(run_id, from_node_id)
  → _upstream_nodes() — topological walk to find nodes that precede from_node_id
  → create_splice_child() in run_store (new run, parent archived)
  → launch with skip_nodes=[...upstream...]
  → return new child run record
```

**Graph extraction:**

`get_graph()` queries the task manager for live task state and merges it with the static graph snapshot stored in the run. If the workflow's `reference_graph` is absent, it falls back to a hardcoded default stage list (known limitation — see below).

---

### `run_store.py` — `OrchestrationRunStore`

SQLite-backed store for orchestration run records.

**Table: `orchestration_runs`**

| Column | Description |
|--------|-------------|
| `id` | UUID (run ID) |
| `assignment_id` | Links runs in the same assignment chain |
| `orchestration_id` | Bound orchestration from task manager (set after launch) |
| `graph_snapshot` | JSON snapshot of the workflow graph at run creation |
| `node_overrides_json` | Per-node configuration overrides (e.g., custom prompts) |
| `lineage_parent_run_id` | Parent run ID for spliced children |
| `spliced_from_node_id` | Node where the splice was initiated |
| `state` | `active`, `archived` |
| `created_at`, `updated_at` | ISO 8601 timestamps |

**Key methods:**

| Method | Description |
|--------|-------------|
| `create_run(...)` | Store new run with graph snapshot and node overrides |
| `get_run(run_id)` | Fetch run by ID |
| `get_run_by_orchestration(orchestration_id)` | Reverse lookup by task manager orchestration ID |
| `get_latest_run_for_assignment(assignment_id)` | Most recent non-archived run in an assignment |
| `list_lineage(run_id)` | Full chain of parent + child runs |
| `bind_orchestration_id(run_id, orchestration_id)` | Set the orchestration ID after launch |
| `archive_run(run_id)` | Mark run as archived (soft delete) |
| `sync_graph_from_tasks(run_id, tasks)` | Merge live task data into stored graph snapshot |
| `create_splice_child(parent_run_id, from_node_id)` | Clone parent run as child, archive parent |

**Status aggregation:**

`status_from_tasks(tasks)` computes a single status from a list of task objects using priority order:

```
failed > cancelled > running > queued > completed > skipped > unknown
```

---

## Relationship to Other Modules

```
chat/pm_orchestrator.py
  └── AssignmentService.create_assignment()
        └── task_manager.create_task()
              └── OrchestrationRunStore.bind_orchestration_id()

platform_ai/runtime.py
  └── AssignmentService.splice_and_rerun()
        └── OrchestrationRunStore.create_splice_child()
  └── AssignmentService.get_graph()
        └── OrchestrationRunStore.sync_graph_from_tasks()

dashboard/routes/pipelines.py
  └── AssignmentService.get_graph()
  └── AssignmentService.rerun_node()
```

---

## Pipeline Detail Actions (Dashboard)

The pipeline detail page exposes these orchestration actions:

| Action | Description |
|--------|-------------|
| **Rerun node** | Retry a specific task/stage in the current orchestration |
| **Splice** | Create a child run starting from a specific node, skipping all upstream |

Both actions resolve the run via `assignment_id` from task payload, falling back to graph lookup if absent.

---

## Known Issues

| # | Severity | Issue |
|---|----------|-------|
| 1 | 🟠 Medium | Graph extraction falls back to hardcoded default stages if `workflow.reference_graph` is missing — silently degrades quality |
| 2 | 🟠 Medium | `create_splice_child` and `archive_run` are not atomic — brief window where both parent and child are active |
| 3 | 🟠 Medium | `status_from_tasks` returns `succeeded` even when some tasks are `skipped` — ambiguous for splice runs |
| 4 | 🟠 Medium | Child runs lose original `conversation_brief`, `transcript`, and `memory_hits` from the parent |
| 5 | 🟡 Low | `splice_and_rerun` sets `execution_mode="preserve_upstream"` in metadata but this flag is never consumed downstream |
| 6 | 🟡 Low | `_validate_project_bindings` accepts int strings without verifying they match the expected connection type |

---

## Refactor Notes

- `create_splice_child` and `archive_run` should be wrapped in a single SQLite transaction.
- Child runs should inherit `conversation_brief` and `transcript` from parent, not lose them.
- `status_from_tasks` should distinguish `partially_skipped` from `succeeded`.
- Graph extraction should validate the extracted node list against the bot workflow definition, not silently fall back.
