"""The Embedder interface."""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    model_id: str
    dims: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Unit-length vectors for corpus chunks, in the order given."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """A unit-length vector for a search query. Some models embed queries differently
        from documents (e5, Qwen3); bge-m3 treats them the same."""
        ...
