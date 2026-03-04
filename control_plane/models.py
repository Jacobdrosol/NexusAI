# Re-export shared models for convenience within control_plane
from shared.models import (  # noqa: F401
    BackendConfig,
    BackendParams,
    Bot,
    Capability,
    CatalogModel,
    ChatConversation,
    ChatMessage,
    Task,
    TaskError,
    TaskMetadata,
    VaultChunk,
    VaultItem,
    Worker,
    WorkerMetrics,
)
