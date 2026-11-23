#!/usr/bin/env python3
"""Validate a golden-set JSONL file (format: evals/README.md, design: docs/DESIGN.md D16-D20).

Usage: python3 scripts/validate_golden.py evals/golden.dev.jsonl [more.jsonl ...]

Exits non-zero if any item is invalid. Prints a coverage table by type and language.
Standard library only.
"""

import json
import re
import sys
from collections import Counter
from datetime import date, timedelta

TYPES = [
    "lookup",
    "identifier",
    "multi-source",
    "time-dependent",
    "version-dependent",
    "needs-follow-up",
    "not-settled",
    "out-of-scope",
]
LANGS = {"en", "sv"}
REVIEW = {"draft", "checked"}
REQUIRED = {
    "id": str,
    "question": str,
    "original_language": str,
    "type": str,
    "as_of": str,
    "expected": list,
    "reference_answer": str,
    "expects_follow_up": bool,
    "expects_not_settled": bool,
    "expects_out_of_scope": bool,
    "review": str,
}
OPTIONAL = {"translation_of", "notes", "authority"}
AUTHORITY = {"law", "agency", "both"}  # D22

HISTORY_START = date(2023, 1, 1)  # D10
FUTURE_LIMIT = date.today() + timedelta(days=2 * 365)  # D9

UNIT_PATTERNS = [
    re.compile(r"^sfs:\d{4}:\d+/\d+[a-z]?kap/\d+[a-z]?§$"),
    re.compile(r"^sfs:\d{4}:\d+/\d+[a-z]?§$"),
    re.compile(r"^sfs:\d{4}:\d+/övergång/\d+$"),
    re.compile(r"^rs:RS/\d{3}/\d{4}/\d+(?:\.\d+)*$"),  # numbered sections: 2.3.3.1
    re.compile(r"^mig:\d{4}:\d+$"),
    re.compile(r"^web:(sv|en)/[a-z0-9\-/]+(#[a-z0-9\-]+)?$"),
]
# A Migrationsverket page that answers the item but is not indexed yet. Allowed only in draft items.
PLACEHOLDER = re.compile(r"^web:tbd/[a-z0-9\-]+$")

# Sanitising heuristics (public files only). These catch obvious leaks, not all of them.
PII_PATTERNS = {
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "Swedish personal number": re.compile(r"\b(19|20)?\d{6}[-+]\d{4}\b"),
    "phone number": re.compile(r"(\+\d{2}[\s-]?)?\b0\d{1,3}[\s-]?\d{2,3}[\s-]?\d{2}[\s-]?\d{2}\b"),
    "case number (Dnr / Ärendenr)": re.compile(r"\b(dnr|ärendenr|ärendenummer|case no)\b", re.I),
}


def unit_ok(unit):
    return any(p.match(unit) for p in UNIT_PATTERNS) or bool(PLACEHOLDER.match(unit))


def check_item(item, line):
    errors = []

    def err(msg):
        errors.append("line %d (%s): %s" % (line, item.get("id", "?"), msg))

    for field, kind in REQUIRED.items():
        if field not in item:
            err("missing field '%s'" % field)
        elif not isinstance(item[field], kind):
            err("field '%s' must be %s" % (field, kind.__name__))
    unknown = set(item) - set(REQUIRED) - OPTIONAL
    if unknown:
        err("unknown fields: %s" % ", ".join(sorted(unknown)))
    if errors:
        return errors

    if not item["question"].strip():
        err("empty question")
    if item["original_language"] not in LANGS:
        err("original_language must be one of %s" % sorted(LANGS))
    if item["type"] not in TYPES:
        err("type must be one of %s" % TYPES)
    if item["review"] not in REVIEW:
        err("review must be one of %s" % sorted(REVIEW))
    if "authority" in item and item["authority"] not in AUTHORITY:
        err("authority must be one of %s" % sorted(AUTHORITY))

    try:
        as_of = date.fromisoformat(item["as_of"])
        if as_of < HISTORY_START:
            err(
                "as_of %s is before %s, the start of rebuilt history (D10)" % (as_of, HISTORY_START)
            )
        if as_of > FUTURE_LIMIT:
            err("as_of %s is more than 2 years ahead (D9)" % as_of)
    except ValueError:
        err("as_of must be YYYY-MM-DD")

    groups = item["expected"]
    out_of_scope = item["expects_out_of_scope"]
    if out_of_scope != (item["type"] == "out-of-scope"):
        err("expects_out_of_scope must be true exactly when type is 'out-of-scope'")
    flags = [
        f for f in ("expects_follow_up", "expects_not_settled", "expects_out_of_scope") if item[f]
    ]
    if len(flags) > 1:
        err("at most one expects_* flag may be true, got %s" % ", ".join(flags))
    if item["type"] == "not-settled" and not item["expects_not_settled"]:
        err("type 'not-settled' needs expects_not_settled: true")
    if item["type"] == "needs-follow-up" and not item["expects_follow_up"]:
        err("type 'needs-follow-up' needs expects_follow_up: true")
    if not groups and not out_of_scope:
        err("expected is empty; only out-of-scope items may have no expected sources")
    for g, group in enumerate(groups):
        if not isinstance(group, list) or not group:
            err("expected[%d] must be a non-empty list of unit IDs" % g)
            continue
        for unit in group:
            if not isinstance(unit, str) or not unit_ok(unit):
                err("expected[%d]: '%s' is not a valid unit ID (see evals/README.md)" % (g, unit))
    placeholders = [
        u
        for g in groups
        if isinstance(g, list)
        for u in g
        if isinstance(u, str) and PLACEHOLDER.match(u)
    ]
    if placeholders and item["review"] == "checked":
        err("checked items cannot have placeholder sources: %s" % ", ".join(placeholders))
    if item["type"] == "multi-source" and len(groups) < 2:
        err("type 'multi-source' needs at least 2 required groups")

    for field in ("question", "reference_answer", "notes"):
        text = item.get(field) or ""
        for name, pattern in PII_PATTERNS.items():
            if pattern.search(text):
                err("possible %s in '%s'; sanitise before committing" % (name, field))
    return errors


def main(paths):
    if not paths:
        print(__doc__.strip())
        return 2
    items, errors = [], []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError as e:
                    errors.append("%s line %d: invalid JSON (%s)" % (path, n, e))
                    continue
                if not isinstance(item, dict):
                    errors.append("%s line %d: each line must be a JSON object" % (path, n))
                    continue
                errors += ["%s %s" % (path, e) for e in check_item(item, n)]
                items.append(item)

    by_id = {}
    for item in items:
        i = item.get("id")
        if i in by_id:
            errors.append("duplicate id '%s'" % i)
        by_id[i] = item
    for item in items:
        t = item.get("translation_of")
        if t is None:
            continue
        orig = by_id.get(t)
        if orig is None:
            errors.append("%s: translation_of '%s' not found" % (item.get("id"), t))
        elif orig.get("translation_of") is not None:
            errors.append(
                "%s: translation_of must point at an original, not another translation"
                % item.get("id")
            )
        elif orig.get("original_language") == item.get("original_language"):
            errors.append("%s: translation has the same language as its original" % item.get("id"))
        elif orig.get("type") != item.get("type") or orig.get("expected") != item.get("expected"):
            errors.append(
                "%s: translation must have the same type and expected sources as '%s'"
                % (item.get("id"), t)
            )
    originals = {i["id"] for i in items if i.get("translation_of") is None}
    translated = {i.get("translation_of") for i in items if i.get("translation_of")}
    missing = sorted(originals - translated)

    # Coverage table
    counts = Counter((i.get("type"), i.get("original_language")) for i in items)
    checked = sum(1 for i in items if i.get("review") == "checked")
    kinds = Counter(i.get("authority", "unset") for i in items)
    pending = sum(
        1
        for i in items
        if any(
            isinstance(u, str) and PLACEHOLDER.match(u)
            for g in i.get("expected", [])
            if isinstance(g, list)
            for u in g
        )
    )
    print("%-18s %4s %4s %6s" % ("type", "en", "sv", "total"))
    for t in TYPES:
        en, sv = counts[(t, "en")], counts[(t, "sv")]
        print("%-18s %4d %4d %6d" % (t, en, sv, en + sv))
    print("%-18s %4s %4s %6d" % ("all", "", "", len(items)))
    print(
        "\nauthority (D22): "
        + ", ".join("%s %d" % (k, kinds[k]) for k in ("law", "agency", "both", "unset"))
    )
    print("reviewed: %d of %d (drafts are excluded from reported results)" % (checked, len(items)))
    if pending:
        print("waiting for web units (web:tbd/...): %d" % pending)
    if missing:
        print("originals without a translation (D20): %s" % ", ".join(missing))

    if errors:
        print("\n%d problem(s):" % len(errors))
        for e in errors:
            print("  - " + e)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
