"""The second search (phase 5, step 2): a model checks the evidence; deterministic tools add
at most two steps' worth of sources; any failure leaves the first search's sources."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime

from permitnav.agent.second_search import (
    SecondSearcher,
    parse_plan,
    render,
    section_id,
    table_of_contents,
)
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.port import CompletionRequest, LLMError
from permitnav.retrieval.retriever import Hit

AS_OF = date(2026, 11, 24)


def hit(unit: str, text: str = "text") -> Hit:
    return Hit(
        unit_id=unit, version_id=hash(unit) % 10_000, chunk_id=1, text=text, score=0.6,
        authority="law", heading=unit, url="u", valid_from=None, valid_to=None,
        fetched_at=datetime(2026, 11, 1, tzinfo=UTC), text_hash=unit,
    )  # fmt: skip


class FakeRetriever:
    """The three tools, with what each was asked recorded."""

    def __init__(self, cites: dict[str, set[str]] | None = None) -> None:
        self.cites = cites or {}
        self.calls: list[tuple] = []

    def rank_units(self, query, as_of, units, k):
        self.calls.append(("rank_units", tuple(units), k))
        return [hit(u) for u in units][:k]

    def section_headings(self, chapter_prefix, as_of):
        return []

    def cited_sections(self, h):
        return set(self.cites.get(h.unit_id, set()))

    def search_types(self, query, as_of, k, source_types):
        self.calls.append(("search_types", query, source_types))
        return [hit("sfs:2005:716/1kap/1§"), hit("sfs:2006:97/4kap/4a§"), hit("x/1"), hit("x/2")]


def searcher(
    reply: str | Callable[[CompletionRequest], str], retriever: FakeRetriever | None = None
) -> tuple[SecondSearcher, ScriptedLLM, FakeRetriever]:
    llm = ScriptedLLM(reply)
    retriever = retriever or FakeRetriever()
    return SecondSearcher(retriever, llm, model="m"), llm, retriever  # type: ignore[arg-type]


FIRST = [hit("sfs:2005:716/1kap/1§"), hit("web:en/page#a")]


def test_enough_evidence_adds_nothing_and_runs_no_tool() -> None:
    s, llm, retriever = searcher(json.dumps({"enough": True, "missing": "", "steps": []}))
    hits, result = s.expand("q?", AS_OF, FIRST)
    assert hits == FIRST and result.enough is True and retriever.calls == []
    (request,) = llm.requests
    assert request.reasoning_effort == "low" and "[S2] web:en/page#a" in request.system


def test_each_tool_adds_new_sources_after_the_first_search() -> None:
    plan = {
        "enough": False,
        "missing": "the ordinance rule",
        "steps": [
            {"tool": "get_section", "statute": "utlänningsförordningen", "chapter": "4",
             "section": "4 a"},
            {"tool": "follow_citations", "from": "S1"},
        ],
    }  # fmt: skip
    cites = {"sfs:2005:716/1kap/1§": {"sfs:2005:716/5kap/7§", "sfs:2005:716/1kap/1§"}}
    s, _, retriever = searcher(json.dumps(plan), FakeRetriever(cites))
    hits, result = s.expand("q?", AS_OF, FIRST)
    assert [h.unit_id for h in hits] == [
        *(h.unit_id for h in FIRST),
        "sfs:2006:97/4kap/4a§",
        "sfs:2005:716/5kap/7§",  # cited; the source itself is not added again
    ]
    assert result.added == ["sfs:2006:97/4kap/4a§", "sfs:2005:716/5kap/7§"]
    assert ("rank_units", ("sfs:2005:716/5kap/7§",), 2) in retriever.calls


def test_a_new_search_skips_what_is_already_there_and_adds_at_most_three() -> None:
    plan = {
        "enough": False,
        "steps": [{"tool": "search_rules", "query": "anhörig", "sources": "law"}],
    }
    s, _, retriever = searcher(json.dumps(plan))
    hits, result = s.expand("q?", AS_OF, FIRST)
    assert result.added == ["sfs:2006:97/4kap/4a§", "x/1", "x/2"]
    assert retriever.calls[0] == ("search_types", "anhörig", ("statute", "transitional"))


def test_any_failure_keeps_the_first_search_as_it_was() -> None:
    def fail(request: CompletionRequest) -> str:
        raise LLMError("down")

    for reply in (fail, "not json", json.dumps({"steps": []})):
        s, _, retriever = searcher(reply)
        hits, result = s.expand("q?", AS_OF, FIRST)
        assert hits == FIRST and result.enough is None and retriever.calls == []


def test_invalid_steps_are_dropped_and_at_most_two_run() -> None:
    plan = parse_plan(
        json.dumps({"enough": False, "steps": [
            {"tool": "get_section", "statute": "brottsbalken", "chapter": "1", "section": "1"},
            {"tool": "follow_citations", "from": "S9"},  # only 2 sources
            {"tool": "search_rules", "query": "x", "sources": "everything"},
            {"tool": "delete_corpus"},
            {"tool": "follow_citations", "from": "S2"},
            {"tool": "follow_citations", "from": "S2"},  # a repeat
            {"tool": "search_rules", "query": "a", "sources": "papers"},
            {"tool": "search_rules", "query": "b", "sources": "rulings"},
        ]}),
        n_sources=2,
    )  # fmt: skip
    assert [(s.tool, s.args) for s in plan.steps] == [
        ("follow_citations", {"from": "S2"}),
        ("search_rules", {"query": "a", "sources": "papers"}),
    ]
    assert (
        parse_plan(
            '{"enough": true, "steps": [{"tool": "follow_citations", "from": "S1"}]}', 2
        ).steps
        == []
    )


def test_section_ids_from_the_models_words() -> None:
    def sid(statute, chapter, section):
        return section_id({"statute": statute, "chapter": chapter, "section": section})

    assert sid("utlänningsförordningen", "4", "4 a") == "sfs:2006:97/4kap/4a§"
    assert sid("Utlänningslagen", "5 kap.", "3 j §") == "sfs:2005:716/5kap/3j§"
    assert sid("utlänningslagen", "6 b", "14") == "sfs:2005:716/6bkap/14§"
    assert sid("utlänningslagen", "", "7") == "sfs:2005:716/7§"
    assert sid("utlänningslagen", "5", "seven") is None
    assert sid("lagen om x", "5", "7") is None


def test_the_check_sees_short_excerpts_not_whole_sources() -> None:
    long = hit("sfs:2005:716/5kap/5§", "ord " * 1000)
    text = render([long])
    assert text.startswith("[S1] sfs:2005:716/5kap/5§ (sfs:2005:716/5kap/5§)")
    assert len(text) < 500


def test_the_table_of_contents_groups_sections_under_the_laws_own_headings() -> None:
    rows = [
        (
            "sfs:2005:716/5kap/3j§",
            "5 kap. Uppehållstillstånd, Krav på försörjning och bostad, 3 j §",
        ),
        ("sfs:2005:716/5kap/3k§", "5 kap. Uppehållstillstånd, 3 k §"),  # under the heading above
        ("sfs:2005:716/5kap/7§", "5 kap. Uppehållstillstånd, Särskilda krav för permanent, 7 §"),
        ("sfs:2005:716/5kap/10§", "5 kap. Uppehållstillstånd, 10 §"),
        ("sfs:2005:716/6kap/1§", "6 kap. Arbetstillstånd, 1 §"),
        ("sfs:2006:97/4kap/4a§", "4 kap. Uppehållstillstånd, För familjemedlemmar, 4 a §"),
        ("sfs:2005:716/6akap/1§", "6 a kap. EU-blåkort, 1 §"),
    ]
    assert table_of_contents(rows).splitlines() == [
        "utlänningslagen 5 kap. Uppehållstillstånd",
        "  3 j–3 k §: Krav på försörjning och bostad",
        "  7–10 §: Särskilda krav för permanent",
        "utlänningslagen 6 kap. Arbetstillstånd",
        "  1 §",
        "utlänningslagen 6 a kap. EU-blåkort",
        "  1 §",
        "utlänningsförordningen 4 kap. Uppehållstillstånd",
        "  4 a §: För familjemedlemmar",
    ]
