import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_bot_exports.py"
spec = importlib.util.spec_from_file_location("validate_bot_exports", SCRIPT)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = validator
spec.loader.exec_module(validator)


def _bot_export(bot_id: str, *, extra=None):
    bot = {
        "id": bot_id,
        "name": bot_id,
        "role": "assistant",
        "enabled": True,
        "backends": [{"type": "ollama_cloud", "provider": "ollama_cloud", "model": "qwen3.5:397b"}],
        "routing_rules": {"output_contract": {"format": "plain_text"}},
    }
    if extra:
        bot.update(extra)
    return {"schema_version": "nexusai.bot-export.v1", "bot": bot}


def test_bot_export_validator_accepts_safe_credential_refs(capsys):
    exports = [
        validator._load_export_from_raw(
            _bot_export(
                "chat-safe",
                extra={
                    "connections": [
                        {
                            "name": "provider",
                            "auth": {"api_key": "OLLAMA_CLOUD_API_KEY", "token": "vault:provider-token"},
                        }
                    ]
                },
            ),
            Path("chat-safe.bot.json"),
        )
    ]

    assert validator._validate(exports, [], False, False, []) == 0
    assert "OK: export set validated" in capsys.readouterr().out


def test_bot_export_validator_blocks_duplicate_ids_and_raw_secret_values(tmp_path, capsys):
    secret_value = "sk-" + "1234567890abcdef1234567890"
    secret_export = _bot_export(
        "chat-secret",
        extra={"connections": [{"name": "provider", "auth": {"api_key": secret_value}}]},
    )
    first = tmp_path / "first.bot.json"
    second = tmp_path / "second.bot.json"
    first.write_text(json.dumps(_bot_export("chat-secret")), encoding="utf-8")
    second.write_text(json.dumps(secret_export), encoding="utf-8")

    exports = [validator._load_export(first), validator._load_export(second)]

    assert validator._validate(exports, [], False, False, []) == 1
    output = capsys.readouterr().out
    assert "duplicate bot.id 'chat-secret'" in output
    assert "contains raw secret-like values" in output
