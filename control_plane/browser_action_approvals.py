from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from control_plane.sqlite_helpers import open_sqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent / "data" / "nexusai.db")
_MIN_APPROVAL_TTL_SECONDS = 30
_MAX_APPROVAL_TTL_SECONDS = 900

_CREATE_BROWSER_ACTION_APPROVALS = """
CREATE TABLE IF NOT EXISTS browser_action_approvals (
    id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    action_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_browser_action_approvals_expiry "
    "ON browser_action_approvals(expires_at)",
)


def _db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def browser_action_key(payload: Dict[str, Any]) -> str:
    """Return the policy action key for a browser mutation request."""

    if not isinstance(payload, dict):
        raise ValueError("Browser action approval requires an object payload")
    browser_action = str(payload.get("browser_action") or "").strip().lower()
    action = str(payload.get("action") or "").strip().lower()
    if browser_action not in {"question_bank", "test_builder"} or not action:
        raise ValueError("Approval requests must declare a supported browser_action and action")
    return f"{browser_action}.{action}"


def browser_action_payload_digest(payload: Dict[str, Any]) -> str:
    """Hash an action request without its transport-only approval identifier."""

    if not isinstance(payload, dict):
        raise ValueError("Browser action approval requires an object payload")
    canonical_payload = dict(payload)
    canonical_payload.pop("owner_approval_id", None)
    try:
        encoded = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Browser action approval payload must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


class BrowserActionApprovalStore:
    """One-time, payload-bound approvals for high-impact browser actions."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                await db.execute(_CREATE_BROWSER_ACTION_APPROVALS)
                for statement in _CREATE_INDEXES:
                    await db.execute(statement)
                await db.commit()
            self._db_ready = True

    async def create(
        self,
        *,
        bot_id: str,
        action_key: str,
        payload: Dict[str, Any],
        expires_in_seconds: int = 300,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        ttl = int(expires_in_seconds)
        if not _MIN_APPROVAL_TTL_SECONDS <= ttl <= _MAX_APPROVAL_TTL_SECONDS:
            raise ValueError(
                "expires_in_seconds must be between "
                f"{_MIN_APPROVAL_TTL_SECONDS} and {_MAX_APPROVAL_TTL_SECONDS}"
            )
        now = datetime.now(timezone.utc)
        row = {
            "id": str(uuid.uuid4()),
            "bot_id": str(bot_id or "").strip(),
            "action_key": str(action_key or "").strip(),
            "payload_digest": browser_action_payload_digest(payload),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "consumed_at": None,
            "created_at": now.isoformat(),
        }
        if not row["bot_id"] or not row["action_key"]:
            raise ValueError("bot_id and action_key are required")
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO browser_action_approvals
                        (id, bot_id, action_key, payload_digest, expires_at, consumed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["bot_id"],
                        row["action_key"],
                        row["payload_digest"],
                        row["expires_at"],
                        row["consumed_at"],
                        row["created_at"],
                    ),
                )
                await db.commit()
        return row

    async def consume(
        self,
        *,
        approval_id: str,
        bot_id: str,
        action_key: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Consume an exact approval once. Mismatches and expired approvals fail closed."""

        await self._ensure_db()
        approval_id = str(approval_id or "").strip()
        if not approval_id:
            return False
        payload_digest = browser_action_payload_digest(payload)
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                cursor = await db.execute(
                    """
                    UPDATE browser_action_approvals
                    SET consumed_at = ?
                    WHERE id = ?
                      AND bot_id = ?
                      AND action_key = ?
                      AND payload_digest = ?
                      AND consumed_at IS NULL
                      AND expires_at > ?
                    """,
                    (
                        now,
                        approval_id,
                        str(bot_id or "").strip(),
                        str(action_key or "").strip(),
                        payload_digest,
                        now,
                    ),
                )
                await db.commit()
        return int(cursor.rowcount or 0) == 1
