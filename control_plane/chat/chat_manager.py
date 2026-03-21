import asyncio
import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from control_plane.vault.chunker import chunk_text
from shared.exceptions import ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project_id TEXT,
    bridge_project_ids TEXT,
    scope TEXT NOT NULL,
    default_bot_id TEXT,
    default_model_id TEXT,
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
                await db.execute(_CREATE_CONVERSATIONS_ARCHIVED_UPDATED_INDEX)
                await self._ensure_conversation_columns(db)
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

    def _message_is_indexable(self, *, role: str, metadata: Any) -> bool:
        if str(role or "").strip().lower() not in {"user", "assistant"}:
            return False
        if not isinstance(metadata, dict):
            return True
        mode = str(metadata.get("mode") or "").strip().lower()
        return mode not in {"assign_pending", "pm_run_report", "assign_summary"}

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

    async def create_conversation(
        self,
        title: str,
        project_id: Optional[str] = None,
        bridge_project_ids: Optional[List[str]] = None,
        scope: str = "global",
        default_bot_id: Optional[str] = None,
        default_model_id: Optional[str] = None,
        tool_access_enabled: bool = False,
        tool_access_filesystem: bool = False,
        tool_access_repo_search: bool = False,
    ) -> ChatConversation:
        await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        conversation = ChatConversation(
            id=str(uuid.uuid4()),
            title=title.strip() or "New Conversation",
            project_id=project_id,
            bridge_project_ids=list(bridge_project_ids or []),
            scope=scope,
            default_bot_id=default_bot_id,
            default_model_id=default_model_id,
            tool_access_enabled=bool(tool_access_enabled),
            tool_access_filesystem=bool(tool_access_filesystem),
            tool_access_repo_search=bool(tool_access_repo_search),
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO conversations (
                        id, title, project_id, bridge_project_ids, scope, default_bot_id, default_model_id, tool_access_enabled, tool_access_filesystem, tool_access_repo_search, archived_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation.id,
                        conversation.title,
                        conversation.project_id,
                        json.dumps(conversation.bridge_project_ids),
                        conversation.scope,
                        conversation.default_bot_id,
                        conversation.default_model_id,
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
            if project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
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
                    data["tool_access_enabled"] = bool(data.get("tool_access_enabled") or False)
                    data["tool_access_filesystem"] = bool(data.get("tool_access_filesystem") or False)
                    data["tool_access_repo_search"] = bool(data.get("tool_access_repo_search") or False)
                    result.append(ChatConversation.model_validate(data))
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
                data["tool_access_enabled"] = bool(data.get("tool_access_enabled") or False)
                data["tool_access_filesystem"] = bool(data.get("tool_access_filesystem") or False)
                data["tool_access_repo_search"] = bool(data.get("tool_access_repo_search") or False)
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
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE conversations
                    SET tool_access_enabled = ?, tool_access_filesystem = ?, tool_access_repo_search = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        1 if tool_access_enabled else 0,
                        1 if tool_access_filesystem else 0,
                        1 if tool_access_repo_search else 0,
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
        scored: List[Dict[str, Any]] = []
        for row in rows:
            emb = json.loads(row["embedding"])
            score = self._cosine(qvec, emb)
            scored.append(
                {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "chunk_index": row["chunk_index"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "score": score,
                }
            )
        scored.sort(key=lambda item: (item["score"], item["created_at"]), reverse=True)
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
