"""Unit tests for KeyVault."""

import pytest

from shared.exceptions import APIKeyNotFoundError


@pytest.mark.anyio
async def test_set_and_get_secret(tmp_path):
    from control_plane.keys.key_vault import KeyVault

    vault = KeyVault(db_path=str(tmp_path / "keys.db"), master_key="test-master-key")
    await vault.set_key(name="openai-dev", provider="openai", value="sk-test")

    secret = await vault.get_secret("openai-dev")
    meta = await vault.get_key("openai-dev")
    assert secret == "sk-test"
    assert meta["provider"] == "openai"


@pytest.mark.anyio
async def test_delete_key(tmp_path):
    from control_plane.keys.key_vault import KeyVault

    vault = KeyVault(db_path=str(tmp_path / "keys.db"), master_key="test-master-key")
    await vault.set_key(name="claude-dev", provider="claude", value="ak-test")
    await vault.delete_key("claude-dev")

    with pytest.raises(APIKeyNotFoundError):
        await vault.get_key("claude-dev")


@pytest.mark.anyio
async def test_master_key_mismatch_fails_decrypt(tmp_path):
    from control_plane.keys.key_vault import KeyVault

    db_path = str(tmp_path / "keys.db")
    vault_1 = KeyVault(db_path=db_path, master_key="master-a")
    await vault_1.set_key(name="gemini-dev", provider="gemini", value="gk-test")

    vault_2 = KeyVault(db_path=db_path, master_key="master-b")
    with pytest.raises(ValueError):
        await vault_2.get_secret("gemini-dev")
