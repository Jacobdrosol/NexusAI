import importlib.util
from pathlib import Path
import sys


def _load_setup_pm_bot_pack_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "setup_pm_bot_pack.py"
    spec = importlib.util.spec_from_file_location("setup_pm_bot_pack", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_setup_pm_bot_pack_exports_expected_models_and_triggers() -> None:
    module = _load_setup_pm_bot_pack_module()
    specs = module._pm_specs()
    bundles = {
        spec.bot_id: module._bundle_payload(spec, "Ollama_Cloud1", "repo_and_filesystem")
        for spec in specs
    }

    assert set(bundles) == {
        "pm-orchestrator",
        "pm-research-analyst",
        "pm-engineer",
        "pm-coder",
        "pm-tester",
        "pm-security-reviewer",
        "pm-database-engineer",
        "pm-ui-tester",
    }

    assert bundles["pm-orchestrator"]["bot"]["backends"][0]["model"] == "gpt-oss:120b-cloud"
    assert bundles["pm-engineer"]["bot"]["backends"][0]["model"] == "gpt-oss:120b-cloud"
    assert bundles["pm-ui-tester"]["bot"]["backends"][0]["model"] == "gpt-oss:120b-cloud"
    assert bundles["pm-research-analyst"]["bot"]["backends"][0]["model"] == "qwen3.5:397b-cloud"
    assert bundles["pm-coder"]["bot"]["backends"][0]["model"] == "qwen3.5:397b-cloud"
    assert bundles["pm-database-engineer"]["bot"]["backends"][0]["model"] == "qwen3.5:397b-cloud"

    for bundle in bundles.values():
        bot = bundle["bot"]
        assert bot["workflow"] == bot["routing_rules"]["workflow"]
        assert bot["routing_rules"]["output_contract"]["enabled"] is True
        assert bot["routing_rules"]["output_contract"]["fallback_mode"] == "disabled"

    tester_triggers = bundles["pm-tester"]["bot"]["workflow"]["triggers"]
    assert any(
        trigger["target_bot_id"] == "pm-security-reviewer" and trigger["result_equals"] == "pass"
        for trigger in tester_triggers
    )
    assert any(
        trigger["target_bot_id"] == "pm-coder" and trigger["result_equals"] == "implementation_issue"
        for trigger in tester_triggers
    )

    ui_triggers = bundles["pm-ui-tester"]["bot"]["workflow"]["triggers"]
    assert any(
        trigger["target_bot_id"] == "pm-coder" and trigger["result_equals"] == "ui_render_issue"
        for trigger in ui_triggers
    )
    assert any(
        trigger["target_bot_id"] == "pm-database-engineer" and trigger["result_equals"] == "ui_data_issue"
        for trigger in ui_triggers
    )
    assert any(
        trigger["target_bot_id"] == "pm-database-engineer" and trigger["result_equals"] == "ui_config_issue"
        for trigger in ui_triggers
    )
