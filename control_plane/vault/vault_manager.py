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

from control_plane.vault.chunker import chunk_text
from shared.exceptions import VaultItemNotFoundError
from shared.models import VaultChunk, VaultItem

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_VAULT_ITEMS = """
CREATE TABLE IF NOT EXISTS vault_items (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    namespace TEXT NOT NULL,
    project_id TEXT,
    metadata TEXT,
    embedding_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_VAULT_CHUNKS = """
CREATE TABLE IF NOT EXISTS vault_chunks (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES vault_items(id) ON DELETE CASCADE
)
"""


class VaultManager:
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
                await db.execute(_CREATE_VAULT_ITEMS)
                await db.execute(_CREATE_VAULT_CHUNKS)
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

    async def ingest_text(
        self,
        title: str,
        content: str,
        namespace: str = "global",
        project_id: Optional[str] = None,
        source_type: str = "text",
        source_ref: Optional[str] = None,
        metadata: Optional[Any] = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
    ) -> VaultItem:
        await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        item_id = str(uuid.uuid4())
        item = VaultItem(
            id=item_id,
            source_type=source_type,
            source_ref=source_ref,
            title=title.strip() or "Untitled",
            content=content,
            namespace=namespace,
            project_id=project_id,
            metadata=metadata,
            embedding_status="completed",
            created_at=now,
            updated_at=now,
        )

        chunks = chunk_text(content, chunk_size=chunk_size, overlap=chunk_overlap)
        chunk_models: List[VaultChunk] = []
        for idx, chunk in enumerate(chunks):
            chunk_models.append(
                VaultChunk(
                    id=str(uuid.uuid4()),
                    item_id=item_id,
                    chunk_index=idx,
                    content=chunk,
                    embedding=self._embed(chunk),
                    created_at=now,
                )
            )

        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(
                    """
                    INSERT INTO vault_items (
                        id, source_type, source_ref, title, content, namespace, project_id,
                        metadata, embedding_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        item.source_type,
                        item.source_ref,
                        item.title,
                        item.content,
                        item.namespace,
                        item.project_id,
                        json.dumps(item.metadata) if item.metadata is not None else None,
                        item.embedding_status,
                        item.created_at,
                        item.updated_at,
                    ),
                )
                for chunk in chunk_models:
                    await db.execute(
                        """
                        INSERT INTO vault_chunks (
                            id, item_id, chunk_index, content, embedding, metadata, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.id,
                            chunk.item_id,
                            chunk.chunk_index,
                            chunk.content,
                            json.dumps(chunk.embedding),
                            json.dumps(chunk.metadata) if chunk.metadata is not None else None,
                            chunk.created_at,
                        ),
                    )
                await db.commit()
        return item

    async def get_item(self, item_id: str) -> VaultItem:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM vault_items WHERE id = ?", (item_id,)) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    raise VaultItemNotFoundError(f"Vault item not found: {item_id}")
                data: Dict[str, Any] = dict(row)
                if data.get("metadata"):
                    data["metadata"] = json.loads(data["metadata"])
                return VaultItem.model_validate(data)

    async def list_items(
        self,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[VaultItem]:
        await self._ensure_db()
        clauses: List[str] = []
        params: List[Any] = []
        if namespace:
            clauses.append("namespace = ?")
            params.append(namespace)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT * FROM vault_items
            {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
        """
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                items: List[VaultItem] = []
                for row in rows:
                    data: Dict[str, Any] = dict(row)
                    if data.get("metadata"):
                        data["metadata"] = json.loads(data["metadata"])
                    items.append(VaultItem.model_validate(data))
                return items

    async def list_chunks(self, item_id: str) -> List[VaultChunk]:
        await self.get_item(item_id)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM vault_chunks
                WHERE item_id = ?
                ORDER BY chunk_index ASC
                """,
                (item_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                chunks: List[VaultChunk] = []
                for row in rows:
                    data: Dict[str, Any] = dict(row)
                    data["embedding"] = json.loads(data["embedding"])
                    if data.get("metadata"):
                        data["metadata"] = json.loads(data["metadata"])
                    chunks.append(VaultChunk.model_validate(data))
                return chunks

    async def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        qvec = self._embed(query)
        clauses: List[str] = []
        params: List[Any] = []
        if namespace:
            clauses.append("i.namespace = ?")
            params.append(namespace)
        if project_id:
            clauses.append("i.project_id = ?")
            params.append(project_id)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        query_sql = f"""
            SELECT
                c.id,
                c.item_id,
                c.chunk_index,
                c.content,
                c.embedding,
                i.title,
                i.namespace,
                i.project_id
            FROM vault_chunks c
            JOIN vault_items i ON i.id = c.item_id
            {where_clause}
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query_sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()

        scored: List[Dict[str, Any]] = []
        for row in rows:
            emb = json.loads(row["embedding"])
            score = self._cosine(qvec, emb)
            scored.append(
                {
                    "chunk_id": row["id"],
                    "item_id": row["item_id"],
                    "chunk_index": row["chunk_index"],
                    "content": row["content"],
                    "title": row["title"],
                    "namespace": row["namespace"],
                    "project_id": row["project_id"],
                    "score": score,
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
