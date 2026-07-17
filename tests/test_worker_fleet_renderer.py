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
    assert compose["networks"]["nexus-net"]["name"] == "nexusai_nexus-net"

    worker_cfg = yaml.safe_load((out / "workers" / "content-repair-01.yaml").read_text())
    assert worker_cfg["host"] == "worker-content"
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
