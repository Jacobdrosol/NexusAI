"""SQLite-backed store for ticket/issue sources and their fetched items.

Each project can have multiple ticket sources (GitHub Issues, generic HTTP,
Jira, Asana).  The store tracks source configs, credential references, poll
status, and a dedup table of items that have already been seen and optionally
linked to tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

_DEFAULT_DB_PATH = os.path.join("data", "nexusai.db")

_CREATE_SOURCES = """
CREATE TABLE IF NOT EXISTS ticket_sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    credential_key_ref TEXT,
    enabled INTEGER DEFAULT 1,
    last_polled_at TEXT,
    last_poll_status TEXT,
    last_poll_error TEXT,
    last_poll_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS ticket_source_items (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    body TEXT,
    url TEXT,
    state TEXT,
    labels TEXT,
    author TEXT,
    raw TEXT,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    manager_bot_id TEXT,
    assigned_at TEXT,
    completed_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, external_id)
)
"""

_CREATE_IDX_SOURCES_PROJECT = "CREATE INDEX IF NOT EXISTS idx_ticket_sources_project ON ticket_sources (project_id)"
_CREATE_IDX_ITEMS_SOURCE = "CREATE INDEX IF NOT EXISTS idx_ticket_items_source ON ticket_source_items (source_id)"
_CREATE_IDX_ITEMS_TASK = "CREATE INDEX IF NOT EXISTS idx_ticket_items_task ON ticket_source_items (task_id)"
_CREATE_IDX_ITEMS_STATUS = "CREATE INDEX IF NOT EXISTS idx_ticket_items_status ON ticket_source_items (status, manager_bot_id)"

# Item lifecycle statuses
ITEM_STATUS_PENDING = "pending"
ITEM_STATUS_IGNORED = "ignored"
ITEM_STATUS_ASSIGNED = "assigned"
ITEM_STATUS_DONE = "done"
_VALID_ITEM_STATUSES = {ITEM_STATUS_PENDING, ITEM_STATUS_IGNORED, ITEM_STATUS_ASSIGNED, ITEM_STATUS_DONE}


class TicketSourceStore:
    """Async SQLite store for ticket sources and their items."""

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False
        if db_path:
            self._db_path = db_path
        else:
            url = os.environ.get("DATABASE_URL", "")
            if url.startswith("sqlite:///"):
                self._db_path = url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(_CREATE_SOURCES)
                await db.execute(_CREATE_ITEMS)
                await db.execute(_CREATE_IDX_SOURCES_PROJECT)
                await db.execute(_CREATE_IDX_ITEMS_SOURCE)
                await db.execute(_CREATE_IDX_ITEMS_TASK)
                await db.execute(_CREATE_IDX_ITEMS_STATUS)
                await self._ensure_item_columns(db)
                await db.commit()
            self._db_ready = True

    async def _ensure_item_columns(self, db: aiosqlite.Connection) -> None:
        """Add lifecycle columns to ticket_source_items for existing DBs."""
        db.row_factory = aiosqlite.Row
        async with db.execute("PRAGMA table_info(ticket_source_items)") as cursor:
            rows = await cursor.fetchall()
        columns = {str(row["name"]): row for row in rows}
        if not columns:
            return
        if "status" not in columns:
            await db.execute("ALTER TABLE ticket_source_items ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if "manager_bot_id" not in columns:
            await db.execute("ALTER TABLE ticket_source_items ADD COLUMN manager_bot_id TEXT")
        if "assigned_at" not in columns:
            await db.execute("ALTER TABLE ticket_source_items ADD COLUMN assigned_at TEXT")
        if "completed_at" not in columns:
            await db.execute("ALTER TABLE ticket_source_items ADD COLUMN completed_at TEXT")
        # Backfill existing unlinked items to 'pending' if status default didn't apply.
        await db.execute(
            "UPDATE ticket_source_items SET status = 'pending' WHERE status IS NULL OR status = ''"
        )

    # ------------------------------------------------------------------
    #  Helper
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_source(row: aiosqlite.Row) -> Dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d.pop("enabled", 0))
        d["config"] = json.loads(d.get("config") or "{}")
        return d

    @staticmethod
    def _row_to_item(row: aiosqlite.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("labels"):
            try:
                d["labels"] = json.loads(d["labels"])
            except (json.JSONDecodeError, TypeError):
                d["labels"] = []
        else:
            d["labels"] = []
        if d.get("raw"):
            try:
                d["raw"] = json.loads(d["raw"])
            except (json.JSONDecodeError, TypeError):
                pass
        d["status"] = str(d.get("status") or "pending")
        return d

    # ------------------------------------------------------------------
    #  Source CRUD
    # ------------------------------------------------------------------
    async def create_source(
        self,
        *,
        project_id: str,
        name: str,
        source_type: str,
        config: Optional[Dict[str, Any]] = None,
        credential_key_ref: Optional[str] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        source_id = str(uuid.uuid4())
        now = self._now()
        config_json = json.dumps(config or {}, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO ticket_sources
                       (id, project_id, name, source_type, config, credential_key_ref,
                        enabled, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        project_id,
                        name,
                        source_type,
                        config_json,
                        credential_key_ref,
                        1 if enabled else 0,
                        now,
                        now,
                    ),
                )
                await db.commit()
        return await self.get_source(source_id)

    async def get_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ticket_sources WHERE id = ?", (source_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_source(row)

    async def list_sources(
        self,
        project_id: Optional[str] = None,
        enabled_only: bool = False,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        clauses: List[str] = []
        params: List[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if enabled_only:
            clauses.append("enabled = 1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM ticket_sources{where} ORDER BY created_at ASC", params
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_source(r) for r in rows]

    async def update_source(
        self,
        source_id: str,
        *,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credential_key_ref: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if config is not None:
            sets.append("config = ?")
            params.append(json.dumps(config, sort_keys=True, separators=(",", ":")))
        if credential_key_ref is not None:
            sets.append("credential_key_ref = ?")
            params.append(credential_key_ref)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if not sets:
            return await self.get_source(source_id)
        sets.append("updated_at = ?")
        params.append(self._now())
        params.append(source_id)
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    f"UPDATE ticket_sources SET {', '.join(sets)} WHERE id = ?", params
                )
                await db.commit()
        return await self.get_source(source_id)

    async def delete_source(self, source_id: str) -> bool:
        await self._ensure_db()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("DELETE FROM ticket_source_items WHERE source_id = ?", (source_id,))
                cursor = await db.execute("DELETE FROM ticket_sources WHERE id = ?", (source_id,))
                await db.commit()
        return cursor.rowcount > 0

    async def record_poll(
        self,
        source_id: str,
        *,
        status: str,
        item_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        await self._ensure_db()
        now = self._now()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """UPDATE ticket_sources SET
                       last_polled_at = ?, last_poll_status = ?,
                       last_poll_error = ?, last_poll_count = ?, updated_at = ?
                       WHERE id = ?""",
                    (now, status, error, item_count, now, source_id),
                )
                await db.commit()

    # ------------------------------------------------------------------
    #  Item CRUD + dedup
    # ------------------------------------------------------------------
    async def upsert_item(
        self,
        *,
        source_id: str,
        external_id: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        url: Optional[str] = None,
        state: Optional[str] = None,
        labels: Optional[List[str]] = None,
        author: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Insert or update a ticket item.  Returns the item row (new or existing)."""
        await self._ensure_db()
        existing = await self.get_item_by_external_id(source_id, external_id)
        now = self._now()
        labels_json = json.dumps(labels or [])
        raw_json = json.dumps(raw or {}, sort_keys=True, separators=(",", ":")) if raw else None
        if existing:
            item_id = existing["id"]
            async with self._lock:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute(
                        """UPDATE ticket_source_items SET
                           title = COALESCE(?, title),
                           body = COALESCE(?, body),
                           url = COALESCE(?, url),
                           state = COALESCE(?, state),
                           labels = COALESCE(?, labels),
                           author = COALESCE(?, author),
                           raw = COALESCE(?, raw),
                           updated_at = ?
                           WHERE id = ?""",
                        (title, body, url, state, labels_json, author, raw_json, now, item_id),
                    )
                    await db.commit()
            return await self.get_item_by_external_id(source_id, external_id)  # type: ignore[return-value]
        item_id = str(uuid.uuid4())
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO ticket_source_items
                       (id, source_id, external_id, title, body, url, state,
                        labels, author, raw, task_id, status, first_seen_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)""",
                    (
                        item_id,
                        source_id,
                        external_id,
                        title,
                        body,
                        url,
                        state,
                        labels_json,
                        author,
                        raw_json,
                        now,
                        now,
                    ),
                )
                await db.commit()
        return await self.get_item_by_external_id(source_id, external_id)  # type: ignore[return-value]

    async def get_item_by_external_id(
        self, source_id: str, external_id: str
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ticket_source_items WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_item(row) if row else None

    async def list_items(
        self,
        source_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        unlinked_only: bool = False,
        status: Optional[str] = None,
        manager_bot_id: Optional[str] = None,
        manager_unassigned_ok: bool = False,
    ) -> List[Dict[str, Any]]:
        """List items with optional filters.

        When manager_bot_id is set and manager_unassigned_ok is True, returns
        items whose manager is either NULL or equal to manager_bot_id (i.e.
        unassigned items are still available to that bot's schedule).
        """
        await self._ensure_db()
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        clause = "source_id = ?"
        params: List[Any] = [source_id]
        if unlinked_only:
            clause += " AND task_id IS NULL"
        if status:
            clause += " AND status = ?"
            params.append(status)
        if manager_bot_id:
            if manager_unassigned_ok:
                clause += " AND (manager_bot_id IS NULL OR manager_bot_id = ?)"
            else:
                clause += " AND manager_bot_id = ?"
            params.append(manager_bot_id)
        params.extend([limit, offset])
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM ticket_source_items WHERE {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_item(r) for r in rows]

    async def update_item_status(
        self,
        source_id: str,
        external_id: str,
        *,
        status: str,
        task_id: Optional[str] = None,
        clear_task: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Transition an item to a new lifecycle status.

        - status='assigned' sets assigned_at and (optionally) links a task.
        - status='done' sets completed_at.
        - status='pending'/'ignored' clears assigned/completed timestamps.
        """
        if status not in _VALID_ITEM_STATUSES:
            raise ValueError(f"invalid item status: {status}")
        await self._ensure_db()
        now = self._now()
        sets = ["status = ?", "updated_at = ?"]
        params: List[Any] = [status, now]
        if status == ITEM_STATUS_ASSIGNED:
            sets.append("assigned_at = ?")
            params.append(now)
            sets.append("completed_at = NULL")
            if task_id:
                sets.append("task_id = ?")
                params.append(task_id)
        elif status == ITEM_STATUS_DONE:
            sets.append("completed_at = ?")
            params.append(now)
        else:
            sets.append("assigned_at = NULL")
            sets.append("completed_at = NULL")
            if clear_task:
                sets.append("task_id = NULL")
        params.extend([source_id, external_id])
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    f"UPDATE ticket_source_items SET {', '.join(sets)} WHERE source_id = ? AND external_id = ?",
                    params,
                )
                await db.commit()
        return await self.get_item_by_external_id(source_id, external_id)

    async def set_item_manager(
        self,
        source_id: str,
        external_id: str,
        manager_bot_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Assign (or clear) the manager bot responsible for this item."""
        await self._ensure_db()
        now = self._now()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "UPDATE ticket_source_items SET manager_bot_id = ?, updated_at = ? WHERE source_id = ? AND external_id = ?",
                    (manager_bot_id, now, source_id, external_id),
                )
                await db.commit()
        return await self.get_item_by_external_id(source_id, external_id)

    async def link_item_to_task(
        self, source_id: str, external_id: str, task_id: str
    ) -> bool:
        await self._ensure_db()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    """UPDATE ticket_source_items SET task_id = ?, status = ?, assigned_at = ?, updated_at = ?
                       WHERE source_id = ? AND external_id = ?""",
                    (task_id, ITEM_STATUS_ASSIGNED, self._now(), self._now(), source_id, external_id),
                )
                await db.commit()
        return cursor.rowcount > 0

    async def get_item_by_task_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ticket_source_items WHERE task_id = ?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_item(row) if row else None

    async def count_items(self, source_id: str) -> int:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM ticket_source_items WHERE source_id = ?", (source_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0