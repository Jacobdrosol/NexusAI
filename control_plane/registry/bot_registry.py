import asyncio
import logging
from typing import Dict, List

from shared.exceptions import BotNotFoundError
from shared.models import Bot

logger = logging.getLogger(__name__)


class BotRegistry:
    def __init__(self) -> None:
        self._bots: Dict[str, Bot] = {}
        self._lock = asyncio.Lock()

    async def register(self, bot: Bot) -> None:
        async with self._lock:
            self._bots[bot.id] = bot
            logger.info("Registered bot %s", bot.id)

    async def get(self, bot_id: str) -> Bot:
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            return self._bots[bot_id]

    async def list(self) -> List[Bot]:
        async with self._lock:
            return list(self._bots.values())

    async def update(self, bot_id: str, bot: Bot) -> None:
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            self._bots[bot_id] = bot

    async def remove(self, bot_id: str) -> None:
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            del self._bots[bot_id]

    async def enable(self, bot_id: str) -> None:
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            self._bots[bot_id] = self._bots[bot_id].model_copy(update={"enabled": True})

    async def disable(self, bot_id: str) -> None:
        async with self._lock:
            if bot_id not in self._bots:
                raise BotNotFoundError(f"Bot not found: {bot_id}")
            self._bots[bot_id] = self._bots[bot_id].model_copy(update={"enabled": False})

    def load_from_configs(self, configs: list, worker_ids: set) -> None:
        for cfg in configs:
            try:
                bot = Bot.model_validate(cfg)
                for backend in bot.backends:
                    if backend.worker_id and backend.worker_id not in worker_ids:
                        logger.warning(
                            "Bot %s references unknown worker %s",
                            bot.id,
                            backend.worker_id,
                        )
                self._bots[bot.id] = bot
                logger.info("Loaded bot from config: %s", bot.id)
            except Exception as e:
                logger.warning("Failed to load bot config: %s", e)
