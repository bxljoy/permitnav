"""Query translation for retrieval (phase 2, slice 4)."""

from __future__ import annotations

import json
from datetime import date

import psycopg
import pytest

from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.llm.fake import ScriptedLLM
from permitnav.retrieval.retriever import RetrievalConfig, Retriever
from permitnav.retrieval.translate import LLMTranslator, is_swedish
from tests.test_ingest import FIXTURE, ingest
from tests.test_retriever import FAKE


@pytest.fixture
def corpus(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)


def test_swedish_questions_are_recognised_without_a_model() -> None:
    assert is_swedish("Hur hög lön krävs för att få arbetstillstånd i Sverige?")
    assert is_swedish("Vad säger 6 kap. 2 a § utlänningslagen?")
    assert is_swedish("Kan min sambo jobba här")  # no åäö, Swedish words
    assert not is_swedish("What salary do I need to get a Swedish work permit?")
    assert not is_swedish("Can my spouse work in Sweden?")
    assert not is_swedish("What does 6 kap. 2 a § utlänningslagen say?")


def test_the_translator_asks_once_per_question_and_caches_to_a_file(tmp_path) -> None:
    llm = ScriptedLLM('"Vilken lön krävs för arbetstillstånd?"\n')
    cache = tmp_path / "translations.json"
    translator = LLMTranslator(llm, model="m", cache_path=cache)
    assert translator.translate("What salary is needed?") == "Vilken lön krävs för arbetstillstånd?"
    assert translator.translate("What salary is needed?") == "Vilken lön krävs för arbetstillstånd?"
    assert len(llm.requests) == 1
    assert "6 kap. 2 §" in llm.requests[0].system  # keeps section references
    assert llm.requests[0].messages == [{"role": "user", "content": "What salary is needed?"}]
    # A new translator reads the file: no model call.
    again = LLMTranslator(ScriptedLLM("unused"), model="m", cache_path=cache)
    assert again.translate("What salary is needed?") == "Vilken lön krävs för arbetstillstånd?"
    assert json.loads(cache.read_text(encoding="utf-8"))  # written
    # Another model is another cache entry.
    other = ScriptedLLM("Annan översättning")
    assert LLMTranslator(other, model="m2", cache_path=cache).translate("What salary is needed?")
    assert len(other.requests) == 1


def test_an_empty_reply_falls_back_to_the_question() -> None:
    translator = LLMTranslator(ScriptedLLM("   "), model="m")
    assert translator.translate("What salary is needed?") == "What salary is needed?"


class Dictionary:
    """A translator that looks questions up, and records what it was asked."""

    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.asked: list[str] = []

    def translate(self, question: str) -> str:
        self.asked.append(question)
        return self.entries.get(question, question)


@pytest.mark.db
def test_keyword_search_matches_swedish_law_through_the_translation(
    conn: psycopg.Connection, corpus: None
) -> None:
    question = "Which salary requirement applies, and how does the median salary matter?"
    words = Dictionary({question: "Vilket lönekrav gäller och hur påverkar medianlönen?"})
    plain = Retriever(conn, FAKE, RetrievalConfig(mode="keyword"))
    translated = Retriever(conn, FAKE, RetrievalConfig(mode="keyword", translate=True), words)
    before = [h.unit_id for h in plain.search(question, as_of=date(2026, 11, 20))]
    after = [h.unit_id for h in translated.search(question, as_of=date(2026, 11, 20))]
    assert "sfs:2005:716/6kap/2§" not in before[:1]
    assert after[0] == "sfs:2005:716/6kap/2§"


@pytest.mark.db
def test_swedish_questions_are_not_translated(conn: psycopg.Connection, corpus: None) -> None:
    words = Dictionary({})
    config = RetrievalConfig(mode="hybrid", translate=True, keyword_weight=0.5)
    searcher = Retriever(conn, FAKE, config, words)
    searcher.search("Hur hög lön krävs?", as_of=date(2026, 11, 20))
    searcher.search("What salary is needed?", as_of=date(2026, 11, 20))
    assert words.asked == ["What salary is needed?"]


@pytest.mark.db
def test_the_translated_vector_is_used_for_the_law_only(
    conn: psycopg.Connection, corpus: None
) -> None:
    question = "Which salary requirement applies?"
    words = Dictionary({question: "lönen medianlönen procent lönekravet"})
    config = RetrievalConfig(mode="vector", translate=True, translated_vector=True)
    hits = Retriever(conn, FAKE, config, words).search(question, as_of=date(2026, 11, 20))
    assert hits[0].unit_id == "sfs:2005:716/6kap/2§"


@pytest.mark.db
def test_translation_needs_a_translator(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="translator"):
        Retriever(conn, FAKE, RetrievalConfig(translate=True))
    with pytest.raises(ValueError, match="translate"):
        Retriever(conn, FAKE, RetrievalConfig(translated_vector=True), Dictionary({}))


def test_the_cli_defaults_to_translation_with_the_measured_keyword_weight() -> None:
    import argparse

    from permitnav.cli import TRANSLATED_KEYWORD_WEIGHT, _add_retrieval_options, _retrieval_config

    parser = argparse.ArgumentParser()
    _add_retrieval_options(parser)
    default = _retrieval_config(parser.parse_args([]))
    assert (default.mode, default.translate, default.keyword_weight) == (
        "hybrid",
        True,
        TRANSLATED_KEYWORD_WEIGHT,
    )
    assert default.translated_page_weight == 1.0  # Swedish-only pages, measured
    offline = _retrieval_config(parser.parse_args(["--no-translate"]))
    assert (offline.translate, offline.keyword_weight) == (False, 0.0)
    assert offline.translated_page_weight == 0.0
    assert _retrieval_config(parser.parse_args(["--keyword-weight", "0.3"])).keyword_weight == 0.3
    vector = _retrieval_config(parser.parse_args(["--mode", "vector"]))
    assert not vector.translate
    law = _retrieval_config(parser.parse_args(["--mode", "vector", "--translated-vector"]))
    assert law.translate and law.translated_vector
    # Keyword weight 0: the page ranking still uses the translation; with it off too, nothing does.
    assert _retrieval_config(parser.parse_args(["--keyword-weight", "0"])).translate
    nothing = parser.parse_args(["--keyword-weight", "0", "--translated-page-weight", "0"])
    assert not _retrieval_config(nothing).translate
    # The page ranking only exists in hybrid mode with interleaving.
    assert _retrieval_config(parser.parse_args(["--mix", "none"])).translated_page_weight == 0.0
    with pytest.raises(SystemExit, match="needs --mode hybrid"):
        _retrieval_config(parser.parse_args(["--mode", "vector", "--translated-page-weight", "1"]))
    with pytest.raises(SystemExit):
        parser.parse_args(["--no-translate", "--translated-vector"])


def test_english_questions_naming_swedish_places_or_terms_stay_english() -> None:
    assert not is_swedish("I live in Malmö, can my spouse work here?")
    assert not is_swedish("How long does a uppehållstillstånd take to get?")
    assert not is_swedish("What does utlänningslagens 6 kap. say about my work permit?")
    assert is_swedish("Hur länge tar det att få uppehållstillstånd?")


def test_a_changed_prompt_is_not_served_old_translations(tmp_path, monkeypatch) -> None:
    from permitnav.retrieval import translate

    cache = tmp_path / "t.json"
    LLMTranslator(ScriptedLLM("gammal"), model="m", cache_path=cache).translate("Hello?")
    monkeypatch.setattr(translate, "load_prompt", lambda name: "a new prompt")
    llm = ScriptedLLM("ny")
    assert LLMTranslator(llm, model="m", cache_path=cache).translate("Hello?") == "ny"
    assert len(llm.requests) == 1


def test_a_damaged_cache_is_ignored_and_rewritten(tmp_path, capsys) -> None:
    cache = tmp_path / "t.json"
    cache.write_text('{"half": "writ', encoding="utf-8")
    translator = LLMTranslator(ScriptedLLM("Hej?"), model="m", cache_path=cache)
    assert translator.translate("Hello?") == "Hej?"
    assert "damaged translation cache" in capsys.readouterr().err
    assert json.loads(cache.read_text(encoding="utf-8"))  # valid again
    assert not list(tmp_path.glob("*.tmp"))


def test_the_translator_counts_its_calls_and_cost() -> None:
    from permitnav.llm.port import Completion

    class Priced(ScriptedLLM):
        def complete(self, request):  # type: ignore[override]
            super().complete(request)
            return Completion(text="Hej?", model="m", cost_usd=0.0002)

    translator = LLMTranslator(Priced(""), model="m")
    translator.translate("Hello?")
    translator.translate("Hello?")  # cached: free
    translator.translate("Goodbye?")
    assert (translator.calls, round(translator.cost_usd, 6), translator.cost_unknown) == (
        2,
        0.0004,
        0,
    )


@pytest.mark.db
def test_scores_stay_against_the_question_when_the_law_is_ordered_by_its_translation(
    conn: psycopg.Connection, corpus: None
) -> None:
    question = "Which salary requirement applies?"
    words = Dictionary({question: "lönen medianlönen procent lönekravet"})
    plain = Retriever(conn, FAKE, RetrievalConfig(mode="vector"))
    ordered = Retriever(
        conn, FAKE, RetrievalConfig(mode="vector", translate=True, translated_vector=True), words
    )
    scores = {h.unit_id: h.score for h in plain.search(question, date(2026, 11, 20), k=50)}
    for hit in ordered.search(question, date(2026, 11, 20)):
        assert hit.score == pytest.approx(scores[hit.unit_id])


@pytest.mark.db
def test_no_translation_is_requested_when_nothing_would_use_it(
    conn: psycopg.Connection, corpus: None
) -> None:
    words = Dictionary({})
    for config in (
        RetrievalConfig(mode="hybrid", translate=True, keyword_weight=0.0),
        RetrievalConfig(mode="vector", translate=True),
    ):
        Retriever(conn, FAKE, config, words).search("What salary?", date(2026, 11, 20))
    assert words.asked == []


@pytest.mark.db
def test_pages_can_also_be_searched_with_the_translated_question(
    conn: psycopg.Connection, corpus: None
) -> None:
    """A Swedish-only page section reaches an English question through a third ranking: the
    page group ordered by the Swedish translation's vector (phase 2, Swedish-only pages)."""
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page
    from tests.test_webpage import EN, SV, raw_page
    from tests.test_webpage import FIXTURE as PAGE

    ingest_page(conn, raw_page(PAGE, 20), parse_page(PAGE, SV))
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    # "kronor" and "sverige" occur in other sections, not the union one: the English ranking
    # alone ranks those first (the fake embedder matches words, like a real one in-language).
    question = "Does the trade union get to comment on kronor in sverige?"
    words = Dictionary({question: "fackförbund erbjudandet anställning yttra"})
    anchor = "#fackforbundetfarmojlighetattyttrasig"

    def page_rank(weight: float, untranslated_only: bool = False) -> int | None:
        config = RetrievalConfig(
            mode="hybrid",
            translate=True,
            keyword_weight=0.0,
            translated_page_weight=weight,
            translated_pages_untranslated_only=untranslated_only,
        )
        hits = Retriever(conn, FAKE, config, words).search(question, date(2026, 11, 21), k=20)
        pages = [h.unit_id for h in hits if h.unit_id.startswith("web:")]
        return next((n + 1 for n, u in enumerate(pages) if u.endswith(anchor)), None)

    without, with_translation = page_rank(0.0), page_rank(1.0)
    # The translated ranking lifts it; the English ranking still counts in the fusion.
    assert with_translation is not None
    assert without is None or with_translation < without
    assert words.asked == [question]  # only the search that uses it asks for a translation
    # By default the translated ranking covers only Swedish pages without a live English
    # version. With no English twin stored, this page counts as Swedish-only and is lifted.
    assert page_rank(1.0, untranslated_only=True) == with_translation
    # Once its English twin is stored (with current sections), the ranking leaves it alone.
    ingest_page(conn, raw_page(PAGE, 20, url=EN), parse_page(PAGE, EN))
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    swedish = [u for u in _units_lifted(conn, words, question) if u.startswith("web:sv/")]
    assert not swedish


@pytest.mark.db
def test_the_translated_page_ranking_keeps_scores_against_the_question(
    conn: psycopg.Connection, corpus: None
) -> None:
    question = "Which salary requirement applies?"
    words = Dictionary({question: "lönen medianlönen procent lönekravet"})
    plain = Retriever(conn, FAKE, RetrievalConfig(mode="vector"))
    scores = {h.unit_id: h.score for h in plain.search(question, date(2026, 11, 20), k=50)}
    config = RetrievalConfig(
        mode="hybrid", translate=True, keyword_weight=0.0, translated_page_weight=1.0
    )
    for hit in Retriever(conn, FAKE, config, words).search(question, date(2026, 11, 20)):
        if hit.unit_id in scores:
            assert hit.score == pytest.approx(scores[hit.unit_id])


@pytest.mark.db
def test_a_negative_translated_page_weight_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="translated_page_weight"):
        Retriever(conn, FAKE, RetrievalConfig(translated_page_weight=-1), Dictionary({}))


def _units_lifted(conn, words, question) -> list[str]:
    """Sections the translated page ranking alone would put forward (keyword and vector off)."""
    from permitnav.corpus.indexing import to_pgvector

    config = RetrievalConfig(mode="hybrid", translate=True, translated_page_weight=1.0)
    retriever = Retriever(conn, FAKE, config, words)
    swedish = to_pgvector(FAKE.embed_query(words.translate(question)))
    vector = to_pgvector(FAKE.embed_query(question))
    hits = retriever._search(
        vector, date(2026, 11, 21), 20, ("web_page",), order_vector=swedish, untranslated_only=True
    )
    return [h.unit_id for h in hits]
