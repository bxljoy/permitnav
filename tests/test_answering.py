"""Answering: context assembly, prompt, parsing, citation checks, the OpenRouter adapter."""

from __future__ import annotations

import io
import json
from datetime import date

import psycopg
import pytest

from permitnav.answering.answerer import AnswerConfig, Answerer, _json_object, parse_answer
from permitnav.answering.context import Context, Source, _section_of, build_context
from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.llm import openrouter
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.openrouter import LLMError, OpenRouterLLM
from permitnav.llm.port import Completion, CompletionRequest
from permitnav.retrieval.retriever import Retriever
from tests.test_ingest import FIXTURE, ingest
from tests.test_retriever import FAKE

# --- parsing (no database) --------------------------------------------------------------------


def context_with(*texts: str) -> Context:
    return Context(
        sources=[
            Source(
                f"S{i}",
                f"sfs:2005:716/6kap/{i}§",
                i,
                "retrieved",
                "law",
                f"6 kap. {i} §",
                "https://x",
                text,
                "applies from 2026-06-01",
            )
            for i, text in enumerate(texts, start=1)
        ]
    )


def reply(obj: dict) -> Completion:
    return Completion(text=json.dumps(obj, ensure_ascii=False), model="m")


def test_citations_to_unknown_sources_are_removed() -> None:
    ctx = context_with("Lönen ska vara minst 90 procent av medianlönen.")
    answer = parse_answer(
        reply({"kind": "answered", "answer": "At least 90 percent [S1] [S7]."}), ctx
    )
    assert answer.text == "At least 90 percent [S1]."
    assert answer.dropped_markers == ["S7"]
    assert [c.unit_id for c in answer.citations] == ["sfs:2005:716/6kap/1§"]


def test_only_exact_quotes_are_shown() -> None:
    ctx = context_with("Lönen ska vara minst 90 procent av medianlönen.")
    good = {
        "source": "S1",
        "swedish": "minst 90  procent av medianlönen",
        "translation": "at least 90 percent",
    }
    answer = parse_answer(reply({"kind": "answered", "answer": "Yes [S1].", "quotes": [good]}), ctx)
    assert answer.citations[0].quote == good["swedish"]
    invented = {"source": "S1", "swedish": "minst 80 procent", "translation": "at least 80 percent"}
    answer = parse_answer(
        reply({"kind": "answered", "answer": "Yes [S1].", "quotes": [invented]}), ctx
    )
    assert answer.citations[0].quote == "" and answer.citations[0].translation == ""


def test_kinds_and_lenient_json() -> None:
    ctx = context_with("x")
    fenced = '```json\n{"kind": "out_of_scope", "answer": "I cannot help with asylum."}\n```'
    assert parse_answer(Completion(text=fenced, model="m"), ctx).kind == "out_of_scope"
    assert parse_answer(reply({"kind": "weird", "answer": "a"}), ctx).kind == "answered"
    assert parse_answer(Completion(text="no json at all", model="m"), ctx).kind == "failed"
    assert _json_object('text {"a": "brace } in \\"string\\""} after') == {
        "a": 'brace } in "string"'
    }


def test_statute_references_in_provisions_use_the_written_form() -> None:
    assert _section_of("sfs:2005:716/6kap/2§") == ("sfs:2005:716", "6", "2")
    assert _section_of("sfs:2005:716/5akap/2b§") == ("sfs:2005:716", "5 a", "2 b")
    assert _section_of("web:sv/x#y") is None


# --- with a database ----------------------------------------------------------------------------


@pytest.fixture
def stored(conn: psycopg.Connection) -> Retriever:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    return Retriever(conn, FAKE)


@pytest.mark.db
def test_context_adds_only_the_transitional_points_that_concern_the_section(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    hits = stored.search("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20), k=1)
    assert hits[0].unit_id == "sfs:2005:716/6kap/2§"
    ctx = build_context(conn, hits, date(2026, 11, 20))
    provisions = [s.unit_id for s in ctx.sources if s.role == "transitional"]
    # Points 2 and 3 name 6 kap. 2 §; point 4 names 20 kap. and is left out; point 1 names none.
    assert "sfs:2026:295/övergång/2" in provisions
    assert "sfs:2026:295/övergång/3" in provisions
    assert "sfs:2026:295/övergång/4" not in provisions
    assert ctx.sources[0].dates == "applies from 2026-06-01"
    assert ctx.render().startswith("[S1] 6 kap. Arbetstillstånd, 2 §")


@pytest.mark.db
def test_context_follows_a_transitional_point_back_to_the_older_wording(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    query = "utlänning ansökt beviljats tillstånd före inresan"
    hits = [
        h
        for h in stored.search(query, as_of=date(2028, 2, 1), k=20)
        if h.unit_id.endswith("6kap/4§")
    ]
    ctx = build_context(conn, hits, date(2028, 2, 1))
    roles = [(s.role, s.unit_id) for s in ctx.sources]
    assert ("transitional", "sfs:2026:1687/övergång/2") in roles
    assert ("older_wording", "sfs:2005:716/6kap/4§") in roles


@pytest.mark.db
def test_answerer_sends_sources_and_the_date_and_returns_cited_answers(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM(
        json.dumps({"kind": "answered", "answer": "At least 90 percent of the median salary [S1]."})
    )
    answerer = Answerer(conn, stored, llm, AnswerConfig(model="test-model", k=3))
    answer = answerer.answer("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20))
    (request,) = llm.requests
    assert request.model == "test-model"
    assert "as of 2026-11-20" in request.system
    assert "[S1] 6 kap. Arbetstillstånd, 2 §" in request.system
    assert request.messages == [{"role": "user", "content": "lönen medianlönen procent lönekravet"}]
    assert answer.kind == "answered"
    assert answer.citations[0].unit_id == "sfs:2005:716/6kap/2§"


# --- the OpenRouter adapter (network stubbed) ---------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def test_openrouter_request_shape_and_cost(monkeypatch) -> None:
    sent = []

    def urlopen(request, timeout, context):
        sent.append(json.loads(request.data))
        return FakeResponse(
            json.dumps(
                {
                    "model": "anthropic/claude-haiku-5.5",
                    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.000012},
                }
            ).encode()
        )

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    result = OpenRouterLLM("key").complete(
        CompletionRequest(
            model="anthropic/claude-haiku-5.5",
            system="sys",
            messages=[{"role": "user", "content": "q"}],
        )
    )
    assert (result.text, result.input_tokens, result.output_tokens, result.cost_usd) == (
        "hi",
        10,
        2,
        0.000012,
    )
    body = sent[0]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert "temperature" not in body and "top_p" not in body  # current Claude models reject them
    assert "reasoning" not in body  # only sent when a request asks for it

    OpenRouterLLM("key").complete(
        CompletionRequest(
            model="mistralai/mistral-large-4-0",
            system="sys",
            messages=[{"role": "user", "content": "q"}],
            reasoning_effort="low",
        )
    )
    assert sent[1]["reasoning"] == {"effort": "low"}


def test_openrouter_retries_rate_limits_then_gives_up(monkeypatch) -> None:
    import urllib.error

    calls = []

    def urlopen(request, timeout, context):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, io.BytesIO(b"rate limited"))  # type: ignore[arg-type]

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    slept = []
    llm = OpenRouterLLM("key", retries=2, sleep=slept.append)
    with pytest.raises(LLMError, match="429"):
        llm.complete(CompletionRequest(model="m", system="s", messages=[]))
    assert len(calls) == 3 and slept == [2.0, 4.0]


def test_openrouter_needs_a_key() -> None:
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        OpenRouterLLM("")


# --- regressions from the slice 6 code review (2026-11-22) -------------------------------------


def test_section_lists_ranges_and_other_statutes_in_provisions() -> None:
    from permitnav.answering.context import section_refs

    law = "sfs:2005:716"
    assert section_refs("5 kap. 1 § och 6 kap. 2 och 3 §§ i den äldre lydelsen", law) == {
        ("5", "1"),
        ("6", "2"),
        ("6", "3"),
    }
    assert ("10", "9") in section_refs("upphävda 10 kap. 1–17 §§ gäller", law)
    assert section_refs("6 kap. 2, 2 a och 3 §§", law) == {("6", "2"), ("6", "2 a"), ("6", "3")}
    assert section_refs("enligt 8 kap. 2 § utlänningsförordningen", law) == set()
    assert section_refs("enligt 8 kap. 2 § utlänningslagen", law) == {("8", "2")}


def test_grouped_and_ranged_citations_are_split_and_checked() -> None:
    ctx = context_with("a", "b", "c")
    answer = parse_answer(
        reply({"kind": "answered", "answer": "Yes [S1, S2] and [S2–S3] not [S1, S9]."}), ctx
    )
    assert answer.text == "Yes [S1][S2] and [S2][S3] not [S1]."
    assert answer.dropped_markers == ["S9"]
    assert [c.marker for c in answer.citations] == ["S1", "S2", "S3"]


def test_malformed_model_output_never_crashes() -> None:
    ctx = context_with("x")
    assert parse_answer(reply({"kind": "follow_up", "answer": None}), ctx).kind == "failed"
    assert parse_answer(reply({"kind": "answered"}), ctx).kind == "failed"
    odd = {"answer": "x [S1]", "quotes": 5}
    assert parse_answer(reply(odd), ctx).citations[0].quote == ""
    odd = {"answer": "x [S1]", "quotes": [{"source": ["S1"], "swedish": "x"}]}
    assert parse_answer(reply(odd), ctx).citations[0].quote == ""


def test_the_first_exact_quote_per_source_is_used() -> None:
    ctx = context_with("Lönen ska vara minst 90 procent av medianlönen.")
    quotes = [
        {"source": "S1", "swedish": "en parafras", "translation": "a paraphrase"},
        {
            "source": "S1",
            "swedish": "minst 90 procent av medianlönen",
            "translation": "at least 90%",
        },
    ]
    answer = parse_answer(reply({"answer": "Yes [S1].", "quotes": quotes}), ctx)
    assert answer.citations[0].quote == "minst 90 procent av medianlönen"
    assert answer.citations[0].translation == "at least 90%"


def test_the_prompt_ships_with_the_package() -> None:
    from permitnav.answering.answerer import load_prompt

    assert "{sources}" in load_prompt() and "{as_of}" in load_prompt()
    assert "{language}" in load_prompt()  # rule 8: the answer language is set per question


@pytest.mark.db
def test_a_reply_cut_off_at_the_token_limit_is_retried_larger(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.llm.port import Completion as C

    class CutOffOnce(ScriptedLLM):
        def complete(self, request: CompletionRequest) -> C:
            self.requests.append(request)
            if len(self.requests) == 1:
                return C(
                    text='{"kind": "answered", "answer": "Lo',
                    model="m",
                    extra={"finish_reason": "length"},
                )
            return C(
                text='{"kind": "answered", "answer": "Done [S1]."}',
                model="m",
                extra={"finish_reason": "stop"},
            )

    llm = CutOffOnce("")
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m", max_tokens=1000)).answer("lönen")
    assert [r.max_tokens for r in llm.requests] == [1000, 2000]
    assert answer.kind == "answered"


@pytest.mark.db
def test_transitional_provisions_that_do_not_apply_on_the_date_are_left_out(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    conn.execute(
        "UPDATE unit_version SET pending = 'starts', valid_from = NULL WHERE unit_id = %s",
        ("sfs:2026:295/övergång/2",),
    )
    hits = stored.search("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20), k=1)
    ids = [s.unit_id for s in build_context(conn, hits, date(2026, 11, 20)).sources]
    assert "sfs:2026:295/övergång/2" not in ids
    assert "sfs:2026:295/övergång/3" in ids


def test_openrouter_retries_a_dropped_connection_and_a_non_json_reply(monkeypatch) -> None:
    import http.client

    calls = []

    def urlopen(request, timeout, context):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("closed mid-reply")
        if len(calls) == 2:
            return FakeResponse(b"<html>gateway</html>")
        return FakeResponse(
            json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {}}).encode()
        )

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    result = OpenRouterLLM("key", sleep=lambda s: None).complete(
        CompletionRequest(model="m", system="s", messages=[])
    )
    assert result.text == "ok" and len(calls) == 3


# --- relevance floor and the behaviour eval (phase 2, slice 2) --------------------------------


@pytest.mark.db
def test_questions_nothing_relevant_matches_never_reach_the_model(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": "x [S1]."}))
    answerer = Answerer(conn, stored, llm, AnswerConfig(model="m", min_score=0.99))
    answer = answerer.answer("How do I exchange my driving licence?", as_of=date(2026, 11, 20))
    assert llm.requests == []
    assert answer.kind == "out_of_scope"
    assert "Migrationsverket" in answer.text and answer.citations == []


@pytest.mark.db
def test_a_section_the_question_names_is_answered_whatever_its_score(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": "It says… [S1]."}))
    answerer = Answerer(conn, stored, llm, AnswerConfig(model="m", min_score=0.99))
    answerer.answer("Vad säger 6 kap. 3 § utlänningslagen?", as_of=date(2026, 11, 20))
    assert len(llm.requests) == 1


@pytest.mark.db
def test_the_floor_is_off_unless_configured(conn: psycopg.Connection, stored: Retriever) -> None:
    llm = ScriptedLLM(json.dumps({"kind": "out_of_scope", "answer": "No."}))
    Answerer(conn, stored, llm, AnswerConfig(model="m")).answer("körkort", date(2026, 11, 20))
    assert len(llm.requests) == 1


def test_the_prompt_declines_any_topic_outside_the_service() -> None:
    from permitnav.answering.answerer import load_prompt

    rule = next(line for line in load_prompt().splitlines() if "**Out of scope.**" in line)
    assert "asylum" in rule.lower() and "any other topic" in rule.lower()


@pytest.mark.db
def test_the_behaviour_eval_compares_answer_kinds_with_the_golden_flags(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.evals.answers import kind_table, run_answers
    from tests.test_retriever import item

    def reply(request: CompletionRequest) -> str:
        question = request.messages[0]["content"]
        kind = "out_of_scope" if "asyl" in question else "answered"
        return json.dumps({"kind": kind, "answer": "x"})

    items = [
        item("a", "lönen medianlönen", [["sfs:2005:716/6kap/2§"]]),
        item("b", "asylum question", [], expected_kind="out_of_scope"),
        item("c", "do I need to say more?", [], expected_kind="follow_up"),
    ]
    answerer = Answerer(conn, stored, ScriptedLLM(reply), AnswerConfig(model="m"))
    results = run_answers(items, answerer)
    assert [(r.item.id, r.kind) for r in results] == [
        ("a", "answered"),
        ("b", "out_of_scope"),
        ("c", "answered"),
    ]
    rows = {row.expected: row for row in kind_table(results)}
    assert (rows["answered"].n, rows["answered"].correct) == (1, 1)
    assert (rows["out_of_scope"].n, rows["out_of_scope"].correct) == (1, 1)
    assert (rows["follow_up"].correct, dict(rows["follow_up"].got)) == (0, {"answered": 1})


@pytest.mark.db
def test_with_the_floor_off_even_no_sources_reach_the_model(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM(json.dumps({"kind": "not_settled", "answer": "The sources are silent."}))
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m")).answer("lönen", date(2020, 1, 1))
    assert len(llm.requests) == 1 and answer.kind == "not_settled"


@pytest.mark.db
def test_a_named_section_reaches_the_model_in_every_retrieval_mode(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.retrieval.retriever import RetrievalConfig
    from tests.test_retriever import FAKE

    llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": "It says… [S1]."}))
    vector = Retriever(conn, FAKE, RetrievalConfig(mode="vector"))
    Answerer(conn, vector, llm, AnswerConfig(model="m", min_score=0.99)).answer(
        "Vad säger 6 kap. 3 § utlänningslagen?", date(2026, 11, 20)
    )
    assert len(llm.requests) == 1


@pytest.mark.db
def test_a_model_failure_on_one_item_keeps_the_rest_of_the_run(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.evals.answers import kind_table, run_answers
    from tests.test_retriever import item

    def reply(request: CompletionRequest) -> str:
        if "fails" in request.messages[0]["content"]:
            raise LLMError("OpenRouter: HTTP 502")
        return json.dumps({"kind": "answered", "answer": "x"})

    items = [item("a", "lönen", []), item("b", "this one fails", []), item("c", "lönen", [])]
    results = run_answers(items, Answerer(conn, stored, ScriptedLLM(reply), AnswerConfig("m")))
    assert [r.kind for r in results] == ["answered", "failed", "answered"]
    assert "HTTP 502" in results[1].answer.text
    (row,) = kind_table(results)
    assert (row.n, row.correct, dict(row.got)) == (3, 2, {"failed": 1})


def test_combined_expects_flags_are_rejected(tmp_path) -> None:
    from permitnav.evals.golden import load_golden

    line = {
        "id": "x",
        "question": "q",
        "original_language": "en",
        "translation_of": None,
        "type": "not-settled",
        "as_of": "2026-11-20",
        "expected": [],
        "review": "draft",
        "expects_follow_up": True,
        "expects_not_settled": True,
        "expects_out_of_scope": False,
    }
    path = tmp_path / "g.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at most one"):
        load_golden(path)


# --- cross-references (phase 2, slice 3) --------------------------------------------------------


def test_cited_units_resolve_chapterless_references_to_the_same_chapter() -> None:
    from permitnav.parsing.references import cited_units

    law = "sfs:2005:716"
    text = (
        "får förlängas om anställningen uppfyller förutsättningarna i 2 § första stycket. Om "
        "ansökan avser en ny anställning gäller även det som föreskrivs i 2 § andra stycket. "
        "Förlängning ska vägras om ... enligt 7 kap. 7 e § första stycket 1."
    )
    assert cited_units(text, law, "6") == ["sfs:2005:716/6kap/2§", "sfs:2005:716/7kap/7e§"]
    # Lists with "eller" and "och", and a range; the chapter comes from the citing section.
    assert cited_units("med stöd av 3 eller 3 a § eller 10–11 §§", law, "5") == [
        "sfs:2005:716/5kap/3§",
        "sfs:2005:716/5kap/3a§",
        "sfs:2005:716/5kap/10§",
        "sfs:2005:716/5kap/11§",
    ]
    # Other statutes: named ours resolve to them; anything else is left out.
    assert cited_units("enligt 5 kap. 3 § utlänningslagen", "sfs:2006:97", "4") == [
        "sfs:2005:716/5kap/3§"
    ]
    assert cited_units("enligt 4 kap. 1 a eller 1 b § brottsbalken", law, "20") == []
    assert cited_units("enligt 8 § lagen (2016:752) om tillfälliga begränsningar", law, "5") == []
    assert cited_units("Lag (2022:303).", law, "6") == []


@pytest.mark.db
def test_context_adds_the_sections_a_retrieved_section_refers_to(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    hits = stored.search("Vad säger 6 kap. 4 a § utlänningslagen?", date(2026, 11, 20), k=1)
    assert hits[0].unit_id == "sfs:2005:716/6kap/4a§"
    ctx = build_context(conn, hits, date(2026, 11, 20))
    referenced = [s for s in ctx.sources if s.role == "referenced"]
    # "2 § andra stycket" is 6 kap. 2 §; 7 kap. 7 e § is not in the fixture, so it is skipped.
    assert [s.unit_id for s in referenced] == ["sfs:2005:716/6kap/2§"]
    assert referenced[0].dates == "applies from 2026-06-01"
    assert "| referenced |" in ctx.render()


@pytest.mark.db
def test_a_referenced_section_already_in_the_context_is_not_repeated(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    hits = stored.search("Vad säger 6 kap. 2 och 4 a §§ utlänningslagen?", date(2026, 11, 20), k=2)
    ctx = build_context(conn, hits, date(2026, 11, 20))
    assert [s.unit_id for s in ctx.sources].count("sfs:2005:716/6kap/2§") == 1


@pytest.mark.db
def test_references_are_capped(
    conn: psycopg.Connection, stored: Retriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    from permitnav.answering import context

    monkeypatch.setattr(context, "MAX_REFERENCES", 0)
    hits = stored.search("Vad säger 6 kap. 4 a § utlänningslagen?", date(2026, 11, 20), k=1)
    assert not [
        s for s in build_context(conn, hits, date(2026, 11, 20)).sources if s.role == "referenced"
    ]


def test_the_prompt_explains_referenced_sources() -> None:
    from permitnav.answering.answerer import load_prompt

    assert "`referenced`" in load_prompt()


def test_reference_parsing_review_cases() -> None:
    from permitnav.parsing.references import cited_units, ordered_section_refs

    law = "sfs:2005:716"
    # An unknown document still skips references to other acts (finding 2).
    assert ordered_section_refs("6 kap. 2 § lagen (2016:752)", "") == []
    # The chapter named after a chapterless reference, or before it in a joined list (4).
    assert cited_units("enligt 2 § i 5 kap. gäller", law, "6") == ["sfs:2005:716/5kap/2§"]
    assert cited_units("6 kap. 2 § och 3 §", law, "7") == [
        "sfs:2005:716/6kap/2§",
        "sfs:2005:716/6kap/3§",
    ]
    assert cited_units("8 kap. 2 § brottsbalken och 3 §", law, "7") == []
    # Letter ranges (6).
    assert cited_units("enligt 1 a–1 c §§", law, "6") == [
        "sfs:2005:716/6kap/1a§",
        "sfs:2005:716/6kap/1b§",
        "sfs:2005:716/6kap/1c§",
    ]
    # Common words are not statute names (7).
    assert cited_units("enligt 2 § i förslagen", law, "6") == ["sfs:2005:716/6kap/2§"]


@pytest.mark.db
def test_a_section_search_found_keeps_its_role_when_an_earlier_hit_cites_it(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    hits = stored.search("Vad säger 6 kap. 4 a och 2 §§ utlänningslagen?", date(2026, 11, 20), k=2)
    assert [h.unit_id for h in hits] == ["sfs:2005:716/6kap/4a§", "sfs:2005:716/6kap/2§"]
    ctx = build_context(conn, hits, date(2026, 11, 20))
    roles = {s.unit_id: s.role for s in ctx.sources}
    assert roles["sfs:2005:716/6kap/2§"] == "retrieved"


def test_a_long_paragraph_is_cut_at_a_sentence() -> None:
    from permitnav.answering.context import WHOLE_UNIT_TOKENS, _shortened

    text = "Första meningen. " * (WHOLE_UNIT_TOKENS // 3)
    short = _shortened(text)
    assert short.endswith("meningen. […]")
    assert len(short) <= WHOLE_UNIT_TOKENS * 4 + 4


def test_a_reply_with_literal_line_breaks_inside_strings_is_read() -> None:
    """The model sometimes writes real line breaks inside JSON strings. Strict JSON rejects
    them, and the parser then returned the first quote object instead of the reply."""
    ctx = context_with("Lönen ska vara minst 90 procent av medianlönen.")
    quote = '{"source": "S1", "swedish": "minst 90 procent", "translation": "at least 90 percent"}'
    raw = (
        '```json\n{\n  "kind": "answered",\n'
        '  "answer": "First paragraph [S1].\n\nSecond paragraph.",\n'
        f'  "quotes": [{quote}]\n}}\n```'
    )
    answer = parse_answer(Completion(text=raw, model="m"), ctx)
    assert answer.kind == "answered"
    assert answer.text.startswith("First paragraph [S1].")


@pytest.mark.db
def test_an_unreadable_reply_is_asked_for_again_once(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    replies = iter(
        [
            "The salary must be at least 90 percent of the median salary.",  # no JSON at all
            json.dumps({"kind": "answered", "answer": "At least 90 percent [S1]."}),
        ]
    )
    llm = ScriptedLLM(lambda request: next(replies))
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m")).answer(
        "lönen", date(2026, 11, 20)
    )
    assert answer.kind == "answered"
    first, second = llm.requests
    assert second.messages[0] == first.messages[0]  # the question
    assert second.messages[1] == {
        "role": "assistant",
        "content": "The salary must be at least 90 percent of the median salary.",
    }
    assert "JSON" in second.messages[2]["content"]


@pytest.mark.db
def test_a_second_unreadable_reply_fails_without_a_third_call(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM("Not JSON either time.")
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m")).answer(
        "lönen", date(2026, 11, 20)
    )
    assert answer.kind == "failed" and len(llm.requests) == 2


@pytest.mark.db
def test_an_empty_reply_is_asked_again_without_an_empty_assistant_turn(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    replies = iter(["", json.dumps({"kind": "answered", "answer": "Svar [S1]."})])
    llm = ScriptedLLM(lambda request: next(replies))
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m")).answer(
        "lönen", date(2026, 11, 20)
    )
    assert answer.kind == "answered"
    assert llm.requests[1].messages == llm.requests[0].messages  # the same request, repeated


@pytest.mark.db
def test_a_failing_re_ask_keeps_the_first_answer_and_its_cost(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.llm.port import Completion as C

    class FailsSecond(ScriptedLLM):
        def complete(self, request: CompletionRequest) -> C:
            self.requests.append(request)
            if len(self.requests) == 2:
                raise LLMError("OpenRouter: timeout")
            return C(text="Prose, no JSON.", model="m", cost_usd=0.001)

    llm = FailsSecond("")
    answer = Answerer(conn, stored, llm, AnswerConfig(model="m")).answer(
        "lönen", date(2026, 11, 20)
    )
    assert answer.kind == "failed"
    assert answer.completion is not None and answer.completion.cost_usd == 0.001
    assert answer.earlier == []


@pytest.mark.db
def test_a_re_asked_reply_that_is_cut_off_says_it_was_too_long(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    from permitnav.llm.port import Completion as C

    class CutOffSecond(ScriptedLLM):
        def complete(self, request: CompletionRequest) -> C:
            self.requests.append(request)
            if len(self.requests) == 1:
                return C(text="Prose.", model="m", extra={"finish_reason": "stop"})
            return C(
                text='{"kind": "answered", "answer": "Lo',
                model="m",
                extra={"finish_reason": "length"},
            )

    answer = Answerer(conn, stored, CutOffSecond(""), AnswerConfig(model="m")).answer(
        "lönen", date(2026, 11, 20)
    )
    assert answer.kind == "failed" and "too long" in answer.text


def test_the_re_ask_covers_a_missing_answer_field_and_costs_are_floats() -> None:
    from permitnav.answering.answerer import REASK, Answer
    from permitnav.evals.answers import AnswerResult
    from tests.test_retriever import item

    assert '"answer" field' in REASK
    cost = AnswerResult(item("a", "q", []), Answer(kind="out_of_scope", text="x")).cost_usd
    assert cost == 0.0 and isinstance(cost, float)


@pytest.mark.db
def test_the_answer_language_is_set_from_the_question(
    conn: psycopg.Connection, stored: Retriever
) -> None:
    llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": "Svar [S1]."}))
    answerer = Answerer(conn, stored, llm, AnswerConfig(model="m"))
    answerer.answer("Hur hög lön krävs för arbetstillstånd?", date(2026, 11, 20))
    answerer.answer("What salary is required for a work permit?", date(2026, 11, 20))
    answerer.answer("Kann mein Ehepartner in Schweden arbeiten?", date(2026, 11, 20))
    answerer.answer("Kan jag få a work permit?", date(2026, 11, 20))  # a tie
    swedish, english, german, tie = (r.system for r in llm.requests)
    assert "Answer in Swedish" in swedish and "Answer in English" in english
    # Neither clearly Swedish nor English: no language is imposed.
    for system in (german, tie):
        assert "Answer in the language of the question" in system


def test_the_behaviour_eval_counts_answers_in_the_questions_language() -> None:
    from permitnav.answering.answerer import Answer
    from permitnav.evals.answers import AnswerResult, language_matches
    from permitnav.llm.port import Completion
    from tests.test_retriever import item

    def result(question: str, language: str, text: str, kind: str = "answered") -> AnswerResult:
        answer = Answer(kind=kind, text=text, completion=Completion(text=text, model="m"))
        return AnswerResult(item("x", question, [], language=language), answer)

    results = [
        result("Hur hög lön krävs?", "sv", "Lönen måste vara minst 90 procent av medianlönen."),
        result("Hur hög lön krävs?", "sv", "The salary must be at least 90 percent of the median."),
        result("What salary?", "en", "The salary must be at least 90 percent of the median."),
        # English with quoted Swedish law text is still English.
        result(
            "What salary?",
            "en",
            'The law says "lönen ska vara minst 90 procent av medianlönen" for the permit [S1].',
        ),
        # A decline the model wrote counts; one PermitNav wrote itself (no model call) does not.
        result("Får jag medborgarskap?", "sv", "This is outside the service.", kind="out_of_scope"),
        AnswerResult(
            item("y", "Får jag?", [], language="sv"), Answer(kind="out_of_scope", text="Nothing.")
        ),
    ]
    assert language_matches(results) == (3, 5)
