"""The answering service: shared models behind a lock, no question text kept on disk."""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

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


@pytest.mark.db
def test_a_source_is_read_in_the_exact_wording_that_was_cited(fresh_database_url) -> None:
    """The citation panel (phase 6) shows the wording the answer was given from."""

    import psycopg

    from tests.test_ingest import FIXTURE, ingest

    with psycopg.connect(fresh_database_url) as conn:
        ingest(conn, FIXTURE, 20)
        conn.commit()
    service = AnswerService(
        model="m", embedder=SlowEmbedder(), llm=None, config=RetrievalConfig(),
        database_url=fresh_database_url,
    )  # fmt: skip
    with psycopg.connect(fresh_database_url) as conn:
        row = conn.execute(  # a section with an older and a newer wording
            "SELECT unit_id FROM unit_version GROUP BY unit_id HAVING count(*) > 1 LIMIT 1"
        ).fetchone()
        assert row is not None
        unit = row[0]
        versions = conn.execute(
            "SELECT id, text, valid_from FROM unit_version WHERE unit_id = %s ORDER BY id",
            (unit,),
        ).fetchall()
    assert len(versions) >= 2
    for version_id, text, valid_from in versions:  # each exactly as cited, older ones too
        got = service.source(version_id)
        assert got is not None and got["text"] == text and got["unit_id"] == unit
        assert str(valid_from) in got["dates"] if valid_from else True
    assert service.source(10**9) is None


@pytest.mark.db
def test_a_question_is_declined_before_any_work_once_the_days_cap_is_reached(
    fresh_database_url,
) -> None:
    import psycopg

    from permitnav import spend

    class NeverCalled:
        def complete(self, request):
            raise AssertionError("no model call after the cap is reached")

    service = AnswerService(
        model="m", embedder=SlowEmbedder(), llm=NeverCalled(), config=RetrievalConfig(),
        database_url=fresh_database_url, spend_cap_usd=1.0,
    )  # fmt: skip
    with psycopg.connect(fresh_database_url) as conn:
        spend.record_call(conn, 1.0)
    with pytest.raises(spend.SpendCapReached):
        service.answer("Lönekrav?", date(2026, 11, 25))
    with psycopg.connect(fresh_database_url) as conn:
        assert spend.days(conn)[0].refused == 1  # counted although the question failed


def test_online_evals_are_optional_and_reached_through_the_service() -> None:
    from permitnav.answering.answerer import Answer

    class Online:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def answered(self, answer, question, history, trace_id) -> None:
            self.calls.append(("answered", answer.kind, question, history, trace_id))

        def rated(self, trace_id, helpful) -> bool:
            self.calls.append(("rated", trace_id, helpful))
            return True

        def close(self) -> None:
            self.calls.append(("close",))

    answer = Answer(kind="answered", text="x")
    without = AnswerService(model="m", embedder=SlowEmbedder(), llm=None, config=RetrievalConfig())
    without.record(answer, "q", [], "t")  # nothing configured: nothing happens
    assert without.rate("t", True) is False  # not counted anywhere: not "saved"
    without.close()
    online = Online()
    service = AnswerService(
        model="m", embedder=SlowEmbedder(), llm=None, config=RetrievalConfig(), online=online
    )
    service.record(answer, "q", ["earlier"], "t")
    assert service.rate(None, False) is True
    service.close()
    assert online.calls == [
        ("answered", "answered", "q", ["earlier"], "t"),
        ("rated", None, False),
        ("close",),
    ]
