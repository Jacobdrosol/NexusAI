# Keys — `control_plane/keys/`

Encrypted API key storage for NexusAI. Keys are encrypted with
[Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption
before being written to SQLite, so the database file alone is not sufficient
to recover any plaintext key.

---

## Files

| File | Purpose |
|---|---|
| `key_vault.py` | `KeyVault` class — encrypt/decrypt, CRUD operations |

---

## Encryption

`KeyVault` uses **Fernet** (AES-128-CBC + HMAC-SHA256) from the `cryptography`
package. The Fernet key is derived deterministically from a master secret:

### Master key resolution (priority order)

1. Explicit `master_key` argument passed to `KeyVault()`
2. `NEXUS_MASTER_KEY` environment variable
3. `NEXUSAI_SECRET_KEY` environment variable
4. Hard-coded fallback: `"nexusai-dev-insecure-default-key"` ⚠️

### Key derivation

```
fernet_key = base64url(SHA-256(master_secret.encode("utf-8")))
```

SHA-256 is used to normalise the master secret to exactly 32 bytes, which
Fernet requires as a 32-byte URL-safe base64-encoded key.

> **Warning:** The hard-coded default key is intentionally insecure. Always
> set `NEXUS_MASTER_KEY` or `NEXUSAI_SECRET_KEY` in production.

---

## SQLite Schema

Table: **`api_keys`** (in `data/nexusai.db` by default)

| Column | Type | Description |
|---|---|---|
| `name` | `TEXT PRIMARY KEY` | Logical key name (user-chosen, e.g. `"openai-prod"`) |
| `provider` | `TEXT NOT NULL` | Provider label (e.g. `"openai"`, `"anthropic"`) |
| `encrypted_value` | `TEXT NOT NULL` | Fernet-encrypted API key string |
| `created_at` | `TEXT NOT NULL` | UTC ISO-8601 creation timestamp |
| `updated_at` | `TEXT NOT NULL` | UTC ISO-8601 last-update timestamp |

The plaintext key value is **never** stored. `get_key` / `list_keys` return
metadata only; callers must use `get_secret` to obtain the decrypted value.

---

## How `BackendConfig.api_key_ref` Resolves to an Actual Key

`BackendConfig` (in `shared/models.py`) has an `api_key_ref` field that holds
a logical key name (e.g. `"openai-prod"`). At task dispatch time the scheduler
or worker agent calls:

```python
vault = KeyVault()
plaintext = await vault.get_secret(backend_config.api_key_ref)
```

`get_secret` looks up the row by name and decrypts `encrypted_value` with the
Fernet instance built from the current master key. The plaintext is then
injected into the outbound LLM API request header.

---

## `key_vault.py` — `KeyVault` class

### Constructor

```python
KeyVault(db_path: Optional[str] = None, master_key: Optional[str] = None)
```

Resolves `db_path` with the same priority order as `AuditLog`:

1. Explicit argument
2. `DATABASE_URL` env var (`sqlite:///` prefix stripped)
3. Default `<repo_root>/data/nexusai.db`

Immediately constructs the `Fernet` instance — key derivation errors surface
at construction time, not at first use.

Uses two `asyncio.Lock` objects:
- `_init_lock` — schema creation (double-checked locking)
- `_lock` — write serialisation

### Methods

#### `async _ensure_db() → None`

Lazily creates the `api_keys` table on first use.

#### `_derive_fernet_key(explicit_key: Optional[str]) → bytes`

Applies the master key resolution order described above and returns a 32-byte
URL-safe base64 Fernet key.

#### `_encrypt(raw: str) → str`

Encrypts a UTF-8 string with Fernet and returns the token as a UTF-8 string.

#### `_decrypt(encrypted: str) → str`

Decrypts a Fernet token. Raises `ValueError` (wrapping `InvalidToken`) if the
ciphertext cannot be decrypted with the current master key — this happens when
the master key has been changed after keys were stored.

#### `async set_key(name, provider, value) → None`

```python
async def set_key(name: str, provider: str, value: str) -> None
```

Upserts an API key. Uses SQLite `ON CONFLICT(name) DO UPDATE` so calling
`set_key` on an existing name overwrites the value and `updated_at` without
changing `created_at`. Raises `ValueError` if `value` is blank.

#### `async get_key(name) → Dict[str, str]`

```python
async def get_key(name: str) -> Dict[str, str]
```

Returns `{name, provider, created_at, updated_at}` — **no** `encrypted_value`.
Raises `APIKeyNotFoundError` if the key does not exist.

#### `async list_keys() → List[Dict[str, str]]`

Returns metadata for all keys sorted by `name` ascending. No plaintext values
are included.

#### `async delete_key(name) → None`

```python
async def delete_key(name: str) -> None
```

Deletes the row. Raises `APIKeyNotFoundError` if nothing was deleted
(`rowcount == 0`).

#### `async get_secret(name) → str`

```python
async def get_secret(name: str) -> str
```

The only method that returns a plaintext key. Fetches `encrypted_value` and
decrypts it. Raises `APIKeyNotFoundError` if the key does not exist, and
`ValueError` (from `_decrypt`) if decryption fails.

---

## Known Issues

- **Master key rotation is not supported** — changing `NEXUS_MASTER_KEY` after
  keys have been stored makes all existing keys unreadable (`_decrypt` raises
  `ValueError`). There is no re-encryption utility.
- **Hard-coded insecure default** — the fallback `"nexusai-dev-insecure-default-key"`
  is used in development but easy to forget in production.
- **No key expiry** — `api_keys` has no TTL or expiry column; keys live forever
  unless explicitly deleted.
- **In-process write lock only** — `_lock` is an `asyncio.Lock`; concurrent
  writes from multiple processes are not protected beyond SQLite's own
  file-level locking.
- **Fernet key length fixed at SHA-256** — if a master key shorter than 32
  bytes is supplied it is hashed rather than used directly, which may be
  surprising for callers who provide a pre-derived key.
