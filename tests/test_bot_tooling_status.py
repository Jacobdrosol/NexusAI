from unittest.mock import patch

import bcrypt

from dashboard.bot_tooling_status import build_bot_tooling_status


def _login_admin(dashboard_client):
    from dashboard.db import get_db
    from dashboard.models import User

    password = "password123"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        db.add(User(email="admin@test.com", password_hash=password_hash, role="admin", is_active=True))
        db.commit()
    finally:
        db.close()
    response = dashboard_client.post(
        "/login",
        data={"email": "admin@test.com", "password": password},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}


def test_bot_tooling_status_groups_blocked_worker_tool_causes():
    status = build_bot_tooling_status(
        bots=[
            {
                "id": "browser-bot",
                "name": "Browser Bot",
                "role": "browser-inspector",
                "enabled": True,
                "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                "execution_policy": {
                    "required_worker_tools": ["browser-ui"],
                    "browser_action_allowlist": ["question_bank.patch_existing"],
                    "browser_action_owner_approval_required": ["question_bank.patch_existing"],
                },
                "routing_rules": {
                    "worker_profile": {
                        "worker_id": "browser-worker",
                        "service": "acme-browser-worker",
                        "role": "browser-inspector",
                        "task_scope": "single-lesson-browser-qc",
                        "can_edit": False,
                        "course_scope": ["57"],
                        "lesson_scope": ["1201"],
                        "site_account": "content-kc@acme.local",
                    }
                },
            },
            {
                "id": "ready-bot",
                "name": "Ready Bot",
                "enabled": True,
                "backends": [{"worker_id": "llm-worker", "type": "remote_llm"}],
            },
            {
                "id": "connection-bot",
                "name": "Connection Bot",
                "enabled": True,
                "backends": [{"type": "custom", "provider": "http_connection", "model": "attached-http", "api_key_ref": "acme_AGENT_TOKEN"}],
                "routing_rules": {"connection_context": {"connection_name": "acme-agent-api"}},
                "execution_policy": {
                    "connection_action_allowlist": [
                        "acme-agent-api.updateLesson",
                        "acme-agent-api.updateLesson",
                    ],
                    "connection_action_owner_approval_required": ["acme-agent-api.updateLesson"],
                },
            },
            {
                "id": "disabled-bot",
                "name": "Disabled Bot",
                "enabled": False,
                "backends": [{"worker_id": "missing-worker", "type": "remote_llm"}],
            },
            {
                "id": "missing-worker-bot",
                "name": "Missing Worker Bot",
                "enabled": True,
                "backends": [{"worker_id": "missing-worker", "type": "remote_llm"}],
            },
            {
                "id": "bot-policy-blocked",
                "name": "Bot Policy Blocked",
                "enabled": True,
                "backends": [{"worker_id": "llm-worker", "type": "remote_llm"}],
            },
            {
                "id": "project-policy-blocked",
                "name": "Project Policy Blocked",
                "enabled": True,
                "backends": [{"worker_id": "llm-worker", "type": "remote_llm"}],
            },
            {
                "id": "raw-secret-bot",
                "name": "Raw Secret Bot",
                "enabled": True,
                "backends": [{"type": "cloud_api", "provider": "openai", "api_key_ref": "sk-live-secret"}],
            },
        ],
        readiness_payload={
            "readiness": [
                {
                    "bot_id": "browser-bot",
                    "state": "blocked",
                    "ready": False,
                    "checks": [
                        {
                            "status": "failed",
                            "message": "Worker 'browser-worker' browser runtime is not ready: browser_session_check_failed",
                        }
                    ],
                },
                {"bot_id": "ready-bot", "state": "ready", "ready": True, "checks": []},
                {"bot_id": "connection-bot", "state": "ready", "ready": True, "checks": []},
                {
                    "bot_id": "disabled-bot",
                    "state": "disabled",
                    "ready": False,
                    "checks": [
                        {"component": "bot", "status": "failed", "message": "Bot is disabled."},
                        {"component": "backend[0]", "status": "failed", "message": "Model 'offline-model' is not present/enabled in the model catalog."},
                    ],
                },
                {
                    "bot_id": "missing-worker-bot",
                    "state": "blocked",
                    "ready": False,
                    "checks": [{"status": "failed", "message": "Worker 'missing-worker' is missing."}],
                },
                {
                    "bot_id": "bot-policy-blocked",
                    "state": "blocked",
                    "ready": False,
                    "checks": [{"status": "failed", "message": "Bot policy does not allow workspace tools."}],
                },
                {
                    "bot_id": "project-policy-blocked",
                    "state": "blocked",
                    "ready": False,
                    "checks": [{"status": "failed", "message": "Project policy does not allow repo search."}],
                },
                {"bot_id": "raw-secret-bot", "state": "ready", "ready": True, "checks": []},
            ]
        },
        workers=[
            {"id": "browser-worker", "status": "online", "enabled": True},
            {"id": "llm-worker", "status": "offline", "enabled": True},
        ],
        worker_probes_payload={
            "probes": [
                {
                    "worker_id": "browser-worker",
                    "probe_status": "degraded",
                    "detail": "Browser profile exists but site auth check failed.",
                    "runtime_evidence": {
                        "browser": {
                            "configured": True,
                            "ready": False,
                            "browser": "chromium",
                            "reason": "browser_session_check_failed",
                        }
                    },
                },
                {"worker_id": "llm-worker", "probe_status": "ready"},
            ]
        },
        api_keys=[{"name": "acme_AGENT_TOKEN"}],
    )

    assert status["summary"]["ready"] == 2
    assert status["summary"]["blocked"] == 5
    assert status["summary"]["disabled"] == 1
    assert status["summary"]["tooling_bot_count"] == 1
    assert status["summary"]["connection_action_bot_count"] == 1
    assert status["summary"]["browser_action_bot_count"] == 1
    assert status["summary"]["owner_approval_action_count"] == 1
    assert status["summary"]["browser_owner_approval_action_count"] == 1
    assert status["summary"]["http_connection_backend_count"] == 1
    assert status["summary"]["credential_ref_bot_count"] == 2
    assert status["summary"]["backend_credential_ref_count"] == 2
    assert status["summary"]["raw_credential_ref_bot_count"] == 1
    assert status["summary"]["worker_profile_bot_count"] == 1
    assert status["summary"]["editable_worker_profile_bot_count"] == 0
    assert status["summary"]["disabled_activation_blocker_bot_count"] == 1
    assert status["summary"]["disabled_activation_blocker_count"] == 1
    assert status["summary"]["worker_assignment_count"] == 5
    assert status["summary"]["missing_worker_assignment_count"] == 1
    assert status["summary"]["offline_worker_assignment_count"] == 3
    assert status["summary"]["degraded_worker_probe_count"] == 1
    assert status["summary"]["recommended_action"]["label"] == "configure vault key"
    assert status["summary"]["recommended_action"]["level"] == "critical"
    assert "1 bot(s)" in status["summary"]["recommended_action"]["detail"]
    assert status["required_tools"] == [{"tool": "browser-ui", "bot_count": 1}]
    assert status["connection_actions"] == [{"action": "acme-agent-api.updateLesson", "bot_count": 1}]
    assert status["browser_actions"] == [{"action": "question_bank.patch_existing", "bot_count": 1}]
    browser_row = next(row for row in status["rows"] if row["bot_id"] == "browser-bot")
    assert browser_row["browser_actions"] == ["question_bank.patch_existing"]
    assert browser_row["browser_owner_approval_actions"] == ["question_bank.patch_existing"]
    assert browser_row["blocking_category_view"]["label"] == "Authenticated browser session"
    assert browser_row["recommended_action"]["label"] == "restore browser session"
    assert browser_row["worker_profile"]["task_scope"] == "single-lesson-browser-qc"
    assert browser_row["worker_profile"]["can_edit"] is False
    assert browser_row["worker_profile"]["course_scope"] == ["57"]
    assert browser_row["worker_profile"]["lesson_scope"] == ["1201"]
    assert browser_row["worker_profile"]["site_account"] == "content-kc@acme.local"
    connection_row = next(row for row in status["rows"] if row["bot_id"] == "connection-bot")
    assert connection_row["connection_actions"] == ["acme-agent-api.updateLesson"]
    assert connection_row["owner_approval_actions"] == ["acme-agent-api.updateLesson"]
    assert connection_row["connection_backend_count"] == 1
    assert connection_row["connection_context"] == "acme-agent-api"
    assert connection_row["credential_refs"] == ["acme_AGENT_TOKEN"]
    raw_secret_row = next(row for row in status["rows"] if row["bot_id"] == "raw-secret-bot")
    assert raw_secret_row["state"] == "blocked"
    assert raw_secret_row["credential_refs"] == ["[redacted raw credential]"]
    assert raw_secret_row["raw_credential_ref_detected"] is True
    assert raw_secret_row["blocking_category"] == "credential"
    assert raw_secret_row["recommended_action"]["label"] == "configure vault key"
    assert "Raw credential material" in raw_secret_row["blocking_messages"][0]
    disabled_row = next(row for row in status["rows"] if row["bot_id"] == "disabled-bot")
    assert disabled_row["disabled_activation_messages"] == ["Model 'offline-model' is not present/enabled in the model catalog."]
    groups = {group["category"]: group for group in status["blocked_groups"]}
    assert groups["browser_session"]["label"] == "Authenticated browser session"
    assert "site account can exist" in groups["browser_session"]["detail"]
    assert groups["browser_session"]["recommended_action"]["label"] == "restore browser session"
    assert groups["browser_session"]["bots"][0]["workers"][0]["probe_status"] == "degraded"
    assert groups["browser_session"]["bots"][0]["workers"][0]["detail"] == "Browser profile exists but site auth check failed."
    assert groups["browser_session"]["bots"][0]["workers"][0]["browser_reason"] == "browser_session_check_failed"
    assert groups["bot_policy"]["label"] == "Bot tool policy"
    assert "bot configuration" in groups["bot_policy"]["detail"]
    assert groups["bot_policy"]["recommended_action"]["label"] == "review bot policy"
    assert groups["project_policy"]["label"] == "Project tool policy"
    assert "scoped project" in groups["project_policy"]["detail"]
    assert groups["project_policy"]["recommended_action"]["label"] == "review project policy"


def test_bot_tooling_status_blocks_missing_credential_refs():
    status = build_bot_tooling_status(
        bots=[
            {
                "id": "site-updater",
                "name": "Site Updater",
                "enabled": True,
                "backends": [
                    {
                        "type": "custom",
                        "provider": "http_connection",
                        "model": "attached-http",
                        "api_key_ref": "MISSING_SITE_TOKEN",
                    }
                ],
                "execution_policy": {
                    "connection_action_allowlist": ["site.update"],
                },
            }
        ],
        readiness_payload={"readiness": [{"bot_id": "site-updater", "state": "ready", "ready": True, "checks": []}]},
        workers=[],
        api_keys=[{"name": "OTHER_TOKEN"}],
    )

    row = status["rows"][0]
    assert status["summary"]["ready"] == 0
    assert status["summary"]["blocked"] == 1
    assert status["summary"]["missing_credential_ref_bot_count"] == 1
    assert status["summary"]["missing_credential_ref_count"] == 1
    assert status["summary"]["recommended_action"]["label"] == "configure vault key"
    assert row["state"] == "blocked"
    assert row["blocking_category"] == "credential"
    assert row["missing_credential_refs"] == ["MISSING_SITE_TOKEN"]
    assert "Missing key-vault credential reference(s): MISSING_SITE_TOKEN" in row["blocking_messages"]
    groups = {group["category"]: group for group in status["blocked_groups"]}
    assert groups["credential"]["recommended_action"]["label"] == "configure vault key"


def test_bots_page_surfaces_tooling_readiness_panel(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def __init__(self):
            self._last_error = {}

        def list_bots(self):
            return [
                {
                    "id": "browser-bot",
                    "name": "Browser Bot",
                    "role": "browser-inspector",
                    "enabled": True,
                    "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                    "execution_policy": {
                        "required_worker_tools": ["browser-ui"],
                        "browser_action_allowlist": ["question_bank.patch_existing"],
                        "browser_action_owner_approval_required": ["question_bank.patch_existing"],
                    },
                    "routing_rules": {
                        "worker_profile": {
                            "worker_id": "browser-worker",
                            "service": "acme-browser-worker",
                            "role": "browser-inspector",
                            "task_scope": "single-lesson-browser-qc",
                            "can_edit": False,
                            "course_scope": ["57"],
                            "site_username": "content-kc@acme.local",
                            "cli_tools": ["browser-ui", "codex-cli"],
                        }
                    },
                },
                {
                    "id": "connection-bot",
                    "name": "Connection Bot",
                    "role": "site-updater",
                    "enabled": True,
                    "backends": [{"type": "custom", "provider": "http_connection", "model": "attached-http", "api_key_ref": "acme_AGENT_TOKEN"}],
                    "routing_rules": {"connection_context": {"connection_name": "acme-agent-api"}},
                    "execution_policy": {
                        "connection_action_allowlist": ["acme-agent-api.updateLesson"],
                        "connection_action_owner_approval_required": ["acme-agent-api.updateLesson"],
                    },
                },
                {
                    "id": "disabled-needs-model",
                    "name": "Disabled Needs Model",
                    "role": "chat",
                    "enabled": False,
                    "backends": [{"type": "cloud_api", "provider": "ollama", "model": "missing-model"}],
                },
            ]

        def list_bot_readiness(self):
            return {
                "readiness": [
                    {
                        "bot_id": "browser-bot",
                        "state": "blocked",
                        "ready": False,
                        "checks": [{"status": "failed", "message": "browser_session_check_failed"}],
                    },
                    {
                        "bot_id": "connection-bot",
                        "state": "ready",
                        "ready": True,
                        "checks": [],
                    },
                    {
                        "bot_id": "disabled-needs-model",
                        "state": "disabled",
                        "ready": False,
                        "checks": [
                            {"component": "bot", "status": "failed", "message": "Bot is disabled."},
                            {"component": "backend[0]", "status": "failed", "message": "Model 'missing-model' is not present/enabled in the model catalog."},
                        ],
                    },
                ]
            }

        def list_workers(self):
            return [{"id": "browser-worker", "name": "Browser Worker", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {
                "probes": [
                    {
                        "worker_id": "browser-worker",
                        "probe_status": "degraded",
                        "runtime_evidence": {
                            "browser": {
                                "configured": True,
                                "ready": False,
                                "browser": "chromium",
                                "reason": "browser_session_check_failed",
                            }
                        },
                    }
                ]
            }

        def list_schedules(self, limit=200):
            return {"schedules": []}

        def list_models(self):
            return []

        def list_keys(self):
            return [{"name": "acme_AGENT_TOKEN"}]

        def list_projects(self):
            return []

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        page = dashboard_client.get("/bots")
        api = dashboard_client.get("/api/bots/tooling-status")

    assert page.status_code == 200
    assert b"Bot Tooling Readiness" in page.data
    assert b"function botImportErrorMessage" in page.data
    assert b"imported disabled or needs readiness work" in page.data
    assert b"Readiness blockers:" in page.data
    assert b"throw new Error(botImportErrorMessage(data))" in page.data
    assert b"Authenticated browser session" in page.data
    assert b"site account can exist" in page.data
    assert b"browser-ui" in page.data
    assert b"Connection Action Bots" in page.data
    assert b"Browser Action Bots" in page.data
    assert b"Worker Assignments" in page.data
    assert b"Scoped Worker Profiles" in page.data
    assert b"Edit-Capable Profiles" in page.data
    assert b"Credential Ref Bots" in page.data
    assert b"Missing Credential Refs" in page.data
    assert b"Raw Credential Refs" in page.data
    assert b"Route: browser on browser-worker" in page.data
    assert b"Scope: single-lesson-browser-qc" in page.data
    assert b"Worker profile: browser-worker" in page.data
    assert b"Service: acme-browser-worker" in page.data
    assert b"Edits: not allowed" in page.data
    assert b"Courses: 57" in page.data
    assert b"CLI tools: browser-ui, codex-cli" in page.data
    assert b"Site login: content-kc@acme.local" in page.data
    assert b"Route: http_connection / attached-http" in page.data
    assert b"Disabled Needs Fix" in page.data
    assert b"setBotTableFilter('disabled-needs-fix')" in page.data
    assert b"Recommended action:" in page.data
    assert b"restore browser session" in page.data
    assert b"Action: restore browser session" in page.data
    assert b"browser-worker: degraded" in page.data
    assert b"Browser session unavailable: browser_session_check_failed" in page.data
    assert b"Open the worker browser profile" in page.data
    assert b"Missing Workers" in page.data
    assert b"Offline Workers" in page.data
    assert b"Degraded Probes" in page.data
    assert b"Connection actions" in page.data
    assert b"Browser actions" in page.data
    assert b"acme-agent-api.updateLesson" in page.data
    assert b"question_bank.patch_existing" in page.data
    assert b"Owner approval: 1" in page.data
    assert b"Browser owner approval: 1" in page.data
    assert b"Context: acme-agent-api" in page.data
    assert b"Credential refs: acme_AGENT_TOKEN" in page.data
    page_html = page.data.decode("utf-8")
    connection_row_start = page_html.index('data-id="connection-bot"')
    connection_row = page_html[connection_row_start:page_html.index("</tr>", connection_row_start)]
    assert 'data-has-tools="true"' in connection_row
    disabled_row_start = page_html.index('data-id="disabled-needs-model"')
    disabled_row = page_html[disabled_row_start:page_html.index("</tr>", disabled_row_start)]
    assert 'data-disabled-needs-fix="true"' in disabled_row
    assert "Enable blockers:" in disabled_row
    assert "Model &#39;missing-model&#39; is not present/enabled in the model catalog." in disabled_row
    assert api.status_code == 200
    payload = api.get_json()
    assert payload["summary"]["blocked"] == 1
    assert payload["summary"]["connection_action_bot_count"] == 1
    assert payload["summary"]["browser_action_bot_count"] == 1
    assert payload["summary"]["credential_ref_bot_count"] == 1
    assert payload["summary"]["backend_credential_ref_count"] == 1
    assert payload["summary"]["missing_credential_ref_count"] == 0
    assert payload["summary"]["disabled_activation_blocker_bot_count"] == 1
    assert payload["summary"]["disabled_activation_blocker_count"] == 1
    assert payload["summary"]["worker_assignment_count"] == 1
    assert payload["summary"]["worker_profile_bot_count"] == 1
    assert payload["summary"]["editable_worker_profile_bot_count"] == 0
    assert payload["summary"]["degraded_worker_probe_count"] == 1
    assert payload["summary"]["recommended_action"]["label"] == "restore browser session"
    assert payload["blocked_groups"][0]["category"] == "browser_session"
    assert payload["blocked_groups"][0]["label"] == "Authenticated browser session"
    assert payload["blocked_groups"][0]["recommended_action"]["label"] == "restore browser session"
    assert payload["rows"][0]["recommended_action"]["label"] == "restore browser session"
    assert payload["rows"][0]["workers"][0]["browser_reason"] == "browser_session_check_failed"
    assert payload["rows"][0]["workers"][0]["detail"] == "Browser session unavailable: browser_session_check_failed"


def test_bots_tooling_status_surfaces_partial_control_plane_data(dashboard_client):
    _login_admin(dashboard_client)

    class FakeCP:
        def list_bots(self):
            return [
                {
                    "id": "browser-bot",
                    "name": "Browser Bot",
                    "role": "browser-inspector",
                    "enabled": True,
                    "backends": [{"worker_id": "browser-worker", "type": "browser"}],
                    "execution_policy": {"required_worker_tools": ["browser-ui"]},
                }
            ]

        def list_bot_readiness(self):
            return None

        def list_schedules(self, limit=200):
            return {"schedules": []}

        def list_workers(self):
            return [{"id": "browser-worker", "name": "Browser Worker", "status": "online", "enabled": True}]

        def list_worker_probes(self):
            return {"probes": [{"worker_id": "browser-worker", "probe_status": "ready"}]}

        def list_models(self):
            return []

        def list_keys(self):
            return []

        def list_projects(self):
            return []

        def last_error(self):
            return {"status_code": 503, "detail": "readiness endpoint unavailable"}

    with patch("dashboard.cp_client.get_cp_client", return_value=FakeCP()):
        page = dashboard_client.get("/bots")
        api = dashboard_client.get("/api/bots/tooling-status")

    assert page.status_code == 200
    assert b"Bot tooling data is incomplete" in page.data
    assert b"bot readiness" in page.data
    assert b"readiness endpoint unavailable" in page.data
    assert api.status_code == 200
    payload = api.get_json()
    assert payload["data_degraded"] is True
    assert payload["data_warnings"][0]["source"] == "bot readiness"
    assert payload["data_warnings"][0]["detail"] == "readiness endpoint unavailable"
