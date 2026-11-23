"""The retrieval eval without models (CI): recorded queries and the eval corpus snapshot."""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import psycopg
import pytest

from permitnav.evals.replay import (
    MissingQuery,
    QueryCache,
    RecordingEmbedder,
    RecordingTranslator,
    ReplayEmbedder,
    ReplayTranslator,
)
from tests.test_retriever import FAKE


class Translator:
    calls = 1
    cost_usd = 0.01
    cost_unknown = 0

    def translate(self, question: str) -> str:
        return "sv: " + question


def test_recorded_queries_replay_exactly(tmp_path: Path) -> None:
    cache = QueryCache()
    embedder = RecordingEmbedder(FAKE, cache)
    translator = RecordingTranslator(Translator(), cache)
    vector = embedder.embed_query("salary requirement")
    assert translator.translate("salary requirement") == "sv: salary requirement"
    assert translator.calls == 1  # the real translator's counters show through
    cache.save(tmp_path / "q.json")

    loaded = QueryCache.load(tmp_path / "q.json")
    assert loaded.model_id == FAKE.model_id
    assert ReplayEmbedder(loaded).embed_query("salary requirement") == vector
    assert ReplayTranslator(loaded).translate("salary requirement") == "sv: salary requirement"


def test_recorded_vectors_are_the_float32_values_replay_returns() -> None:
    cache = QueryCache()
    vector = RecordingEmbedder(FAKE, cache).embed_query("q")
    assert vector == cache.vectors["q"]  # the recording run already sees float32 values


def test_an_unrecorded_text_fails_loudly() -> None:
    with pytest.raises(MissingQuery, match="vector"):
        ReplayEmbedder(QueryCache()).embed_query("new question")
    with pytest.raises(MissingQuery, match="translation"):
        ReplayTranslator(QueryCache()).translate("new question")


def test_an_unknown_replay_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text('{"format": 99}')
    with pytest.raises(ValueError, match="format"):
        QueryCache.load(path)


def _indexed_corpus(url: str) -> None:
    from permitnav.corpus.indexing import build_chunks, embed_chunks
    from tests.test_ingest import FIXTURE, ingest

    with psycopg.connect(url) as conn:
        ingest(conn, FIXTURE, 20)
        build_chunks(conn, "structure")
        build_chunks(conn, "fixed500")
        embed_chunks(conn, FAKE)
        conn.commit()


@pytest.mark.db
def test_the_snapshot_round_trips_and_searches_the_same(tmp_path: Path, database_url) -> None:
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from permitnav import db
    from permitnav.corpus.snapshot import export_snapshot, import_snapshot
    from permitnav.retrieval.retriever import Retriever

    names = []
    urls = []
    with psycopg.connect(database_url, autocommit=True) as admin:
        for suffix in ("src", "dst"):
            name = f"{make_conninfo(database_url).split('dbname=')[-1].split()[0]}_{suffix}"
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            names.append(name)
            urls.append(make_conninfo(database_url, dbname=name))
    try:
        for url in urls:
            with psycopg.connect(url) as conn:
                db.migrate(conn)
        _indexed_corpus(urls[0])
        path = tmp_path / "corpus.txt.gz"
        with psycopg.connect(urls[0]) as conn:
            manifest = export_snapshot(conn, path, "abc123")
            query = "lönen medianlönen procent lönekravet"
            before = [
                h.unit_id for h in Retriever(conn, FAKE).search(query, date(2026, 11, 20), k=8)
            ]
        assert manifest["commit"] == "abc123" and manifest["strategy"] == "structure"
        assert manifest["counts"]["chunk"] == manifest["counts"]["chunk_embedding_bge_m3"] > 0
        with gzip.open(path, "rt") as f:
            assert "fixed500" not in f.read()  # only the strategy the eval uses

        with psycopg.connect(urls[1]) as conn:
            assert import_snapshot(conn, path)["counts"] == manifest["counts"]
            after = [
                h.unit_id for h in Retriever(conn, FAKE).search(query, date(2026, 11, 20), k=8)
            ]
            # New rows after an import get fresh ids (the sequences were moved on).
            conn.execute(
                "INSERT INTO download (source, url, fetched_at, content_hash, storage_path,"
                " http_status) VALUES ('t', 'u', now(), 'h', 'p', 200)"
            )
            with pytest.raises(ValueError, match="already holds"):
                import_snapshot(conn, path)
        assert after == before
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            for name in names:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


@pytest.mark.db
def test_a_snapshot_from_another_schema_is_refused(
    tmp_path: Path, conn: psycopg.Connection
) -> None:
    from permitnav.corpus.snapshot import HEADER, import_snapshot

    path = tmp_path / "old.txt.gz"
    with gzip.open(path, "wt") as f:
        f.write(f'{HEADER}\n{{"schema": "999_future.sql", "counts": {{}}}}\n')
    with pytest.raises(ValueError, match="schema"):
        import_snapshot(conn, path)


def test_a_file_that_is_not_a_snapshot_is_refused(tmp_path: Path) -> None:
    from permitnav.corpus.snapshot import read_manifest

    path = tmp_path / "x.gz"
    with gzip.open(path, "wt") as f:
        f.write("something else\n")
    with pytest.raises(ValueError, match="not an eval corpus"):
        read_manifest(path)


@pytest.mark.db
def test_closed_wordings_are_exported_too(tmp_path: Path, conn: psycopg.Connection) -> None:
    from permitnav.corpus.snapshot import TABLES

    # The export's version filter keeps every stored wording, not only those observed now.
    where = dict(TABLES)["unit_version"]
    assert "observed_to" not in where


@pytest.mark.db
def test_import_refuses_a_database_with_any_corpus_rows(
    tmp_path: Path, conn: psycopg.Connection
) -> None:
    from permitnav.corpus.snapshot import HEADER, import_snapshot, latest_migration

    path = tmp_path / "s.txt.gz"
    with gzip.open(path, "wt") as f:
        f.write(f'{HEADER}\n{{"schema": "{latest_migration(conn)}", "counts": {{}}}}\n')
    conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES ('t', 'u', now(), 'h', 'p', 200)"
    )
    with pytest.raises(ValueError, match="download rows"):
        import_snapshot(conn, path)


@pytest.mark.db
def test_export_needs_a_connection_outside_a_transaction(
    tmp_path: Path, conn: psycopg.Connection
) -> None:
    from permitnav.corpus.snapshot import export_snapshot

    with pytest.raises(RuntimeError, match="outside a transaction"):
        export_snapshot(conn, tmp_path / "x.txt.gz", "c")


def test_recorded_queries_are_replayed_only_with_the_same_settings(tmp_path: Path, capsys) -> None:
    from permitnav import cli

    QueryCache(settings={"file": "other.jsonl"}).save(tmp_path / "q.json")
    status = cli.main(["eval", "--queries", str(tmp_path / "q.json")])
    assert status == 1
    assert "recorded with other settings" in capsys.readouterr().out


def test_a_missing_queries_file_is_a_message_not_a_traceback(tmp_path: Path, capsys) -> None:
    from permitnav import cli

    assert cli.main(["eval", "--queries", str(tmp_path / "nope.json")]) == 1
    assert "cannot read recorded queries" in capsys.readouterr().out


def test_recording_wrappers_refuse_document_embedding() -> None:
    with pytest.raises(NotImplementedError):
        RecordingEmbedder(FAKE, QueryCache()).embed_documents(["chunk text"])
