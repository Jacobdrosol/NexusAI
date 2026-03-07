from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Capability(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: Literal["llm", "embedding", "tool", "custom"]
    provider: Literal["ollama", "vllm", "lmstudio", "openai", "claude", "gemini", "cli", "custom"]
    models: List[str]
    gpus: Optional[List[str]] = None


class WorkerMetrics(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    load: Optional[float] = None
    gpu_utilization: Optional[List[float]] = None
    queue_depth: Optional[int] = None


class Worker(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    host: str
    port: int
    capabilities: List[Capability]
    status: Literal["online", "offline", "degraded"] = "offline"
    metrics: Optional[WorkerMetrics] = None
    enabled: bool = True


class BackendParams(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    num_ctx: Optional[int] = None
    num_gpu: Optional[int] = None
    main_gpu: Optional[int] = None
    num_thread: Optional[int] = None
    repeat_penalty: Optional[float] = None


class BackendConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: Literal["local_llm", "remote_llm", "cloud_api", "cli", "custom"]
    worker_id: Optional[str] = None
    model: str
    provider: str
    api_key_ref: Optional[str] = None
    gpu_id: Optional[str] = None
    params: Optional[BackendParams] = None


class Bot(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    role: str
    system_prompt: Optional[str] = None
    priority: int = 0
    enabled: bool = True
    backends: List[BackendConfig]
    routing_rules: Optional[Any] = None


class TaskMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    source: Optional[str] = None
    priority: Optional[int] = None
    conversation_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    step_id: Optional[str] = None


class TaskError(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    message: str
    code: Optional[str] = None
    details: Optional[Any] = None


class Task(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    bot_id: str
    payload: Any
    metadata: Optional[TaskMetadata] = None
    depends_on: List[str] = Field(default_factory=list)
    status: Literal["queued", "blocked", "running", "completed", "failed"] = "queued"
    result: Optional[Any] = None
    error: Optional[TaskError] = None
    created_at: str
    updated_at: str


class Project(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: Optional[str] = None
    mode: Literal["isolated", "bridged"] = "isolated"
    bridge_project_ids: List[str] = Field(default_factory=list)
    bot_ids: List[str] = Field(default_factory=list)
    settings_overrides: Optional[Any] = None
    enabled: bool = True


class CatalogModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    provider: str
    context_window: Optional[int] = None
    capabilities: List[str] = Field(default_factory=list)
    input_cost_per_1k: Optional[float] = None
    output_cost_per_1k: Optional[float] = None
    notes: Optional[str] = None
    enabled: bool = True


class ChatConversation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    project_id: Optional[str] = None
    bridge_project_ids: List[str] = Field(default_factory=list)
    scope: Literal["global", "project", "bridged"] = "global"
    default_bot_id: Optional[str] = None
    default_model_id: Optional[str] = None
    archived_at: Optional[str] = None
    created_at: str
    updated_at: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    bot_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    metadata: Optional[Any] = None
    created_at: str


class VaultItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_type: Literal["text", "file", "url", "chat", "task", "custom"] = "text"
    source_ref: Optional[str] = None
    title: str
    content: str
    namespace: str = "global"
    project_id: Optional[str] = None
    metadata: Optional[Any] = None
    embedding_status: Literal["pending", "completed", "failed"] = "completed"
    created_at: str
    updated_at: str


class VaultChunk(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    item_id: str
    chunk_index: int
    content: str
    embedding: List[float] = Field(default_factory=list)
    metadata: Optional[Any] = None
    created_at: str
