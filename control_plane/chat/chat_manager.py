import asyncio
import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from control_plane.vault.chunker import chunk_text
from shared.exceptions import ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")


def _normalize_tool_access_flags(
    *,
    enabled: Any,
    filesystem: Any,
    repo_search: Any,
) -> tuple[bool, bool, bool]:
    normalized_enabled = bool(enabled)
    return (
        normalized_enabled,
        bool(filesystem) if normalized_enabled else False,
        bool(repo_search) if normalized_enabled else False,
    )

_CREATE_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project_id TEXT,
    bridge_project_ids TEXT,
    scope TEXT NOT NULL,
    default_bot_id TEXT,
    default_model_id TEXT,
    owner_user_id TEXT,
    memory_profiles_enabled INTEGER NOT NULL DEFAULT 1,
    memory_profile_id TEXT,
    tool_access_enabled INTEGER NOT NULL DEFAULT 0,
    tool_access_filesystem INTEGER NOT NULL DEFAULT 0,
    tool_access_repo_search INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    bot_id TEXT,
    model TEXT,
    provider TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
)
"""

_CREATE_MESSAGES_CONVERSATION_CREATED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created_at
ON messages(conversation_id, created_at)
"""

_CREATE_MESSAGE_MEMORY = """
CREATE TABLE IF NOT EXISTS chat_message_memory (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
)
"""

_CREATE_MESSAGE_MEMORY_CONVERSATION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chat_message_memory_conversation
ON chat_message_memory(conversation_id, created_at)
"""

_CREATE_MESSAGE_MEMORY_MESSAGE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chat_message_memory_message
ON chat_message_memory(message_id)
"""

_CREATE_MEMORY_PROFILE_ITEMS = """
CREATE TABLE IF NOT EXISTS memory_profile_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, profile_id, message_id)
)
"""

_CREATE_MEMORY_PROFILE_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_memory_profile_items_user_profile
ON memory_profile_items(user_id, profile_id, created_at)
"""

_CREATE_CONVERSATIONS_ARCHIVED_UPDATED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_conversations_archived_updated_at
ON conversations(archived_at, updated_at)
"""


class ChatManager:
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
            async with open_sqlite(self._db_path, foreign_keys=True) as db:
                await db.execute(_CREATE_CONVERSATIONS)
                await db.execute(_CREATE_MESSAGES)
                await db.execute(_CREATE_MESSAGES_CONVERSATION_CREATED_INDEX)
                await db.execute(_CREATE_MESSAGE_MEMORY)
                await db.execute(_CREATE_MESSAGE_MEMORY_CONVERSATION_INDEX)
                await db.execute(_CREATE_MESSAGE_MEMORY_MESSAGE_INDEX)
                await db.execute(_CREATE_MEMORY_PROFILE_ITEMS)
                await db.execute(_CREATE_MEMORY_PROFILE_USER_INDEX)
                await db.execute(_CREATE_CONVERSATIONS_ARCHIVED_UPDATED_INDEX)
                await self._ensure_conversation_columns(db)
                await self._ensure_memory_profile_item_columns(db)
                await db.commit()
            self._db_ready = True

    def _embed(self, text: str, dims: int = 64) -> List[float]:
        vec = [0.0] * dims
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:2], "big") % dims
            sign = 1.0 if (digest[2] % 2 == 0) else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def _cosine(self, a: List[float], b: List[float]) -> float:
        if not a or not b:
            return 0.0
        return float(sum(x * y for x, y in zip(a, b)))

    def _iso_to_ts(self, value: str) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except Exception:
            return 0.0

    def _message_is_indexable(self, *, role: str, metadata: Any) -> bool:
        if str(role or "").strip().lower() not in {"user", "assistant"}:
            return False
        if not isinstance(metadata, dict):
            return True
        mode = str(metadata.get("mode") or "").strip().lower()
        if mode == "assign_error":
            return False
        if mode in {"assign_request", "assign_pending", "pm_run_report", "assign_summary"}:
            return metadata.get("ingest_allowed") is True
        return True

    async def _reindex_message(
        self,
        db: aiosqlite.Connection,
        *,
        message_id: str,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Any,
        created_at: str,
    ) -> None:
        await db.execute("DELETE FROM chat_message_memory WHERE message_id = ?", (message_id,))
        if not self._message_is_indexable(role=role, metadata=metadata):
            return
        normalized = str(content or "").strip()
        if not normalized:
            return
        chunks = chunk_text(normalized, chunk_size=800, overlap=120)
        for idx, chunk in enumerate(chunks):
            text = str(chunk or "").strip()
            if not text:
                continue
            await db.execute(
                """
                INSERT INTO chat_message_memory (
                    id, message_id, conversation_id, role, chunk_index, content, embedding, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    message_id,
                    conversation_id,
                    role,
                    idx,
                    text,
                    json.dumps(self._embed(text)),
                    created_at,
                ),
            )

    async def _ensure_conversation_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(conversations)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "archived_at" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
        if "bridge_project_ids" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN bridge_project_ids TEXT")
        if "tool_access_enabled" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN tool_access_enabled INTEGER NOT NULL DEFAULT 0")
        if "tool_access_filesystem" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN tool_access_filesystem INTEGER NOT NULL DEFAULT 0")
        if "tool_access_repo_search" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN tool_access_repo_search INTEGER NOT NULL DEFAULT 0")
        if "owner_user_id" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN owner_user_id TEXT")
        if "memory_profiles_enabled" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN memory_profiles_enabled INTEGER NOT NULL DEFAULT 1")
        if "memory_profile_id" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN memory_profile_id TEXT")

    async def _ensure_memory_profile_item_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(memory_profile_items)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "updated_at" not in columns:
            await db.execute("ALTER TABLE memory_profile_items ADD COLUMN updated_at TEXT")
            await db.execute("UPDATE memory_profile_items SET updated_at = created_at WHERE updated_at IS NULL")

    async def create_conversation(
        self,
        title: str,
        project_id: Optional[str] = None,
        bridge_project_ids: Optional[List[str]] = None,
        scope: str = "global",
        default_bot_id: Optional[str] = None,
        default_model_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        memory_profiles_enabled: bool = True,
        memory_profile_id: Optional[str] = "default",
        tool_access_enabled: bool = False,
        tool_access_filesystem: bool = False,
        tool_access_repo_search: bool = False,
    ) -> ChatConversation:
        await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        normalized_tool_enabled, normalized_tool_filesystem, normalized_tool_repo_search = _normalize_tool_access_flags(
            enabled=tool_access_enabled,
            filesystem=tool_access_filesystem,
            repo_search=tool_access_repo_search,
        )
        conversation = ChatConversation(
            id=str(uuid.uuid4()),
            title=title.strip() or "New Conversation",
            project_id=project_id,
            bridge_project_ids=list(bridge_project_ids or []),
            scope=scope,
            default_bot_id=default_bot_id,
            default_model_id=default_model_id,
            owner_user_id=str(owner_user_id or "").strip() or None,
            memory_profiles_enabled=bool(memory_profiles_enabled),
            memory_profile_id=str(memory_profile_id or "default").strip() or "default",
            tool_access_enabled=normalized_tool_enabled,
            tool_access_filesystem=normalized_tool_filesystem,
            tool_access_repo_search=normalized_tool_repo_search,
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO conversations (
                        id, title, project_id, bridge_project_ids, scope, default_bot_id, default_model_id, owner_user_id, memory_profiles_enabled, memory_profile_id, tool_access_enabled, tool_access_filesystem, tool_access_repo_search, archived_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation.id,
                        conversation.title,
                        conversation.project_id,
                        json.dumps(conversation.bridge_project_ids),
                        conversation.scope,
                        conversation.default_bot_id,
                        conversation.default_model_id,
                        conversation.owner_user_id,
                        1 if conversation.memory_profiles_enabled else 0,
                        conversation.memory_profile_id,
                        1 if conversation.tool_access_enabled else 0,
                        1 if conversation.tool_access_filesystem else 0,
                        1 if conversation.tool_access_repo_search else 0,
                        conversation.archived_at,
                        conversation.created_at,
                        conversation.updated_at,
                    ),
                )
                await db.commit()
        return conversation

    async def list_conversations(
        self,
        project_id: Optional[str] = None,
        archived: str = "active",
    ) -> List[ChatConversation]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            clauses: list[str] = []
            params: list[Any] = []
            normalized_project_id = str(project_id or "").strip()
            if archived == "active":
                clauses.append("archived_at IS NULL")
            elif archived == "archived":
                clauses.append("archived_at IS NOT NULL")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            query = f"SELECT * FROM conversations{where} ORDER BY updated_at DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                result: List[ChatConversation] = []
                for row in rows:
                    data = dict(row)
                    raw_bridges = data.get("bridge_project_ids")
                    if raw_bridges:
                        try:
                            data["bridge_project_ids"] = json.loads(raw_bridges)
                        except Exception:
                            data["bridge_project_ids"] = []
                    else:
                        data["bridge_project_ids"] = []
                    (
                        data["tool_access_enabled"],
                        data["tool_access_filesystem"],
                        data["tool_access_repo_search"],
                    ) = _normalize_tool_access_flags(
                        enabled=data.get("tool_access_enabled"),
                        filesystem=data.get("tool_access_filesystem"),
                        repo_search=data.get("tool_access_repo_search"),
                    )
                    data["memory_profiles_enabled"] = bool(data.get("memory_profiles_enabled", 1))
                    data["memory_profile_id"] = str(data.get("memory_profile_id") or "default").strip() or "default"
                    conversation = ChatConversation.model_validate(data)
                    if normalized_project_id:
                        project_ids = [str(conversation.project_id or "").strip()]
                        project_ids.extend(str(pid or "").strip() for pid in conversation.bridge_project_ids)
                        if normalized_project_id not in {pid for pid in project_ids if pid}:
                            continue
                    result.append(conversation)
                return result

    async def get_conversation(self, conversation_id: str) -> ChatConversation:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    raise ConversationNotFoundError(f"Conversation not found: {conversation_id}")
                data = dict(row)
                raw_bridges = data.get("bridge_project_ids")
                if raw_bridges:
                    try:
                        data["bridge_project_ids"] = json.loads(raw_bridges)
                    except Exception:
                        data["bridge_project_ids"] = []
                else:
                    data["bridge_project_ids"] = []
                (
                    data["tool_access_enabled"],
                    data["tool_access_filesystem"],
                    data["tool_access_repo_search"],
                ) = _normalize_tool_access_flags(
                    enabled=data.get("tool_access_enabled"),
                    filesystem=data.get("tool_access_filesystem"),
                    repo_search=data.get("tool_access_repo_search"),
                )
                data["memory_profiles_enabled"] = bool(data.get("memory_profiles_enabled", 1))
                data["memory_profile_id"] = str(data.get("memory_profile_id") or "default").strip() or "default"
                return ChatConversation.model_validate(data)

    async def delete_conversation(self, conversation_id: str) -> None:
        conversation = await self.get_conversation(conversation_id)
        if not conversation.archived_at:
            raise ValueError("conversation must be archived before deletion")
        async with self._lock:
            async with open_sqlite(self._db_path, foreign_keys=True) as db:
                await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                await db.commit()

    async def archive_conversation(self, conversation_id: str) -> ChatConversation:
        conversation = await self.get_conversation(conversation_id)
        if conversation.archived_at:
            return conversation
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    "UPDATE conversations SET archived_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, conversation_id),
                )
                await db.commit()
        return await self.get_conversation(conversation_id)

    async def restore_conversation(self, conversation_id: str) -> ChatConversation:
        conversation = await self.get_conversation(conversation_id)
        if not conversation.archived_at:
            return conversation
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    "UPDATE conversations SET archived_at = NULL, updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                await db.commit()
        return await self.get_conversation(conversation_id)

    async def update_conversation_tool_access(
        self,
        conversation_id: str,
        *,
        tool_access_enabled: bool,
        tool_access_filesystem: bool,
        tool_access_repo_search: bool,
    ) -> ChatConversation:
        await self.get_conversation(conversation_id)
        now = datetime.now(timezone.utc).isoformat()
        normalized_tool_enabled, normalized_tool_filesystem, normalized_tool_repo_search = _normalize_tool_access_flags(
            enabled=tool_access_enabled,
            filesystem=tool_access_filesystem,
            repo_search=tool_access_repo_search,
        )
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE conversations
                    SET tool_access_enabled = ?, tool_access_filesystem = ?, tool_access_repo_search = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        1 if normalized_tool_enabled else 0,
                        1 if normalized_tool_filesystem else 0,
                        1 if normalized_tool_repo_search else 0,
                        now,
                        conversation_id,
                    ),
                )
                await db.commit()
        return await self.get_conversation(conversation_id)

    async def update_conversation_memory_profile(
        self,
        conversation_id: str,
        *,
        memory_profiles_enabled: bool,
        memory_profile_id: Optional[str] = "default",
    ) -> ChatConversation:
        await self.get_conversation(conversation_id)
        now = datetime.now(timezone.utc).isoformat()
        profile_id = str(memory_profile_id or "default").strip() or "default"
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE conversations
                    SET memory_profiles_enabled = ?, memory_profile_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        1 if memory_profiles_enabled else 0,
                        profile_id,
                        now,
                        conversation_id,
                    ),
                )
                await db.commit()
        return await self.get_conversation(conversation_id)

    async def update_conversation_route_defaults(
        self,
        conversation_id: str,
        *,
        default_bot_id: Optional[str] = None,
        default_model_id: Optional[str] = None,
    ) -> ChatConversation:
        await self.get_conversation(conversation_id)
        now = datetime.now(timezone.utc).isoformat()
        bot_id = str(default_bot_id or "").strip() or None
        model_id = str(default_model_id or "").strip() or None
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE conversations
                    SET default_bot_id = ?, default_model_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        bot_id,
                        model_id,
                        now,
                        conversation_id,
                    ),
                )
                await db.commit()
        return await self.get_conversation(conversation_id)

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        bot_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        metadata: Optional[Any] = None,
    ) -> ChatMessage:
        await self.get_conversation(conversation_id)
        now = datetime.now(timezone.utc).isoformat()
        message = ChatMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role=role,
            content=content,
            bot_id=bot_id,
            model=model,
            provider=provider,
            metadata=metadata,
            created_at=now,
        )
        async with self._lock:
            async with open_sqlite(self._db_path, foreign_keys=True) as db:
                await db.execute(
                    """
                    INSERT INTO messages (
                        id, conversation_id, role, content, bot_id, model, provider, metadata, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        message.conversation_id,
                        message.role,
                        message.content,
                        message.bot_id,
                        message.model,
                        message.provider,
                        json.dumps(message.metadata) if message.metadata is not None else None,
                        message.created_at,
                    ),
                )
                await db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                await self._reindex_message(
                    db,
                    message_id=message.id,
                    conversation_id=message.conversation_id,
                    role=message.role,
                    content=message.content,
                    metadata=message.metadata,
                    created_at=message.created_at,
                )
                await db.commit()
        return message

    async def add_memory_profile_item(
        self,
        *,
        user_id: str,
        profile_id: str,
        message: ChatMessage,
        metadata: Optional[Any] = None,
    ) -> None:
        await self._ensure_db()
        normalized_user_id = str(user_id or "").strip()
        normalized_profile_id = str(profile_id or "default").strip() or "default"
        if not normalized_user_id:
            return
        if not self._message_is_indexable(role=message.role, metadata=message.metadata):
            return
        normalized_content = str(message.content or "").strip()
        if not normalized_content:
            return
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO memory_profile_items (
                        id, user_id, profile_id, message_id, conversation_id, role, content, embedding, metadata, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, profile_id, message_id) DO UPDATE SET
                        role = excluded.role,
                        content = excluded.content,
                        embedding = excluded.embedding,
                        metadata = excluded.metadata,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        normalized_user_id,
                        normalized_profile_id,
                        message.id,
                        message.conversation_id,
                        message.role,
                        normalized_content,
                        json.dumps(self._embed(normalized_content)),
                        json.dumps(metadata) if metadata is not None else None,
                        message.created_at,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.commit()

    def _memory_profile_row_to_dict(self, row: aiosqlite.Row) -> Dict[str, Any]:
        metadata = None
        if row["metadata"]:
            try:
                metadata = json.loads(row["metadata"])
            except Exception:
                metadata = None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "profile_id": row["profile_id"],
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or row["created_at"],
        }

    async def list_memory_profile_items(
        self,
        *,
        user_id: str,
        profile_id: str = "default",
        limit: int = 200,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        normalized_user_id = str(user_id or "").strip()
        normalized_profile_id = str(profile_id or "default").strip() or "default"
        normalized_query = str(query or "").strip()
        if not normalized_user_id:
            return []
        if normalized_query:
            results = await self.search_memory_profile(
                user_id=normalized_user_id,
                profile_id=normalized_profile_id,
                query=normalized_query,
                limit=limit,
            )
            result_ids = [item["id"] for item in results]
            if not result_ids:
                return []
            placeholders = ",".join("?" for _ in result_ids)
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    f"SELECT * FROM memory_profile_items WHERE id IN ({placeholders})",
                    result_ids,
                ) as cursor:
                    rows = await cursor.fetchall()
            rows_by_id = {row["id"]: self._memory_profile_row_to_dict(row) for row in rows}
            ordered = []
            for item in results:
                row = rows_by_id.get(item["id"])
                if row:
                    row["score"] = item.get("score")
                    ordered.append(row)
            return ordered
        safe_limit = max(1, min(int(limit or 200), 500))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM memory_profile_items
                WHERE user_id = ? AND profile_id = ?
                ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC
                LIMIT ?
                """,
                (normalized_user_id, normalized_profile_id, safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._memory_profile_row_to_dict(row) for row in rows]

    async def create_memory_profile_item(
        self,
        *,
        user_id: str,
        profile_id: str = "default",
        content: str,
        role: str = "user",
        conversation_id: Optional[str] = None,
        metadata: Optional[Any] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        normalized_user_id = str(user_id or "").strip()
        normalized_profile_id = str(profile_id or "default").strip() or "default"
        normalized_content = str(content or "").strip()
        normalized_role = str(role or "user").strip().lower()
        if normalized_role not in {"user", "assistant"}:
            normalized_role = "user"
        if not normalized_user_id:
            raise ValueError("user_id is required")
        if not normalized_content:
            raise ValueError("content is required")
        now = datetime.now(timezone.utc).isoformat()
        item_id = str(uuid.uuid4())
        message_id = f"manual:{item_id}"
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(
                    """
                    INSERT INTO memory_profile_items (
                        id, user_id, profile_id, message_id, conversation_id, role, content, embedding, metadata, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        normalized_user_id,
                        normalized_profile_id,
                        message_id,
                        str(conversation_id or "manual"),
                        normalized_role,
                        normalized_content,
                        json.dumps(self._embed(normalized_content)),
                        json.dumps(metadata) if metadata is not None else json.dumps({"source": "manual"}),
                        now,
                        now,
                    ),
                )
                await db.commit()
                async with db.execute("SELECT * FROM memory_profile_items WHERE id = ?", (item_id,)) as cursor:
                    row = await cursor.fetchone()
        return self._memory_profile_row_to_dict(row)

    async def update_memory_profile_item(
        self,
        item_id: str,
        *,
        user_id: str,
        profile_id: str = "default",
        content: str,
        role: str = "user",
        metadata: Optional[Any] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        normalized_item_id = str(item_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        normalized_profile_id = str(profile_id or "default").strip() or "default"
        normalized_content = str(content or "").strip()
        normalized_role = str(role or "user").strip().lower()
        if normalized_role not in {"user", "assistant"}:
            normalized_role = "user"
        if not normalized_item_id or not normalized_user_id:
            raise ValueError("item_id and user_id are required")
        if not normalized_content:
            raise ValueError("content is required")
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    UPDATE memory_profile_items
                    SET profile_id = ?, role = ?, content = ?, embedding = ?, metadata = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        normalized_profile_id,
                        normalized_role,
                        normalized_content,
                        json.dumps(self._embed(normalized_content)),
                        json.dumps(metadata) if metadata is not None else None,
                        now,
                        normalized_item_id,
                        normalized_user_id,
                    ),
                )
                if cursor.rowcount < 1:
                    raise KeyError("memory item not found")
                await db.commit()
                async with db.execute("SELECT * FROM memory_profile_items WHERE id = ?", (normalized_item_id,)) as cursor:
                    row = await cursor.fetchone()
        return self._memory_profile_row_to_dict(row)

    async def delete_memory_profile_item(self, item_id: str, *, user_id: str) -> bool:
        await self._ensure_db()
        normalized_item_id = str(item_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        if not normalized_item_id or not normalized_user_id:
            return False
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM memory_profile_items WHERE id = ? AND user_id = ?",
                    (normalized_item_id, normalized_user_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def search_memory_profile(
        self,
        *,
        user_id: str,
        profile_id: str = "default",
        query: str,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        normalized_user_id = str(user_id or "").strip()
        normalized_profile_id = str(profile_id or "default").strip() or "default"
        normalized_query = str(query or "").strip()
        if not normalized_user_id or not normalized_query:
            return []
        qvec = self._embed(normalized_query)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM memory_profile_items
                WHERE user_id = ? AND profile_id = ?
                """,
                (normalized_user_id, normalized_profile_id),
            ) as cursor:
                rows = await cursor.fetchall()
        scored: List[Dict[str, Any]] = []
        for row in rows:
            try:
                embedding = json.loads(row["embedding"])
            except Exception:
                continue
            role = str(row["role"] or "").strip().lower()
            score = self._cosine(qvec, embedding)
            role_bonus = 0.08 if role == "user" else 0.02
            scored.append(
                {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "conversation_id": row["conversation_id"],
                    "role": role,
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "score": score,
                    "weighted_score": score + role_bonus,
                }
            )
        scored.sort(key=lambda item: (item["weighted_score"], item["score"], item["created_at"]), reverse=True)
        return scored[: max(1, min(int(limit or 8), 25))]

    async def list_messages(self, conversation_id: str, limit: Optional[int] = None) -> List[ChatMessage]:
        await self.get_conversation(conversation_id)
        safe_limit = None
        if isinstance(limit, int) and limit > 0:
            safe_limit = min(limit, 2000)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if safe_limit is None:
                query = """
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC
                """
                params: tuple[Any, ...] = (conversation_id,)
            else:
                # Pull latest N rows using indexed DESC scan, then restore chronological order.
                query = """
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE conversation_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    ) recent
                    ORDER BY created_at ASC
                """
                params = (conversation_id, safe_limit)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                result: List[ChatMessage] = []
                for row in rows:
                    data = dict(row)
                    if data.get("metadata"):
                        data["metadata"] = json.loads(data["metadata"])
                    result.append(ChatMessage.model_validate(data))
                return result

    async def summarize_message_usage(self, *, hours: int = 24, limit_conversations: int = 25) -> Dict[str, Any]:
        await self._ensure_db()
        safe_hours = max(1, min(int(hours or 24), 2160))
        safe_limit = max(1, min(int(limit_conversations or 25), 250))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=safe_hours)
        totals: Dict[str, Any] = {
            "messages": 0,
            "messages_with_usage": 0,
            "messages_without_usage": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "unknown_total_messages": 0,
        }
        by_conversation: Dict[str, Dict[str, Any]] = {}
        by_bot: Dict[str, Dict[str, Any]] = {}
        by_provider_model: Dict[str, Dict[str, Any]] = {}

        def _new_bucket(**identity: Any) -> Dict[str, Any]:
            return {
                **identity,
                "messages": 0,
                "messages_with_usage": 0,
                "messages_without_usage": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "unknown_total_messages": 0,
            }

        def _metadata_dict(raw: Any) -> Dict[str, Any]:
            if isinstance(raw, dict):
                return raw
            if not isinstance(raw, str) or not raw.strip():
                return {}
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _usage_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _usage_summary(metadata: Dict[str, Any]) -> Dict[str, Any]:
            usage = metadata.get("usage")
            if not isinstance(usage, dict):
                return {}
            prompt_tokens = _usage_int(
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or usage.get("promptTokenCount")
            )
            completion_tokens = _usage_int(
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or usage.get("eval_count")
                or usage.get("candidatesTokenCount")
            )
            total_tokens = _usage_int(usage.get("total_tokens") or usage.get("totalTokenCount"))
            if total_tokens is None and (prompt_tokens or completion_tokens):
                total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
            result: Dict[str, Any] = {}
            if prompt_tokens is not None:
                result["prompt_tokens"] = prompt_tokens
            if completion_tokens is not None:
                result["completion_tokens"] = completion_tokens
            if total_tokens is not None:
                result["total_tokens"] = total_tokens
            return result

        def _add_usage(bucket: Dict[str, Any], prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
            bucket["messages_with_usage"] += 1
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["total_tokens"] += total_tokens

        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    msg.id,
                    msg.conversation_id,
                    msg.bot_id,
                    msg.model,
                    msg.provider,
                    msg.metadata,
                    msg.created_at,
                    conv.title AS conversation_title,
                    conv.project_id,
                    conv.scope
                FROM messages msg
                JOIN conversations conv ON conv.id = msg.conversation_id
                WHERE msg.role = 'assistant' AND msg.created_at >= ?
                ORDER BY msg.created_at DESC
                """,
                (since.isoformat(),),
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            totals["messages"] += 1
            metadata = _metadata_dict(row["metadata"])
            conversation_id = str(row["conversation_id"] or "unknown").strip() or "unknown"
            bot_id = str(row["bot_id"] or "unknown").strip() or "unknown"
            provider = str(row["provider"] or "unknown").strip().lower() or "unknown"
            model = str(row["model"] or "unknown").strip() or "unknown"
            if provider == "unknown" or model == "unknown":
                metadata_model = metadata.get("model") if isinstance(metadata.get("model"), dict) else {}
                if provider == "unknown":
                    provider = str(metadata_model.get("provider") or "unknown").strip().lower() or "unknown"
                if model == "unknown":
                    model = str(metadata_model.get("model") or "unknown").strip() or "unknown"
            created_at = str(row["created_at"] or "")
            conv_bucket = by_conversation.setdefault(
                conversation_id,
                _new_bucket(
                    conversation_id=conversation_id,
                    conversation_title=str(row["conversation_title"] or conversation_id),
                    project_id=str(row["project_id"] or "").strip() or None,
                    scope=str(row["scope"] or "global").strip() or "global",
                ),
            )
            bot_bucket = by_bot.setdefault(bot_id, _new_bucket(bot_id=bot_id))
            provider_model_key = f"{provider}::{model}"
            provider_model_bucket = by_provider_model.setdefault(
                provider_model_key,
                _new_bucket(provider=provider, model=model),
            )
            for bucket in (conv_bucket, bot_bucket, provider_model_bucket):
                bucket["messages"] += 1
                if created_at > str(bucket.get("last_message_at") or ""):
                    bucket["last_message_at"] = created_at

            usage = _usage_summary(metadata)
            if not usage:
                totals["messages_without_usage"] += 1
                for bucket in (conv_bucket, bot_bucket, provider_model_bucket):
                    bucket["messages_without_usage"] += 1
                continue
            prompt_tokens = _usage_int(usage.get("prompt_tokens")) or 0
            completion_tokens = _usage_int(usage.get("completion_tokens")) or 0
            total_tokens = _usage_int(usage.get("total_tokens"))
            if total_tokens is None:
                total_tokens = prompt_tokens + completion_tokens if prompt_tokens or completion_tokens else 0
            if total_tokens == 0 and not (prompt_tokens or completion_tokens):
                totals["unknown_total_messages"] += 1
                for bucket in (conv_bucket, bot_bucket, provider_model_bucket):
                    bucket["unknown_total_messages"] += 1
            for bucket in (totals, conv_bucket, bot_bucket, provider_model_bucket):
                _add_usage(bucket, prompt_tokens, completion_tokens, total_tokens)

        def _sort(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return sorted(rows, key=lambda item: int(item.get("total_tokens") or 0), reverse=True)

        return {
            "window": {
                "hours": safe_hours,
                "since": since.isoformat(),
                "until": now.isoformat(),
            },
            "totals": totals,
            "by_conversation": _sort(list(by_conversation.values()))[:safe_limit],
            "conversation_count": len(by_conversation),
            "by_bot": _sort(list(by_bot.values())),
            "by_provider_model": _sort(list(by_provider_model.values())),
        }

    async def list_message_slice(
        self,
        conversation_id: str,
        *,
        limit: int,
        newest: bool,
    ) -> List[ChatMessage]:
        await self.get_conversation(conversation_id)
        safe_limit = max(1, min(int(limit or 0), 500))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if newest:
                query = """
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE conversation_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    ) recent
                    ORDER BY created_at ASC
                """
            else:
                query = """
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                """
            async with db.execute(query, (conversation_id, safe_limit)) as cursor:
                rows = await cursor.fetchall()
                result: List[ChatMessage] = []
                for row in rows:
                    data = dict(row)
                    if data.get("metadata"):
                        data["metadata"] = json.loads(data["metadata"])
                    result.append(ChatMessage.model_validate(data))
                return result

    async def update_message(
        self,
        message_id: str,
        *,
        content: Optional[str] = None,
        metadata: Optional[Any] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> ChatMessage:
        await self._ensure_db()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM messages WHERE id = ?",
                    (message_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row is None:
                        raise ConversationNotFoundError(f"Message not found: {message_id}")
                    data = dict(row)
                    existing_metadata = json.loads(data["metadata"]) if data.get("metadata") else None
                    updated = {
                        "content": data["content"] if content is None else content,
                        "metadata": existing_metadata if metadata is None else metadata,
                        "model": data.get("model") if model is None else model,
                        "provider": data.get("provider") if provider is None else provider,
                    }
                    await db.execute(
                        """
                        UPDATE messages
                        SET content = ?, metadata = ?, model = ?, provider = ?
                        WHERE id = ?
                        """,
                        (
                            updated["content"],
                            json.dumps(updated["metadata"]) if updated["metadata"] is not None else None,
                            updated["model"],
                            updated["provider"],
                            message_id,
                        ),
                    )
                    await db.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), data["conversation_id"]),
                    )
                    await self._reindex_message(
                        db,
                        message_id=message_id,
                        conversation_id=str(data["conversation_id"]),
                        role=str(data["role"]),
                        content=str(updated["content"] or ""),
                        metadata=updated["metadata"],
                        created_at=str(data["created_at"]),
                    )
                    await db.commit()
                    data.update(updated)
                    return ChatMessage.model_validate(data)

    async def count_messages(self, conversation_id: str) -> int:
        await self.get_conversation(conversation_id)
        async with open_sqlite(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0

    async def count_indexable_messages(self, conversation_id: str) -> int:
        await self.get_conversation(conversation_id)
        query = """
            SELECT COUNT(*)
            FROM messages
            WHERE conversation_id = ?
              AND lower(role) IN ('user', 'assistant')
              AND (
                COALESCE(lower(json_extract(metadata, '$.mode')), '') NOT IN (
                  'assign_request',
                  'assign_pending',
                  'pm_run_report',
                  'assign_summary',
                  'assign_error'
                )
                OR (
                  COALESCE(lower(json_extract(metadata, '$.mode')), '') NOT IN ('assign_error')
                  AND COALESCE(json_extract(metadata, '$.ingest_allowed'), 0) = 1
                )
              )
        """
        try:
            async with open_sqlite(self._db_path) as db:
                async with db.execute(query, (conversation_id,)) as cursor:
                    row = await cursor.fetchone()
                    return int(row[0] or 0) if row else 0
        except Exception:
            messages = await self.list_messages(conversation_id)
            count = 0
            for message in messages:
                if self._message_is_indexable(role=message.role, metadata=message.metadata):
                    count += 1
            return count

    async def search_message_memory(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 12,
        roles: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        await self.get_conversation(conversation_id)
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        qvec = self._embed(normalized_query)
        clauses = ["m.conversation_id = ?"]
        params: List[Any] = [conversation_id]
        normalized_roles = [str(role).strip().lower() for role in (roles or []) if str(role).strip()]
        if normalized_roles:
            placeholders = ", ".join("?" for _ in normalized_roles)
            clauses.append(f"m.role IN ({placeholders})")
            params.extend(normalized_roles)
        query_sql = f"""
            SELECT
                m.id,
                m.message_id,
                m.role,
                m.chunk_index,
                m.content,
                m.embedding,
                msg.created_at
            FROM chat_message_memory m
            JOIN messages msg ON msg.id = m.message_id
            WHERE {' AND '.join(clauses)}
        """
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query_sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return []
        timestamps = [self._iso_to_ts(str(row["created_at"] or "")) for row in rows]
        min_ts = min(timestamps) if timestamps else 0.0
        max_ts = max(timestamps) if timestamps else 0.0
        ts_span = max(max_ts - min_ts, 1.0)
        per_message: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            emb = json.loads(row["embedding"])
            raw_score = self._cosine(qvec, emb)
            role = str(row["role"] or "").strip().lower()
            role_bonus = 0.18 if role == "user" else 0.02
            created_at = str(row["created_at"] or "")
            recency_bonus = max(0.0, min(0.08, 0.08 * ((self._iso_to_ts(created_at) - min_ts) / ts_span)))
            weighted_score = raw_score + role_bonus + recency_bonus
            candidate = {
                "id": row["id"],
                "message_id": row["message_id"],
                "role": role,
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "created_at": created_at,
                "score": raw_score,
                "weighted_score": weighted_score,
                "role_bonus": role_bonus,
                "recency_bonus": recency_bonus,
            }
            existing = per_message.get(str(row["message_id"] or ""))
            if existing is None or float(candidate["weighted_score"]) > float(existing["weighted_score"]):
                per_message[str(row["message_id"] or "")] = candidate
        scored = list(per_message.values())
        scored.sort(key=lambda item: (item["weighted_score"], item["score"], item["created_at"]), reverse=True)
        return scored[: max(1, min(limit, 50))]

    async def get_messages_by_ids(self, conversation_id: str, message_ids: List[str]) -> List[ChatMessage]:
        await self.get_conversation(conversation_id)
        normalized_ids = [str(message_id).strip() for message_id in message_ids if str(message_id).strip()]
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        query = f"""
            SELECT * FROM messages
            WHERE conversation_id = ? AND id IN ({placeholders})
            ORDER BY created_at ASC
        """
        params: List[Any] = [conversation_id]
        params.extend(normalized_ids)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                result: List[ChatMessage] = []
                for row in rows:
                    data = dict(row)
                    if data.get("metadata"):
                        data["metadata"] = json.loads(data["metadata"])
                    result.append(ChatMessage.model_validate(data))
                return result
