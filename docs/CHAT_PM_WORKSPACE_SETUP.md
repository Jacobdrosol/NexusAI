# Chat + PM + Workspace Setup (Open Source)

This guide sets up a project so chat can use repository context, PM orchestration can run with Ollama Cloud models, and repository workspace actions can be done from the UI.

## 1. Prerequisites

1. Control plane and dashboard are running.
2. You can open:
   - `http://<control-plane-host>:8000/health`
   - `http://<dashboard-host>:5000`
3. You have a control-plane API token if your environment enforces API auth.
4. The control plane host has write access to `NEXUSAI_REPO_WORKSPACE_ROOT` (or default `data/repo_workspaces`).

## 2. Configure Repository Workspace (Project Level)

In `Projects -> <project> -> Repository Workspace`:

1. Enable `Repository Workspace`.
2. Keep managed workspace mode (default). Do not set a host path in UI.
3. Set `Clone URL` and `Default Branch`.
4. Enable `Allow git push` only if needed.
5. Enable `Allow command execution` if you want build/test from UI.
6. Save policy.
7. Click `Clone` (or `Pull` if already cloned).
8. Click `Refresh Repo Status` and confirm branch/status are returned.

Optional checks in the same panel:

1. Run `git status --short`.
2. Run language checks with `Run in temporary isolated workspace` enabled for clean execution.

## 3. Configure Chat Workspace Tools (Project Level)

In `Projects -> <project> -> Chat Workspace Tools`:

1. Enable `chat workspace tools for this project`.
2. Enable `Allow semantic repo search`.
3. Enable `Allow filesystem read/search` if needed.
4. Save workspace tool policy.

## 4. Install PM Bot Pack (Ollama Cloud)

Use the setup script from repo root:

```powershell
py scripts/setup_pm_bot_pack.py --apply --base-url http://127.0.0.1:8000 --api-token <token> --api-key-ref Ollama_Cloud1 --chat-tools-mode repo_and_filesystem
```

If you prefer export/import flow:

```powershell
py scripts/setup_pm_bot_pack.py --export-dir "C:\temp\pm-pack" --chat-tools-mode repo_and_filesystem
```

Then import those `*.bot.json` files in `Bots -> Import`.

## 5. Confirm Bot-Level Tool Access

For each PM bot (`pm-orchestrator`, `pm-coder`, `pm-research-analyst`, `pm-tester`, `pm-security-reviewer`, `pm-database-engineer`):

1. Open `Bots -> <bot> -> Chat Tool Access`.
2. Enable workspace tools.
3. Enable `semantic repo search`.
4. Enable `filesystem read/search` where required.
5. Save.

## 6. Configure Conversation-Level Tool Access

In `Chat`:

1. Create/select a conversation scoped to the target project.
2. In `Chat Tool Access`, enable workspace tools for this chat.
3. Enable repo search and filesystem (as needed).
4. Save chat tool access.
5. In the composer, keep `Use workspace tools for this message` enabled for messages that need repo/filesystem context.
6. Use `@assign <instruction>` to run PM orchestration.

## 7. Three-Switch Policy (Required)

Workspace tools run only when all are true:

1. Bot-level `Chat Tool Access` is enabled.
2. Project-level `Chat Workspace Tools` is enabled.
3. Conversation-level `Chat Tool Access` is enabled (and message requests tool use).

If any switch is off, workspace tool usage is denied.

## 8. Quick Verification

1. Send a normal chat question without tools; confirm normal response.
2. Send a repo question with `Use workspace tools for this message` enabled; confirm response cites repo files.
3. Send `@assign` with a coding task; confirm task graph is created and role-routed.
4. Run one repository command from Project Detail (`git status --short`) and confirm output plus usage metrics appear.

## 9. Troubleshooting

1. Tools not used in chat:
   - verify all three switches
   - verify project repository workspace is enabled and cloned
2. Filesystem snippets missing:
   - verify project-level `filesystem` is enabled
   - verify bot-level `filesystem` is enabled
3. Repo semantic context missing:
   - verify project-level `repo_search` is enabled
   - ingest/sync project repo context if using project repo retrieval features
4. PM assignment not decomposing:
   - verify `pm-orchestrator` exists and has role `pm`
   - verify bot backend model/API key reference is valid
