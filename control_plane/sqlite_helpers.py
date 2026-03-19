from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite


SQLITE_BUSY_TIMEOUT_MS = 30000


@asynccontextmanager
async def open_sqlite(db_path: str, *, foreign_keys: bool = False) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(db_path, timeout=max(1.0, SQLITE_BUSY_TIMEOUT_MS / 1000.0)) as db:
        await db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        if foreign_keys:
            await db.execute("PRAGMA foreign_keys = ON")
        yield db
