"""Questions that say what something is not ("not an EU Blue Card").

Search matches words and topics and cannot read "not", so such a question is handled with
care in two places: intake writes a search question without what was set aside
(answering/answerer.py, agent/intake.py), and the search does not go looking for a page
variant the question only names to rule it out (retrieval/retriever.py).
"""

from __future__ import annotations

import re

# Words that rule something out, in English and Swedish. Broad on purpose: a match only
# means the search question is asked for (single questions) or used (all questions).
_RULES_OUT = re.compile(
    r"\b(?:not|no(?!\.)|never|without|cannot|except|excluding|rather\s+than|instead\s+of"
    r"|other\s+than|apart\s+from|neither|nor|non"
    r"|inte|ej|icke|utan|ingen|inget|inga|ingenting|förutom|i\s+stället\s+för|istället\s+för"
    r"|aldrig|varken)\b"
    # don't, dont, don´t, isn’t, can't, cant …: the apostrophe is often missing or an accent
    r"|\b(?:do|does|did|is|are|was|were|have|has|had|ca|wo|would|should|could)n[’'´`]?t\b"
    r"|n[’'´`]t\b",
    re.IGNORECASE,
)


def rules_out(question: str) -> bool:
    """Whether the question says what something is not, which search would misread."""
    return _RULES_OUT.search(question) is not None
