import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_AUDIT_EVENTS = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL
)
"""


class AuditLog:
    def __init__(self, db_path: Optional[str] = None) -> None:
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

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(_CREATE_AUDIT_EVENTS)
                await db.commit()
            self._db_ready = True

    async def record(
        self,
        action: str,
        resource: str,
        status: str = "ok",
        actor: Optional[str] = None,
        details: Optional[Any] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        row = {
            "id": str(uuid.uuid4()),
            "actor": actor,
            "action": action,
            "resource": resource,
            "status": status,
            "details": details,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO audit_events (id, actor, action, resource, status, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["actor"],
                        row["action"],
                        row["resource"],
                        row["status"],
                        json.dumps(row["details"]) if row["details"] is not None else None,
                        row["created_at"],
                    ),
                )
                await db.commit()
        return row

    async def list_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 1000))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, actor, action, resource, status, details, created_at
                FROM audit_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                out: List[Dict[str, Any]] = []
                for row in rows:
                    data = dict(row)
                    if data.get("details"):
                        data["details"] = json.loads(data["details"])
                    out.append(data)
                return out

