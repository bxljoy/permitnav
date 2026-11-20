"""Command line: `permitnav <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from permitnav import db
from permitnav.corpus.chunking import STRATEGIES

# Statutes in scope for the first version (D1).
STATUTES = {
    "2005:716": "Utlänningslag",
    "2006:97": "Utlänningsförordning",
}


def _raw_dir() -> Path:
    return Path(os.environ.get("RAW_DIR", "data/raw"))


def _user_agent() -> str:
    # No personal details by default; set CRAWLER_CONTACT to add a contact address.
    contact = os.environ.get("CRAWLER_CONTACT")
    return (
        "PermitNav/0.0.1 (open-source research crawler" + (f"; {contact}" if contact else "") + ")"
    )


def cmd_migrate(_: argparse.Namespace) -> int:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print("\n".join(f"applied {name}" for name in applied) or "database is up to date")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from permitnav.sources.http import LiveHttp
    from permitnav.sources.riksdagen import fetch_statute

    http = LiveHttp(user_agent=_user_agent())
    for sfs in args.sfs or STATUTES:
        fetched = fetch_statute(http, sfs, _raw_dir())
        print(f"{sfs}: {len(fetched.content):,} bytes -> {fetched.storage_path}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from permitnav.corpus.store import ingest_statute
    from permitnav.parsing.statute import parse_statute
    from permitnav.sources.riksdagen import latest_fetch

    status = 0
    with db.connect() as conn:
        for sfs in args.sfs or STATUTES:
            raw = latest_fetch(_raw_dir(), sfs)
            if raw is None:
                print(f"{sfs}: nothing downloaded yet; run `permitnav fetch` first")
                status = 1
                continue
            statute = parse_statute(raw.content.decode("utf-8"), url=raw.url)
            if statute.warnings:
                print(f"{sfs}: parser could not place {len(statute.warnings)} blocks; not ingested")
                for warning in statute.warnings[:10]:
                    print(f"  {warning}")
                status = 1
                continue
            report = ingest_statute(conn, raw, statute)
            print(
                f"{sfs} {statute.title}: {report.units} units, {report.new_versions} new, "
                f"{report.updated_versions} updated, {report.unchanged_versions} unchanged, "
                f"{report.closed_versions} closed versions, {report.links} links"
            )
            for line in report.conflicts + [
                f"estimated change date: {u}" for u in report.estimated
            ]:
                print(f"  note: {line}")
    return status


def cmd_index(args: argparse.Namespace) -> int:
    import resource
    import time

    from permitnav.corpus.indexing import build_chunks, embed_chunks

    with db.connect() as conn:
        for strategy in args.strategy:
            print(f"chunks ({strategy}): {build_chunks(conn, strategy):,} new")
        if args.no_embed:
            return 0
        from permitnav.embedding.bge_m3 import BgeM3Embedder

        started = time.perf_counter()
        try:
            embedder = BgeM3Embedder()  # imports torch and sentence-transformers here
        except ImportError:
            print("bge-m3 needs the embed extra: uv sync --extra embed")
            return 1
        loaded_in = time.perf_counter() - started
        print(f"loaded {embedder.model_id} on {embedder.device} in {loaded_in:.1f}s")
        started = time.perf_counter()

        def progress(done: int, total: int) -> None:
            print(f"\r  embedded {done:,}/{total:,}", end="", flush=True)

        count = embed_chunks(conn, embedder, on_batch=progress)
        elapsed = time.perf_counter() - started
        # ru_maxrss is in bytes on macOS and in kilobytes on Linux.
        unit = 1024 * 1024 if sys.platform == "darwin" else 1024
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit
        rate = f", {count / elapsed:.1f} chunks/s" if count and elapsed else ""
        print(f"\nembedded {count:,} chunks in {elapsed:.1f}s{rate}; peak memory {peak_mb:,.0f} MB")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permitnav")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(run=cmd_migrate)
    fetch = sub.add_parser("fetch", help="download statutes from the riksdagen API")
    fetch.add_argument("sfs", nargs="*", help="SFS numbers, e.g. 2005:716 (default: all in scope)")
    fetch.set_defaults(run=cmd_fetch)
    ingest = sub.add_parser("ingest", help="parse the latest downloads and store them")
    ingest.add_argument("sfs", nargs="*", help="SFS numbers (default: all in scope)")
    ingest.set_defaults(run=cmd_ingest)
    index = sub.add_parser("index", help="build chunks and embeddings for stored versions")
    index.add_argument("--strategy", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    index.add_argument("--no-embed", action="store_true", help="only build chunks")
    index.set_defaults(run=cmd_index)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
