# Dashboard Routes

All routes require login (via `@login_required`) unless noted. CSRF protection is active on all POST forms.

---

## Auth (`auth.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/auth/login` | Login page |
| `POST` | `/auth/login` | Process login credentials |
| `POST` | `/auth/logout` | Log out current user |
| `POST` | `/auth/api/login` | API login (CSRF-exempt) |
| `POST` | `/auth/api/logout` | API logout (CSRF-exempt) |

---

## Onboarding (`onboarding.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET/POST` | `/onboarding` | First-run setup wizard (admin account creation) |

---

## Workers (`routes/workers.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/workers` | Worker list with status |
| `GET` | `/workers/<id>` | Worker detail: live metrics, GPU graphs, queue depth |
| `POST` | `/workers/<id>/enable` | Enable worker |
| `POST` | `/workers/<id>/disable` | Disable worker |

---

## Bots (`routes/bots.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/bots` | Bot list; supports import from exported JSON |
| `POST` | `/bots/import` | Import bot from JSON (with overwrite confirmation) |
| `GET` | `/bots/<id>` | Bot detail: backends, workflow, triggers, test runs, run history |
| `POST` | `/bots/<id>` | Update bot configuration |
| `POST` | `/bots/<id>/enable` | Enable bot |
| `POST` | `/bots/<id>/disable` | Disable bot |
| `POST` | `/bots/<id>/run` | Run bot with test payload |
| `POST` | `/bots/<id>/launch` | Launch bot via saved profile |
| `GET` | `/bots/<id>/export` | Export bot config as JSON |
| `GET` | `/bots/<id>/runs` | Run history |
| `GET` | `/bots/<id>/runs/<run_id>` | Run detail with artifacts |

---

## Tasks (`routes/tasks.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | Task board (filterable by status, bot, orchestration) |
| `GET` | `/tasks/<id>` | Task detail: payload, result, error, artifacts |
| `POST` | `/tasks/<id>/retry` | Retry a failed task |
| `POST` | `/tasks/<id>/cancel` | Cancel a task |
| `GET` | `/tasks/<id>/artifacts/<artifact_id>` | Download artifact |

---

## Projects (`routes/projects.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/projects` | Project list |
| `POST` | `/projects` | Create project |
| `GET` | `/projects/<id>` | Project detail: bots, GitHub config, workspace |
| `POST` | `/projects/<id>` | Update project |
| `POST` | `/projects/<id>/delete` | Delete project |
| `POST` | `/projects/<id>/github/connect` | Connect GitHub PAT |
| `POST` | `/projects/<id>/github/disconnect` | Disconnect GitHub |
| `POST` | `/projects/<id>/github/sync` | Sync repo to vault |
| `POST` | `/projects/<id>/repo-workspace/clone` | Clone repo workspace |
| `POST` | `/projects/<id>/repo-workspace/pull` | Pull latest |
| `POST` | `/projects/<id>/repo-workspace/run` | Run command in workspace |

---

## Pipelines (`routes/pipelines.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | Pipeline run list grouped by `pipeline_name` |
| `GET` | `/pipelines/<orchestration_id>` | Pipeline detail: all tasks, DAG view, per-stage status |
| `POST` | `/pipelines/<orchestration_id>/cancel` | Cancel all tasks in pipeline |

---

## Chat (`routes/chat.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/chat` | Conversation list |
| `POST` | `/chat` | Create conversation |
| `GET` | `/chat/<id>` | Conversation UI with message history |
| `POST` | `/chat/<id>/message` | Post message (triggers SSE streaming) |
| `POST` | `/chat/<id>/assign` | @assign orchestration |
| `POST` | `/chat/<id>/archive` | Archive conversation |
| `GET` | `/chat/<id>/context` | Context picker |
| `POST` | `/chat/<id>/ingest` | Ingest chat to vault |

---

## Connections (`routes/connections.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/connections` | Connection list |
| `POST` | `/connections` | Create connection |
| `GET` | `/connections/<id>` | Connection detail |
| `POST` | `/connections/<id>` | Update connection |
| `POST` | `/connections/<id>/delete` | Delete connection |
| `POST` | `/connections/<id>/test` | Test connection |
| `GET` | `/connections/<id>/schema` | View/refresh schema |
| `POST` | `/bots/<bot_id>/connections/<conn_id>/attach` | Attach connection to bot |
| `POST` | `/bots/<bot_id>/connections/<conn_id>/detach` | Detach connection |

---

## Vault (`routes/vault.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/vault` | Vault item list (filterable by namespace, project) |
| `POST` | `/vault/ingest` | Ingest text/file/URL |
| `GET` | `/vault/<id>` | Item preview with chunks |
| `DELETE` | `/vault/<id>` | Delete item |
| `GET` | `/vault/search` | Semantic search |

---

## Users (`routes/users.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/users` | User list (admin only) |
| `POST` | `/users` | Create user |
| `POST` | `/users/<id>/delete` | Delete user |
| `POST` | `/users/<id>/password` | Change password |

---

## Events (`routes/events.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/events/stream` | SSE stream for task status updates and chat messages |

CSRF-exempt (GET-only). Streams JSON events to the browser for real-time updates.

---

## Settings (`settings.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings` | Settings page |
| `POST` | `/settings` | Update settings |
| `POST` | `/settings/deploy` | Trigger blue/green slot swap |
