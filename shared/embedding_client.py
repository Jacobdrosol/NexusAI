"""Semantic embedding client for memory and vault retrieval.

Uses a local Ollama instance (or any Ollama-compatible /api/embed endpoint)
to produce real neural embeddings. Falls back to a deterministic hash-based
bag-of-words embedding when no embedding backend is configured, so the
platform keeps working without one.

Configuration (env vars):
  NEXUSAI_EMBEDDING_HOST   default http://172.18.0.1:11434 (host gateway)
  NEXUSAI_EMBEDDING_MODEL   default mxbai-embed-large
  NEXUSAI_EMBEDDING_DISABLED  set "1" to force hash fallback
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, List, Optional

_DEFAULT_HOST = os.environ.get("NEXUSAI_EMBEDDING_HOST", "http://172.18.0.1:11434")
_DEFAULT_MODEL = os.environ.get("NEXUSAI_EMBEDDING_MODEL", "mxbai-embed-large")
_HASH_DIMS = 64
_TIMEOUT = 10.0


def _hash_embed(text: str, dims: int = _HASH_DIMS) -> List[float]:
    """Deterministic hash-based bag-of-words embedding (fallback)."""
    vec = [0.0] * dims
    for token in str(text or "").lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "big") % dims
        sign = 1.0 if (digest[2] % 2 == 0) else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


class EmbeddingClient:
    """Produces semantic embeddings via an Ollama-compatible endpoint."""

    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        disabled: bool = False,
    ) -> None:
        self._host = (host or _DEFAULT_HOST).rstrip("/")
        self._model = model or _DEFAULT_MODEL
        self._disabled = disabled or os.environ.get("NEXUSAI_EMBEDDING_DISABLED", "") == "1"
        self._last_error: Optional[str] = None
        # Circuit breaker: after a failed connection, stop trying for a while
        # so callers don't block on a long timeout every time.
        self._cooldown_until: float = 0.0
        self._cooldown_seconds = 60.0

    @property
    def model(self) -> str:
        return self._model

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def _in_cooldown(self) -> bool:
        import time

        return time.monotonic() < self._cooldown_until

    def _mark_failed(self, exc: Exception) -> None:
        import time

        self._last_error = str(exc)
        self._cooldown_until = time.monotonic() + self._cooldown_seconds

    def embed(self, text: str) -> List[float]:
        """Return a semantic embedding for text, falling back to hash."""
        if self._disabled or self._in_cooldown():
            return _hash_embed(text)
        try:
            vec = self._embed_remote(text)
            if vec:
                return vec
        except Exception as exc:
            self._mark_failed(exc)
        return _hash_embed(text)

    def embed_many(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts in one request (fallback per item)."""
        if self._disabled or self._in_cooldown() or not texts:
            return [_hash_embed(t) for t in texts]
        try:
            vecs = self._embed_remote_batch(texts)
            if vecs and len(vecs) == len(texts):
                return vecs
        except Exception as exc:
            self._mark_failed(exc)
        return [_hash_embed(t) for t in texts]

    def _embed_remote(self, text: str) -> List[float]:
        body = json.dumps({"model": self._model, "input": [str(text or "")]}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._host}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings = data.get("embeddings") or data.get("data") or []
        if embeddings and isinstance(embeddings[0], list):
            return [float(v) for v in embeddings[0]]
        return []

    def _embed_remote_batch(self, texts: List[str]) -> List[List[float]]:
        body = json.dumps({"model": self._model, "input": [str(t or "") for t in texts]}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._host}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings = data.get("embeddings") or data.get("data") or []
        return [[float(v) for v in vec] for vec in embeddings if isinstance(vec, list)]


_shared_client: Optional[EmbeddingClient] = None


def get_embedding_client() -> EmbeddingClient:
    """Return a process-wide shared embedding client."""
    global _shared_client
    if _shared_client is None:
        _shared_client = EmbeddingClient()
    return _shared_client


def embed_text(text: str) -> List[float]:
    return get_embedding_client().embed(text)


def embed_texts(texts: List[str]) -> List[List[float]]:
    return get_embedding_client().embed_many(texts)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    return _cosine(a, b)