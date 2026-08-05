# NexusAI One-Stop Workspace Hardening

## Objective

Make NexusAI usable as the primary workspace for chat, project context, worker operations, tooling, quality control, and eventually scoped coding/research workflows.

## Scope

- Improve operator visibility into bots, workers, tools, readiness, and active work.
- Keep current agentic workers safe and scoped before adding higher-risk coding workers.
- Improve chat/project ergonomics so the platform is usable day to day.
- Commit stable implementation batches only; temporary scratch lists stay uncommitted.

## Completion Criteria

- Chat can be used reliably for normal assistant-style work.
- Bot and worker readiness is visible without SSH.
- Blocked tools show actionable causes.
- Project and manager work lanes show task pressure, usage, and holds.
- New tooling changes have focused tests.
- Remaining external blockers are documented separately from NexusAI code defects.

## Current State

- Public NexusAI is deployed at `3808ea1`.
- Live readiness before this hardening pass: 105 ready, 2 enabled blocked, 23 disabled.
- The only enabled blockers are GlobeIQ browser-session attestation failures for the browser inspector lane.
- Work overview already tracks project/manager lanes, token usage, queue pressure, holds, route evidence, and task freshness.

## Batch Plan

- Batch 1: Bot tooling visibility and readiness triage.
- Batch 2: Chat usability and live-message verification fixes.
- Batch 3: Bot profile/tooling config checks for chat/research/tutor assistants.
- Batch 4: Safer operator controls for scoped worker activation and proof runs.

## Progress

- Started Batch 1 by adding a reusable bot tooling status builder and dashboard/API integration.
