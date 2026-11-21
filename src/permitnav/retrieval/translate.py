"""Translating questions to Swedish for retrieval (phase 2, slice 4).

The law is in Swedish. An English question's words never occur in it, so keyword search
(BM25) can only match the law through a Swedish version of the question. Swedish questions
are recognised without a model call and left as they are.

Translations are cached in a JSON file per (model, question), so repeated evals are free and
give the same results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Protocol

from permitnav.llm.port import LLM, CompletionRequest
from permitnav.prompting import load_prompt

_SWEDISH = (
    "och att jag är det som för på med har kan min mitt mina vad hur när om inte ett av "
    "till får måste ska jobba arbeta söka ansöka sambo make maka barn lön ny nytt efter "
    "innan"
)
_ENGLISH = (
    "the and is are to my can what how when do does for of a an in on with have has not "
    "need if or his her their our you your work job apply permit spouse say"
)
SWEDISH_WORDS = frozenset(_SWEDISH.split())
ENGLISH_WORDS = frozenset(_ENGLISH.split())
WORD = re.compile(r"[A-Za-zÅÄÖåäöÉé]+")
STATUTE_NAME = re.compile(r"\b\w*(?:lagen|förordningen)s?\b", re.IGNORECASE)


def is_swedish(text: str) -> bool:
    """More Swedish than English evidence. Swedish evidence: Swedish function words, and
    lowercase words with å, ä or ö; English evidence: English function words. Statute names
    ("utlänningslagens") and capitalised words ("Malmö") are ignored, so an English question
    that mentions them, or a Swedish term like "uppehållstillstånd", stays English."""
    words = WORD.findall(STATUTE_NAME.sub(" ", text))
    lower = [w.lower() for w in words]
    swedish = sum(w in SWEDISH_WORDS for w in lower)
    swedish += sum(w.islower() and any(ch in "åäö" for ch in w) for w in words)
    english = sum(w in ENGLISH_WORDS for w in lower)
    return swedish > english


class Translator(Protocol):
    def translate(self, question: str) -> str: ...


MAX_TOKENS = 400


class LLMTranslator:
    """Translates with an LLM. `calls` and `cost_usd` count the model calls made (cache hits
    are free); `cost_unknown` counts replies whose cost the provider did not report."""

    def __init__(self, llm: LLM, model: str, cache_path: Path | None = None) -> None:
        self._llm = llm
        self._model = model
        self._cache_path = cache_path
        self._prompt = load_prompt("translate.md")
        # A changed prompt or token limit is a different translator: its own cache entries.
        recipe = f"{self._prompt}\n{MAX_TOKENS}".encode()
        self._version = hashlib.sha256(recipe).hexdigest()[:12]
        self._cache: dict[str, str] = _read_cache(cache_path)
        self.calls = 0
        self.cost_usd = 0.0
        self.cost_unknown = 0

    def translate(self, question: str) -> str:
        key = f"{self._model}\t{self._version}\t{question}"
        if key not in self._cache:
            completion = self._llm.complete(
                CompletionRequest(
                    model=self._model,
                    system=self._prompt,
                    messages=[{"role": "user", "content": question}],
                    max_tokens=MAX_TOKENS,
                )
            )
            self.calls += 1
            if completion.cost_usd is None:
                self.cost_unknown += 1
            else:
                self.cost_usd += completion.cost_usd
            translation = completion.text.strip().strip('"').strip()
            if not translation:
                return question  # nothing usable; not cached, so a later run can retry
            self._cache[key] = translation
            _write_cache(self._cache_path, self._cache)
        return self._cache[key]


def _read_cache(path: Path | None) -> dict[str, str]:
    """The cache file's entries; a missing or damaged file is an empty cache."""
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"warning: ignoring a damaged translation cache: {path}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path | None, cache: dict[str, str]) -> None:
    """Written to a temporary file and moved into place, so an interrupted write never leaves
    a half-written cache."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)
