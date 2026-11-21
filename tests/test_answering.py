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
