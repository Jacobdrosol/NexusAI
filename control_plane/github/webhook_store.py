import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_GITHUB_WEBHOOK_EVENTS = """
CREATE TABLE IF NOT EXISTS github_webhook_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    delivery_id TEXT,
    event_type TEXT NOT NULL,
    action TEXT,
    repository_full_name TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class GitHubWebhookStore:
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
                await db.execute(_CREATE_GITHUB_WEBHOOK_EVENTS)
                await db.commit()
            self._db_ready = True

    async def record_event(
        self,
        project_id: str,
        event_type: str,
        payload: Dict[str, Any],
        delivery_id: Optional[str] = None,
        action: Optional[str] = None,
        repository_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": event_id,
            "project_id": project_id,
            "delivery_id": delivery_id,
            "event_type": event_type,
            "action": action,
            "repository_full_name": repository_full_name,
            "payload": payload,
            "created_at": now,
        }
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO github_webhook_events
                        (id, project_id, delivery_id, event_type, action, repository_full_name, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["project_id"],
                        row["delivery_id"],
                        row["event_type"],
                        row["action"],
                        row["repository_full_name"],
                        json.dumps(row["payload"]),
                        row["created_at"],
                    ),
                )
                await db.commit()
        return row

    async def list_events(self, project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, project_id, delivery_id, event_type, action, repository_full_name, payload, created_at
                FROM github_webhook_events
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                out: List[Dict[str, Any]] = []
                for row in rows:
                    data = dict(row)
                    data["payload"] = json.loads(data["payload"]) if data.get("payload") else {}
                    out.append(data)
                return out
