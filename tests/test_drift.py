"""Golden-set drift (D17, phase 4 slice 5): items whose expected sources changed since their
date need a person's review; a fresh copy dated today is drafted for it."""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest

from permitnav.corpus.drift import fresh_copies, golden_drift
from permitnav.evals.golden import GoldenItem

AS_OF = date(2026, 11, 20)
TODAY = date(2026, 12, 15)


def item(unit: str, kind: str = "time-dependent", as_of: date = AS_OF) -> GoldenItem:
    return GoldenItem(
        id=f"q-{unit.split('/')[-1]}", question="?", original_language="en",
        translation_of=None, type=kind, as_of=as_of, expected=((unit,),), review="checked",
        authority="law",
    )  # fmt: skip


@pytest.fixture
def sources(conn: psycopg.Connection) -> psycopg.Connection:
    download = conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES ('t', 'u', now(), 'h', 'p', 200) RETURNING id"
    ).fetchone()
    assert download is not None

    def version(unit, basis, text, valid=(None, None), seen=None, ended=None, withdrawn=False):
        conn.execute(
            "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
            " VALUES (%s, 'statute', 'law', 'sv', 'd', 'h', 'u') ON CONFLICT DO NOTHING",
            (unit,),
        )
        conn.execute(
            "INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from,"
            " valid_to, observed_from, observed_to, withdrawn, download_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (unit, text, text, basis, *valid, seen or datetime(2026, 11, 1, tzinfo=UTC),
             ended, withdrawn, download[0]),
        )  # fmt: skip

    version("law/new", "valid", "old", (date(2020, 1, 1), date(2026, 12, 1)))
    version("law/new", "valid", "new", (date(2026, 12, 1), None))  # in force since the item
    version("law/later", "valid", "now", (date(2020, 1, 1), date(2028, 1, 1)))
    version("law/later", "valid", "2028", (date(2028, 1, 1), None))  # still to come
    version("law/same", "valid", "same", (date(2020, 1, 1), None))
    version("web/page", "observed", "before", ended=datetime(2026, 12, 2, tzinfo=UTC))
    version("web/page", "observed", "after", seen=datetime(2026, 12, 2, tzinfo=UTC))
    version("law/fixed", "valid", "misread", (date(2006, 3, 31), None),
            ended=datetime(2026, 11, 24, tzinfo=UTC), withdrawn=True)  # fmt: skip
    version("law/fixed", "valid", "read right", (date(2006, 3, 31), None))
    version("law/gone", "valid", "x", (date(2030, 1, 1), None))  # nothing on the item's date
    return conn


@pytest.mark.db
def test_each_kind_of_drift_is_found(sources: psycopg.Connection) -> None:
    units = ["law/new", "law/later", "law/same", "web/page", "law/fixed", "law/gone"]
    found = {(d.unit, d.kind) for d in golden_drift(sources, [item(u) for u in units], TODAY)}
    assert found == {
        ("law/new", "newer wording"),
        ("law/later", "upcoming"),
        ("web/page", "page changed"),
        ("law/fixed", "corrected"),
        ("law/gone", "gone"),
    }  # law/same: nothing changed


@pytest.mark.db
def test_items_testing_another_date_on_purpose_are_left_alone(sources: psycopg.Connection) -> None:
    historical = item("law/new", kind="version-dependent", as_of=date(2024, 6, 1))
    assert golden_drift(sources, [historical], TODAY) == []


@pytest.mark.db
def test_a_drifted_item_gets_a_fresh_draft_copy_dated_today(sources: psycopg.Connection) -> None:
    old = item("law/new")
    drift = golden_drift(sources, [old], TODAY)
    raw = {old.id: {"id": old.id, "as_of": "2026-11-20", "review": "checked",
                    "translation_of": None, "notes": ""}}  # fmt: skip
    (copy,) = list(fresh_copies([old], raw, drift, TODAY))
    assert copy["id"] == f"{old.id}-20261215"
    assert (copy["as_of"], copy["review"]) == ("2026-12-15", "draft")
    assert "law/new: newer wording, from 2026-12-01" in copy["notes"]


@pytest.mark.db
def test_the_command_reports_a_drifted_item_as_new_only_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import json

    from permitnav.cli import cmd_golden_drift
    from permitnav.corpus import drift as drift_module

    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({
        "id": "q1", "question": "?", "original_language": "en", "translation_of": None,
        "type": "time-dependent", "as_of": "2026-11-20", "expected": [["law/new"]],
        "reference_answer": "r", "expects_follow_up": False, "expects_not_settled": False,
        "expects_out_of_scope": False, "authority": "law", "review": "checked", "notes": "",
    }) + "\n", encoding="utf-8")  # fmt: skip
    monkeypatch.setenv("RAW_DIR", str(tmp_path))
    # the command's own data source stands in for the database: one item drifted
    monkeypatch.setattr(
        drift_module, "golden_drift",
        lambda conn, items, today: [drift_module.Drift("q1", "law/new", "newer wording", "x")],
    )  # fmt: skip
    args = argparse.Namespace(file=[str(golden)], today=None, draft=None, save=False)
    cmd_golden_drift(args)
    assert "(new: q1)" in capsys.readouterr().out
    cmd_golden_drift(args)  # not saved yet (the alert may not have gone out): still new
    assert "(new: q1)" in capsys.readouterr().out
    cmd_golden_drift(argparse.Namespace(**{**vars(args), "save": True}))
    capsys.readouterr()
    cmd_golden_drift(args)
    out = capsys.readouterr().out
    assert "1 items need review" in out and "new:" not in out
    other = argparse.Namespace(
        **{**vars(args), "file": [str(tmp_path / "other.jsonl")], "save": True}
    )
    (tmp_path / "other.jsonl").write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
    cmd_golden_drift(other)  # a run on another file keeps its own record
    capsys.readouterr()
    cmd_golden_drift(args)
    assert "new:" not in capsys.readouterr().out


# --- review fixes ------------------------------------------------------------------------


@pytest.mark.db
def test_a_section_that_ends_with_nothing_after_it_is_drift(sources: psycopg.Connection) -> None:
    sources.execute(
        "UPDATE unit_version SET valid_to = '2026-12-01' WHERE unit_id = 'law/same'"
    )  # repealed: no new wording
    assert [(d.unit, d.kind) for d in golden_drift(sources, [item("law/same")], TODAY)] == [
        ("law/same", "gone")
    ]
    later = date(2026, 11, 25)  # before the repeal: it is coming, with nothing after
    assert [d.detail for d in golden_drift(sources, [item("law/same")], later)] == [
        "ends on 2026-12-01, nothing after"
    ]


@pytest.mark.db
def test_an_item_with_a_fresh_copy_is_historical(sources: psycopg.Connection) -> None:
    old = item("law/new")
    copy = GoldenItem(**{**old.__dict__, "id": f"{old.id}-20261215", "as_of": TODAY})
    found = golden_drift(sources, [old, copy], TODAY)
    assert {d.item for d in found} <= {copy.id}  # the old item is no longer followed


def test_a_drafted_translation_points_at_its_drafted_original_or_the_existing_one() -> None:
    from permitnav.corpus.drift import Drift

    en = GoldenItem(
        id="q-en", question="?", original_language="en", translation_of=None, type="t",
        as_of=AS_OF, expected=(("u",),), review="checked", authority="law",
    )  # fmt: skip
    sv = GoldenItem(**{**en.__dict__, "id": "q-sv", "original_language": "sv",
                       "translation_of": "q-en"})  # fmt: skip
    raw = {i.id: {"id": i.id, "translation_of": i.translation_of} for i in (en, sv)}
    both = [Drift("q-en", "u", "newer wording", "x"), Drift("q-sv", "u", "newer wording", "x")]
    rows = {r["id"]: r for r in fresh_copies([en, sv], raw, both, TODAY)}
    assert rows["q-sv-20261215"]["translation_of"] == "q-en-20261215"
    only_sv = [Drift("q-sv", "u", "newer wording", "x")]
    (row,) = fresh_copies([en, sv], raw, only_sv, TODAY)
    assert row["translation_of"] == "q-en"  # no draft of the original: keep the existing one
