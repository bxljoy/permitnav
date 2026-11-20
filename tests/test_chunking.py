"""Chunking strategies (corpus.chunking) and the fake embedder."""

from __future__ import annotations

import math

import pytest

from permitnav.corpus.chunking import chunk_text, estimate_tokens
from permitnav.embedding.fake import FakeEmbedder

HEADING = "6 kap. Arbetstillstånd, 2 §"


def test_structure_puts_a_short_section_in_one_chunk_with_its_heading() -> None:
    (chunk,) = chunk_text("structure", HEADING, "Arbetstillstånd får ges.")
    assert chunk == f"{HEADING}\n\nArbetstillstånd får ges."


def test_structure_splits_a_long_section_at_paragraphs_with_the_heading_on_each() -> None:
    paragraphs = [f"Stycke {i}. " + "ord " * 300 for i in range(4)]
    chunks = chunk_text("structure", HEADING, "\n\n".join(paragraphs), max_tokens=800)
    assert len(chunks) >= 2
    assert all(c.startswith(HEADING + "\n\n") for c in chunks)
    assert all(estimate_tokens(c) <= 800 for c in chunks)
    joined = " ".join(chunks)
    assert all(f"Stycke {i}." in joined for i in range(4))


def test_structure_windows_a_single_paragraph_that_is_too_long() -> None:
    chunks = chunk_text("structure", HEADING, "ord " * 2000, max_tokens=800)
    assert len(chunks) > 1
    assert all(c.startswith(HEADING) and estimate_tokens(c) <= 800 for c in chunks)


def test_fixed_windows_overlap_and_carry_no_heading() -> None:
    words = [f"w{i}" for i in range(1500)]
    chunks = chunk_text("fixed500", HEADING, " ".join(words))
    assert len(chunks) >= 3
    assert not any(c.startswith(HEADING) for c in chunks)
    assert all(estimate_tokens(c) <= 500 for c in chunks)
    first, second = set(chunks[0].split()), set(chunks[1].split())
    assert first & second  # consecutive windows share text
    assert set(" ".join(chunks).split()) == set(words)  # nothing is lost


def test_fixed_keeps_a_short_text_whole() -> None:
    assert chunk_text("fixed500", HEADING, "Kort text.") == ["Kort text."]


def test_unknown_strategy_is_an_error() -> None:
    with pytest.raises(ValueError):
        chunk_text("semantic", HEADING, "x")


def test_fake_embedder_is_deterministic_normalised_and_word_sensitive() -> None:
    fake = FakeEmbedder(dims=64)
    a, b, c = fake.embed_documents(["arbetstillstånd lön", "lön arbetstillstånd", "medborgarskap"])
    assert a == b and len(a) == 64
    assert math.isclose(sum(x * x for x in a), 1.0)
    assert sum(x * y for x, y in zip(a, c, strict=True)) < sum(
        x * y for x, y in zip(a, b, strict=True)
    )


# --- regressions from the slice 3 code review (2026-11-22) -------------------------------------


def test_a_heading_too_long_for_the_limit_is_an_error_not_a_hang() -> None:
    with pytest.raises(ValueError, match="heading too long"):
        chunk_text("structure", "H" * 40, "ord " * 100, max_tokens=10)


def test_structure_chunks_never_exceed_the_limit_including_the_heading() -> None:
    chunks = chunk_text("structure", "abcdefg", "x" * 3195 + "\n\n" + "y" * 3195, max_tokens=800)
    assert all(estimate_tokens(c) <= 800 for c in chunks)
