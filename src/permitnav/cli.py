"""Command line: `permitnav <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from permitnav import db
from permitnav.corpus.chunking import STRATEGIES
from permitnav.retrieval.retriever import MODES, RetrievalConfig

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

    http = LiveHttp(user_agent=_user_agent())
    if args.source in ("riksdagen", "all"):
        from permitnav.sources.riksdagen import fetch_statute

        for sfs in args.sfs or STATUTES:
            statute_fetch = fetch_statute(http, sfs, _raw_dir())
            size = len(statute_fetch.content)
            print(f"{sfs}: {size:,} bytes -> {statute_fetch.storage_path}")
    failed: list[str] = []
    if args.source in ("migrationsverket", "all"):
        from permitnav.parsing.webpage import parse_page
        from permitnav.sources.migrationsverket import (
            FetchError,
            discover,
            fetch_page,
            finish_crawl,
            load_robots,
        )

        robots = load_robots(http)
        found = discover(http)
        swedish = [u for u in found if robots.allowed(u)]
        print(
            f"migrationsverket: {len(swedish)} Swedish pages in scope"
            f" ({len(found) - len(swedish)} skipped: disallowed by robots.txt)"
        )
        fetched: list[str] = []
        gone = 0
        for n, url in enumerate(swedish, start=1):
            target = url  # the URL being fetched, so a failure is recorded against the right page
            try:
                page = fetch_page(http, robots, target, _raw_dir())
                fetched.append(target)
                english = parse_page(page.content.decode("utf-8"), url).translations.get("en")
                if english and english != url and robots.allowed(english):
                    target = english
                    fetch_page(http, robots, target, _raw_dir())
                    fetched.append(target)
            except FetchError as error:
                if error.gone:
                    gone += 1  # not in `fetched`: ingest closes its sections
                else:
                    failed.append(target)
                print(f"\n  {'gone' if error.gone else 'failed'}: {error}")
            except (PermissionError, ValueError) as error:
                failed.append(target)
                print(f"\n  failed: {error}")
            print(f"\r  {n}/{len(swedish)} pages (with English versions)", end="", flush=True)
        finish_crawl(_raw_dir(), fetched, failed)
        print(f"\nmigrationsverket: {len(fetched)} fetched, {gone} gone, {len(failed)} failed")
    return 1 if failed else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from permitnav.corpus.store import ingest_page, ingest_statute
    from permitnav.parsing.statute import parse_statute
    from permitnav.sources.riksdagen import latest_fetch

    status = 0
    with db.connect() as conn:
        if args.source in ("riksdagen", "all"):
            for sfs in args.sfs or STATUTES:
                raw = latest_fetch(_raw_dir(), sfs)
                if raw is None:
                    print(f"{sfs}: nothing downloaded yet; run `permitnav fetch` first")
                    status = 1
                    continue
                statute = parse_statute(raw.content.decode("utf-8"), url=raw.url)
                if statute.warnings:
                    print(
                        f"{sfs}: {len(statute.warnings)} blocks could not be placed; not ingested"
                    )
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
                notes = report.conflicts + [f"estimated change date: {u}" for u in report.estimated]
                for line in notes:
                    print(f"  note: {line}")
        if args.source in ("migrationsverket", "all"):
            from permitnav.parsing.webpage import parse_page
            from permitnav.sources.migrationsverket import saved_pages

            totals = {"pages": 0, "units": 0, "new": 0, "unchanged": 0, "closed": 0, "failed": 0}
            for raw in saved_pages(_raw_dir()):
                try:
                    page = parse_page(raw.content.decode("utf-8"), raw.url)
                except ValueError as error:
                    totals["failed"] += 1
                    print(f"  not ingested: {error}")
                    continue
                report = ingest_page(conn, raw, page)
                totals["pages"] += 1
                totals["units"] += report.units
                totals["new"] += report.new_versions
                totals["unchanged"] += report.unchanged_versions
                totals["closed"] += report.closed_versions
            from permitnav.corpus.store import close_documents
            from permitnav.sources.migrationsverket import load_crawl

            crawl = load_crawl(_raw_dir())
            if crawl is not None:  # pages the latest crawl no longer found
                totals["closed"] += close_documents(
                    conn, set(crawl.fetched) | set(crawl.failed), crawl.finished_at
                )
            print(
                f"migrationsverket: {totals['pages']} pages, {totals['units']} sections, "
                f"{totals['new']} new, {totals['unchanged']} unchanged, {totals['closed']} closed, "
                f"{totals['failed']} failed"
            )
            status = status or (1 if totals["failed"] else 0)
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
    from permitnav.retrieval.retriever import (
        Retriever,
        units_applying_on,
        units_with_same_text,
    )

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
        config = RetrievalConfig(
            mode=args.mode, strategy=args.strategy, keyword_weight=args.keyword_weight
        )
        retriever = Retriever(conn, embedder, config)
        cache: dict = {}

        def applying_on(d):
            if d not in cache:
                cache[d] = units_applying_on(conn, d)
            return cache[d]

        run = run_retrieval(
            items, retriever, existing, applying_on, lambda u, d: units_with_same_text(conn, u, d)
        )

    label = (
        "DEVELOPMENT RUN, includes drafts, not reportable"
        if args.include_drafts
        else "checked items"
    )
    weight = f", keyword_weight={args.keyword_weight}" if args.mode == "hybrid" else ""
    print(
        f"\nRetrieval eval: {args.file}, strategy={args.strategy}, mode={args.mode}{weight}"
        f" ({label})"
    )
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


KIND_LABELS = {
    "answered": "",
    "follow_up": "I need one more detail to answer this:",
    "not_settled": "The rules don't settle this on their own:",
    "out_of_scope": "Outside what PermitNav covers:",
    "failed": "Something went wrong:",
}


def cmd_ask(args: argparse.Namespace) -> int:
    from datetime import date

    from permitnav.answering.answerer import AnswerConfig, Answerer
    from permitnav.llm.openrouter import LLMError, OpenRouterLLM
    from permitnav.retrieval.retriever import Retriever

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    try:
        from permitnav.embedding.bge_m3 import BgeM3Embedder

        embedder = BgeM3Embedder()
    except ImportError:
        print("bge-m3 needs the embed extra: uv sync --extra embed")
        return 1
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    try:
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
        with db.connect() as conn:
            retriever = Retriever(conn, embedder, RetrievalConfig(mode=args.mode))
            answerer = Answerer(conn, retriever, llm, AnswerConfig(model=model))
            answer = answerer.answer(args.question, as_of=as_of)
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1

    print()
    if KIND_LABELS.get(answer.kind):
        print(KIND_LABELS[answer.kind])
    print(answer.text)
    if answer.citations:
        print("\nSources")
        for c in answer.citations:
            print(f"  [{c.marker}] {c.heading}")
            print(f"       {c.dates} | {c.url}")
            if c.quote:
                print(f'       "{c.quote}"')
                if c.translation:
                    print(f"       (unofficial translation: {c.translation})")
    credits = []
    if any(c.authority in ("law", "ordinance") for c in answer.citations):
        credits.append("Källa: Sveriges riksdag")
    if any(c.authority == "web_page" for c in answer.citations):
        credits.append("Källa: Migrationsverket")
    if credits:
        print("\n" + " · ".join(credits))
    print(
        f"\nAs of {as_of}. Not legal advice; check with Migrationsverket. You are talking to an AI."
    )
    if answer.dropped_markers:
        print(f"(removed citations to unknown sources: {', '.join(answer.dropped_markers)})")
    if args.show_sources:
        print("\nShown to the model:")
        for s in answer.sources:
            print(f"  [{s.marker}] {s.role:<13} {s.unit_id}")
    c = answer.completion
    if c is not None:
        cost = f", ${c.cost_usd:.4f}" if c.cost_usd is not None else ""
        print(f"[{c.model}: {c.input_tokens:,} in / {c.output_tokens:,} out tokens{cost}]")
    return 0 if answer.kind != "failed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permitnav")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(run=cmd_migrate)
    sources = ["riksdagen", "migrationsverket", "all"]
    fetch = sub.add_parser("fetch", help="download statutes and Migrationsverket pages")
    fetch.add_argument("sfs", nargs="*", help="SFS numbers, e.g. 2005:716 (default: all in scope)")
    fetch.add_argument("--source", default="all", choices=sources)
    fetch.set_defaults(run=cmd_fetch)
    ingest = sub.add_parser("ingest", help="parse the latest downloads and store them")
    ingest.add_argument("sfs", nargs="*", help="SFS numbers (default: all in scope)")
    ingest.add_argument("--source", default="all", choices=sources)
    ingest.set_defaults(run=cmd_ingest)
    index = sub.add_parser("index", help="build chunks and embeddings for stored versions")
    index.add_argument("--strategy", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    index.add_argument("--no-embed", action="store_true", help="only build chunks")
    index.set_defaults(run=cmd_index)
    ask = sub.add_parser("ask", help="answer a question from the stored sources")
    ask.add_argument("question")
    ask.add_argument("--mode", default=RetrievalConfig.mode, choices=list(MODES))
    ask.add_argument("--as-of", help="date the answer should apply on (YYYY-MM-DD); default today")
    ask.add_argument(
        "--show-sources", action="store_true", help="list everything shown to the model"
    )
    ask.set_defaults(run=cmd_ask)
    ev = sub.add_parser("eval", help="run the retrieval eval on a golden-set file")
    ev.add_argument("--file", default="evals/golden.dev.jsonl")
    ev.add_argument("--strategy", default="structure", choices=list(STRATEGIES))
    ev.add_argument("--mode", default=RetrievalConfig.mode, choices=list(MODES))
    ev.add_argument("--keyword-weight", type=float, default=RetrievalConfig.keyword_weight)
    ev.add_argument("--include-drafts", action="store_true", help="development run; not reportable")
    ev.add_argument("--verbose", "-v", action="store_true", help="show each item's ranks")
    ev.add_argument("--out", help="write per-item results as JSON")
    ev.set_defaults(run=cmd_eval)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
