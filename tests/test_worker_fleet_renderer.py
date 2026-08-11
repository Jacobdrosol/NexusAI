import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_worker_fleet.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_worker_fleet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_worker_fleet_default_output_uses_configured_private_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUSAI_PRIVATE_CONFIG_DIR", str(tmp_path / "private-nexusai"))

    renderer = _load_renderer()

    assert renderer.DEFAULT_OUTPUT_DIR == tmp_path / "private-nexusai" / "worker-fleet"
    assert not renderer.DEFAULT_OUTPUT_DIR.is_relative_to(renderer.ROOT)


def _profile(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "fleet": {
                    "control_plane_url": "http://control_plane:8000",
                    "compose_project_name": "test-worker-fleet",
                    "docker_network": "nexusai_nexus-net",
                    "worker_node_source": "../nexus-worker-node",
                    "provider": "ollama_cloud",
                    "default_model": "glm-5.2:cloud",
                    "backend_params": {"temperature": 0.2, "max_tokens": 1000},
                },
                "workers": [
                    {
                        "id": "content-repair-01",
                        "name": "Content Worker",
                        "role": "content-repair",
                        "service": "worker-content",
                        "can_edit": True,
                        "task_scope": "single-lesson-controlled-content-repair",
                        "site_account": "content-repair@globaliq.local",
                        "allowed_pages": ["/admin/dashboard", "/admin/documentation"],
                        "course_scope": ["60"],
                        "bot": {"id": "content-worker-bot"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_render_worker_fleet_outputs_compose_worker_config_and_bot(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"

    summary = renderer.render(
        profile,
        out,
        {
            "CONTROL_PLANE_API_TOKEN": "control-token",
            "OLLAMA_API_KEY": "ollama-token",
        },
    )

    assert summary["workers"][0]["id"] == "content-repair-01"
    assert summary["compose_project_name"] == "test-worker-fleet"

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert compose["name"] == "test-worker-fleet"
    service = compose["services"]["worker-content"]
    assert service["networks"] == ["nexus-net"]
    assert service["env_file"][0].endswith("content-repair-01.env")
    assert service["cpus"] == "1.0"
    assert service["mem_limit"] == "1g"
    assert service["pids_limit"] == 256
    assert compose["networks"]["nexus-net"]["name"] == "nexusai_nexus-net"

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["host"] == "worker-content"
    assert worker_cfg["listen_host"] == "0.0.0.0"
    assert worker_cfg["capabilities"][0]["provider"] == "ollama_cloud"
    assert worker_cfg["capabilities"][0]["models"] == ["glm-5.2:cloud"]
    assert worker_cfg["runtime_limits"] == {
        "cpus": 1.0,
        "memory_limit": "1g",
        "pids_limit": 256,
    }

    env_text = (out / "env" / "content-repair-01.env").read_text()
    assert "CONTROL_PLANE_API_TOKEN=control-token" in env_text
    assert "OLLAMA_API_KEY=ollama-token" in env_text

    bot = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot["id"] == "content-worker-bot"
    assert bot["backends"][0]["type"] == "remote_llm"
    assert bot["backends"][0]["worker_id"] == "content-repair-01"
    assert bot["backends"][0]["provider"] == "ollama_cloud"
    assert bot["routing_rules"]["worker_profile"]["course_scope"] == ["60"]
    assert bot["routing_rules"]["worker_profile"]["site_account"] == "content-repair@globaliq.local"

    catalog_models = json.loads((out / "models" / "catalog-models.json").read_text())
    assert catalog_models == [
        {
            "id": "fleet-ollama_cloud-glm-5-2-cloud",
            "name": "glm-5.2:cloud",
            "provider": "ollama_cloud",
            "capabilities": [],
            "enabled": True,
        }
    ]


def test_render_worker_fleet_expands_guarded_replica_templates(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["max_workers"] = 4
    profile_data["workers"][0]["id"] = "content-repair"
    profile_data["workers"][0]["name"] = "Content Worker"
    profile_data["workers"][0]["service"] = "worker-content"
    profile_data["workers"][0]["site_account"] = "content-repair-{index:02d}@globaliq.local"
    profile_data["workers"][0]["bot"]["id"] = "content-worker-bot"
    profile_data["workers"][0]["replicas"] = 3
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    summary = renderer.render(
        profile,
        out,
        {
            "CONTROL_PLANE_API_TOKEN": "control-token",
            "OLLAMA_API_KEY": "ollama-token",
        },
    )

    assert [item["id"] for item in summary["workers"]] == [
        "content-repair-01",
        "content-repair-02",
        "content-repair-03",
    ]
    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert set(compose["services"]) == {
        "worker-content-01",
        "worker-content-02",
        "worker-content-03",
    }
    assert json.loads((out / "bots" / "content-repair-01.bot.json").read_text())["id"] == "content-worker-bot-01"
    replica_bot = json.loads((out / "bots" / "content-repair-02.bot.json").read_text())
    assert replica_bot["routing_rules"]["worker_profile"]["site_account"] == "content-repair-02@globaliq.local"


def test_render_worker_fleet_rejects_newline_site_account_labels(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["site_account"] = "content-repair@globaliq.local\nADMIN=true"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="site_account cannot contain a newline"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_propagates_declared_worker_request_token(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["worker_request_token_env"] = "NEXUS_WORKER_REQUEST_TOKEN"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")
    out = tmp_path / "runtime"

    renderer.render(
        profile,
        out,
        {
            "CONTROL_PLANE_API_TOKEN": "control-token",
            "OLLAMA_API_KEY": "ollama-token",
            "NEXUS_WORKER_REQUEST_TOKEN": "node-token",
        },
    )

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["request_token_env"] == "NEXUS_WORKER_REQUEST_TOKEN"
    env_text = (out / "env" / "content-repair-01.env").read_text()
    assert "NEXUS_WORKER_REQUEST_TOKEN=node-token" in env_text


def test_apply_models_creates_only_missing_models(tmp_path, monkeypatch):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    class _Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

        def raise_for_status(self):
            assert 200 <= self.status_code < 300

    created = []

    def _get(*_args, **_kwargs):
        return _Response(200, [{"id": "operator-model", "name": "other-model", "provider": "ollama_cloud"}])

    def _post(_url, *, data, **_kwargs):
        created.append(json.loads(data))
        return _Response(201, created[-1])

    monkeypatch.setattr(renderer.requests, "get", _get)
    monkeypatch.setattr(renderer.requests, "post", _post)

    results = renderer.apply_models(out, api_url="http://control-plane.test", api_token="control-token")

    assert results[0]["action"] == "created"
    assert results[0]["ok"] is True
    assert created == [
        {
            "id": "fleet-ollama_cloud-glm-5-2-cloud",
            "name": "glm-5.2:cloud",
            "provider": "ollama_cloud",
            "capabilities": [],
            "enabled": True,
        }
    ]


def test_apply_models_preserves_existing_catalog_entry(tmp_path, monkeypatch):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return [
                {
                    "id": "operator-curated-glm",
                    "name": "glm-5.2:cloud",
                    "provider": "ollama_cloud",
                    "enabled": True,
                    "notes": "managed by an operator",
                }
            ]

        def raise_for_status(self):
            return None

    monkeypatch.setattr(renderer.requests, "get", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(
        renderer.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("existing catalog models must not be overwritten"),
    )

    results = renderer.apply_models(out, api_url="http://control-plane.test", api_token="control-token")

    assert results == [
        {
            "model_id": "fleet-ollama_cloud-glm-5-2-cloud",
            "provider": "ollama_cloud",
            "name": "glm-5.2:cloud",
            "ok": True,
            "action": "existing",
            "catalog_model_id": "operator-curated-glm",
        }
    ]


def test_verify_bots_ready_reports_private_fleet_readiness(tmp_path, monkeypatch):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "bot_id": "content-worker-bot",
                "ready": True,
                "state": "ready",
                "summary": {"checks": 3, "failed": 0, "blocking": 0},
                "checks": [{"status": "ready", "message": "Worker is online."}],
            }

    monkeypatch.setattr(renderer.requests, "get", lambda *_args, **_kwargs: _Response())

    results = renderer.verify_bots_ready(out, api_url="http://control-plane.test", api_token="control-token")

    assert results == [
        {
            "bot_id": "content-worker-bot",
            "ok": True,
            "action": "verified",
            "state": "ready",
            "summary": {"checks": 3, "failed": 0, "blocking": 0},
            "blockers": [],
        }
    ]
    assert json.loads((out / "verify-readiness-summary.json").read_text(encoding="utf-8")) == results


def test_verify_bots_ready_blocks_disabled_or_unready_bots(tmp_path, monkeypatch):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "bot_id": "content-worker-bot",
                "ready": False,
                "state": "blocked",
                "summary": {"checks": 3, "failed": 1, "blocking": 1},
                "checks": [
                    {
                        "status": "failed",
                        "message": "Worker 'content-repair-01' is offline.",
                    }
                ],
            }

    monkeypatch.setattr(renderer.requests, "get", lambda *_args, **_kwargs: _Response())

    results = renderer.verify_bots_ready(out, api_url="http://control-plane.test", api_token="control-token")

    assert results[0]["ok"] is False
    assert results[0]["state"] == "blocked"
    assert results[0]["blockers"] == ["Worker 'content-repair-01' is offline."]


def test_renderer_cli_waits_for_workers_before_verifying_readiness(tmp_path, monkeypatch):
    renderer = _load_renderer()
    sequence = []
    env_file = tmp_path / "control-plane.env"
    env_file.write_text("CONTROL_PLANE_API_TOKEN=control-token\n", encoding="utf-8")

    monkeypatch.setattr(
        renderer,
        "render",
        lambda *_args, **_kwargs: {"workers": [{"id": "content-repair-01"}]},
    )
    monkeypatch.setattr(
        renderer,
        "apply_models",
        lambda *_args, **_kwargs: sequence.append("models") or [{"ok": True}],
    )
    monkeypatch.setattr(
        renderer,
        "apply_bots",
        lambda *_args, **_kwargs: sequence.append("bots") or [{"ok": True}],
    )
    monkeypatch.setattr(
        renderer,
        "wait_workers",
        lambda *_args, **_kwargs: sequence.append("wait") or {"ok": True},
    )
    monkeypatch.setattr(
        renderer,
        "verify_bots_ready",
        lambda *_args, **_kwargs: sequence.append("verify") or [{"ok": True}],
    )

    exit_code = renderer.main(
        [
            "--profile",
            str(tmp_path / "workers.yaml"),
            "--output-dir",
            str(tmp_path / "runtime"),
            "--env-file",
            str(env_file),
            "--apply-models",
            "--apply-bots",
            "--wait-workers",
            "--verify-readiness",
        ]
    )

    assert exit_code == 0
    assert sequence == ["models", "bots", "wait", "verify"]


def test_render_worker_fleet_requires_ollama_key_by_default(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")

    with pytest.raises(ValueError, match="Missing Ollama Cloud API key"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token"},
        )


def test_render_worker_fleet_rejects_invalid_compose_project_name(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["compose_project_name"] = "invalid/project"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="compose_project_name"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_can_allow_missing_ollama_key(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")

    summary = renderer.render(
        profile,
        tmp_path / "runtime",
        {"CONTROL_PLANE_API_TOKEN": "control-token"},
        allow_missing_ollama_key=True,
    )

    assert summary["warnings"] == ["Missing Ollama Cloud API key env value: OLLAMA_API_KEY"]


def test_render_worker_fleet_merges_validated_resource_limits(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["resource_limits"] = {
        "cpus": 1.5,
        "memory": "1536m",
        "pids_limit": 384,
    }
    profile_data["workers"][0]["runtime"] = {
        "resource_limits": {
            "memory": "2g",
            "memory_reservation": "1g",
        }
    }
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    service = compose["services"]["worker-content"]
    assert service["cpus"] == "1.5"
    assert service["mem_limit"] == "2g"
    assert service["mem_reservation"] == "1g"
    assert service["pids_limit"] == 384

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["runtime_limits"] == {
        "cpus": 1.5,
        "memory_limit": "2g",
        "memory_reservation": "1g",
        "pids_limit": 384,
    }


def test_render_worker_fleet_profiles_disabled_workers_and_excludes_them_from_budget(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["resource_budget"] = {"cpus": "0.25", "memory": "128m"}
    profile_data["workers"][0]["enabled"] = False
    profile_data["workers"][0]["bot"]["enabled"] = False
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert compose["services"]["worker-content"]["profiles"] == ["staged"]
    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["enabled"] is False


def test_render_worker_fleet_rejects_enabled_workers_that_exceed_resource_budget(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["resource_budget"] = {"cpus": "0.5", "memory": "512m"}
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="resource_budget"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_deduplicates_identical_image_builds(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    duplicate = dict(profile_data["workers"][0])
    duplicate.update(
        {
            "id": "content-repair-02",
            "name": "Second Content Worker",
            "service": "worker-content-second",
            "bot": {"id": "content-worker-second-bot"},
        }
    )
    profile_data["workers"].append(duplicate)
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    builds = [service.get("build") for service in compose["services"].values() if service.get("build")]

    assert len(builds) == 1


def test_render_worker_fleet_rejects_invalid_resource_limits(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["resource_limits"] = {"memory": "unbounded"}
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="resource limit memory"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_supports_node_local_tooling_and_prebuilt_images(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    worker = profile_data["workers"][0]
    worker["runtime"] = {
        "image": "private-claude-tool-worker:latest",
        "env_from": {
            "ANTHROPIC_BASE_URL": "CLAUDE_GATEWAY_URL",
            "ANTHROPIC_AUTH_TOKEN": "CLAUDE_GATEWAY_TOKEN",
        },
    }
    worker["tooling"] = {"cli_tools": ["claude", "git"]}
    worker["bot"]["backends"] = [
        {
            "type": "cli",
            "provider": "cli",
            "model": "claude",
            "command": "claude -p",
        }
    ]
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    summary = renderer.render(
        profile,
        out,
        {
            "CONTROL_PLANE_API_TOKEN": "control-token",
            "OLLAMA_API_KEY": "ollama-token",
            "CLAUDE_GATEWAY_URL": "http://gateway:4000",
            "CLAUDE_GATEWAY_TOKEN": "gateway-token",
        },
    )

    assert summary["warnings"] == []
    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    service = compose["services"]["worker-content"]
    assert service["image"] == "private-claude-tool-worker:latest"
    assert "build" not in service

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["tooling"] == {"cli_tools": ["claude", "git"]}
    assert worker_cfg["capabilities"][1] == {
        "type": "tool",
        "provider": "cli",
        "models": ["claude", "git"],
    }

    bot_payload = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot_payload["backends"] == [
        {
            "type": "cli",
            "provider": "cli",
            "model": "claude",
            "worker_id": "content-repair-01",
            "command": "claude -p",
        }
    ]
    assert bot_payload["routing_rules"]["launch_profile"] == {
        "worker_node_service": "worker-content",
        "backend_type": "cli",
        "provider": "cli",
        "model": "claude",
    }

    env_text = (out / "env" / "content-repair-01.env").read_text()
    assert "ANTHROPIC_BASE_URL=http://gateway:4000" in env_text
    assert "ANTHROPIC_AUTH_TOKEN=gateway-token" in env_text


def test_render_worker_fleet_supports_bounded_browser_inspection_workers(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    worker = profile_data["workers"][0]
    worker["can_edit"] = False
    worker["tooling"] = {
        "browser": {
            "enabled": True,
            "base_url": "https://admin.example.test",
            "allowed_paths": ["/admin/courses", "/admin/health"],
            "user_data_dir": "/var/lib/nexus-worker/browser-profile",
            "request_token_env": "NEXUS_BROWSER_WORKER_TOKEN",
            "headless": True,
            "timeout_seconds": 30,
        }
    }
    worker["runtime"] = {
        "image": "nexus-worker-node-browser:latest",
        "volumes": ["/srv/nexus/browser-profile:/var/lib/nexus-worker/browser-profile"],
        "shm_size": "1gb",
    }
    worker["bot"]["backends"] = [
        {
            "type": "browser",
            "provider": "browser",
            "model": "browser-ui",
            "api_key_ref": "NEXUS_BROWSER_WORKER_TOKEN",
        }
    ]
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["tooling"]["browser"] == worker["tooling"]["browser"]

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    service = compose["services"]["worker-content"]
    assert service["shm_size"] == "1gb"
    assert "/srv/nexus/browser-profile:/var/lib/nexus-worker/browser-profile" in service["volumes"]

    bot_payload = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot_payload["backends"] == [
        {
            "type": "browser",
            "provider": "browser",
            "model": "browser-ui",
            "worker_id": "content-repair-01",
            "api_key_ref": "NEXUS_BROWSER_WORKER_TOKEN",
        }
    ]
    assert bot_payload["execution_policy"]["required_worker_tools"] == ["browser-ui"]


def test_render_worker_fleet_preserves_private_bot_input_contract(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["bot"]["routing_rules"] = {
        "input_contract": {
            "enabled": True,
            "format": "json_object",
            "required_fields": ["question_bank"],
            "form_fields": [{"key": "question_bank", "type": "textarea", "required": True}],
        }
    }
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    bot_payload = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot_payload["routing_rules"]["input_contract"] == {
        "enabled": True,
        "format": "json_object",
        "required_fields": ["question_bank"],
        "form_fields": [{"key": "question_bank", "type": "textarea", "required": True}],
    }
    assert bot_payload["routing_rules"]["worker_profile"]["worker_id"] == "content-repair-01"


def test_render_worker_fleet_preserves_connection_action_safety_policy(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["bot"]["execution_policy"] = {
        "connection_action_allowlist": ["globeiq-agent-api.createdraftcourseunit"],
        "connection_action_owner_approval_required": ["globeiq-agent-api.createdraftcourseunit"],
    }
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "runtime"
    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    bot_payload = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot_payload["execution_policy"]["connection_action_allowlist"] == [
        "globeiq-agent-api.createdraftcourseunit"
    ]
    assert bot_payload["execution_policy"]["connection_action_owner_approval_required"] == [
        "globeiq-agent-api.createdraftcourseunit"
    ]


def test_render_worker_fleet_rejects_private_override_of_managed_routing_fields(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["bot"]["routing_rules"] = {"worker_profile": {"can_edit": True}}
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot override renderer-managed fields"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


@pytest.mark.parametrize(
    ("browser", "backend", "message"),
    [
        (
            None,
            {"type": "browser", "provider": "browser", "model": "browser-ui", "api_key_ref": "TOKEN"},
            "without enabled tooling.browser",
        ),
        (
            {
                "enabled": True,
                "base_url": "https://admin.example.test",
                "allowed_paths": ["/admin/courses"],
                "user_data_dir": "/var/lib/nexus-worker/browser-profile",
                "request_token_env": "NEXUS_BROWSER_WORKER_TOKEN",
            },
            {"type": "browser", "provider": "browser", "model": "browser-ui"},
            "requires api_key_ref",
        ),
    ],
)
def test_render_worker_fleet_rejects_unbounded_browser_backends(tmp_path, browser, backend, message):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    worker = profile_data["workers"][0]
    if browser is not None:
        worker["tooling"] = {"browser": browser}
    worker["bot"]["backends"] = [backend]
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_warns_when_node_local_runtime_env_is_missing(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["runtime"] = {
        "env_from": {"ANTHROPIC_AUTH_TOKEN": "CLAUDE_GATEWAY_TOKEN"}
    }
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    summary = renderer.render(
        profile,
        tmp_path / "runtime",
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    assert summary["warnings"] == [
        "Missing node runtime env value: CLAUDE_GATEWAY_TOKEN for worker content-repair-01"
    ]


def test_render_worker_fleet_rejects_node_runtime_override_of_control_plane_settings(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["runtime"] = {
        "env_from": {"CONTROL_PLANE_API_TOKEN": "UNTRUSTED_TOKEN"}
    }
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot override the reserved worker setting CONTROL_PLANE_API_TOKEN"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_rejects_cli_command_for_an_undeclared_tool(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["tooling"] = {"cli_tools": ["claude"]}
    profile_data["workers"][0]["bot"]["backends"] = [
        {
            "type": "cli",
            "provider": "cli",
            "model": "claude",
            "command": "git status",
        }
    ]
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="is not declared in tooling.cli_tools"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_default_restart_policy_is_unless_stopped(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    out = tmp_path / "runtime"

    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert compose["services"]["worker-content"]["restart"] == "unless-stopped"
    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["restart_policy"] == "auto"


def test_render_worker_fleet_fleet_level_restart_policy_manual(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["restart_policy"] = "manual"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")
    out = tmp_path / "runtime"

    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert compose["services"]["worker-content"]["restart"] == "no"
    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["restart_policy"] == "manual"
    summary = json.loads((out / "summary.json").read_text())
    assert summary["workers"][0]["restart_policy"] == "manual"


def test_render_worker_fleet_worker_level_restart_policy_overrides_fleet(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["restart_policy"] = "manual"
    profile_data["workers"][0]["restart_policy"] = "always"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")
    out = tmp_path / "runtime"

    renderer.render(
        profile,
        out,
        {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
    )

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
    assert compose["services"]["worker-content"]["restart"] == "always"
    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["restart_policy"] == "always"


def test_render_worker_fleet_rejects_invalid_fleet_restart_policy(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["fleet"]["restart_policy"] = "on-failure"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="fleet.restart_policy must be one of"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )


def test_render_worker_fleet_rejects_invalid_worker_restart_policy(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")
    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_data["workers"][0]["restart_policy"] = "unless-stopped"
    profile.write_text(yaml.safe_dump(profile_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="restart_policy must be one of"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token", "OLLAMA_API_KEY": "ollama-token"},
        )
