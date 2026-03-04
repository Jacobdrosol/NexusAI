import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite

from shared.exceptions import CatalogModelNotFoundError
from shared.models import CatalogModel

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_MODELS = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class ModelRegistry:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._models: Dict[str, CatalogModel] = {}
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
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_MODELS)
                await db.commit()
                async with db.execute("SELECT id, data FROM models") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        self._models[row["id"]] = CatalogModel.model_validate(json.loads(row["data"]))
            self._db_ready = True

    async def _persist_model(self, model: CatalogModel) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO models (id, data)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data
                """,
                (model.id, json.dumps(model.model_dump())),
            )
            await db.commit()

    async def _delete_model(self, model_id: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM models WHERE id = ?", (model_id,))
            await db.commit()

    async def register(self, model: CatalogModel) -> None:
        await self._ensure_db()
        async with self._lock:
            self._models[model.id] = model
        await self._persist_model(model)

    async def get(self, model_id: str) -> CatalogModel:
        await self._ensure_db()
        async with self._lock:
            model = self._models.get(model_id)
            if model is None:
                raise CatalogModelNotFoundError(f"Catalog model not found: {model_id}")
            return model

    async def list(self) -> List[CatalogModel]:
        await self._ensure_db()
        async with self._lock:
            return list(self._models.values())

    async def update(self, model_id: str, model: CatalogModel) -> None:
        await self._ensure_db()
        async with self._lock:
            if model_id not in self._models:
                raise CatalogModelNotFoundError(f"Catalog model not found: {model_id}")
            self._models[model_id] = model
        await self._persist_model(model)

    async def remove(self, model_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if model_id not in self._models:
                raise CatalogModelNotFoundError(f"Catalog model not found: {model_id}")
            del self._models[model_id]
        await self._delete_model(model_id)

    async def exists(self, provider: str, name: str) -> bool:
        await self._ensure_db()
        async with self._lock:
            for model in self._models.values():
                if model.provider == provider and model.name == name and model.enabled:
                    return True
            return False

    async def has_any(self) -> bool:
        await self._ensure_db()
        async with self._lock:
            return len(self._models) > 0
