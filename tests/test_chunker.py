"""Unit tests for vault chunker."""

import pytest

from control_plane.vault.chunker import chunk_text


def test_chunk_text_splits_with_overlap():
    text = "a" * 2500
    chunks = chunk_text(text, chunk_size=1000, overlap=100)
    assert len(chunks) == 3
    assert len(chunks[0]) == 1000
    assert len(chunks[1]) == 1000


def test_chunk_text_rejects_invalid_overlap():
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=100, overlap=100)
