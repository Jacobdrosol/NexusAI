"""Tests for the semantic embedding client."""

import pytest

from shared.embedding_client import (
    EmbeddingClient,
    _hash_embed,
    cosine_similarity,
    embed_text,
)


def test_hash_embed_is_deterministic():
    a = _hash_embed("quantum tunneling wavefunction")
    b = _hash_embed("quantum tunneling wavefunction")
    assert a == b
    assert len(a) == 64


def test_hash_embed_differs_for_different_text():
    a = _hash_embed("quantum tunneling")
    b = _hash_embed("the weather today")
    assert a != b


def test_cosine_similarity_identical_is_one():
    v = _hash_embed("research notes notes")
    assert cosine_similarity(v, v) > 0.999


def test_cosine_similarity_orthogonal_is_low():
    a = _hash_embed("quantum mechanics")
    b = _hash_embed("zzzz qqqq xxxx yyyy")
    assert cosine_similarity(a, b) < 0.5


def test_embed_falls_back_to_hash_when_disabled():
    client = EmbeddingClient(disabled=True)
    vec = client.embed("research notes")
    assert len(vec) == 64
    assert vec == _hash_embed("research notes")


def test_embed_falls_back_to_hash_when_remote_unreachable(monkeypatch):
    client = EmbeddingClient(host="http://127.0.0.1:1", model="mxbai-embed-large")

    def boom(text):
        raise ConnectionError("refused")

    monkeypatch.setattr(client, "_embed_remote", boom)
    vec = client.embed("research notes")
    assert len(vec) == 64
    assert vec == _hash_embed("research notes")


def test_embed_many_falls_back_per_item(monkeypatch):
    client = EmbeddingClient(host="http://127.0.0.1:1", model="mxbai-embed-large")

    def boom(texts):
        raise ConnectionError("refused")

    monkeypatch.setattr(client, "_embed_remote_batch", boom)
    vecs = client.embed_many(["one", "two"])
    assert len(vecs) == 2
    assert vecs[0] == _hash_embed("one")
    assert vecs[1] == _hash_embed("two")


def test_circuit_breaker_skips_remote_after_failure(monkeypatch):
    client = EmbeddingClient(host="http://127.0.0.1:1", model="mxbai-embed-large")
    calls = {"n": 0}

    def boom(text):
        calls["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr(client, "_embed_remote", boom)
    client.embed("first")
    client.embed("second")
    client.embed("third")
    # Only the first call should hit the network; the rest are in cooldown.
    assert calls["n"] == 1