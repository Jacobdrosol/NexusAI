from typing import Any, Dict, List, Optional

from control_plane.vault.vault_manager import VaultManager


class MCPBroker:
    """Provides a standardized context-pull interface for model execution."""

    def __init__(self, vault_manager: VaultManager) -> None:
        self.vault_manager = vault_manager

    async def pull_context(
        self,
        query: str,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 5,
    ) -> Dict[str, Any]:
        results = await self.vault_manager.search(
            query=query,
            namespace=namespace,
            project_id=project_id,
            limit=limit,
        )
        contexts: List[Dict[str, Any]] = []
        for r in results:
            contexts.append(
                {
                    "item_id": r["item_id"],
                    "chunk_id": r["chunk_id"],
                    "content": r["content"],
                    "score": r["score"],
                    "metadata": {
                        "title": r["title"],
                        "namespace": r["namespace"],
                        "project_id": r["project_id"],
                        "chunk_index": r["chunk_index"],
                    },
                }
            )
        return {
            "query": query,
            "context_count": len(contexts),
            "contexts": contexts,
        }
