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

    compose = yaml.safe_load((out / "docker-compose.worker-node.generated.yml").read_text())
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

    env_text = (out / "env" / "content-repair-01.env").read_text()
    assert "CONTROL_PLANE_API_TOKEN=control-token" in env_text
    assert "OLLAMA_API_KEY=ollama-token" in env_text

    bot = json.loads((out / "bots" / "content-repair-01.bot.json").read_text())
    assert bot["id"] == "content-worker-bot"
    assert bot["backends"][0]["type"] == "remote_llm"
    assert bot["backends"][0]["worker_id"] == "content-repair-01"
    assert bot["backends"][0]["provider"] == "ollama_cloud"
    assert bot["routing_rules"]["worker_profile"]["course_scope"] == ["60"]


def test_render_worker_fleet_requires_ollama_key_by_default(tmp_path):
    renderer = _load_renderer()
    profile = _profile(tmp_path / "workers.yaml")

    with pytest.raises(ValueError, match="Missing Ollama Cloud API key"):
        renderer.render(
            profile,
            tmp_path / "runtime",
            {"CONTROL_PLANE_API_TOKEN": "control-token"},
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
