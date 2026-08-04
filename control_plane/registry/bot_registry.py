import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from shared.exceptions import BotNotFoundError
from shared.bot_policy import validate_bot_configuration
from shared.models import Bot

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_BOTS = """
CREATE TABLE IF NOT EXISTS cp_bots (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp_bot_updated_at(bot: Bot, *, timestamp: str | None = None) -> Bot:
    return bot.model_copy(update={"updated_at": timestamp or _utc_now_iso()})


class BotRegistry:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._bots: Dict[str, Bot] = {}
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
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_BOTS)
                await db.commit()
                async with db.execute("SELECT id, data FROM cp_bots") as cursor:
                    rows = await cursor.fetchall()
                    missing_updated_at: list[Bot] = []
                    for row in rows:
                        bot = Bot.model_validate(json.loads(row["data"]))
                        if not bot.updated_at:
                            bot = _stamp_bot_updated_at(bot)
                            missing_updated_at.append(bot)
                        self._bots[row["id"]] = bot
                    for bot in missing_updated_at:
                        await db.execute(
                            """
                            INSERT INTO cp_bots (id, data)
                            VALUES (?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                data = excluded.data
                            """,
                            (bot.id, json.dumps(bot.model_dump())),
                        )
                    if missing_updated_at:
                        await db.commit()
            self._db_ready = True

    async def _persist_bot(self, bot: Bot) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_bots (id, data)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data
                """,
                (bot.id, json.dumps(bot.model_dump())),
            )
            await db.commit()

    async def _delete_bot(self, bot_id: str) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute("DELETE FROM cp_bots WHERE id = ?", (bot_id,))
            await db.commit()

    async def register(self, bot: Bot) -> None:
        await self._ensure_db()
        errors = validate_bot_configuration(bot)
        if errors:
            raise ValueError(" ".join(errors))
        stamped = _stamp_bot_updated_at(bot)
        async with self._lock:
            self._bots[stamped.id] = stamped
            logger.info("Registered bot %s", stamped.id)
        await self._persist_bot(stamped)

    async def get(self, bot_id: str) -> Bot:
        await self._ensure_db()
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            return self._bots[bot_id]

    async def list(self) -> List[Bot]:
        await self._ensure_db()
        async with self._lock:
            return list(self._bots.values())

    async def update(self, bot_id: str, bot: Bot) -> None:
        await self._ensure_db()
        errors = validate_bot_configuration(bot)
        if errors:
            raise ValueError(" ".join(errors))
        stamped = _stamp_bot_updated_at(bot)
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            self._bots[bot_id] = stamped
        await self._persist_bot(stamped)

    async def remove(self, bot_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            del self._bots[bot_id]
        await self._delete_bot(bot_id)

    async def enable(self, bot_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            updated = _stamp_bot_updated_at(self._bots[bot_id].model_copy(update={"enabled": True}))
            self._bots[bot_id] = updated
        await self._persist_bot(updated)

    async def disable(self, bot_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            updated = _stamp_bot_updated_at(self._bots[bot_id].model_copy(update={"enabled": False}))
            self._bots[bot_id] = updated
        await self._persist_bot(updated)

    async def seed_from_configs(self, configs: list, worker_ids: set[str], *, force: bool = False) -> None:
        await self._ensure_db()
        async with self._lock:
            for cfg in configs:
                try:
                    bot = Bot.model_validate(cfg)
                    errors = validate_bot_configuration(bot)
                    if errors:
                        raise ValueError(" ".join(errors))
                    if bot.id in self._bots and not force:
                        continue
                    if bot.id in self._bots or force:
                        bot = _stamp_bot_updated_at(bot)
                    elif not bot.updated_at:
                        bot = _stamp_bot_updated_at(bot)
                    for backend in bot.backends:
                        if backend.worker_id and backend.worker_id not in worker_ids:
                            logger.warning(
                                "Bot %s references unknown worker %s",
                                bot.id,
                                backend.worker_id,
                            )
                    if bot.id in self._bots:
                        logger.info("Force-reseeded bot from config: %s", bot.id)
                    else:
                        logger.info("Seeded bot from config: %s", bot.id)
                    self._bots[bot.id] = bot
                except Exception as e:
                    logger.warning("Failed to load bot config: %s", e)
            bots_to_persist = list(self._bots.values())
        for bot in bots_to_persist:
            await self._persist_bot(bot)
