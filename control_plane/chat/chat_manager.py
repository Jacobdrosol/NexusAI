import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import aiosqlite

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
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(_CREATE_CONVERSATIONS)
                await db.execute(_CREATE_MESSAGES)
                await self._ensure_conversation_columns(db)
                await db.commit()
            self._db_ready = True

    async def _ensure_conversation_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(conversations)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "archived_at" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
        if "bridge_project_ids" not in columns:
            await db.execute("ALTER TABLE conversations ADD COLUMN bridge_project_ids TEXT")

    async def create_conversation(
        self,
        title: str,
        project_id: Optional[str] = None,
        bridge_project_ids: Optional[List[str]] = None,
        scope: str = "global",
        default_bot_id: Optional[str] = None,
        default_model_id: Optional[str] = None,
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
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO conversations (
                        id, title, project_id, bridge_project_ids, scope, default_bot_id, default_model_id, archived_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation.id,
                        conversation.title,
                        conversation.project_id,
                        json.dumps(conversation.bridge_project_ids),
                        conversation.scope,
                        conversation.default_bot_id,
                        conversation.default_model_id,
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
        async with aiosqlite.connect(self._db_path) as db:
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
                    result.append(ChatConversation.model_validate(data))
                return result

    async def get_conversation(self, conversation_id: str) -> ChatConversation:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
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
                return ChatConversation.model_validate(data)

    async def delete_conversation(self, conversation_id: str) -> None:
        conversation = await self.get_conversation(conversation_id)
        if not conversation.archived_at:
            raise ValueError("conversation must be archived before deletion")
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                await db.commit()

    async def archive_conversation(self, conversation_id: str) -> ChatConversation:
        conversation = await self.get_conversation(conversation_id)
        if conversation.archived_at:
            return conversation
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
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
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "UPDATE conversations SET archived_at = NULL, updated_at = ? WHERE id = ?",
                    (now, conversation_id),
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
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
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
                await db.commit()
        return message

    async def list_messages(self, conversation_id: str) -> List[ChatMessage]:
        await self.get_conversation(conversation_id)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            ) as cursor:
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
            async with aiosqlite.connect(self._db_path) as db:
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
                    await db.commit()
                    data.update(updated)
                    return ChatMessage.model_validate(data)
