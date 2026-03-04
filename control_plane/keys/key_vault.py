import asyncio
import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from shared.exceptions import APIKeyNotFoundError

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_API_KEYS = """
CREATE TABLE IF NOT EXISTS api_keys (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class KeyVault:
    def __init__(self, db_path: Optional[str] = None, master_key: Optional[str] = None) -> None:
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False

        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            if db_url.startswith("sqlite:///"):
                self._db_path = db_url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

        self._fernet = Fernet(self._derive_fernet_key(master_key))

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(_CREATE_API_KEYS)
                await db.commit()
            self._db_ready = True

    def _derive_fernet_key(self, explicit_key: Optional[str]) -> bytes:
        seed = (
            explicit_key
            or os.environ.get("NEXUS_MASTER_KEY")
            or os.environ.get("NEXUSAI_SECRET_KEY")
            or "nexusai-dev-insecure-default-key"
        )
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def _encrypt(self, raw: str) -> str:
        return self._fernet.encrypt(raw.encode("utf-8")).decode("utf-8")

    def _decrypt(self, encrypted: str) -> str:
        try:
            return self._fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken as e:
            raise ValueError("Stored API key could not be decrypted with current master key") from e

    async def set_key(self, name: str, provider: str, value: str) -> None:
        await self._ensure_db()
        if not value.strip():
            raise ValueError("API key value cannot be empty")
        now = datetime.now(timezone.utc).isoformat()
        encrypted = self._encrypt(value.strip())
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO api_keys (name, provider, encrypted_value, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        provider = excluded.provider,
                        encrypted_value = excluded.encrypted_value,
                        updated_at = excluded.updated_at
                    """,
                    (name, provider, encrypted, now, now),
                )
                await db.commit()

    async def get_key(self, name: str) -> Dict[str, str]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name, provider, created_at, updated_at FROM api_keys WHERE name = ?",
                (name,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    raise APIKeyNotFoundError(f"API key not found: {name}")
                return dict(row)

    async def list_keys(self) -> List[Dict[str, str]]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name, provider, created_at, updated_at FROM api_keys ORDER BY name ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def delete_key(self, name: str) -> None:
        await self._ensure_db()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                cur = await db.execute("DELETE FROM api_keys WHERE name = ?", (name,))
                await db.commit()
                if cur.rowcount == 0:
                    raise APIKeyNotFoundError(f"API key not found: {name}")

    async def get_secret(self, name: str) -> str:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT encrypted_value FROM api_keys WHERE name = ?",
                (name,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    raise APIKeyNotFoundError(f"API key not found: {name}")
                return self._decrypt(row["encrypted_value"])
