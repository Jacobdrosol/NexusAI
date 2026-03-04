from typing import Any, List, Literal, Optional
from pydantic import BaseModel, ConfigDict


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
    source: Optional[str] = None
    priority: Optional[int] = None


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
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    result: Optional[Any] = None
    error: Optional[TaskError] = None
    created_at: str
    updated_at: str
