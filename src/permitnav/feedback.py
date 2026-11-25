"""Feedback a person chose to save (phase 6, slice 3).

The default is that nothing a person types is stored. "Report a problem" under an answer
opens a form showing exactly what would be saved; only an explicit save sends it here. It is
stored for the maintainer's review in a private table: never in traces or logs, never in the
public corpus snapshot. A case enters the golden set only after review and rewriting into a
generic question; the stored row is then deleted (`permitnav feedback delete`).

Nothing ties a row to a request: the day is stored, not the moment; the id is random, not a
counter; and the feedback endpoints are kept out of the access logs (`quiet_access_log`,
deploy/Caddyfile). Text that carries an email address or a Swedish personal number is
refused: the form says not to include them, and this is the check behind that promise.

The browser makes the withdrawal code and sends it with the save; only its hash is stored.
So the person can undo a submission even when the reply to the save was lost, and saving
again with the same code replaces the row instead of adding one.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import date

import psycopg

MAX_TEXT_CHARS = 8000  # the answer as shown; a question is at most 2,000
MAX_COMMENT_CHARS = 2000
MAX_CITED = 40
MAX_WAITING = 500  # unreviewed rows kept; more are refused until some are reviewed
KINDS = ("answered", "follow_up", "not_settled", "out_of_scope")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w-]+")
PERSONAL_NUMBER = re.compile(r"(?<![0-9])((?:19|20)?)([0-9]{6})([-+]?)([0-9]{4})(?![0-9])")


class FeedbackRefused(ValueError):
    """The submission cannot be stored; the message says why, for the person."""


@dataclass(frozen=True)
class Case:
    id: str
    created_on: date
    question: str
    answer: str
    comment: str
    kind: str
    as_of: date
    cited: list[str]


def is_personal_number(century: str, born: str, separator: str, last: str) -> bool:
    """A real personnummer or samordningsnummer: a date of birth (the day +60 for a
    coordination number) and a correct check digit. An organisation number (its "month" is
    20 or more) or an arbitrary ten-digit number is not one; nor are ten bare digits
    starting 07, which is how a mobile number is written (one in twenty of those would
    otherwise pass as a child born in the 2000s)."""
    if not century and not separator and born.startswith("07"):
        return False
    month, day = int(born[2:4]), int(born[4:6])
    if not (1 <= month <= 12 and (1 <= day <= 31 or 61 <= day <= 91)):
        return False
    total = 0
    for position, digit in enumerate(born + last):
        value = int(digit) * (2 if position % 2 == 0 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def identifiers_in(*texts: str) -> list[str]:
    """Kinds of identifying data found in the texts (empty: none of the known kinds).
    Deliberately few and precise: a false alarm stops a person who is trying to help. Names
    and dates cannot be detected reliably; the form asks for care and a person reviews."""
    found = []
    if any(EMAIL.search(t) for t in texts):
        found.append("an email address")
    if any(is_personal_number(*m.groups()) for t in texts for m in PERSONAL_NUMBER.finditer(t)):
        found.append("a Swedish personal number")
    return found


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def save(
    conn: psycopg.Connection,
    code: str,
    question: str,
    answer: str,
    comment: str,
    kind: str,
    as_of: date,
    cited: list[str],
) -> None:
    """Store a submission under the browser's withdrawal code. Saving again with the same
    code replaces it (a retry after a lost reply, perhaps edited), so nothing is stored twice."""
    if "\x00" in question + answer + comment:
        raise FeedbackRefused("The text contains a character that cannot be saved. Remove it.")
    found = identifiers_in(question, answer, comment)
    if found:
        raise FeedbackRefused(f"The text contains {' and '.join(found)}. Remove it and save again.")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('permitnav.feedback'))")
        # Only ids of units that exist: `cited` is not shown for editing, so it must not be
        # a way to store free text.
        known = conn.execute(
            "SELECT id FROM unit WHERE id = ANY(%s) ORDER BY id", ([c for c in cited if c],)
        ).fetchall()
        values = (question, answer, comment, kind, as_of, [row[0] for row in known])
        updated = conn.execute(
            "UPDATE feedback SET question = %s, answer = %s, comment = %s, kind = %s,"
            " as_of = %s, cited = %s WHERE withdraw_hash = %s",
            (*values, _hash(code)),
        ).rowcount
        if updated:
            return
        waiting = conn.execute("SELECT count(*) FROM feedback").fetchone()
        if waiting is not None and waiting[0] >= MAX_WAITING:
            raise FeedbackRefused("Feedback is full for now. Please try again another day.")
        conn.execute(
            "INSERT INTO feedback (id, question, answer, comment, kind, as_of, cited,"
            " withdraw_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (secrets.token_hex(5), *values, _hash(code)),
        )


def withdraw(conn: psycopg.Connection, code: str) -> bool:
    """Delete the submission saved under this code. False if there is none (never saved,
    already withdrawn, or reviewed and deleted)."""
    return (
        conn.execute("DELETE FROM feedback WHERE withdraw_hash = %s", (_hash(code),)).rowcount > 0
    )


def waiting(conn: psycopg.Connection) -> list[Case]:
    rows = conn.execute(
        "SELECT id, created_on, question, answer, comment, kind, as_of, cited FROM feedback"
        " ORDER BY created_on, id"
    ).fetchall()
    return [Case(*row) for row in rows]


def delete(conn: psycopg.Connection, ids: list[str]) -> int:
    """Remove reviewed submissions; returns how many were there."""
    return conn.execute("DELETE FROM feedback WHERE id = ANY(%s)", (ids,)).rowcount


class _NoFeedbackLines(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args  # uvicorn.access: (client, method, path, http version, status)
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?")[0]
            return not (path == "/feedback" or path.startswith("/feedback/"))
        return True


def quiet_access_log() -> None:
    """Keep the feedback endpoints out of the server's access log: a line with the moment
    of a request could be matched to the row it stored."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoFeedbackLines) for f in access.filters):
        access.addFilter(_NoFeedbackLines())
