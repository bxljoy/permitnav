"""The answering pipeline as a long-lived service: the parts that are expensive to build
(the embedding model, the reranker, the LLM client) are built once and shared; each request
gets its own database connection, retriever, translator and answerer.

The retrieval settings are the CLI's defaults (`permitnav ask` / `eval-answers` with no
options), so the deployed service runs the configuration the evals measured.

Requests run concurrently (FastAPI's worker threads), so:
- the embedder and the reranker are shared behind a lock: their Hugging Face tokenizers are
  not safe to call from several threads at once;
- the translator is built per request, with no disk cache: the CLI's cache keeps every
  question's text for repeatable evals, which production must not (metadata only, phase 3
  and phase 6), and a shared one would need locking too.
"""

from __future__ import annotations

import argparse
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from permitnav import db
from permitnav.answering.answerer import BGE_M3_FLOOR, Answer, AnswerConfig, Answerer
from permitnav.retrieval.retriever import RetrievalConfig, Retriever

HEALTH_CONNECT_TIMEOUT_S = 3


def default_retrieval_config() -> RetrievalConfig:
    """The CLI's default retrieval configuration (the one the evals report)."""
    from permitnav.cli import _add_retrieval_options, _retrieval_config

    parser = argparse.ArgumentParser()
    _add_retrieval_options(parser)
    return _retrieval_config(parser.parse_args([]))


class LockedEmbedder:
    """An embedder that one thread at a time may call."""

    def __init__(self, embedder: Any, lock: threading.Lock) -> None:
        self._embedder = embedder
        self._lock = lock
        self.model_id = embedder.model_id
        self.dims = embedder.dims

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            return self._embedder.embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self._embedder.embed_documents(texts)


class LockedReranker:
    def __init__(self, reranker: Any, lock: threading.Lock) -> None:
        self._reranker = reranker
        self._lock = lock

    def score(self, question: str, texts: list[str]) -> list[float]:
        with self._lock:
            return self._reranker.score(question, texts)


@dataclass
class AnswerService:
    model: str
    embedder: Any
    llm: Any
    config: RetrievalConfig
    translate_model: str | None = None  # None: no query translation
    reranker: Any = None
    database_url: str | None = None
    _model_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        # One lock for both models: they share the CPU, so running them together gains nothing.
        self.embedder = LockedEmbedder(self.embedder, self._model_lock)
        if self.reranker is not None:
            self.reranker = LockedReranker(self.reranker, self._model_lock)
        from permitnav.agent.intake import IntakeRewriter

        # Built once (it reads its prompt); it calls the model only for follow-on questions.
        self._intake = IntakeRewriter(self.llm, self.model)

    def answer(
        self,
        question: str,
        as_of: date,
        history: list[str] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> Answer:
        """`history`: the user's earlier questions (phase 5 memory, kept by the client). With
        it, intake makes the question self-contained first (one more model call); without
        it, nothing changes."""
        translator = self._translator()
        with db.connect(self.database_url) as conn:
            retriever = Retriever(conn, self.embedder, self.config, translator, self.reranker)
            answer_config = AnswerConfig(model=self.model, min_score=BGE_M3_FLOOR)
            return Answerer(conn, retriever, self.llm, answer_config, intake=self._intake).answer(
                question, as_of, history=history or [], progress=progress
            )

    def source(self, version_id: int) -> dict[str, str] | None:
        """One wording by id (the exact one an answer cited), with the dates line the
        answer model saw."""
        from permitnav.answering.context import _dates, _stockholm_date

        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT u.id, u.heading, u.authority, u.url, v.text, v.valid_from, v.valid_to,"
                " v.observed_from FROM unit_version v JOIN unit u ON u.id = v.unit_id"
                " WHERE v.id = %s",
                (version_id,),
            ).fetchone()
        if row is None:
            return None
        unit, heading, authority, url, text, valid_from, valid_to, seen = row
        return {
            "unit_id": unit,
            "heading": heading,
            "authority": authority,
            "url": url,
            "dates": _dates(authority, valid_from, valid_to, _stockholm_date(seen)),
            "text": text,
        }

    def save_feedback(self, case: Any) -> None:
        """Store what the person chose to save (api.FeedbackIn); see permitnav.feedback."""
        from permitnav import feedback

        with db.connect(self.database_url) as conn:
            feedback.save(
                conn,
                case.withdraw_code,
                case.question,
                case.answer,
                case.comment,
                case.kind,
                case.as_of,
                case.cited,
            )
            conn.commit()

    def withdraw_feedback(self, code: str) -> bool:
        from permitnav import feedback

        with db.connect(self.database_url) as conn:
            done = feedback.withdraw(conn, code)
            conn.commit()
            return done

    def healthy(self) -> bool:
        try:
            url = self.database_url or db.database_url()
            # A connect timeout, so a probe cannot hang on an unreachable database.
            with psycopg.connect(url, connect_timeout=HEALTH_CONNECT_TIMEOUT_S) as conn:
                return conn.execute("SELECT 1").fetchone() == (1,)
        except Exception:
            return False

    def _translator(self) -> Any:
        if not self.config.translate or self.translate_model is None:
            return None
        from permitnav.retrieval.translate import LLMTranslator

        return LLMTranslator(self.llm, self.translate_model, cache_path=None)


def build_service() -> AnswerService:
    """The production wiring, from the environment (see .env.example). Loads bge-m3."""
    from permitnav.cli import _reranker
    from permitnav.embedding.bge_m3 import BgeM3Embedder
    from permitnav.llm.openrouter import OpenRouterLLM

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        raise RuntimeError("ANSWER_MODEL is not set; see .env.example")
    config = default_retrieval_config()
    return AnswerService(
        model=model,
        embedder=BgeM3Embedder(),
        llm=OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", "")),
        config=config,
        translate_model=os.environ.get("TRANSLATE_MODEL", "").strip() or model,
        reranker=_reranker(config.rerank),
    )
