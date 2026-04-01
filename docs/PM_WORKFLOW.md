# PM Workflow — Full Orchestration Reference

## Overview

The PM (Project Manager) workflow is a fixed-topology multi-bot DAG that decomposes a user `@assign` instruction into research → engineering → implementation → test → security → database → UI → QC stages.

The PM Orchestrator (`control_plane/chat/pm_orchestrator.py`) creates a single root task for the selected PM bot. That bot's `BotWorkflowTrigger` rules fire subsequent tasks automatically as each stage completes.

---

## Stage DAG

```
@assign instruction
        │
        ▼
┌───────────────────┐
│  pm-orchestrator  │  (root task, planning stage)
│  step_kind:       │  Decomposes instruction into a structured plan JSON.
│  planning         │  Output: {global_acceptance_criteria, steps:[...]}
└─────────┬─────────┘
          │ task_completed trigger (fan-out × 3)
          │
    ┌─────┴───────────────────────┐
    │             │               │
    ▼             ▼               ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│ pm-res.  │ │ pm-res.  │ │ pm-res.  │
│ analyst  │ │ analyst  │ │ analyst  │
│ step_1   │ │ step_1   │ │ step_1   │
│ _code    │ │ _data    │ │ _online  │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     │             │               │
     └─────────────┴───────────────┘
                   │ join (all 3 must complete)
                   ▼
          ┌────────────────┐
          │  pm-engineer   │  Synthesizes research → concrete plan.
          │  step_2        │  Output: {implementation_workstreams:[...]}
          └────────┬───────┘
                   │ fan-out (1 per workstream, or 1 if small)
          ┌────────┴──────────────────┐
          │                           │
          ▼                           ▼
   ┌────────────┐              ┌────────────┐
   │ pm-coder   │     ...      │ pm-coder   │
   │ step_3_1   │              │ step_3_N   │
   └─────┬──────┘              └─────┬──────┘
         │                           │
         ▼                           ▼
   ┌────────────┐              ┌────────────┐
   │ pm-tester  │     ...      │ pm-tester  │
   │ step_4_1   │              │ step_4_N   │
   └─────┬──────┘              └─────┬──────┘
         │                           │
         ▼                           ▼
   ┌──────────────────┐        ┌──────────────────┐
   │ pm-security-     │  ...   │ pm-security-     │
   │ reviewer step_5_1│        │ reviewer step_5_N│
   └─────┬────────────┘        └─────┬────────────┘
         │                           │
         └─────────────┬─────────────┘
                       │ join (all security steps must complete)
                       ▼
              ┌──────────────────────┐
              │  pm-database-engineer│  Single DB migration step.
              │  step_6              │  Forbidden: DELETE/DROP/TRUNCATE.
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  pm-ui-tester        │  UI/build validation.
              │  step_7              │  May run build_only if no browser.
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  pm-final-qc         │  Terminal delivery gate.
              │  step_8              │  Never used as a retry step.
              └──────────────────────┘
```

---

## Backward Routing (Retry Paths)

Backward routes are defined as `BotWorkflowTrigger` entries with `route_kind: backward` and `event: task_failed`:

| Failed Stage | Retries To | Condition |
|---|---|---|
| `pm-tester` | `pm-coder` | Test failures on a branch |
| `pm-security-reviewer` | `pm-coder` | Security findings on a branch |
| `pm-ui-tester` | `pm-database-engineer` or `pm-engineer` | UI failures requiring schema or arch rework |
| `pm-final-qc` | `pm-engineer` | Systemic issues requiring re-planning |

Retry depth is tracked in `TaskMetadata.trigger_depth`. The task manager increments `max_tokens` and `num_ctx` on each retry attempt.

---

## Bot Roles and Output Contracts

### pm-orchestrator
- **Role**: Receives the `@assign` instruction, extracts scope lock, generates structured plan JSON.
- **System prompt**: Enforces scope lock, stage topology, forbidden actions.
- **Required output fields**: `steps` (list), `global_acceptance_criteria`, `global_quality_gates`, `risks`.
- **`step.bot_id`** is required for every step. Missing `bot_id` is a known failure mode.

### pm-research-analyst (×3 parallel)
- **step_1_code**: Repo/codebase research — reads files, understands architecture.
- **step_1_data**: Requirements, vault context, data schemas.
- **step_1_online**: External references (only when needed; may return skip if no internet access).
- **Role hint**: `researcher`.
- **Step kind**: `specification`.

### pm-engineer
- **Role**: Synthesizes the 3 research branches into a concrete implementation plan.
- **Must produce**: `implementation_workstreams` array — each workstream becomes one `pm-coder` task.
- **Role hint**: `engineer`.
- **Step kind**: `planning`.

### pm-coder (1..N)
- **Role**: Implements one workstream. Produces repo-change artifacts.
- **Output contract**: JSON with `status`, `change_summary`, `files_touched`, `artifacts`, `risks`, `handoff_notes`.
- **Step kind**: `repo_change`.
- **Fan-out**: One task per workstream from `pm-engineer.implementation_workstreams`.

### pm-tester (1..N, paired to coder)
- **Role**: Runs tests against the coder's output. Reports pass/fail/findings.
- **Step kind**: `test_execution`.
- **Backward trigger**: Fires `pm-coder` retry on failure.

### pm-security-reviewer (1..N, paired to tester)
- **Role**: Security audit of the coder's output (not the test output).
- **Step kind**: `review`.
- **Backward trigger**: Fires `pm-coder` retry on critical findings.

### pm-database-engineer
- **Role**: Applies/validates database schema changes. Single step joining all security branches.
- **Contract**: Must return exactly one canonical SQL migration artifact if outcome is pass.
- **Forbidden SQL**: `DELETE`, `DROP`, `TRUNCATE`, destructive `ALTER TABLE` forms.
- **Skip condition**: Returns `outcome: skip` / `not_applicable` if no DB changes exist.
- **Step kind**: `review`.

### pm-ui-tester
- **Role**: UI/build validation. Runs install/build/startup checks.
- **`ui_test_mode`**: If `build_only`, runs build validation without interactive browser automation.
- **Never omitted**: Even when UI testing is skipped, the stage remains in the DAG and returns a `build_only` or `skip` outcome.
- **Step kind**: `review`.

### pm-final-qc
- **Role**: Terminal delivery gate. Verifies all upstream evidence before declaring completion.
- **Backward trigger**: Fires `pm-engineer` on systemic failures.
- **Never used as a branch retry step** — only as the final gate.
- **Step kind**: `review`.

---

## Fan-out / Join Mechanics

Fan-out is triggered by `BotWorkflowTrigger` with `fan_out_field` set. For example, the `pm-engineer` completion trigger fans out over `implementation_workstreams`:

```yaml
fan_out_field: result.implementation_workstreams
fan_out_alias: workstream
fan_out_index_alias: workstream_index
```

Each item in the array spawns one `pm-coder` task, with the workstream injected as `{{workstream}}` in `payload_template`.

Join mechanics use `join_group_field` + `join_expected_field` so that `pm-database-engineer` waits for all `pm-security-reviewer` tasks:

```yaml
join_group_field: metadata.orchestration_id
join_expected_field: result.security_branch_count   # or similar
join_items_alias: branch_results
```

---

## Scope Lock

The scope lock is extracted from the instruction by `_extract_scope_lock()` using keyword heuristics:

```python
scope_lock = {
    "domains": [],          # e.g. ["math", "geometry"]
    "allowed_artifacts": [], # e.g. ["*.md"]
    "forbidden_keywords": [], # e.g. [".py", "test"]
    "raw_instruction": "..."
}
```

The scope lock is embedded in `assignment_scope.scope_lock` and prepended to every downstream bot's system prompt via `_assignment_scope_prompt_suffix()`. All bots are instructed to reject steps that violate the scope lock.

---

## Docs-Only Mode

Triggered when the instruction contains both a docs signal (`documentation`, `markdown`, `.md`) and a docs-only signal (`only .md`, `no code`, `docs only`, etc.).

When `docs_only=True`:
- `forbidden_change_domains: ["code", "tests", "database", "ui"]`
- `requested_output_extensions: [".md"]`
- `pm-database-engineer` and `pm-ui-tester` are excluded (unless instruction mentions them).
- Coder branches must output markdown files inside `artifacts[path, content]`.
- Tester/reviewer stages treat upstream artifacts as evidence; they do not require live repo changes.

---

## Stage Exclusions

Stages can be excluded via `explicit_stage_exclusions` in the assignment scope:

| Condition | Excluded Stage | Reason Code |
|---|---|---|
| `docs_only` (no DB mention) | `pm-database-engineer` | `docs_only_no_database_scope` |
| `docs_only` (no UI mention) | `pm-ui-tester` | `docs_only_no_ui_scope` |
| Instruction explicitly excludes DB | `pm-database-engineer` | `assignment_excludes_database_stage` |
| Instruction explicitly excludes UI (not build_only) | `pm-ui-tester` | `assignment_excludes_ui_stage` |

Excluded stages still receive tasks if they appear in the workflow graph; they are expected to return `outcome: skip` / `not_applicable`.

---

## `ui_test_mode=build_only`

When the instruction requests skipping interactive UI testing (but not full exclusion), `ui_test_mode` is set to `build_only`. This:
1. Keeps `pm-ui-tester` in the DAG.
2. Instructs the bot to run install/build/startup validation only (no Playwright/browser automation).
3. Final QC treats `build_only` validation as the intended mode, not a missing stage.

---

## Known Bugs / Gaps

1. **`_extract_scope_lock` hardcoded phrase catalog**: Only detects `math`, `geometry`, `programming` domains. Arbitrary instructions silently fall through to a generic `"general"` domain. The scope lock provides minimal constraint enforcement in practice.

2. **`_extract_focus_topics` hardcoded catalog**: Lists math/geometry topics only. General-purpose instructions get no focus topics extracted.

3. **Fan-out count is not validated**: If `pm-engineer` returns 0 workstreams, no coder tasks are spawned. The workflow silently stalls at the join gate waiting for results that will never arrive.

4. **PM bot must have an explicit workflow**: `orchestrate_assignment()` raises `BotNotFoundError` if the selected bot has no workflow triggers. The error message says "missing explicit workflow configuration" which may confuse operators.

5. **`wait_for_completion()` settle window**: The poller uses a 1.6-second settle window to detect stability. Long-running tasks (>15 min) that happen to not change state within the poll window will be reported as complete prematurely.

6. **No orchestration-level cancellation**: There is no API to cancel an entire orchestration (all tasks for an `orchestration_id`). Individual tasks can be cancelled but not the whole graph.

7. **`step_kind` inference is heuristic**: `_infer_assignment_step_kind()` uses keyword matching on title/instruction/role_hint. A bot can produce the wrong `step_kind` classification if the PM generates non-standard titles.
