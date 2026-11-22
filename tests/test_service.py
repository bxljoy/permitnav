"""The answering service: shared models behind a lock, no question text kept on disk."""

from __future__ import annotations

import threading
import time

from permitnav.retrieval.retriever import RetrievalConfig
from permitnav.retrieval.translate import LLMTranslator
from permitnav.service import AnswerService


class SlowEmbedder:
    model_id = "fake"
    dims = 3

    def __init__(self) -> None:
        self.active = 0
        self.most_at_once = 0

    def embed_query(self, text: str) -> list[float]:
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        time.sleep(0.02)
        self.active -= 1
        return [1.0, 0.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


def test_the_shared_embedder_is_called_by_one_thread_at_a_time() -> None:
    embedder = SlowEmbedder()
    service = AnswerService(model="m", embedder=embedder, llm=None, config=RetrievalConfig())
    threads = [threading.Thread(target=service.embedder.embed_query, args=("q",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert embedder.most_at_once == 1


def test_each_request_gets_its_own_translator_that_keeps_nothing_on_disk() -> None:
    config = RetrievalConfig(translate=True, keyword_weight=0.5)
    service = AnswerService(
        model="m", embedder=SlowEmbedder(), llm=object(), config=config, translate_model="t"
    )
    first, second = service._translator(), service._translator()
    assert isinstance(first, LLMTranslator) and first is not second
    assert first._cache_path is None


def test_no_translator_when_translation_is_off() -> None:
    service = AnswerService(
        model="m", embedder=SlowEmbedder(), llm=object(), config=RetrievalConfig(translate=False)
    )
    assert service._translator() is None
