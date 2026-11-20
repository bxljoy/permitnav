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


def cmd_eval(args: argparse.Namespace) -> int:
    import json

    from permitnav.evals.golden import load_golden
    from permitnav.evals.run import run_retrieval, slices
    from permitnav.retrieval.retriever import RetrievalConfig, Retriever, units_applying_on

    items = load_golden(Path(args.file))
    drafts = [i for i in items if i.review == "draft"]
    if not args.include_drafts:
        items = [i for i in items if i.review == "checked"]
        if not items:
            print(f"no checked items in {args.file} ({len(drafts)} drafts); see evals/README.md,")
            print("or run with --include-drafts for a development (not reportable) run")
            return 1
    try:
        from permitnav.embedding.bge_m3 import BgeM3Embedder

        embedder = BgeM3Embedder()
    except ImportError:
        print("bge-m3 needs the embed extra: uv sync --extra embed")
        return 1
    with db.connect() as conn:
        existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
        retriever = Retriever(conn, embedder, RetrievalConfig(strategy=args.strategy))
        cache: dict = {}

        def applying_on(d):
            if d not in cache:
                cache[d] = units_applying_on(conn, d)
            return cache[d]

        run = run_retrieval(items, retriever, existing, applying_on)

    label = (
        "DEVELOPMENT RUN, includes drafts, not reportable"
        if args.include_drafts
        else "checked items"
    )
    print(f"\nRetrieval eval: {args.file}, strategy={args.strategy}, mode=vector ({label})")
    columns = ("R@1", "R@5", "95% CI", "R@10", "MRR", "nDCG@5")
    widths = (5, 5, 13, 5, 5, 6)
    header = " ".join(f"{c:>{w}}" for c, w in zip(columns, widths, strict=True))
    print(f"{'slice':<28} {'n':>3} {header}")
    for s in slices(run):
        low, high = s.recall_5_ci
        print(
            f"{s.name:<28} {s.n:>3} {s.recall_1:>5.2f} {s.recall_5:>5.2f}"
            f" {f'[{low:.2f}, {high:.2f}]':>13} {s.recall_10:>5.2f} {s.mrr:>5.2f} {s.ndcg_5:>6.2f}"
        )
    for reason, ids in run.skipped.items():
        print(f"skipped {len(ids)}: {reason}")
    for item_id, units in run.unknown_units.items():
        print(f"GOLDEN-SET ERROR {item_id}: expected units not in the corpus: {', '.join(units)}")
    if args.verbose:
        print()
        for r in run.results:
            ranks = ", ".join("-" if x is None else str(x) for x in r.ranks)
            print(f"{r.item.id:<48} group ranks [{ranks}]  top: {', '.join(r.retrieved[:3])}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                [
                    {"id": r.item.id, "ranks": r.ranks, "retrieved": r.retrieved}
                    for r in run.results
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if run.unknown_units:
        return 2  # golden-set errors must be fixed, not ignored
    if not run.results:
        print("no items could be scored")
        return 1
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
    ev = sub.add_parser("eval", help="run the retrieval eval on a golden-set file")
    ev.add_argument("--file", default="evals/golden.dev.jsonl")
    ev.add_argument("--strategy", default="structure", choices=list(STRATEGIES))
    ev.add_argument("--include-drafts", action="store_true", help="development run; not reportable")
    ev.add_argument("--verbose", "-v", action="store_true", help="show each item's ranks")
    ev.add_argument("--out", help="write per-item results as JSON")
    ev.set_defaults(run=cmd_eval)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
