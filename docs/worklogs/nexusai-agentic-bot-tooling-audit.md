# NexusAI Agentic Bot Tooling Audit

## Objective

Verify the current agentic bots have explicit, enforceable tooling policies before adding new bots or chat-driven coding agents. The immediate target is correctness of existing worker-style bots, pipeline bots, automations, and their tool permissions.

## Scope

- Audit current live bot readiness and worker capability state.
- Ensure bot configuration validation rejects action policies that cannot execute safely.
- Keep connection-based HTTP actions distinct from worker tools.
- Do not start new long-running repair coordinators or bulk content repair scripts.

## Completion Criteria

- Existing bot policies cannot authorize browser/documentation actions unless the matching worker tool is required.
- Focused policy, approval, and readiness tests pass.
- Live blocked bots are classified by actionable cause.
- Remaining runtime blockers are documented so they can be fixed without guessing.

## Current State

Initial live snapshot from `globalagent` showed 130 bots:

- 109 agentic or worker-style bots.
- 62 enabled agentic bots were ready.
- 26 enabled agentic bots were blocked.
- 21 agentic bots were disabled.
- Main blocker pattern: registered worker IDs exist, but their workers are offline.

After restarting the existing rendered worker fleets on `globalagent`, the live
snapshot shows:

- 105 ready bots.
- 2 enabled blocked bots.
- 23 disabled bots.
- 44 enabled workers online.
- 0 enabled workers offline.

The only remaining enabled blockers are the browser-inspector bot and the
course-evidence bot. Both depend on
the browser-inspector worker, which is running and healthy but does not expose
`browser-ui` because its authenticated browser session check fails.

Required worker tool use in live configs:

- `browser-ui`: 6 bots.
- `documentation-v1`: 1 bot.
- `claude`: 1 bot.

Action allowlist use in live configs:

- Browser action allowlists: 4 bots.
- Documentation action allowlists: 1 bot.
- Connection action allowlists: 11 bots.

## Decisions

- Browser action allowlists must require `browser-ui` through `execution_policy.required_worker_tools`.
- Documentation action allowlists must require `documentation-v1` through `execution_policy.required_worker_tools`.
- Connection action allowlists do not require worker tools because they execute through attached HTTP connections and are governed by connection action approval policy.
- Disabled legacy course-writer configs without execution policy remain quarantined until they are reworked or intentionally imported.

## Completed Work

- Tightened `validate_bot_configuration` to reject browser/documentation action allowlists that omit their required worker tools.
- Added regression tests for browser and documentation tool-policy mismatches.
- Confirmed connection action policies remain valid without worker tools.
- Started the rendered worker fleets on the deployment host:
  - Original runtime workers.
  - Question-bank draft/review workers.
  - Course details author/QC workers.
  - Course unit planning/application workers.
  - Course lesson planning/application/block/paragraph/subheader workers.
  - Browser inspector, question-bank patch, and coding sandbox tool workers.
  - Collegiate catalog QC/manager NexusAI workers.
- Fixed private runtime compose configuration so browser/collegiate workers load their mounted worker YAMLs and collegiate workers register with the control plane.
- Ran focused tests:
  - `pytest tests/test_browser_action_approvals.py tests/test_connection_action_approvals.py tests/test_bot_tool_policy.py`
  - `pytest tests/test_bot_readiness.py -q`

## Next Steps

- Add valid browser login credentials to the private browser worker environments, then run the manual browser session bootstrap for browser inspector and question-bank patch profiles.
- Re-run live readiness after browser profiles authenticate; expected result is 107 enabled ready bots and 23 disabled bots.
- Then audit each current bot group for whether its allowed tools match its role and workflow scope before adding new bot classes.

## Risks

- A bot can have correct policy but remain unusable if its browser profile is not authenticated.
- Private bot config imports can still be rejected after this validator change if they authorize browser or documentation actions without the required worker tool.
- Legacy disabled configs need deliberate remediation before activation.
