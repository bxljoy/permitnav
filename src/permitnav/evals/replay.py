"""Recorded queries for the retrieval eval in CI (phase 3): no model, no API key.

The retrieval eval embeds each question (bge-m3), translates English questions to Swedish
(an LLM call) and embeds the translations. `--record-queries` saves every text embedded and
every translation made during a local run; `--queries` replays them. With the corpus fixed
too (`export-eval-corpus`), the eval is deterministic, so a change in its numbers comes from
the code under review. A text that was not recorded fails loudly: refresh the file.

Vectors are stored as base64 float32 (little-endian), keyed by the exact text.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1


class MissingQuery(RuntimeError):
    """A text the replay file does not have: the golden set or the code changed."""


@dataclass
class QueryCache:
    model_id: str = ""
    # What the recording run searched with: the golden file and the retrieval settings.
    # Replaying under other settings would ask for texts never recorded.
    settings: dict[str, Any] = field(default_factory=dict)
    vectors: dict[str, list[float]] = field(default_factory=dict)
    translations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> QueryCache:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            raise ValueError(f"{path}: unknown replay format {data.get('format')!r}")
        return cls(
            model_id=data["model_id"],
            settings=data.get("settings", {}),
            vectors={text: _decode(v) for text, v in data["vectors"].items()},
            translations=dict(data["translations"]),
        )

    def save(self, path: Path) -> None:
        data = {
            "format": FORMAT,
            "model_id": self.model_id,
            "settings": self.settings,
            "vectors": {text: _encode(self.vectors[text]) for text in sorted(self.vectors)},
            "translations": dict(sorted(self.translations.items())),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _encode(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _decode(text: str) -> list[float]:
    raw = base64.b64decode(text)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


class RecordingEmbedder:
    """Wraps the real embedder and keeps every query vector (float32, as stored)."""

    def __init__(self, embedder: Any, cache: QueryCache) -> None:
        self._embedder = embedder
        self._cache = cache
        self.model_id = embedder.model_id
        self.dims = embedder.dims
        cache.model_id = embedder.model_id

    def embed_query(self, text: str) -> list[float]:
        vector = _decode(_encode(self._embedder.embed_query(text)))  # what replay will see
        self._cache.vectors[text] = vector
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("only query vectors are recorded")


class ReplayEmbedder:
    def __init__(self, cache: QueryCache, dims: int = 1024) -> None:
        self._cache = cache
        self.model_id = cache.model_id
        self.dims = dims

    def embed_query(self, text: str) -> list[float]:
        if text not in self._cache.vectors:
            raise MissingQuery(f"no recorded vector for {text[:80]!r}")
        return self._cache.vectors[text]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("only query vectors are recorded")


class RecordingTranslator:
    def __init__(self, translator: Any, cache: QueryCache) -> None:
        self._translator = translator
        self._cache = cache

    def translate(self, question: str) -> str:
        translation = self._translator.translate(question)
        self._cache.translations[question] = translation
        return translation

    def __getattr__(self, name: str) -> Any:  # calls, cost_usd, cost_unknown
        return getattr(self._translator, name)


class ReplayTranslator:
    calls = 0
    cost_usd = 0.0
    cost_unknown = 0

    def __init__(self, cache: QueryCache) -> None:
        self._cache = cache

    def translate(self, question: str) -> str:
        if question not in self._cache.translations:
            raise MissingQuery(f"no recorded translation for {question[:80]!r}")
        return self._cache.translations[question]
