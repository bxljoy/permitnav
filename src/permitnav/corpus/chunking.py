"""Splitting a version's text into chunks (phase 1 builds both strategies phase 2 compares).

- `structure`: one chunk per version, the heading prepended ("6 kap. Arbetstillstånd, 2 §").
  A version longer than `max_tokens` is split at paragraph breaks, each piece with the heading.
- `fixed500`: the baseline. Windows of ~500 tokens with 15% overlap over the version's text,
  no heading. Chunks never cross units, because citations and metrics count units (D2).

Token counts are estimated as characters / 4. That is close enough to split by; exact counts
come from the embedding model's tokenizer if phase 2 needs them.
"""

from __future__ import annotations

STRATEGIES = ("structure", "fixed500")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def chunk_text(strategy: str, heading: str, text: str, max_tokens: int = 800) -> list[str]:
    if strategy == "structure":
        return _structure(heading, text, max_tokens)
    if strategy == "fixed500":
        return _fixed(text, size=500, overlap=75)
    raise ValueError(f"unknown chunking strategy: {strategy!r}")


def _structure(heading: str, text: str, max_tokens: int) -> list[str]:
    def with_heading(body: str) -> str:
        return f"{heading}\n\n{body}"

    def fits(body: str) -> bool:
        return estimate_tokens(with_heading(body)) <= max_tokens

    if fits(text):
        return [with_heading(text)]
    # Room left for the body once the heading is in; the -1 absorbs rounding in the estimate.
    budget = max_tokens - estimate_tokens(with_heading("")) - 1
    if budget < 8:
        raise ValueError(f"heading too long to chunk within {max_tokens} tokens: {heading[:60]!r}")
    pieces: list[str] = []
    current: list[str] = []
    for paragraph in text.split("\n\n"):
        if current and not fits("\n\n".join([*current, paragraph])):
            pieces.append("\n\n".join(current))
            current = [paragraph]
        else:
            current.append(paragraph)
    if current:
        pieces.append("\n\n".join(current))
    chunks: list[str] = []
    for piece in pieces:  # a single paragraph longer than the budget falls back to windows
        if fits(piece):
            chunks.append(with_heading(piece))
        else:
            chunks += [with_heading(w) for w in _fixed(piece, size=budget, overlap=budget // 7)]
    return chunks


def _fixed(text: str, size: int, overlap: int) -> list[str]:
    """Windows of `size` tokens (by the estimate) stepping by size - overlap, cut at spaces."""
    if size < 1 or not 0 <= overlap < size:
        raise ValueError(f"bad window: size={size}, overlap={overlap}")
    width, step = size * 4, (size - overlap) * 4
    if len(text) <= width:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + width)
        if end < len(text):
            space = text.rfind(" ", start + step, end)
            end = space if space > start else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        next_start = max(start + step, end - overlap * 4)
        space = text.find(" ", next_start)
        start = space + 1 if 0 <= space < end else next_start
    return [c for c in chunks if c]
