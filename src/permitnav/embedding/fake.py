"""A deterministic stand-in for tests: no model download, same text -> same vector."""

from __future__ import annotations

import hashlib
import math
import re


class FakeEmbedder:
    """Bag-of-words hashing into `dims` buckets, normalised. Texts that share words get
    similar vectors, so retrieval tests behave sensibly without a real model."""

    def __init__(self, dims: int = 1024, model_id: str = "fake") -> None:
        self.dims = dims
        self.model_id = model_id

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in re.findall(r"\w+", text.lower()):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
