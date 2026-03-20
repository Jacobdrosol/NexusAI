# Bot Configuration Guide

NexusAI bots are defined in YAML (or JSON) and imported via the dashboard
**Settings → Bots** UI.  This document describes every field in the schema.

---

## File Format

Bots can be defined as `.yaml` / `.yml` or `.json` files.  Each file describes
**one bot**.  Place your own bot configs anywhere you like — they are never
committed to the repo.  Import them through the UI at any time.

```
config/bots/               ← repo examples (safe to commit)
~/my-bots/pm-coder.yaml    ← your private configs (keep outside repo)
```

---

## Minimal Example

```yaml
id: my-assistant
name: My Assistant Bot
role: assistant
system_prompt: |
  You are a helpful assistant. Answer questions concisely.
backends:
  - type: cloud_api
    model: gpt-4o
    provider: openai
    api_key_ref: OPENAI_API_KEY
```

---

## Full Schema Reference

### Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Unique bot identifier.  Used in triggers, plan steps, and UI references.  Use kebab-case: `pm-coder`, `pm-ui-tester`. |
| `name` | string | ✅ | Human-readable display name shown in the dashboard. |
| `role` | string | ✅ | Bot's functional role.  Used as fallback for heuristic bot selection when `bot_id` is not explicit in a plan step.  Common values: `project_manager`, `researcher`, `engineer`, `coder`, `tester`, `security_reviewer`, `database_engineer`, `ui_tester`, `qa`. |
| `system_prompt` | string | — | The LLM system prompt injected into every task this bot executes. |
| `priority` | int | — | Higher priority bots are selected first when multiple bots match the same role.  Default: `0`. |
| `enabled` | bool | — | Set to `false` to disable this bot without deleting it.  Default: `true`. |
| `backends` | list | ✅ | One or more inference backends (see [Backends](#backends)). |
| `routing_rules` | object | — | Extended routing and tool-access configuration (see [Routing Rules](#routing-rules)). |
| `workflow` | object | — | Workflow trigger configuration (see [Workflow Triggers](#workflow-triggers)). |
| `context_access` | object | — | Declares what context the bot receives and what it can self-serve (see [Context Access](#context-access)). |

---

### Backends

A bot must have at least one backend.  The scheduler picks the first backend
whose worker is online and has capacity.

```yaml
backends:
  - type: cloud_api          # local_llm | remote_llm | cloud_api | cli | custom
    model: gpt-4o
    provider: openai         # openai | claude | gemini | ollama | lmstudio | vllm | cli | custom
    api_key_ref: OPENAI_API_KEY   # name of the secret in Settings → API Keys
    params:
      temperature: 0.2
      max_tokens: 8192
```

**`params` fields** (all optional):

| Field | Description |
|-------|-------------|
| `temperature` | Sampling temperature (0.0–2.0). |
| `max_tokens` | Maximum output tokens. |
| `top_p` | Nucleus sampling threshold. |
| `num_ctx` | Context window size (Ollama / llama.cpp only). |

---

### Routing Rules

`routing_rules` is a freeform object that the dashboard and scheduler interpret.
The most important sub-fields:

```yaml
routing_rules:
  chat_tool_access:
    enabled: true
    filesystem: true      # allow the bot to read/write workspace files
    repo_search: true     # allow semantic search over the project repository
  output_contract:
    enabled: true
    format: json_object
    required_fields:
      - status
      - change_summary
      - files_touched
      - artifacts
      - risks
      - handoff_notes
  input_contract:
    enabled: true
    required_fields:
      - instruction
      - deliverables
  on_failure:
    # Deterministic backward routing (see Workflow Triggers below).
    # Maps failure_type → target_bot_id.
    ui_render_issue: pm-coder
    ui_data_issue: pm-database-engineer
    default: pm-coder
```

---

### Context Access

Tells the orchestrator what context to pass into this bot's task payload and
what data sources the bot may query on its own:

```yaml
context_access:
  receives:
    - chat_message        # the original chat thread
    - instruction         # the assignment instruction
    - previous_step_output  # output from the immediately upstream step
    - requirements        # parsed requirements / research output
  can_self_serve:
    - repo                # query the project repository
    - vault               # query the project data vault
    - web                 # perform web searches
```

These are **advisory** — the scheduler uses them to decide what to include in
the task payload.  They do not hard-block execution.

---

### Workflow Triggers

Triggers define how the pipeline moves after a bot completes or fails a task.
They enable:

- **Linear forward routing** — completed → next bot
- **Dynamic backward routing** — failed → specific earlier bot based on failure type
- **Fan-out** — one task spawns N parallel tasks from an array in the result
- **Fan-in / Join** — wait for all N parallel tasks before continuing

```yaml
workflow:
  triggers:
    - id: on-pass-to-tester
      event: task_completed
      target_bot_id: pm-tester
      condition: has_result
      result_field: outcome
      result_equals: pass

    - id: on-fail-back-to-coder
      event: task_completed
      condition: has_result
      result_field: outcome
      result_equals: fail
      target_bot_id: pm-coder   # explicit: only this bot, no inference

    - id: fanout-to-coders
      event: task_completed
      target_bot_id: pm-coder
      condition: has_result
      fan_out_field: source_result.implementation_workstreams
      fan_out_alias: workstream
      fan_out_index_alias: workstream_index
      payload_template:
        title: "Implement: {{workstream.title}}"
        instruction: "{{workstream.instruction}}"
        deliverables: "{{workstream.deliverables}}"

    - id: join-after-security
      event: task_completed
      target_bot_id: pm-database-engineer
      condition: has_result
      join_group_field: fanout_id
      join_expected_field: fanout_count
      join_result_field: outcome
      join_result_items_alias: security_results
      join_items_alias: security_payloads
```

#### Trigger Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique trigger ID within this bot's config. |
| `event` | `task_completed` \| `task_failed` | When to evaluate this trigger. |
| `target_bot_id` | string | **Exact** bot ID to route to.  Never inferred. |
| `enabled` | bool | Set `false` to disable without deleting. Default: `true`. |
| `condition` | `always` \| `has_result` \| `has_error` | Gate on task result presence. |
| `result_field` | string | Dot-path into `task.result` to inspect (e.g. `outcome`, `failure_type`). |
| `result_equals` | string | Required value at `result_field` for the trigger to fire. |
| `payload_template` | object | Template for the downstream task payload.  Supports `{{alias}}` substitution. |
| `fan_out_field` | string | Dot-path into `task.result` that resolves to an array; spawns one task per item. |
| `fan_out_alias` | string | Variable name for each item in the template (default: `item`). |
| `fan_out_index_alias` | string | Variable name for the array index in the template (default: `index`). |
| `join_group_field` | string | Field used to group sibling tasks for the join (e.g. `fanout_id`). |
| `join_expected_field` | string | Field that tells the join how many siblings to wait for (e.g. `fanout_count`). |
| `join_items_alias` | string | Template alias for the collected sibling payloads. |
| `join_result_field` | string | Field to extract from each sibling's result for the join aggregate. |
| `join_result_items_alias` | string | Template alias for the collected sibling result values. |
| `join_sort_field` | string | Sort sibling payloads by this field before aggregating. |
| `inherit_metadata` | bool | Copy `user_id`, `project_id`, `conversation_id`, etc. from the source task.  Default: `true`. |

---

## Deterministic Backward Routing

**Never use "pass to the previous bot".**  That causes infinite loops when the
previous bot cannot fix the reported failure.

Instead, configure each failure type explicitly in the bot's triggers:

```yaml
# pm-ui-tester.yaml  — example of explicit backward routing
workflow:
  triggers:
    - id: pass-to-final-qc
      event: task_completed
      target_bot_id: pm-final-qc
      condition: has_result
      result_field: outcome
      result_equals: pass

    - id: ui-render-fail-to-coder
      event: task_completed
      condition: has_result
      result_field: outcome
      result_equals: fail
      # Use result_field on the failure_type to further discriminate:
      target_bot_id: pm-coder   # UI/UX rendering issues → coder

    - id: ui-data-fail-to-dbe
      event: task_completed
      condition: has_result
      result_field: failure_type
      result_equals: ui_data_issue
      target_bot_id: pm-database-engineer   # Data issues → DB engineer
```

The rule: **every failure type must map to exactly one explicit `target_bot_id`.**
No catch-all "previous" routing.  No dynamic inference.

---

## Fan-Out and Join Pattern

Fan-out spawns N parallel tasks from an array in the upstream result.
Join waits for all N to complete before continuing downstream.

```
pm-engineer  (emits implementation_workstreams: [{...}, {...}, {...}])
     │
     ├─► pm-coder  (workstream 1)
     ├─► pm-coder  (workstream 2)     ← fan-out
     └─► pm-coder  (workstream 3)
                ↓ (all 3 complete)
          pm-tester  ← join fires once all 3 branches are done
```

Key points:
- All fan-out tasks share the same `fanout_id` in their payload.
- The join trigger reads `fanout_count` from the payload to know how many to wait for.
- The join is **idempotent** — it fires exactly once even if multiple siblings complete simultaneously.

---

## Output Contract

Use `output_contract` to require a structured JSON response from the bot:

```yaml
routing_rules:
  output_contract:
    enabled: true
    format: json_object
    required_fields:
      - status          # "pass" | "fail" | "skip" | "blocked"
      - outcome         # same as status, used by triggers
      - failure_type    # e.g. "ui_render_issue" | "ui_data_issue"
      - findings        # list of issues found
      - evidence        # files/lines cited as evidence
      - handoff_notes   # context for the next bot
```

The platform enforces this contract and re-prompts the bot if the response is
malformed (up to `max_retries`).

---

## Platform Rules

These are enforced by the platform, not the bot config:

1. **Coder bots are the only bots that write artifacts** — all other bots are
   analytical only.  Non-coder bots must not emit file changes.

2. **Prompt takes priority over repo structure** — if the assignment instruction
   says "create Blazor .razor files", the coder creates `.razor` files even if
   the repo is primarily Python.  The platform never errors on this.

3. **Linear forward, dynamic backward** — bots always route forward in the
   configured sequence.  Backward routing is only via explicit trigger config.

4. **No heuristic routing** — if a plan step specifies `bot_id`, that exact bot
   is used.  An unknown `bot_id` raises an error immediately.  Heuristic
   `role_hint` fallback is used only when `bot_id` is absent and logs a warning.

---

## Importing Bot Configs

1. Go to **Dashboard → Bots → Import**.
2. Select your `.yaml` or `.json` file.
3. The bot appears in the bot list immediately.
4. Assign it to a pipeline or use it in a PM workflow.

Bot config files imported via the UI are stored in the database and never
written back to the filesystem — your private configs stay private.

---

## See Also

- [`config/bots/example_bot.yaml`](./example_bot.yaml) — annotated full example
- [`shared/models.py`](../../shared/models.py) — Pydantic models for the full schema
- [`shared/tool_catalog.py`](../../shared/tool_catalog.py) — available tools
