"""BAAI/bge-m3, self-hosted with sentence-transformers (D15).

Install with `uv sync --extra embed`. Uses the Mac GPU (MPS) in half precision when
available, otherwise the CPU. Inputs are capped at `max_tokens` (bge-m3 accepts 8,192, but
long inputs in big batches are what make memory spike; chunks are ~500 tokens).
"""

from __future__ import annotations

from typing import Any


class BgeM3Embedder:
    model_id = "BAAI/bge-m3"
    dims = 1024

    def __init__(
        self, device: str | None = None, max_tokens: int = 1024, batch_size: int = 16
    ) -> None:
        import torch  # pyright: ignore[reportMissingImports]
        from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]

        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.batch_size = batch_size
        self._model: Any = SentenceTransformer(self.model_id, device=device)
        self._model.max_seq_length = max_tokens
        if device == "mps":
            self._model.half()  # about half the memory; no measurable quality loss for retrieval

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, batch_size=self.batch_size, normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.astype("float32").tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
