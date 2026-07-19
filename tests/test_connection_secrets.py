from shared.connection_secrets import (
    REDACTED_VALUE,
    mask_auth_payload,
    mask_connection_config,
    normalize_auth_payload,
    normalize_connection_config,
    resolve_auth_payload,
    resolve_connection_config,
)


def test_auth_secrets_are_encrypted_redacted_and_preserved_from_portable_export():
    stored = normalize_auth_payload({"type": "api_key", "api_key": "private-token"})

    assert stored["api_key"].startswith("enc:")
    assert "private-token" not in stored["api_key"]
    assert mask_auth_payload(stored)["api_key"] == REDACTED_VALUE
    assert resolve_auth_payload(stored)["api_key"] == "private-token"

    reimported = normalize_auth_payload({"type": "api_key", "api_key": REDACTED_VALUE}, existing=stored)
    assert reimported["api_key"] == stored["api_key"]


def test_connection_config_encrypts_dsn_and_headers_without_exposing_them():
    stored = normalize_connection_config(
        {
            "dsn": "postgresql://agent:private-password@example.test/nexusai",
            "headers": {"Authorization": "Bearer private-token", "Accept": "application/json"},
            "base_url": "https://api.example.test",
        }
    )

    assert stored["dsn"].startswith("enc:")
    assert stored["headers"]["Authorization"].startswith("enc:")
    assert stored["headers"]["Accept"].startswith("enc:")
    assert "private-password" not in str(stored)
    assert "private-token" not in str(stored)

    masked = mask_connection_config(stored)
    assert masked["dsn"] == REDACTED_VALUE
    assert masked["headers"]["Authorization"] == REDACTED_VALUE
    assert masked["headers"]["Accept"] == REDACTED_VALUE
    assert masked["base_url"] == "https://api.example.test"

    resolved = resolve_connection_config(stored)
    assert resolved["dsn"].endswith("/nexusai")
    assert resolved["headers"]["Authorization"] == "Bearer private-token"


def test_redacted_config_preserves_a_matching_existing_secret():
    existing = normalize_connection_config({"dsn": "sqlite:////var/lib/nexusai/private.db"})
    imported = normalize_connection_config({"dsn": REDACTED_VALUE, "readonly": True}, existing=existing)

    assert imported["dsn"] == existing["dsn"]
    assert imported["readonly"] is True
