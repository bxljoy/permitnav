"""BAAI/bge-reranker-v2-m3, self-hosted with sentence-transformers (phase 2, slice 6).

Multilingual, built on the same base as bge-m3. Install with `uv sync --extra embed`. Uses
the Mac GPU (MPS) in half precision when available, otherwise the CPU. Pairs are capped at
`max_tokens`: 512 is a third faster than 1,024 with no measurable loss on the dev set (most
sections are far shorter; the model accepts 8,192). Scores are the raw logits: a sigmoid in
half precision would round the best candidates towards an exact 1.0 and tie them.
"""

from __future__ import annotations

from typing import Any


class BgeReranker:
    model_id = "BAAI/bge-reranker-v2-m3"

    def __init__(
        self, device: str | None = None, max_tokens: int = 512, batch_size: int = 16
    ) -> None:
        import torch  # pyright: ignore[reportMissingImports]
        from sentence_transformers import CrossEncoder  # pyright: ignore[reportMissingImports]

        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.batch_size = batch_size
        self._model: Any = CrossEncoder(self.model_id, device=device, max_length=max_tokens)
        self._identity = torch.nn.Identity()
        if device == "mps":
            self._model.model.half()

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        scores = self._model.predict(
            [(question, text) for text in texts],
            batch_size=self.batch_size,
            activation_fn=self._identity,
            convert_to_numpy=True,
        )
        return [float(s) for s in scores]
