from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseBackend(ABC):
    @abstractmethod
    async def infer(self, model: str, messages: List[Dict], params: Dict) -> Dict[str, Any]:
        ...
