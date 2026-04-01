# Key Vault

The key vault stores API keys (OpenAI, Claude, Gemini, etc.) encrypted at rest using Fernet symmetric encryption. Keys are referenced by name in bot backend configs via `api_key_ref`.

---

## KeyVault (`key_vault.py`)

### SQLite Table: `api_keys`

| Column | Type | Description |
|--------|------|-------------|
| `name` | TEXT PK | Logical key name (e.g., `openai-prod`, `claude-main`) |
| `provider` | TEXT | Provider label (e.g., `openai`, `claude`, `gemini`) |
| `encrypted_value` | TEXT | Fernet-encrypted API key value |
| `created_at` | TEXT | ISO 8601 UTC |
| `updated_at` | TEXT | ISO 8601 UTC |

### Encryption

Keys are encrypted with Fernet using a key derived from the master secret:

```
master_seed = NEXUS_MASTER_KEY or NEXUSAI_SECRET_KEY or "nexusai-dev-insecure-default-key"
fernet_key  = base64url(SHA-256(master_seed))
```

**⚠️ Warning**: The fallback `"nexusai-dev-insecure-default-key"` is publicly known. Always set `NEXUS_MASTER_KEY` in production.

Decryption raises `ValueError` (wrapping `cryptography.fernet.InvalidToken`) if the master key has changed since encryption.

### Key Methods

| Method | Description |
|--------|-------------|
| `set_key(name, provider, value)` | Upsert key with Fernet encryption |
| `get_key(name)` | Returns `{name, provider, created_at, updated_at}` — no decrypted value |
| `get_key_value(name)` | Returns decrypted plaintext value — used internally by scheduler |
| `list_keys()` | Returns all keys without decrypted values |
| `delete_key(name)` | Removes key |

### Usage in Bot Backends

In a `BackendConfig`, set `api_key_ref` to a key name:

```yaml
backends:
  - type: cloud_api
    provider: openai
    model: gpt-4o
    api_key_ref: openai-prod   # matches api_keys.name
```

The scheduler calls `key_vault.get_key_value("openai-prod")` at inference time to resolve the plaintext API key.

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/keys` | Upsert key (name, provider, value) |
| `GET` | `/v1/keys` | List all keys (no plaintext values returned) |
| `GET` | `/v1/keys/{name}` | Get key metadata |
| `DELETE` | `/v1/keys/{name}` | Delete key |

---

## Known Issues

- **Insecure default key** — if neither `NEXUS_MASTER_KEY` nor `NEXUSAI_SECRET_KEY` is set, the fallback key is `"nexusai-dev-insecure-default-key"`. This should emit a startup warning or fail-fast in production.
- **No key rotation support** — changing `NEXUS_MASTER_KEY` invalidates all existing encrypted keys. There is no re-encryption utility.
- **`get_key` returns no plaintext** — callers cannot verify the stored value is correct without a test inference call.
