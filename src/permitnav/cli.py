"""Command line: `permitnav <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

from permitnav import db
from permitnav.answering.answerer import AnswerConfig
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


def _translations_cache() -> Path:
    """CACHE_DIR, or `cache` next to the raw downloads (RAW_DIR), so it does not depend on the
    directory a command is run from any more than the downloads do."""
    root = os.environ.get("CACHE_DIR") or _raw_dir().parent / "cache"
    return Path(root) / "translations.json"


def _translator(enabled: bool):
    """The query translator (phase 2, slice 4), or None when translation is off. Uses
    TRANSLATE_MODEL, else ANSWER_MODEL; translations are cached in data/cache."""
    if not enabled:
        return None
    from permitnav.llm.openrouter import OpenRouterLLM
    from permitnav.retrieval.translate import LLMTranslator

    model = os.environ.get("TRANSLATE_MODEL", "").strip() or os.environ.get("ANSWER_MODEL", "")
    if not model.strip():
        raise SystemExit("translation needs TRANSLATE_MODEL or ANSWER_MODEL; see .env.example")
    llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
    return LLMTranslator(llm, model.strip(), cache_path=_translations_cache())


def _print_provenance(
    config,
    answer_model: str | None = None,
    judge: str | None = None,
    answer_effort: str | None = None,
) -> None:
    """The version behind a report, so its numbers can be tied to one reproducible setup.
    `answer_effort`: the reasoning effort the answers were made with, when one was set (it
    changes answers and their cost)."""
    from permitnav.evals.provenance import provenance

    translate = None
    if config.translate:
        translate = (
            os.environ.get("TRANSLATE_MODEL", "").strip()
            or os.environ.get("ANSWER_MODEL", "").strip()
        )
    if answer_model and answer_effort:
        answer_model = f"{answer_model} (reasoning effort {answer_effort})"
    if answer_model and _answer_structured():
        answer_model = f"{answer_model} (reply held to a schema)"
    models = {
        "answer": answer_model,
        "judge": judge,
        "translate": translate,
        "embedder": "BAAI/bge-m3",
        "reranker": "BAAI/bge-reranker-v2-m3" if config.rerank else None,
    }
    for line in provenance(config, models):
        print(line)


def _answer_effort() -> str | None:
    """ANSWER_REASONING_EFFORT, or a one-line exit when it is mistyped (before any model loads)."""
    from permitnav.answering.answerer import answer_reasoning_effort, answer_structured

    try:
        answer_structured()  # checked with it: both settings change the answers
        return answer_reasoning_effort()
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _answer_structured() -> bool:
    """ANSWER_STRUCTURED, or a one-line exit when it is mistyped."""
    from permitnav.answering.answerer import answer_structured

    try:
        return answer_structured()
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _reranker(enabled: bool):
    """The local cross-encoder (phase 2, slice 6), or None when reranking is off."""
    if not enabled:
        return None
    try:
        from permitnav.reranking.bge import BgeReranker
    except ImportError:
        raise SystemExit("the reranker needs the embed extra: uv sync --extra embed") from None
    return BgeReranker()


def _translation_cost(translator) -> str:
    if translator is None or not translator.calls:
        return ""
    unknown = f", {translator.cost_unknown} of unknown cost" if translator.cost_unknown else ""
    return f"translation: {translator.calls} calls, ${translator.cost_usd:.4f}{unknown}"


# The measured best (phase 2, slice 4): English questions translated for keyword search, with
# the keyword ranking at weight 0.5. Without translation, keyword search only adds noise for
# English questions, so its weight drops to 0. RetrievalConfig's own defaults stay offline.
TRANSLATED_KEYWORD_WEIGHT = 0.5
# Measured too: a third page ranking by the translated question's vector, only for pages with
# no English version, lets English questions reach Swedish-only guidance (the trade-union
# page) without crowding out English twins. Weight 1 (2 crowded out other sources).
TRANSLATED_PAGE_WEIGHT = 1.0


# The settings that decide which texts a run embeds or translates, and so what a recording of
# its queries holds. How results are laid out (slots, ranks, candidates) does not: a new layout
# option must not invalidate every recording. A text never recorded still fails the replay.
QUERY_SETTINGS = (
    "mode", "translate", "translated_vector", "translated_page_weight",
    "translated_pages_untranslated_only", "keyword_weight",
)  # fmt: skip


def _query_settings(settings: dict) -> dict:
    config = settings.get("config", {})
    return {
        "file": settings.get("file"),
        "include_drafts": settings.get("include_drafts"),
        **{name: config.get(name) for name in QUERY_SETTINGS},
    }


def _retrieval_config(args: argparse.Namespace) -> RetrievalConfig:
    # Checked before any model is loaded, so a bad option fails at once with a message.
    if args.mix == "rerank" and not args.rerank:
        raise SystemExit("--mix rerank needs --rerank")
    if args.rerank_depth < 1:
        raise SystemExit("--rerank-depth must be at least 1")
    translate = not args.no_translate
    weight = args.keyword_weight
    if weight is None:
        weight = TRANSLATED_KEYWORD_WEIGHT if translate else RetrievalConfig.keyword_weight
    keyword_runs = args.mode == "keyword" or (args.mode == "hybrid" and weight > 0)
    page_ranking = args.mode == "hybrid" and args.mix != "none"
    if args.translated_page_weight is not None and not page_ranking:
        raise SystemExit("--translated-page-weight needs --mode hybrid and a mix other than none")
    page_weight = 0.0
    if translate and page_ranking:
        explicit = args.translated_page_weight
        page_weight = TRANSLATED_PAGE_WEIGHT if explicit is None else explicit
    return RetrievalConfig(
        mode=args.mode,
        strategy=getattr(args, "strategy", RetrievalConfig.strategy),
        keyword_weight=weight,
        # A translation costs a model call: only when keyword search, the law vector or the
        # page ranking uses it.
        translate=translate and (keyword_runs or args.translated_vector or page_weight > 0),
        translated_vector=args.translated_vector,
        collapse_variants=not args.no_collapse_variants,
        translated_page_weight=page_weight,
        rerank=args.rerank,
        rerank_depth=args.rerank_depth,
        paper_slots=getattr(args, "paper_slots", RetrievalConfig.paper_slots),
        paper_follow_law=getattr(args, "paper_follow_law", RetrievalConfig.paper_follow_law),
        mix=args.mix,
        description_weight=getattr(args, "description_weight", 0.0),
    )


def _add_retrieval_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", default=RetrievalConfig.mode, choices=list(MODES))
    parser.add_argument(
        "--description-weight",
        type=float,
        default=RetrievalConfig.description_weight,
        help="phase 5 experiment: fuse a ranking of law sections by their plain-language "
        "descriptions (made by `permitnav describe`) with this weight; 0 = off",
    )
    parser.add_argument(
        "--keyword-weight",
        type=float,
        help=f"hybrid fusion weight (default {TRANSLATED_KEYWORD_WEIGHT} with translation, else 0)",
    )
    parser.add_argument(
        "--translated-page-weight",
        type=float,
        help=f"rank Swedish-only pages by the translated question (default {TRANSLATED_PAGE_WEIGHT}"
        " with translation, else 0)",
    )
    parser.add_argument(
        "--rerank", action="store_true", help="reorder candidates with bge-reranker-v2-m3 (local)"
    )
    parser.add_argument("--rerank-depth", type=int, default=RetrievalConfig.rerank_depth)
    parser.add_argument(
        "--paper-slots",
        type=int,
        default=RetrievalConfig.paper_slots,
        help="give position papers their own slots (from rank 7), not the rulings' (0: shared)",
    )
    parser.add_argument(
        "--paper-follow-law",
        type=int,
        default=RetrievalConfig.paper_follow_law,
        help="with paper slots: prefer papers citing one of the top N law sections (0: off)",
    )
    parser.add_argument(
        "--mix",
        default=RetrievalConfig.mix,
        choices=["interleave", "none", "rerank"],
        help="how law and guidance results combine (rerank: one ranking by reranker score)",
    )
    parser.add_argument(
        "--no-collapse-variants",
        action="store_true",
        help="keep near-duplicate page sections (employee, artist, seasonal… pages) apart",
    )
    translation = parser.add_mutually_exclusive_group()
    translation.add_argument(
        "--no-translate",
        action="store_true",
        help="don't translate English questions for keyword search (no model call)",
    )
    translation.add_argument(
        "--translated-vector",
        action="store_true",
        help="also search the law with the translated question's vector (an experiment)",
    )


def cmd_migrate(_: argparse.Namespace) -> int:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print("\n".join(f"applied {name}" for name in applied) or "database is up to date")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from permitnav.sources.http import LiveHttp

    http = LiveHttp(user_agent=_user_agent())
    failed: list[str] = []
    if args.source in ("riksdagen", "all"):
        from permitnav.sources.riksdagen import fetch_statute

        for sfs in args.sfs or STATUTES:
            try:
                statute_fetch = fetch_statute(http, sfs, _raw_dir())
            except (RuntimeError, OSError) as error:  # HTTP error or network failure
                failed.append(sfs)
                print(f"{sfs}: failed: {error}")
                continue
            size = len(statute_fetch.content)
            print(f"{sfs}: {size:,} bytes -> {statute_fetch.storage_path}")
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
            except (PermissionError, ValueError, OSError) as error:  # OSError: network
                failed.append(target)
                print(f"\n  failed: {error}")
            print(f"\r  {n}/{len(swedish)} pages (with English versions)", end="", flush=True)
        finish_crawl(_raw_dir(), fetched, failed)
        print(f"\nmigrationsverket: {len(fetched)} fetched, {gone} gone, {len(failed)} failed")
    if args.source in ("sfsr", "all"):
        from permitnav.sources.sfsr import fetch_feed, fetch_register

        for sfs in args.sfs or STATUTES:
            try:
                register = fetch_register(http, _raw_dir(), sfs)
                print(f"sfsr {sfs}: {len(register.content):,} bytes -> {register.storage_path}")
            except (RuntimeError, OSError) as error:
                failed.append(f"sfsr {sfs}")
                print(f"sfsr {sfs}: failed: {error}")
        try:
            feed = fetch_feed(http, _raw_dir())
            print(f"sfs feed: {len(feed.content):,} bytes -> {feed.storage_path}")
        except (RuntimeError, OSError) as error:
            failed.append("sfs feed")
            print(f"sfs feed: failed: {error}")
        failed += _fetch_history_acts(http, args.sfs or list(STATUTES))
    if args.source in ("lifos", "all"):
        failed += _fetch_lifos(http)
    if args.source in ("domstol", "all"):
        from permitnav.sources.domstol import fetch_rulings

        try:
            rulings_fetch = fetch_rulings(http, _raw_dir())
        except (RuntimeError, ValueError, OSError) as error:  # malformed JSON, network
            print(f"domstol: failed: {error}")
            return 1
        print(f"domstol: {len(rulings_fetch.content):,} bytes -> {rulings_fetch.storage_path}")
    return 1 if failed else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from permitnav.corpus.store import ingest_page, ingest_statute
    from permitnav.parsing.ruling import EARLIEST as EARLIEST_RULING
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
        if args.source in ("sfsr", "all"):
            status = _ingest_register(conn, args.sfs or STATUTES) or status
            status = _rebuild_history(conn, args.sfs or list(STATUTES)) or status
        if args.source in ("riksdagen", "sfsr"):  # statute dates moved under the rulings' links
            from permitnav.corpus.store import relink_rulings  # ("all" rebuilds them below)
            from permitnav.parsing.ruling import parse_rulings
            from permitnav.sources.domstol import latest_rulings

            if (rulings_raw := latest_rulings(_raw_dir())) is not None:
                links = relink_rulings(conn, parse_rulings(rulings_raw.content))
                print(f"domstol: {links} ruling links rebuilt")
        if args.source in ("lifos", "all"):
            status = _ingest_lifos(conn) or status
        if args.source in ("domstol", "all"):
            from permitnav.corpus.store import ingest_rulings
            from permitnav.parsing.ruling import parse_rulings
            from permitnav.sources.domstol import latest_rulings

            raw = latest_rulings(_raw_dir())
            if raw is None:  # like a missing statute download: recorded, the rest still runs
                print("domstol: nothing downloaded yet; run `permitnav fetch --source domstol`")
                status = 1
            else:
                rulings = parse_rulings(raw.content)
                report = ingest_rulings(conn, raw, rulings)
                print(
                    f"domstol: {report.units} rulings in scope ({rulings.out_of_scope} out of"
                    f" scope, {rulings.too_old} decided before {EARLIEST_RULING}),"
                    f" {report.new_versions} new, {report.unchanged_versions} unchanged,"
                    f" {report.closed_versions} closed, {report.withdrawn_versions} replaced,"
                    f" {report.links} links to cited sections"
                )
                for number in rulings.no_text:
                    print(f"  note: {number} has no full text in the source; summary only")
    return status


def _statute_kind(sfs: str) -> str:
    """How the statute's act markers read: "Lag (2026:385)." or "Förordning (2026:1746)."."""
    return "Förordning" if STATUTES.get(sfs, "").lower().endswith("förordning") else "Lag"


def _history_inputs(sfs: str):
    """The rebuild's view of one statute's register; None (with the reason printed) when the
    register is missing or unreadable, so the other sources still run."""
    from permitnav.corpus.history import inputs_from_register
    from permitnav.parsing.sfsr import parse_register
    from permitnav.sources.sfsr import latest_register

    raw = latest_register(_raw_dir(), sfs)
    if raw is None:
        return None
    try:
        register = parse_register(raw.content.decode("utf-8"), sfs)
        return inputs_from_register(register, _statute_kind(sfs))
    except (ValueError, UnicodeDecodeError) as error:
        print(f"history {sfs}: register not readable: {error}")
        return None


# Parsed act PDFs, kept for the process (pdfminer is slow; refresh fetches and ingests in
# one run): (SFS number, content hash) -> ParsedAct.
_ACTS: dict = {}


def _parsed_acts(unreadable: list[str] | None = None) -> dict:
    """Every act PDF on disk, parsed once: SFS number -> ParsedAct. A PDF that cannot be read
    is left out and named in `unreadable`; the walks that need it stop on a missing act."""
    from permitnav.parsing.sfs_act import parse_act, pdf_pages
    from permitnav.sources.sfs_acts import acts_on_disk, latest_act

    acts = {}
    for sfs in acts_on_disk(_raw_dir()):
        raw = latest_act(_raw_dir(), sfs)
        if raw is None:
            continue
        key = (sfs, raw.content_hash)
        if key not in _ACTS:
            try:
                _ACTS[key] = parse_act(pdf_pages(raw.content), sfs)
            except Exception as error:  # noqa: BLE001  pypdf and pdfminer raise many kinds
                if unreadable is not None:
                    unreadable.append(f"{sfs} ({type(error).__name__})")
                continue
        acts[sfs] = _ACTS[key]
    return acts


def _fetch_history_acts(http, statutes: Iterable[str]) -> list[str]:
    """The amending acts' PDFs a rebuild back to 2023 reads (slice 3). An act never changes,
    so only new ones are downloaded; the acts they name are fetched in turn."""
    from permitnav.corpus.history import needed_acts
    from permitnav.sources.sfs_acts import fetch_act

    failed: list[str] = []
    for sfs in statutes:
        inputs = _history_inputs(sfs)
        if inputs is None:
            continue  # the register fetch failed or is unreadable, reported already
        tried: set[str] = set()
        new = 0
        acts: dict = {}
        for _ in range(6):  # each round follows the acts the previous round's acts name
            acts = _parsed_acts()
            wanted = needed_acts(sfs, acts, inputs.in_force, inputs.scopes) - tried
            if not wanted:
                break
            for number in sorted(wanted):
                tried.add(number)
                try:
                    fetch_act(http, _raw_dir(), number)
                    new += 1
                except (RuntimeError, OSError) as error:
                    failed.append(f"sfs act {number}")
                    print(f"sfs act {number}: failed: {error}")
        print(f"sfs acts for {sfs}: {new} downloaded, {len(acts)} on disk")
    return failed


def _rebuild_history(conn, statutes: Iterable[str]) -> int:
    """Older wordings from the amending acts, back to 2023 (slice 3)."""
    from datetime import UTC, datetime

    from permitnav.corpus.history import rebuild_history
    from permitnav.corpus.store import _record_download, relink_transitional
    from permitnav.parsing.statute import parse_statute
    from permitnav.sources.riksdagen import latest_fetch
    from permitnav.sources.sfs_acts import latest_act

    unreadable: list[str] = []
    acts = _parsed_acts(unreadable)
    status = 0
    if unreadable:
        print(f"history: act PDFs not readable: {', '.join(unreadable)}")
        status = 1
    for sfs in statutes:
        inputs = _history_inputs(sfs)
        if inputs is None or sfs not in acts:
            print(f"history {sfs}: register or statute PDF missing; fetch --source sfsr first")
            status = 1
            continue
        with conn.transaction():
            downloads = {}
            for number in acts:
                raw = latest_act(_raw_dir(), number)
                assert raw is not None
                downloads[number] = _record_download(conn, raw)[0]
            report = rebuild_history(
                conn, inputs.statute, acts, inputs.in_force, inputs.kinds, downloads,
                datetime.now(UTC), inputs.scopes,
            )  # fmt: skip
        stops = ", ".join(f"{n} {why}" for why, n in report.stops.most_common())
        print(
            f"history {sfs}: {report.sections} sections walked, {report.wordings} older wordings"
            f" stored, {report.replaced} replaced, {report.unchanged} unchanged,"
            f" {report.reached_cutoff} back to 2023" + (f"; stopped: {stops}" if stops else "")
        )
        if report.missing_acts:
            print(f"  act PDFs missing: {', '.join(sorted(report.missing_acts))}")
            status = 1
        statute_raw = latest_fetch(_raw_dir(), sfs)
        if (report.wordings or report.replaced) and statute_raw is not None:
            # transitional provisions link the wordings their act introduced and ended (D4)
            statute = parse_statute(statute_raw.content.decode("utf-8"), url=statute_raw.url)
            relink_transitional(conn, statute)
    return status


def _ingest_register(conn, statutes: Iterable[str]) -> int:
    """Amending acts from the register and the feed; in-force dates for undated wordings."""
    from datetime import UTC, datetime

    from permitnav.corpus.dates import apply_in_force_dates, record_acts
    from permitnav.corpus.store import _record_download, relink_transitional
    from permitnav.parsing.sfsr import describe, parse_feed, parse_register
    from permitnav.parsing.statute import parse_statute
    from permitnav.sources.riksdagen import latest_fetch
    from permitnav.sources.sfsr import latest_feed, latest_register

    registers, raws = [], []
    for sfs in statutes:
        raw = latest_register(_raw_dir(), sfs)
        if raw is None:
            print(f"sfsr {sfs}: nothing downloaded yet; run `permitnav fetch --source sfsr`")
            return 1
        try:
            registers.append(parse_register(raw.content.decode("utf-8"), sfs))
        except ValueError as error:
            print(f"sfsr {sfs}: not ingested: {error}")
            return 1
        raws.append(raw)
    status = 0
    feed: list = []
    if (feed_raw := latest_feed(_raw_dir())) is not None:
        try:  # the feed is early notice only: dating from the register still runs without it
            feed = parse_feed(feed_raw.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            print(f"sfs feed: not read: {error}")
            status = 1
    # This run's time, not the download's: an unchanged register page keeps its first fetch
    # time, and the refresh summary counts change-log rows written since it started.
    seen = datetime.now(UTC)
    new = record_acts(conn, registers, feed, seen)
    for item in new:
        print(f"NEW AMENDING ACT: {item.sfs} {item.title} (published {item.published})")
    for register, raw in zip(registers, raws, strict=True):
        with conn.transaction():
            download_id, _ = _record_download(conn, raw)
        with conn.transaction():
            report = apply_in_force_dates(conn, register, download_id, seen)
        statute_raw = latest_fetch(_raw_dir(), register.statute)
        if statute_raw is not None and (report.from_acts or report.transitional):
            statute = parse_statute(statute_raw.content.decode("utf-8"), url=statute_raw.url)
            relink_transitional(conn, statute)  # "ended" links follow the new start dates
        skipped = ", ".join(f"{n} {why}" for why, n in sorted(report.skipped.items()))
        print(
            f"sfsr {register.statute}: {len(register.acts)} acts; dated {report.from_acts} by"
            f" their act, {report.original} original, {report.transitional} transitional"
            + (f"; left undated: {skipped}" if skipped else "")
        )
        for conflict in report.conflicts:
            print(f"  conflict: {conflict}")
        undated = [a for a in register.acts if a.in_force is None or a.partly_pending]
        if undated:
            print(f"  acts without a usable date: {', '.join(describe(a) for a in undated[-5:])}")
    return status


def _fetch_lifos(http) -> list[str]:
    """Position papers (slice 5): follow the feed's new versions of in-scope papers, report
    papers not in the review file, and fetch each in-scope paper's latest version once."""
    import csv

    from permitnav.sources.lifos import (
        REVIEW_FILE,
        Known,
        document_id_of,
        fetch_feed,
        fetch_paper,
        latest_known,
        remember,
    )  # fmt: skip

    failed: list[str] = []
    known = latest_known(_raw_dir())
    if not known:
        print(f"lifos: no papers marked include in {REVIEW_FILE}")
        return failed
    decided: set[str] = set()
    with REVIEW_FILE.open(encoding="utf-8", newline="") as f:
        decided = {row["rs"] for row in csv.DictReader(f) if row["rs"]}
    try:
        items = fetch_feed(http)
    except (RuntimeError, OSError, ValueError) as error:
        failed.append("lifos feed")
        print(f"lifos feed: failed: {error}")
        items = []
    for item in items:  # one item failing does not stop the others
        current = known.get(item.rs)
        if current is None:
            if item.rs not in decided:
                print(f"NEW POSITION PAPER (not in the review file): {item.title}")
            continue
        if tuple(map(int, item.version.split("."))) <= tuple(map(int, current.version.split("."))):
            continue
        try:
            document_id = document_id_of(http, item.link, item.rs, item.version)
        except (RuntimeError, OSError, ValueError) as error:
            failed.append(f"lifos {item.rs}")
            print(f"lifos feed {item.rs} version {item.version}: failed: {error}")
            continue
        known[item.rs] = Known(item.rs, item.version, document_id)
        remember(_raw_dir(), known[item.rs])
        print(f"NEW VERSION: {item.rs} version {item.version} (document {document_id})")
    fetched = 0
    for paper in known.values():
        try:
            fetch_paper(http, _raw_dir(), paper.document_id)
            fetched += 1
        except (RuntimeError, OSError) as error:
            failed.append(f"lifos {paper.rs}")
            print(f"lifos {paper.rs}: failed: {error}")
    print(f"lifos: {fetched} of {len(known)} in-scope papers on disk")
    return failed


def _ingest_lifos(conn) -> int:
    """Each in-scope paper's latest known version, one unit per numbered section."""
    import re as _re
    from datetime import UTC, datetime

    from permitnav.corpus.store import ingest_position_paper, withdraw_papers_out_of_scope
    from permitnav.parsing.position_paper import parse_paper, units
    from permitnav.parsing.sfs_act import pdf_pages
    from permitnav.sources.lifos import latest_known, paper_folder
    from permitnav.sources.raw import latest_raw

    status = 0
    totals = {"papers": 0, "sections": 0, "new": 0, "unchanged": 0, "closed": 0}
    known = latest_known(_raw_dir())
    for paper in known.values():
        folder = paper_folder(_raw_dir(), paper.document_id)
        page, pdf = latest_raw(folder / "page"), latest_raw(folder / "pdf")
        if page is None or pdf is None:
            print(f"lifos {paper.rs}: not downloaded yet; run `permitnav fetch --source lifos`")
            status = 1
            continue
        heading = _re.search(
            r"Rättsligt ställningstagande\.\s*(.*?)\s*-?\s*RS/\d{3}/\d{4}",
            page.content.decode("utf-8", errors="replace"),
        )
        title = heading.group(1).strip(" -") if heading else paper.rs
        try:
            parsed = parse_paper(pdf_pages(pdf.content), paper.rs, title, paper.version)
        except Exception as error:  # noqa: BLE001  pypdf and pdfminer raise many kinds
            print(f"lifos {paper.rs}: PDF not readable: {type(error).__name__}")
            status = 1
            continue
        if parsed.warnings or not parsed.sections:
            print(f"lifos {paper.rs}: not ingested: {parsed.warnings or ['no sections']}")
            status = 1
            continue
        version_start = max((d for d, _, _ in parsed.revisions), default=parsed.in_force)
        report = ingest_position_paper(
            conn, pdf, units(parsed, page.url), f"rs:{paper.rs}", version_start
        )
        totals["papers"] += 1
        totals["sections"] += report.units
        totals["new"] += report.new_versions
        totals["unchanged"] += report.unchanged_versions
        totals["closed"] += report.closed_versions
    withdrawn = withdraw_papers_out_of_scope(conn, {f"rs:{rs}" for rs in known}, datetime.now(UTC))
    if withdrawn:
        print(f"lifos: {withdrawn} sections of papers no longer in scope withdrawn")
    print(
        f"lifos: {totals['papers']} papers, {totals['sections']} sections, {totals['new']} new,"
        f" {totals['unchanged']} unchanged, {totals['closed']} closed"
    )
    return status


def cmd_golden_drift(args: argparse.Namespace) -> int:
    """Golden items whose sources changed since their date (D17). Reports the items that need
    review, and which of them are new since the last run (remembered in the raw volume), so
    the nightly refresh alerts once per item, not every night."""
    import json
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from permitnav.corpus.drift import NEEDS_REVIEW, fresh_copies, golden_drift
    from permitnav.evals.golden import load_golden

    # the corpus dates pages in Swedish time; so does "today"
    today = (
        date.fromisoformat(args.today)
        if args.today
        else datetime.now(ZoneInfo("Europe/Stockholm")).date()
    )
    drift, items, raw_rows = [], [], {}
    with db.connect() as conn:
        for name in args.file:
            path = Path(name)
            file_items = load_golden(path)
            items += file_items
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    raw_rows[row["id"]] = row
            drift += golden_drift(conn, file_items, today)
    for d in drift:
        print(f"  {d.item:<46} {d.kind:<14} {d.unit}  {d.detail}")
    review = sorted({d.item for d in drift if d.kind in NEEDS_REVIEW})
    upcoming = sorted({d.item for d in drift if d.kind == "upcoming"})
    # Which items were already reported, per set of golden files: a run on another file must
    # not make the nightly one report its items again.
    state = _raw_dir() / "golden-drift.json"
    key = ",".join(sorted(args.file))
    saved = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    saved = saved if isinstance(saved, dict) else {}
    new = [i for i in review if i not in set(saved.get(key, []))]
    if args.save and not args.today:
        # only after the report went out (the refresh saves after logging it), so an alert
        # that could not be sent is sent the next night
        saved[key] = review
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(saved, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"golden drift: {len(review)} items need review"
        + (f" (new: {', '.join(new)})" if new else "")
        + (f"; {len(upcoming)} with a dated change coming" if upcoming else "")
    )
    if args.draft:
        rows = list(fresh_copies(items, raw_rows, drift, today))
        with Path(args.draft).open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        print(f"  {len(rows)} fresh copies (review: draft) written to {args.draft}")
    return 0


def cmd_import_lifos_results(args: argparse.Namespace) -> int:
    """Read Lifos result pages a person saved from the browser (the search is closed to
    crawlers) into the position-paper review file. Nothing is fetched."""
    import csv

    from permitnav.parsing.lifos import COLUMNS, curate, merge_review, parse_results

    folder = Path(args.folder).expanduser()
    pages = sorted(folder.glob("*.html"))
    if not pages:
        print(f"no saved .html pages in {folder}")
        return 1
    hits = []
    for page in pages:
        found = parse_results(page.read_text(encoding="utf-8", errors="replace"))
        if not found:
            print(f"  {page.name}: no results found (not a Lifos result page?)")
        hits += found
    papers = curate(hits)
    out = Path(args.out)
    existing = []
    if out.exists():
        with out.open(encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    rows = merge_review(papers, existing)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    decided = Counter(r["decision"] or "undecided" for r in rows)
    print(
        f"{len(pages)} pages, {len(hits)} hits, {len(papers)} papers -> {out}: "
        + ", ".join(f"{n} {d}" for d, n in decided.most_common())
    )
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def cmd_spend(args: argparse.Namespace) -> int:
    """What the site's model calls cost per day (the daily spend cap, permitnav.spend).
    `--unreported` and `--mark-reported` are for the daily refresh: one line per finished day
    on which questions were declined (usually a leaked access token), until it is reported."""
    from permitnav import spend

    with db.connect() as conn:
        if args.unreported:
            for day in spend.unreported(conn):
                print(
                    f"SPEND CAP REACHED on {day.day}: ${day.usd:.2f} in {day.calls} model calls,"
                    f" {day.refused} questions declined"
                )
            return 0
        if args.mark_reported:
            print(f"spend: {spend.mark_reported(conn)} capped days marked as reported")
            return 0
        rows = spend.days(conn, args.days)
    cap = spend.cap_usd()
    print(f"daily cap: {'none' if cap is None else f'${cap:.2f}'} (Stockholm days, the site only)")
    for day in rows:
        notes = [f"{day.refused} questions declined"] if day.refused else []
        if day.unpriced:
            notes.append(f"{day.unpriced} calls without a reported cost, estimated")
        print(f"  {day.day}  ${day.usd:.4f}  {day.calls} calls" + "".join(f"; {n}" for n in notes))
    if not rows:
        print("  nothing recorded yet")
    return 0


def stats_report(counts: list, days: int) -> list[str]:
    """The lines `permitnav stats` prints: per measure, each value's count and share."""
    from permitnav import stats

    summed = stats.totals(counts)
    if not summed:
        return [f"no questions counted in the last {days} days"]
    span = sorted({c.day for c in counts})
    lines = [f"last {days} days ({span[0]} to {span[-1]}, {len(span)} with questions)"]
    for measure, values in stats.MEASURES.items():
        of_measure = summed.get(measure)
        if not of_measure:
            continue
        total = sum(of_measure.values())
        parts = [
            f"{value} {of_measure[value]} ({of_measure[value] / total:.0%})"
            for value in values
            if of_measure.get(value)
        ]
        lines.append(f"  {measure:<15} {total:>5}  " + ", ".join(parts))
    return lines


def cmd_stats(args: argparse.Namespace) -> int:
    """What happened to the questions asked on the site, summed over the last days: answer
    kinds, second model calls, the live judge's verdicts, ratings (permitnav.stats). Counts
    only; `--by-day` lists each day."""
    from permitnav import stats

    with db.connect() as conn:
        counts = stats.since(conn, args.days)
    for line in stats_report(counts, args.days):
        print(line)
    if args.by_day:
        for count in counts:
            print(f"  {count.day}  {count.measure}={count.value}  {count.n}")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """The review queue of feedback people chose to save (phase 6, slice 3). A case goes
    into the golden set only rewritten into a generic question; then delete it here."""
    from permitnav import feedback

    with db.connect() as conn:
        if args.action == "delete":
            if not args.ids:
                print("give the ids to delete (see `permitnav feedback list`)")
                return 2
            removed = feedback.delete(conn, args.ids)
            conn.commit()
            print(f"deleted {removed} of {len(args.ids)}")
            return 0
        cases = feedback.waiting(conn)
    if args.action == "export":
        if not args.out:
            print("export needs --out: a file under private/ (which git ignores), or - for stdout")
            return 2
        rows = [
            {"id": c.id, "created_on": c.created_on.isoformat(), "kind": c.kind,
             "as_of": c.as_of.isoformat(), "question": c.question, "answer": c.answer,
             "comment": c.comment, "cited": c.cited}
            for c in cases
        ]  # fmt: skip
        lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in rows]
        if args.out == "-":  # from the server's container, which has nowhere to keep a file
            sys.stdout.writelines(lines)
            return 0
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("".join(lines), encoding="utf-8")
        print(f"wrote {len(rows)} cases to {args.out}")
        return 0
    print(f"{len(cases)} waiting for review")
    for c in cases:
        question = " ".join(c.question.split())
        comment = " ".join(c.comment.split())
        print(f"  {c.id}  {c.created_on}  {c.kind:<12} as of {c.as_of}  {question[:70]}")
        if comment:
            print(f"        comment: {comment[:100]}")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    """Plain-language descriptions of law wordings, used only to find them (phase 5
    experiment). Run `permitnav index` afterwards to embed them."""
    from permitnav.corpus.descriptions import describe, undescribed
    from permitnav.llm.openrouter import OpenRouterLLM
    from permitnav.llm.port import LLMError

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    try:
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1
    with db.connect() as conn:
        wordings = undescribed(conn, args.limit)
        print(f"{len(wordings):,} law wordings without a description; describing with {model}")
        stored, unusable, cost = describe(
            conn, llm, model, wordings, args.workers,
            lambda done, total: print(f"  described {done}/{total}", end="\r", flush=True),
        )  # fmt: skip
    print(f"\nstored {stored:,}; unusable replies {len(unusable)}; cost ${cost:.4f}")
    for unit in unusable[:20]:
        print(f"  unusable: {unit}")
    print("next: permitnav index (embeds the new description chunks)")
    return 0 if not unusable else 3


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
    from dataclasses import asdict

    from permitnav.evals.golden import load_golden
    from permitnav.evals.run import must_find, run_retrieval, slices
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
    from permitnav.evals.replay import (
        MissingQuery,
        QueryCache,
        RecordingEmbedder,
        RecordingTranslator,
        ReplayEmbedder,
        ReplayTranslator,
    )

    config = _retrieval_config(args)
    if args.queries and args.record_queries:
        print("--queries replays recorded queries; --record-queries records them: pick one")
        return 1
    if args.queries and config.rerank:
        print("--queries runs without models; the reranker needs one")
        return 1
    settings = {"file": args.file, "include_drafts": args.include_drafts, "config": asdict(config)}
    replay = None
    if args.queries:
        try:
            replay = QueryCache.load(Path(args.queries))
        except (OSError, ValueError, KeyError) as error:
            print(f"cannot read recorded queries {args.queries}: {error}")
            return 1
        if _query_settings(replay.settings) != _query_settings(settings):
            print(f"{args.queries} was recorded with other settings: {replay.settings}")
            print("record it again with the settings of this run (see evals/README.md)")
            return 1
    recording = QueryCache(settings=settings) if args.record_queries else None
    if replay is not None:  # CI: no model, no API key
        embedder = ReplayEmbedder(replay)
    else:
        try:
            from permitnav.embedding.bge_m3 import BgeM3Embedder

            embedder = BgeM3Embedder()
        except ImportError:
            print("bge-m3 needs the embed extra: uv sync --extra embed")
            return 1
        if recording is not None:
            embedder = RecordingEmbedder(embedder, recording)
    from permitnav.llm.openrouter import LLMError

    try:
        if not config.translate:
            translator = None
        elif replay is not None:
            translator = ReplayTranslator(replay)
        else:
            translator = _translator(True)
            if recording is not None:
                translator = RecordingTranslator(translator, recording)
        with db.connect() as conn:
            existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
            retriever = Retriever(conn, embedder, config, translator, _reranker(config.rerank))
            cache: dict = {}

            def applying_on(d):
                if d not in cache:
                    cache[d] = units_applying_on(conn, d)
                return cache[d]

            run = run_retrieval(
                items,
                retriever,
                existing,
                applying_on,
                lambda u, d: units_with_same_text(conn, u, d),
            )
            if recording is not None:
                # Questions the eval skips (out of scope, nothing findable) are still asked
                # by the answer evals and the smoke slice: record what retrieval needs for
                # them too.
                scored = {r.item.id for r in run.results}
                for item in items:
                    if item.id not in scored:
                        retriever.search(item.question, as_of=item.as_of, k=10)
    except LLMError as error:
        print(f"query translation failed: {error}")
        print("(the retrieval eval translates English questions; --no-translate runs offline)")
        return 1
    except MissingQuery as error:
        print(f"{error}: refresh the recorded queries (see evals/README.md)")
        return 1
    if recording is not None:
        recording.save(Path(args.record_queries))
        print(
            f"recorded {len(recording.vectors)} vectors and {len(recording.translations)}"
            f" translations -> {args.record_queries}"
        )

    label = (
        "DEVELOPMENT RUN, includes drafts, not reportable"
        if args.include_drafts
        else "checked items"
    )
    weight = f", keyword_weight={config.keyword_weight}" if args.mode == "hybrid" else ""
    weight += ", translate" if config.translate else ""
    weight += " (+ law vector)" if config.translated_vector else ""
    weight += ", collapse variants" if config.collapse_variants else ""
    if config.translated_page_weight and config.mode == "hybrid" and config.mix != "none":
        weight += f", translated page ranking {config.translated_page_weight}"
    weight += f", rerank top {config.rerank_depth}" if config.rerank else ""
    weight += f", mix={config.mix}" if config.mix != "interleave" else ""
    print(
        f"\nRetrieval eval: {args.file}, strategy={args.strategy}, mode={args.mode}{weight}"
        f" ({label})"
    )
    _print_provenance(config)
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
    # English readers should get English pages where they exist (D6): of English questions'
    # page sources in the top 8, how many are in English.
    english_pages = [
        u
        for r in run.results
        if r.item.original_language == "en"
        for u in r.retrieved[:8]
        if u.startswith("web:")
    ]
    if english_pages:
        in_english = sum(u.startswith("web:en/") for u in english_pages)
        print(
            f"English questions' page sources in English: {in_english} of {len(english_pages)}"
            f" ({in_english / len(english_pages):.0%})"
        )
    if cost := _translation_cost(translator):
        print(cost)
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
    overall = next(s for s in slices(run) if s.name == "all")
    below = []
    if args.min_r5 is not None and overall.recall_5 < args.min_r5 - 1e-9:
        below.append(f"R@5 {overall.recall_5:.4f} < {args.min_r5}")
    if args.min_mrr is not None and overall.mrr < args.min_mrr - 1e-9:
        below.append(f"MRR {overall.mrr:.4f} < {args.min_mrr}")
    # Named items whose rule must reach the answer model (its top MUST_FIND_K sources).
    unscored, missing = must_find(run, args.must_find or [], AnswerConfig.k)
    if unscored:
        print(
            "MUST-FIND ERROR: not scored (unknown id, a draft, or skipped above):"
            f" {', '.join(unscored)}"
        )
        return 2
    below += missing
    if below:
        print(f"RETRIEVAL REGRESSION: {'; '.join(below)}")
        return 3
    if args.min_r5 is not None or args.min_mrr is not None:
        print(f"gate passed: R@5 {overall.recall_5:.4f}, MRR {overall.mrr:.4f}")
    if args.must_find:
        print(
            f"must-find passed: {len(args.must_find)} named items have an expected source of"
            f" every group in the top {AnswerConfig.k}"
        )
    return 0


SOURCES = ["riksdagen", "migrationsverket", "domstol", "sfsr", "lifos"]


def refresh_steps() -> list[tuple[str, Callable[[argparse.Namespace], int], argparse.Namespace]]:
    """The refresh's steps: each source fetched on its own (one site being down does not stop
    the others), then one ingest of all of them, then the index."""
    fetches = [
        (f"fetch {source}", cmd_fetch, argparse.Namespace(source=source, sfs=[]))
        for source in SOURCES
    ]
    return [
        *fetches,
        ("ingest", cmd_ingest, argparse.Namespace(source="all", sfs=[])),
        ("index", cmd_index, argparse.Namespace(strategy=["structure"], no_embed=False)),
    ]


def cmd_refresh(args: argparse.Namespace) -> int:
    """The daily refresh (phase 4): fetch every source, ingest, index the strategy production
    searches, and summarise the "what changed" log for this run. A failed step is reported
    and the later steps still run, so one site being down does not stop the others."""
    from datetime import UTC, datetime

    started = datetime.now(UTC)
    steps = refresh_steps()
    failed = []
    for name, step, step_args in steps:
        print(f"== {name}")
        try:
            status = step(step_args)
        except Exception as error:  # a crash in one step must not stop the others
            print(f"{name} crashed: {type(error).__name__}: {error}")
            status = 1
        if status:
            failed.append(name)
    with db.connect() as conn:
        counts = dict(
            conn.execute(
                "SELECT kind, count(*) FROM change_log WHERE observed_at >= %s GROUP BY kind",
                (started,),
            ).fetchall()
        )
        units = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT unit_id FROM change_log WHERE observed_at >= %s"
                " ORDER BY unit_id LIMIT 20",
                (started,),
            ).fetchall()
        ]
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no changes"
    print(f"REFRESH SUMMARY: {summary}" + (f"; failed: {', '.join(failed)}" if failed else ""))
    for unit in units:
        print(f"  changed: {unit}")
    return 1 if failed else 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """The LLM smoke slice: real answers to a few golden questions (evals/smoke.json)."""
    from dataclasses import asdict

    from permitnav.answering.answerer import BGE_M3_FLOOR, AnswerConfig, Answerer
    from permitnav.evals.answers import run_answers
    from permitnav.evals.golden import load_golden
    from permitnav.evals.replay import MissingQuery, QueryCache, ReplayEmbedder, ReplayTranslator
    from permitnav.evals.smoke import SmokeSlice, problems
    from permitnav.llm.openrouter import LLMError, OpenRouterLLM
    from permitnav.retrieval.retriever import Retriever

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    answer_effort = _answer_effort()
    smoke = SmokeSlice.load(Path(args.smoke))
    by_id = {i.id: i for i in load_golden(Path(args.file))}
    missing = [i for i in smoke.items if i not in by_id]
    if missing:
        print(f"smoke items not in {args.file}: {', '.join(missing)}")
        return 1
    items = [by_id[i] for i in smoke.items]
    config = _retrieval_config(args)
    if args.queries and config.rerank:
        print("--queries runs without local models; the reranker needs one")
        return 1
    if args.queries:  # CI: questions replayed, so the only model is the answer model
        try:
            cache = QueryCache.load(Path(args.queries))
        except (OSError, ValueError, KeyError) as error:
            print(f"cannot read recorded queries {args.queries}: {error}")
            return 1
        recorded = _query_settings({"config": cache.settings.get("config", {})})
        if recorded != _query_settings({"config": asdict(config)}):
            print(f"{args.queries} was recorded with other retrieval settings")
            return 1
        embedder = ReplayEmbedder(cache)
        translator = ReplayTranslator(cache) if config.translate else None
    else:
        try:
            from permitnav.embedding.bge_m3 import BgeM3Embedder

            embedder = BgeM3Embedder()
        except ImportError:
            print("bge-m3 needs the embed extra: uv sync --extra embed")
            return 1
        translator = _translator(config.translate)
    try:
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1
    reranker = _reranker(config.rerank)

    def ask(questions: list) -> list:
        with db.connect() as conn:
            retriever = Retriever(conn, embedder, config, translator, reranker)
            answer_config = AnswerConfig(
                model=model, min_score=BGE_M3_FLOOR, reasoning_effort=answer_effort
            )
            return run_answers(questions, Answerer(conn, retriever, llm, answer_config))

    try:
        results = ask(items)
    except MissingQuery as error:
        print(f"{error}: refresh the recorded queries (see evals/README.md)")
        return 1
    print(f"\nSmoke slice: {len(items)} questions, model={model}")
    _print_provenance(config, model, answer_effort=answer_effort)
    failures = 0
    cost = 0.0
    unknown_cost = 0

    def add_cost(result) -> None:
        nonlocal cost, unknown_cost
        if result.cost_usd is None:
            unknown_cost += 1
        else:
            cost += result.cost_usd

    for result in results:
        found = problems(result)
        add_cost(result)
        kind = result.kind
        if found:  # asked once more: a real regression fails again, a random flip rarely does
            (again,) = ask([result.item])
            add_cost(again)
            if problems(again):
                kind = again.kind
                found = [f"first: {'; '.join(found)}", f"again: {'; '.join(problems(again))}"]
            else:
                print(f"  (passed on a second try: {result.item.id}, first {'; '.join(found)})")
                kind, found = again.kind, []
        failures += bool(found)
        print(
            f"  {'FAIL' if found else 'ok  '} {result.item.id:<40} {kind:<13} {' | '.join(found)}"
        )
    costs = f"answers ${cost:.4f}"
    if unknown_cost:
        costs += f" (+{unknown_cost} with no cost reported)"
    if translation := _translation_cost(translator):
        costs += f"; {translation.strip('[]')}"
    print(
        f"{len(results) - failures} of {len(results)} passed"
        f" (at most {smoke.max_failures} may fail); {costs}"
    )
    if failures > smoke.max_failures:
        print("SMOKE FAILED")
        return 1
    return 0


def cmd_export_eval_corpus(args: argparse.Namespace) -> int:
    from permitnav.corpus.snapshot import export_snapshot
    from permitnav.evals.provenance import git_version

    with db.connect() as conn:
        manifest = export_snapshot(conn, Path(args.out), git_version())
    size = Path(args.out).stat().st_size
    counts = ", ".join(f"{n:,} {t}" for t, n in manifest["counts"].items())
    print(f"{args.out}: {size / 1e6:.1f} MB, schema {manifest['schema']}: {counts}")
    return 0


def cmd_import_eval_corpus(args: argparse.Namespace) -> int:
    import psycopg

    from permitnav.corpus.snapshot import import_snapshot

    with db.connect() as conn:
        try:
            manifest = import_snapshot(conn, Path(args.file))
        except (OSError, ValueError, psycopg.Error) as error:
            print(f"not imported: {error}")
            return 1
    counts = ", ".join(f"{n:,} {t}" for t, n in manifest["counts"].items())
    print(f"imported {args.file} (made {manifest['created']} at {manifest['commit']}): {counts}")
    return 0


KIND_LABELS = {
    "answered": "",
    "follow_up": "I need one more detail to answer this:",
    "not_settled": "Answer not confirmed (verify with Migrationsverket before relying on it):",
    "out_of_scope": "Outside what PermitNav covers:",
    "failed": "Something went wrong:",
}


def _second_searcher(args: argparse.Namespace, retriever, llm, model: str):
    """Phase 5's second search when asked for (`--second-search`): the evidence check runs
    on `--check-model`, else the answer model."""
    if not getattr(args, "second_search", False):
        return None
    from permitnav.agent.second_search import SecondSearcher

    return SecondSearcher(retriever, llm, getattr(args, "check_model", None) or model)


def _intake(args: argparse.Namespace, llm, model: str):
    """Intake, as production uses it: on in `ask`, and in `eval-answers` unless
    `--no-memory`. It calls the model only for questions that have earlier turns or that
    rule something out ("not an EU Blue Card"), which search would misread."""
    if args.run is cmd_eval_answers and args.no_memory:
        return None
    from permitnav.agent.intake import IntakeRewriter

    return IntakeRewriter(llm, model)


def cmd_ask(args: argparse.Namespace) -> int:
    from datetime import date

    from permitnav.answering.answerer import BGE_M3_FLOOR, AnswerConfig, Answerer
    from permitnav.llm.openrouter import LLMError, OpenRouterLLM
    from permitnav.retrieval.retriever import Retriever

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    answer_effort = _answer_effort()
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
            config = _retrieval_config(args)
            translator = _translator(config.translate)
            retriever = Retriever(conn, embedder, config, translator, _reranker(config.rerank))
            answer_config = AnswerConfig(
                model=model, min_score=BGE_M3_FLOOR, k=args.k, reasoning_effort=answer_effort
            )
            answerer = Answerer(
                conn,
                retriever,
                llm,
                answer_config,
                _second_searcher(args, retriever, llm, model),
                _intake(args, llm, model),
            )
            answer = answerer.answer(args.question, as_of=as_of, history=args.earlier or [])
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1

    print()
    if answer.intake and answer.intake.rewritten:  # a follow-on: what was searched and answered
        print(f"Understood as: {answer.intake.question}\n")
    if answer.intake and answer.intake.search:  # something was ruled out: searched without it
        print(f"Searched with: {answer.intake.search}\n")
    if KIND_LABELS.get(answer.kind):
        print(KIND_LABELS[answer.kind])
    print(answer.text.replace("**", ""))  # bold labels are for the web UI
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
    if any(c.authority in ("web_page", "position_paper") for c in answer.citations):
        credits.append("Källa: Migrationsverket")
    if any(c.authority == "ruling" for c in answer.citations):
        credits.append("Källa: Sveriges Domstolar")
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
    for name, extra in (("intake rewrite", answer.intake), ("second search", answer.search)):
        if extra is not None and (e := extra.completion) is not None:
            cost = f", ${e.cost_usd:.4f}" if e.cost_usd is not None else ""
            print(f"[{name}, {e.model}: {e.input_tokens:,} in / {e.output_tokens:,} out{cost}]")
    if answer.earlier:  # a reply cut off or unreadable, then asked again
        known = [e.cost_usd for e in answer.earlier if e.cost_usd is not None]
        cost = f": ${sum(known):.4f}" if len(known) == len(answer.earlier) else " (cost unknown)"
        print(f"[{len(answer.earlier)} earlier call(s) for this answer{cost}]")
    if translation := _translation_cost(translator):
        print(f"[{translation}]")
    return 0 if answer.kind != "failed" else 1


def cmd_eval_answers(args: argparse.Namespace) -> int:
    import json

    from permitnav.answering.answerer import BGE_M3_FLOOR, AnswerConfig, Answerer
    from permitnav.evals.answers import kind_table, run_answers
    from permitnav.evals.golden import load_golden
    from permitnav.llm.openrouter import LLMError, OpenRouterLLM
    from permitnav.retrieval.retriever import Retriever

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    answer_effort = _answer_effort()
    items = load_golden(Path(args.file))
    if not args.include_drafts:
        items = [i for i in items if i.review == "checked"]
    if args.kind:
        items = [i for i in items if i.expected_kind in args.kind]
    items = items[: args.limit] if args.limit else items
    if not items:
        drafts = "" if args.include_drafts else "; only checked items count unless --include-drafts"
        print(f"no items left after the filters (--kind, --limit){drafts}")
        return 1
    try:
        from permitnav.embedding.bge_m3 import BgeM3Embedder

        embedder = BgeM3Embedder()
    except ImportError:
        print("bge-m3 needs the embed extra: uv sync --extra embed")
        return 1
    floor = BGE_M3_FLOOR if args.floor is None else args.floor
    config = _retrieval_config(args)
    try:
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
        translator = _translator(config.translate)
        # Built before any answer is paid for, so a missing judge setting fails at once.
        judge, judge_model, judge_effort = _judge() if args.judge else (None, None, None)
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1
    with db.connect() as conn:
        retriever = Retriever(conn, embedder, config, translator, _reranker(config.rerank))
        answerer = Answerer(
            conn,
            retriever,
            llm,
            AnswerConfig(model=model, min_score=floor, k=args.k, reasoning_effort=answer_effort),
            _second_searcher(args, retriever, llm, model),
            _intake(args, llm, model),
        )
        results = run_answers(
            items,
            answerer,
            lambda done, total: print(f"  answered {done}/{total}", end="\r", flush=True),
        )

    verdicts: dict = {}
    judge_cost = 0.0
    if judge is not None:
        from permitnav.evals.answers import judgeable

        for n, r in enumerate(results, start=1):
            print(f"  judging {n}/{len(results)}", end="\r", flush=True)
            item = judgeable(r)
            if item is not None:  # a failed call gives an unreadable verdict, never correct
                verdicts[r.item.id] = judge.judge(item)
        judge_cost = sum(c.cost_usd or 0.0 for v in verdicts.values() for c in v.completions)

    label = "DEVELOPMENT RUN, includes drafts, not reportable" if args.include_drafts else "checked"
    print(f"\nBehaviour eval: {args.file}, model={model}, floor={floor}, k={args.k} ({label})")
    second = "second search (evidence check, at most 2 steps)"
    if args.second_search:
        second += f", check model {args.check_model or model}"
    memory = "no memory (--no-memory)" if args.no_memory else "memory (earlier turns rewritten in)"
    print(f"workflow: {second if args.second_search else 'plain RAG (one search)'}; {memory}")
    effort = f" (reasoning effort {judge_effort})" if judge_effort else ""
    _print_provenance(
        config, model, f"{judge_model}{effort}" if judge_model else None, answer_effort
    )
    print(f"{'expected kind':<14} {'n':>3} {'right':>5}  given instead")
    for row in kind_table(results):
        instead = ", ".join(f"{kind} {n}" for kind, n in row.got.most_common())
        print(f"{row.expected:<14} {row.n:>3} {row.correct:>5}  {instead}")
    right = sum(r.correct for r in results)
    skipped = sum(r.answer.completion is None and r.kind != "failed" for r in results)
    failed = sum(r.kind == "failed" for r in results)
    costs = [r.cost_usd for r in results]
    cost = sum(c for c in costs if c is not None)
    unknown = sum(c is None for c in costs)
    if translator is not None:  # the translations are part of the run's cost
        cost += translator.cost_usd
        unknown += translator.cost_unknown
    print(f"all            {len(results):>3} {right:>5}")
    from permitnav.evals.answers import language_matches

    same, checked = language_matches(results)
    if checked:
        print(f"in the question's language: {same} of {checked} answers")
    from permitnav.evals.answers import lead_first, leads_first

    led, covered = leads_first(results)
    if covered:
        print(f"conclusion first (short, cited first paragraph): {led} of {covered} answers")
    print(
        f"declined without a model call: {skipped}; failed: {failed}; cost ${cost:.4f}"
        + (f" (+ {unknown} replies with unknown cost)" if unknown else "")
    )
    if args.judge:
        from permitnav.evals.answers import judged_summary

        effort_note = f", reasoning effort {judge_effort}" if judge_effort else ""
        print(f"\nJudged by {judge_model}{effort_note} (calibrated, D19; blind spots in DESIGN.md)")
        print(
            f"{'expected kind':<14} {'n':>3} {'correct':>7} {'partly':>6}"
            f" {'wrong':>5} {'faithful':>8}"
        )
        rows = judged_summary(results, verdicts)
        for row in rows:
            gaps = [
                f"{row.not_judged} not judged (declined or failed)" if row.not_judged else "",
                f"{row.unreadable} unreadable" if row.unreadable else "",
            ]
            extra = "; ".join(g for g in gaps if g)
            print(
                f"{row.expected:<14} {row.items:>3} {row.correct:>7} {row.partly:>6}"
                f" {row.incorrect:>5} {row.faithful:>8}" + (f"  ({extra})" if extra else "")
            )
        items = sum(r.items for r in rows)
        read = sum(r.n for r in rows)
        if items:
            correct = sum(r.correct for r in rows)
            faithful = sum(r.faithful for r in rows)
            print(
                f"all            {items:>3} {correct:>7}   correct {correct / items:.0%} of all"
                f" {items}; faithful {faithful}/{read} judged; judge cost ${judge_cost:.4f}"
            )
    if args.verbose:
        for r in results:
            if not r.correct:
                text = " ".join(r.answer.text.split())[:110]
                print(f"  {r.item.id:<40} expected {r.item.expected_kind}, got {r.kind}: {text}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                [
                    {
                        "id": r.item.id,
                        "expected": r.item.expected_kind,
                        "kind": r.kind,
                        "text": r.answer.text,
                        "sources": [s.unit_id for s in r.answer.sources],
                        "cost_usd": r.cost_usd,
                        "lead_first": lead_first(r.kind, r.answer.text),
                        **(
                            {
                                "intake": {
                                    "question": r.answer.intake.question,
                                    "rewritten": r.answer.intake.rewritten,
                                    "search": r.answer.intake.search,
                                    "situation": r.answer.intake.situation,
                                    "fallback": r.answer.intake.fallback,
                                }
                            }
                            if r.answer.intake
                            else {}
                        ),
                        **(
                            {
                                "second_search": {
                                    "enough": r.answer.search.enough,
                                    "missing": r.answer.search.missing,
                                    "steps": [
                                        {"tool": s.tool, **s.args} for s in r.answer.search.steps
                                    ],
                                    "added": r.answer.search.added,
                                }
                            }
                            if r.answer.search
                            else {}
                        ),
                        **(
                            {
                                "judge": {
                                    "correctness": verdicts[r.item.id].correctness,
                                    "faithful": verdicts[r.item.id].faithful,
                                    "unsupported": list(verdicts[r.item.id].unsupported),
                                    "reason": verdicts[r.item.id].reason,
                                }
                            }
                            if r.item.id in verdicts
                            else {}
                        ),
                        **({"recovered": True} if r.answer.recovered else {}),
                        # For failed and recovered answers: what the model actually sent back.
                        **(
                            {
                                "raw": r.answer.completion.text,
                                "finish_reason": r.answer.completion.extra.get("finish_reason"),
                                # The replies before it (unreadable or cut off), oldest first.
                                "earlier_raw": [
                                    {
                                        "text": e.text,
                                        "finish_reason": e.extra.get("finish_reason"),
                                    }
                                    for e in r.answer.earlier
                                ],
                            }
                            if (r.kind == "failed" or r.answer.recovered) and r.answer.completion
                            else {}
                        ),
                    }
                    for r in results
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


def _judge(model: str | None = None, reasoning_effort: str | None = None):
    """The LLM judge (D19): JUDGE_MODEL and JUDGE_REASONING_EFFORT unless overridden. A
    reasoning model gets room for its thinking (it counts against the token limit)."""
    from permitnav.evals.judge import Judge
    from permitnav.llm.openrouter import OpenRouterLLM

    effort = reasoning_effort
    if not model:  # the configured judge: its model and its reasoning effort go together
        model = os.environ.get("JUDGE_MODEL", "").strip()
        effort = effort or os.environ.get("JUDGE_REASONING_EFFORT", "").strip() or None
    if not model:
        raise SystemExit("JUDGE_MODEL is not set; see .env.example")
    if effort and effort not in ("low", "medium", "high"):
        raise SystemExit(f"JUDGE_REASONING_EFFORT must be low, medium or high, not {effort!r}")
    # More patient retries than answering: judge providers rate-limit in bursts.
    llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""), retries=5)
    if effort:
        return Judge(llm, model, max_tokens=8000, reasoning_effort=effort), model, effort
    return Judge(llm, model), model, None


CALIBRATION_ANSWERS = Path("evals/judge/answers.jsonl")
CALIBRATION_LABELS = Path("evals/judge/labels.jsonl")


def cmd_judge_sample(args: argparse.Namespace) -> int:
    """Generate the judge's calibration answers (slice 7): 30 in-scope dev items, answered
    with the current configuration, saved with the sources the answer model was shown."""
    from permitnav.answering.answerer import BGE_M3_FLOOR, AnswerConfig, Answerer
    from permitnav.evals.golden import load_golden
    from permitnav.evals.judge import CalibrationItem, calibration_ids, save_items
    from permitnav.llm.openrouter import LLMError, OpenRouterLLM
    from permitnav.retrieval.retriever import Retriever

    model = os.environ.get("ANSWER_MODEL", "").strip()
    if not model:
        print("ANSWER_MODEL is not set; see .env.example")
        return 1
    answer_effort = _answer_effort()
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists; its labels would no longer match. Use --force to replace it.")
        return 1
    items = load_golden(Path(args.file))
    wanted = calibration_ids(items)
    items = sorted((i for i in items if i.id in wanted), key=lambda i: wanted.index(i.id))
    try:
        from permitnav.embedding.bge_m3 import BgeM3Embedder

        embedder = BgeM3Embedder()
    except ImportError:
        print("bge-m3 needs the embed extra: uv sync --extra embed")
        return 1
    config = _retrieval_config(args)
    try:
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""))
        translator = _translator(config.translate)
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1
    sample = []
    failed: list[str] = []
    with db.connect() as conn:
        retriever = Retriever(conn, embedder, config, translator, _reranker(config.rerank))
        answerer = Answerer(
            conn,
            retriever,
            llm,
            AnswerConfig(model=model, min_score=BGE_M3_FLOOR, reasoning_effort=answer_effort),
        )
        for n, item in enumerate(items, start=1):
            print(f"  answering {n}/{len(items)}", end="\r", flush=True)
            try:
                answer = answerer.answer(item.question, as_of=item.as_of)
            except LLMError as error:  # keep the answers already paid for
                failed.append(item.id)
                print(f"\n  {item.id}: model call failed: {error}")
                continue
            sample.append(
                CalibrationItem(
                    id=item.id,
                    question=item.question,
                    language=item.original_language,
                    expected_kind=item.expected_kind,
                    reference_answer=item.reference_answer,
                    turns=list(item.turns),
                    kind=answer.kind,
                    answer=answer.text,
                    sources=[
                        {
                            "marker": src.marker,
                            "unit_id": src.unit_id,
                            "role": src.role,
                            "heading": src.heading,
                            "dates": src.dates,
                            "url": src.url,
                            "text": src.text,
                        }
                        for src in answer.sources
                    ],
                )
            )
    save_items(out, sample)
    if failed:
        print(
            f"\n{len(failed)} answers failed and are missing: {', '.join(failed)};"
            " rerun with --force to regenerate the sample"
        )
    kinds = ", ".join(
        f"{k} {sum(i.kind == k for i in sample)}" for k in sorted({i.kind for i in sample})
    )
    print(f"\nsaved {len(sample)} answers to {out} ({kinds}); answer model {model}")
    return 0


def cmd_judge_faithfulness(args: argparse.Namespace) -> int:
    """The faithfulness benchmark (evals/judge/faithfulness): every stored configuration
    against the labels, from the stored predictions and with no model call; with --run, ask
    a judge about the 40 answers first (paid) and store what it said under that name."""
    from permitnav.evals import faithfulness

    directory = Path(args.directory)
    cases, labels = faithfulness.load_cases(directory), faithfulness.load_labels(directory)
    if args.run_as:
        if not faithfulness.NAME.fullmatch(args.run_as):
            print("--run takes a short name: letters, digits, dots and dashes")
            return 1
        if args.run_as in faithfulness.configurations(directory) and not args.replace:
            print(f"{args.run_as} is stored already; --replace to run it again")
            return 1
        from datetime import date

        from permitnav.evals.judge import Judge
        from permitnav.evals.provenance import prompt_hash
        from permitnav.llm.openrouter import OpenRouterLLM

        shared = faithfulness.shared_settings(directory)
        current = prompt_hash(shared["prompt"])
        if shared["prompt_hash"] != current:  # allowed: this is how a new prompt is tried
            print(
                f"note: the judge prompt ({current}) is not the one the first runs used"
                f" ({shared['prompt_hash']}); this run is recorded with its own prompt"
            )
        _, model, effort = _judge(args.model, args.reasoning_effort)  # settings as configured
        # The stored runs' token limit, whatever the model: the comparison holds it equal.
        llm = OpenRouterLLM(os.environ.get("OPENROUTER_API_KEY", ""), retries=5)
        judge = Judge(llm, model, max_tokens=shared["max_tokens"], reasoning_effort=effort)
        print(f"asking {model} (reasoning effort {effort or 'default'}), {args.runs} runs…")
        predictions = faithfulness.run(judge, cases, args.runs)
        faithfulness.save_predictions(args.run_as, predictions, directory)
        faithfulness.record_run(
            args.run_as,
            model,
            effort,
            predictions,
            date.today().isoformat(),
            current,
            shared["max_tokens"],
            directory,
        )
    names = [args.run_as] if args.run_as else faithfulness.configurations(directory)
    results = [
        faithfulness.evaluate(name, cases, labels, faithfulness.load_predictions(name, directory))
        for name in names
    ]
    from permitnav.evals.provenance import prompt_hash as hash_of

    used = {n: h for n, h in faithfulness.prompt_hashes(directory).items() if n in names}
    notes = faithfulness.prompt_notes(used, hash_of("judge_faithfulness.md"))
    print("\n".join(faithfulness.report(results, cases, labels, notes)))
    return 0


def cmd_judge_calibrate(args: argparse.Namespace) -> int:
    """Run the judge on the calibration answers and compare with the hand labels (D19)."""
    import json

    from permitnav.evals.judge import (
        THRESHOLD,
        Label,
        agreement,
        load_items,
        load_known_cases,
        load_labels,
        load_seeded,
    )
    from permitnav.llm.openrouter import LLMError

    items = {i.id: i for i in load_items(Path(args.answers))}
    labels = load_labels(Path(args.labels))
    seeded_ids: set[str] = set()
    if args.seeded and Path(args.seeded).exists():
        seeded, seeded_labels = load_seeded(Path(args.seeded), items)
        items.update({i.id: i for i in seeded})
        labels.update(seeded_labels)
        seeded_ids = set(seeded_labels)
    known_labels: dict[str, Label] = {}
    if args.known_cases:  # '' for none; a path that does not exist is a mistake, not "none"
        try:
            known, known_labels = load_known_cases(Path(args.known_cases))
        except (OSError, ValueError) as error:
            print(f"cannot read the known cases: {error}")
            return 1
        clash = sorted(known_labels.keys() & items.keys())
        if clash:  # it would replace a sampled answer and drop it from the bar
            print(f"known cases reuse ids of calibration answers: {', '.join(clash)}")
            return 1
        items.update({i.id: i for i in known})
        labels.update(known_labels)
    missing = sorted(items.keys() - labels.keys())
    if missing:
        more = "…" if len(missing) > 5 else ""
        print(f"{len(missing)} answers have no label yet: {', '.join(missing[:5])}{more}")
    orphans = sorted(labels.keys() - items.keys())
    if orphans:
        more = "…" if len(orphans) > 5 else ""
        print(f"{len(orphans)} labels have no answer (stale ids?): {', '.join(orphans[:5])}{more}")
    to_judge = sorted(items.keys() & labels.keys())
    try:
        judge, model, effort_used = _judge(args.model, args.reasoning_effort)
    except LLMError as error:
        print(f"model call failed: {error}")
        return 1
    verdicts = {}
    for n, i in enumerate(to_judge, start=1):
        print(f"  judging {n}/{len(to_judge)}", end="\r", flush=True)
        verdicts[i] = judge.judge(items[i])  # a failed call: unreadable, never agreement
    failed = sum(v.reason.startswith("model call failed") for v in verdicts.values())
    if failed:
        print(f"\n{failed} verdicts failed (model call errors); they count as disagreements")
    # Known cases are picked, not sampled: they are reported below and kept out of the bar.
    sampled = {k: v for k, v in labels.items() if k not in known_labels}
    result = agreement(sampled, verdicts)
    verdict_word = "PASSES" if result.passes() else "does not pass"
    effort = f", reasoning effort {effort_used}" if effort_used else ""
    print(
        f"\nJudge {model}{effort} on {result.n} labelled answers"
        f" ({verdict_word} the {THRESHOLD:.0%} bar)"
    )
    print(
        f"  correctness agreement  {result.correctness:.0%}"
        f" (exact level: {result.correctness_exact:.0%})"
    )
    print(f"  faithfulness agreement {result.faithfulness:.0%}")
    if seeded_ids:
        for name, subset in (
            ("real answers", {k: v for k, v in sampled.items() if k not in seeded_ids}),
            ("seeded errors", {k: v for k, v in labels.items() if k in seeded_ids}),
        ):
            part = agreement(subset, verdicts)
            print(
                f"  {name:<14} n={part.n:<3} correctness {part.correctness:.0%}, "
                f"faithfulness {part.faithfulness:.0%}"
            )
    # A known case is kept for one mistake about correctness. It is missed when the judge and
    # the label disagree on correct or not ("partly" for "incorrect" is not a miss);
    # faithfulness is shown beside it and does not decide.
    known_missed = []
    for case_id, label in sorted(known_labels.items()):
        verdict = verdicts[case_id]
        right = (verdict.correctness == "correct") == (label.correctness == "correct")
        if not right:
            known_missed.append(case_id)
        words = {True: "faithful", False: "not faithful"}
        faith = ""
        if verdict.faithful != label.faithful:
            faith = (
                f" (faithfulness differs: labelled {words[label.faithful]},"
                f" judge says {words[bool(verdict.faithful)]})"
            )
        print(
            f"  known case {'ok  ' if right else 'MISS'} {case_id}: labelled {label.correctness},"
            f" judge says {verdict.correctness or 'unreadable'}{faith}"
        )
        if not right:
            print(f"      judge's reason: {verdict.reason[:300]}")
    if known_missed:
        print(f"  KNOWN CASES MISSED: {', '.join(known_missed)} (the command fails)")
    print(
        "  correctness (you → judge): "
        + ", ".join(f"{a}→{b} {n}" for (a, b), n in sorted(result.correctness_confusion.items()))
    )
    cost = sum(c.cost_usd or 0.0 for v in verdicts.values() for c in v.completions)
    print(f"  cost ${cost:.4f}")
    if args.verbose:
        for item_id, dimension, detail in result.disagreements:
            print(f"  {item_id:<44} {dimension:<12} {detail}: {verdicts[item_id].reason[:140]}")
    if args.out:
        Path(args.out).write_text(
            "".join(
                json.dumps(
                    {
                        "id": i,
                        "correctness": v.correctness,
                        "faithful": v.faithful,
                        "unsupported": list(v.unsupported),
                        "reason": v.reason,
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for i, v in sorted(verdicts.items())
            ),
            encoding="utf-8",
        )
    return 0 if result.passes() and not known_missed else 2


def traces(args: argparse.Namespace) -> bool:
    """Whether a CLI run sends traces: `ask` always (one question, like the API), eval
    commands only with `--trace`, nothing else. Eval runs and the nightly retrieval check
    used most of the Langfuse plan's monthly units, for details the eval output already has."""
    return args.run is cmd_ask or bool(getattr(args, "trace", False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permitnav")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(run=cmd_migrate)
    sources = [*SOURCES, "all"]
    fetch = sub.add_parser("fetch", help="download statutes and Migrationsverket pages")
    fetch.add_argument("sfs", nargs="*", help="SFS numbers, e.g. 2005:716 (default: all in scope)")
    fetch.add_argument("--source", default="all", choices=sources)
    fetch.set_defaults(run=cmd_fetch)
    ingest = sub.add_parser("ingest", help="parse the latest downloads and store them")
    ingest.add_argument("sfs", nargs="*", help="SFS numbers (default: all in scope)")
    ingest.add_argument("--source", default="all", choices=sources)
    ingest.set_defaults(run=cmd_ingest)
    gd = sub.add_parser(
        "golden-drift", help="golden items whose expected sources changed since their date"
    )
    gd.add_argument("--file", nargs="+", default=["evals/golden.dev.jsonl"])
    gd.add_argument("--today", help="check as if today were this date (a what-if; not saved)")
    gd.add_argument("--draft", help="write fresh copies dated today (D17) here, for review")
    gd.add_argument(
        "--save", action="store_true", help="remember these items as reported (after alerting)"
    )
    gd.set_defaults(run=cmd_golden_drift)
    il = sub.add_parser(
        "import-lifos-results",
        help="read saved Lifos result pages into the position-paper review file (no fetching)",
    )
    il.add_argument("folder", help="folder with the result pages saved from the browser")
    il.add_argument("--out", default="curation/position_papers.csv")
    il.set_defaults(run=cmd_import_lifos_results)
    sp = sub.add_parser("spend", help="the site's model spend per day (the daily cap)")
    sp.add_argument("--days", type=_positive_int, default=7)
    sp.add_argument("--unreported", action="store_true", help="capped days not yet reported")
    sp.add_argument("--mark-reported", action="store_true", help="remember they were reported")
    sp.set_defaults(run=cmd_spend)
    st = sub.add_parser("stats", help="counts of what happened to the site's questions")
    st.add_argument("--days", type=_positive_int, default=30)
    st.add_argument("--by-day", action="store_true", help="also list every day's counts")
    st.set_defaults(run=cmd_stats)
    fb = sub.add_parser("feedback", help="the review queue of feedback people chose to save")
    fb.add_argument("action", choices=["list", "export", "delete"])
    fb.add_argument("ids", nargs="*", help="delete: the ids of reviewed cases")
    fb.add_argument(
        "--out", help="export: a JSON-lines file, e.g. private/feedback.jsonl; - for stdout"
    )
    fb.set_defaults(run=cmd_feedback)
    ds = sub.add_parser(
        "describe", help="phase 5 experiment: plain-language descriptions of law sections"
    )
    ds.add_argument("--limit", type=int, help="describe at most this many wordings")
    ds.add_argument("--workers", type=int, default=8, help="model calls in parallel")
    ds.set_defaults(run=cmd_describe)
    index = sub.add_parser("index", help="build chunks and embeddings for stored versions")
    index.add_argument("--strategy", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    index.add_argument("--no-embed", action="store_true", help="only build chunks")
    index.set_defaults(run=cmd_index)
    ask = sub.add_parser("ask", help="answer a question from the stored sources")
    ask.add_argument("question")
    _add_retrieval_options(ask)
    ask.add_argument("--k", type=int, default=AnswerConfig.k, help="sources given to the model")
    ask.add_argument(
        "--earlier",
        action="append",
        help="an earlier question in the conversation (repeat, oldest first)",
    )
    ask.add_argument(
        "--check-model",
        help="model for the second search's evidence check (default: the answer model)",
    )
    ask.add_argument(
        "--second-search",
        action="store_true",
        help="phase 5: check the evidence and search again (at most 2 steps) when it is not enough",
    )
    ask.add_argument("--as-of", help="date the answer should apply on (YYYY-MM-DD); default today")
    ask.add_argument(
        "--show-sources", action="store_true", help="list everything shown to the model"
    )
    ask.set_defaults(run=cmd_ask)
    ev = sub.add_parser("eval", help="run the retrieval eval on a golden-set file")
    ev.add_argument("--file", default="evals/golden.dev.jsonl")
    ev.add_argument("--strategy", default="structure", choices=list(STRATEGIES))
    _add_retrieval_options(ev)
    ev.add_argument("--include-drafts", action="store_true", help="development run; not reportable")
    ev.add_argument("--verbose", "-v", action="store_true", help="show each item's ranks")
    ev.add_argument("--out", help="write per-item results as JSON")
    ev.add_argument(
        "--queries", help="replay recorded question vectors and translations (no model; CI)"
    )
    ev.add_argument("--record-queries", help="record question vectors and translations here")
    ev.add_argument("--min-r5", type=float, help="fail (exit 3) if overall R@5 is below this")
    ev.add_argument("--min-mrr", type=float, help="fail (exit 3) if overall MRR is below this")
    ev.add_argument(
        "--must-find",
        nargs="+",
        metavar="ID",
        help="fail (exit 3) unless each of these items has an expected source of every group"
        f" among the {AnswerConfig.k} sources an answer is given; exit 2 if one is not scored",
    )
    ev.set_defaults(run=cmd_eval)
    rf = sub.add_parser(
        "refresh", help="fetch, ingest and index everything; summarise what changed (daily)"
    )
    rf.set_defaults(run=cmd_refresh)
    sm = sub.add_parser("smoke", help="the LLM smoke slice (evals/smoke.json); calls the model")
    sm.add_argument("--file", default="evals/golden.dev.jsonl")
    sm.add_argument("--smoke", default="evals/smoke.json")
    sm.add_argument("--queries", help="replay recorded question vectors and translations (CI)")
    _add_retrieval_options(sm)
    sm.set_defaults(run=cmd_smoke)
    ex = sub.add_parser(
        "export-eval-corpus", help="write the eval corpus snapshot (public text; for CI)"
    )
    ex.add_argument("--out", required=True, help="e.g. eval-corpus.txt.gz")
    ex.set_defaults(run=cmd_export_eval_corpus)
    im = sub.add_parser("import-eval-corpus", help="load an eval corpus snapshot (empty database)")
    im.add_argument("file")
    im.set_defaults(run=cmd_import_eval_corpus)
    ea = sub.add_parser(
        "eval-answers", help="run the behaviour eval (answer kinds); calls the model"
    )
    ea.add_argument("--file", default="evals/golden.dev.jsonl")
    _add_retrieval_options(ea)
    ea.add_argument("--k", type=int, default=AnswerConfig.k, help="sources given to the model")
    ea.add_argument(
        "--no-memory",
        action="store_true",
        help="ask follow-on questions alone, as plain RAG did (the baseline); by default the "
        "item's earlier turns are rewritten in, as production does",
    )
    ea.add_argument(
        "--check-model",
        help="model for the second search's evidence check (default: the answer model)",
    )
    ea.add_argument(
        "--second-search",
        action="store_true",
        help="phase 5: check the evidence and search again (at most 2 steps) when it is not enough",
    )
    ea.add_argument("--include-drafts", action="store_true", help="development run; not reportable")
    ea.add_argument(
        "--kind", nargs="+", choices=["answered", "follow_up", "not_settled", "out_of_scope"]
    )
    ea.add_argument("--limit", type=int, help="only the first N items")
    ea.add_argument("--floor", type=float, help="relevance floor (default: calibrated for bge-m3)")
    ea.add_argument(
        "--judge", action="store_true", help="also judge correctness and faithfulness (JUDGE_MODEL)"
    )
    ea.add_argument("--verbose", "-v", action="store_true", help="show the wrong answers")
    ea.add_argument("--out", help="write per-item answers as JSON")
    ea.set_defaults(run=cmd_eval_answers)
    js = sub.add_parser("judge-sample", help="generate the judge's 30 calibration answers")
    js.add_argument("--file", default="evals/golden.dev.jsonl")
    js.add_argument("--out", default=str(CALIBRATION_ANSWERS))
    js.add_argument("--force", action="store_true", help="replace an existing sample")
    _add_retrieval_options(js)
    js.set_defaults(run=cmd_judge_sample)
    jc = sub.add_parser("judge-calibrate", help="compare the judge with the hand labels (D19)")
    jc.add_argument("--answers", default=str(CALIBRATION_ANSWERS))
    jc.add_argument("--labels", default=str(CALIBRATION_LABELS))
    jc.add_argument(
        "--seeded",
        default="evals/judge/seeded.jsonl",
        help="seeded errors to include ('' for none)",
    )
    jc.add_argument(
        "--known-cases",
        default="evals/judge/known_cases.jsonl",
        help="whole answers the judge once got wrong, and their counterparts; reported apart",
    )
    jc.add_argument("--model", help="judge model (default: JUDGE_MODEL)")
    jc.add_argument(
        "--reasoning-effort", choices=["low", "medium", "high"], help="for reasoning models"
    )
    jc.add_argument("--verbose", "-v", action="store_true", help="show each disagreement")
    jc.add_argument("--out", help="write the judge's verdicts as JSON lines")
    jc.set_defaults(run=cmd_judge_calibrate)
    jf = sub.add_parser(
        "judge-faithfulness",
        help="the judge's faithfulness benchmark: stored runs against the labels (no model call)",
    )
    jf.add_argument("--directory", default="evals/judge/faithfulness")
    jf.add_argument(
        "--run",
        dest="run_as",
        metavar="NAME",
        help="ask a judge about the 40 answers (paid) and store its verdicts under this name",
    )
    jf.add_argument("--model", help="with --run: judge model (default: JUDGE_MODEL)")
    jf.add_argument("--reasoning-effort", choices=["low", "medium", "high"], help="with --run")
    jf.add_argument("--runs", type=_positive_int, default=3, help="with --run")
    jf.add_argument("--replace", action="store_true", help="with --run: overwrite a stored name")
    jf.set_defaults(run=cmd_judge_faithfulness)
    for eval_parser in (ev, sm, ea, js, jc):
        eval_parser.add_argument(
            "--trace",
            action="store_true",
            help="send this run's traces to the configured backend (off by default: each "
            "judged question is ~10 observations; the per-item --out file has the details)",
        )
    args = parser.parse_args(argv)
    from permitnav import tracing

    if traces(args):
        # Eval runs ask only golden-set questions, so their traces may hold the full text;
        # anything else (e.g. `ask`) records metadata only. No exporter configured: no tracing.
        full = args.run in (cmd_eval_answers, cmd_judge_sample, cmd_judge_calibrate)
        tracing.configure("permitnav-cli", content="full" if full else "metadata")
    try:
        return args.run(args)
    finally:
        tracing.flush()  # before the process exits
        tracing.set_content_mode("metadata")  # full text only for the eval command itself


if __name__ == "__main__":
    sys.exit(main())
