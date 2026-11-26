# Golden set

The questions PermitNav is measured against. The design behind the format is in [`docs/DESIGN.md`](../docs/DESIGN.md) D16–D20.

| File | What | Public? |
|---|---|---|
| `evals/golden.dev.jsonl` | Dev set, 60–80 items. Used for tuning. | Yes |
| `evals/golden.interpretation.jsonl` | Agency-interpretation slice (phase 4): 4 scenarios × EN/SV whose answer is Migrationsverket's legal position (permanent residence: which incomes count, conduct; a late extension application; the employer in family maintenance), each expecting the paper section and the law it interprets. Needs the position papers, which the CI corpus snapshot does not have yet. | Yes |
| `evals/golden.versions.jsonl` | Version-dependent slice (phase 4): 6 scenarios × EN/SV where the answer depends on which wording applies: transitional provisions (granted/registered dates) and historical `as_of` dates (2024). Needs the rebuilt history; in CI since `eval-corpus-2026-11-23`. | Yes |
| `evals/golden.multiturn.jsonl` | Multi-turn slice (phase 5): 6 scenarios × EN/SV whose scored question only makes sense with an earlier turn ("and can my spouse work too?"). Plain RAG has no memory and asks the last question alone: the baseline for phase 5's memory. Checked 2026-11-24. | Yes |
| `evals/golden.situational.jsonl` | Situational slice (phase 5): 4 scenarios × EN/SV that describe a situation without naming the rule, aimed at law sections no retrieved source cited in the failure analysis (ordinance rules for a worker's family, 5 kap. 7 §, 7 kap. 7 e §). Checked 2026-11-24. | Yes |
| `private/golden.holdout.jsonl` | Hold-out, 30–40 items. Run once per milestone, never tuned on. | **No**, never committed |
| `private/questions-inbox.md` | Raw real questions, before sanitising. | **No**, never committed |

Check a file with:

```
python3 scripts/validate_golden.py evals/golden.dev.jsonl
```

## Workflow

1. **Collect.** Write real questions in `private/questions-inbox.md`, as they come, with whatever personal detail you need to remember the case.
2. **Sanitise.** Rewrite each one as a generic case: no real dates, employers, nationalities, names, case numbers or family details. Keep the legal shape of the question, drop everything that identifies a person.
3. **Find the sources.** Look up the citable units that answer it (see "Unit IDs" below), and write a short reference answer from the source text, not from memory.
4. **Translate.** Add the other language as a separate item with `translation_of` pointing at the original (D20). Check the LLM's translation by hand.
5. **Assign.** Put the item in the dev set or the hold-out. Keep the mix of types similar in both.
6. **Validate**, then mark `"review": "checked"` once you have read the cited sections yourself.

Items marked `"review": "draft"` were drafted from the statute text but not yet checked by a person. Draft items are excluded from reported results.

## Item format

One JSON object per line.

```json
{
  "id": "work-salary-001-en",
  "question": "What salary do I need to get a Swedish work permit?",
  "original_language": "en",
  "translation_of": null,
  "type": "time-dependent",
  "as_of": "2026-11-20",
  "expected": [["sfs:2005:716/6kap/2§"]],
  "reference_answer": "…",
  "expects_follow_up": false,
  "expects_not_settled": false,
  "expects_out_of_scope": false,
  "authority": "law",
  "review": "draft",
  "notes": "…"
}
```

| Field | Meaning |
|---|---|
| `id` | Unique, stable, readable. Convention: `<topic>-<slug>-<nnn>-<lang>`. |
| `question` | The question as a user would ask it. |
| `original_language` | `en` or `sv`: the language of *this* item's question. |
| `translation_of` | `null` for an original; otherwise the `id` of the item it was translated from (D20). |
| `type` | One of the types below. |
| `as_of` | The date the answer is true for (D17). From 2023-01-01 (D10) to 2 years ahead (D9). |
| `expected` | Required groups of alternative units (D16). Each inner list is one group; a group is found if any of its units is retrieved. Empty only for out-of-scope items. |
| `reference_answer` | A short correct answer, written from the source text. Used by the judge, not shown to the model. |
| `expects_follow_up` | The right first response is a question back (D14). |
| `expects_not_settled` | The sources don't settle the question asked: they are silent, they give a general rule that does not decide the situation, or the person asks how their own case will be decided. A question whether something *can* happen ("can my permit be revoked?") is settled when the law says it can, even though each case is assessed individually: that item expects an answer (reviewed 2026-11-26: 954, 204). |
| `expects_out_of_scope` | Outside the service: asylum, protection or deportation, or any unrelated topic (driving licences, taxes, housing); the assistant must decline and point to Migrationsverket or the authority responsible (D18, prompt rule 7). At most one `expects_*` flag is true. |
| `authority` | Optional. Which kind of source answers it (D22): `law` (a legal requirement), `agency` (Migrationsverket practice the law doesn't set) or `both`. Results are reported per kind. |
| `turns` | Optional. Earlier user questions in the same conversation, oldest first; `question` is the turn that is scored. Without memory (plain RAG) only `question` is asked; the judge always sees the earlier turns. Checked for personal data like the question. Results get a `multi-turn` row. |
| `review` | `draft` or `checked`. |
| `notes` | Optional. Why the item exists, what makes it hard, what the source says. What people were told by Migrationsverket's phone service or emails goes here, never into `expected` (D23). |

### Types

| Type | Tests |
|---|---|
| `lookup` | A direct question with one clear answer. |
| `identifier` | A question naming a section, e.g. "6 kap. 2 a §". Tests keyword search. |
| `multi-source` | The answer needs two or more units. |
| `time-dependent` | The answer is true on `as_of` and may change later. |
| `version-dependent` | The answer depends on which version of a rule applies (transitional provisions). Needs the history rebuilt in phase 4; add these in phase 4. |
| `needs-follow-up` | The assistant must ask about the user's situation first. |
| `not-settled` | The sources don't decide the question asked (see `expects_not_settled`). |
| `out-of-scope` | Asylum, protection, deportation. |

Target for the dev set: at least 6 items of each type except `version-dependent`, plus 6–8 out-of-scope items, with every original in both languages. At least a third of the in-scope items should be `agency` or `both` (D22).

## Unit IDs

The citable units from D2. The validator checks these patterns.

| Source | Pattern | Example |
|---|---|---|
| Statute section with chapters | `sfs:<year>:<number>/<chapter>kap/<section>§` | `sfs:2005:716/5akap/1§` |
| Statute section without chapters | `sfs:<year>:<number>/<section>§` | `sfs:2001:82/11§` |
| Transitional provision | `sfs:<year>:<number>/övergång/<point>` | `sfs:2026:295/övergång/1` |
| Position paper section | `rs:<RS number>/<numbered section>` | `rs:RS/084/2021/3.1.1` |
| MIG ruling | `mig:<year>:<number>` | `mig:2023:1` |
| Migrationsverket page section | `web:<sv\|en>/<path>#<anchor>` | `web:sv/arbeta-i-sverige#krav` |

Chapter and section letters are written without spaces: "5 a kap. 3 b §" is `5akap/3b§`.

**Placeholder for pages not indexed yet:** `web:tbd/<slug>`, e.g. `web:tbd/work-permit-insurances`, marks a Migrationsverket page that answers the item but has no unit ID yet. Replace it with the real `web:` ID once the page is ingested (phase 1). Only `draft` items may contain placeholders.

## Drift: when an item's sources change (D17)

Items keep their `as_of`, so they never turn wrong by themselves; they can stop describing
today's rules. `uv run permitnav golden-drift` lists items whose expected sources changed since
their date (a newer wording in force, a changed Migrationsverket page, a corrected reading, a
source gone; dated upcoming changes are listed for information). The nightly refresh runs it
and alerts once per newly drifted item. To review:

```
uv run permitnav golden-drift --draft drafts.jsonl   # fresh copies dated today, review: draft
```

Check each copy's expected units and reference answer against today's sources, set
`review: checked`, add it to the golden set (and re-record the queries file). Keep the old item:
it tests its own date, and with its copy (`<id>-YYYYMMDD`) beside it, it is no longer followed
for drift. `--today YYYY-MM-DD` shows what would drift on a later date.

## The retrieval eval in CI

Every push and pull request runs the retrieval eval on the dev set **without any model or
API key** (`.github/workflows/ci.yml`, job `retrieval-eval`), so it also works for pull
requests from forks:

- **The corpus** comes from a public GitHub release asset, `eval-corpus.txt.gz` (tag
  `eval-corpus-YYYY-MM-DD`): the wordings the sources show now, their links, page
  translations and `structure` chunks with bge-m3 embeddings. Public source text only:
  statutes (Källa: Sveriges riksdag), Migrationsverket's text (Källa: Migrationsverket) and
  MIG rulings (Sveriges Domstolar, CC0), and since `eval-corpus-2026-11-23` the statute history
  rebuilt from the official SFS acts (phase 4; also no copyright). Never the change log. CI
  checks its sha256.
- **The questions** are replayed from `queries.dev.json`: each dev question's bge-m3 vector,
  its Swedish translation and the translation's vector, recorded from a local run. The
  version-dependent slice (`golden.versions.jsonl`) has its own, `queries.versions.json`, and
  its own minimums.
- **The gate**: the job fails if overall R@5 or MRR fall below the minimums in the workflow.
  With the corpus and the queries fixed, the eval is deterministic: a change in its numbers
  comes from the code under review.

When the golden set, the corpus or the retrieval code changes on purpose:

```
uv run permitnav eval --record-queries evals/queries.dev.json   # after golden-set changes
uv run permitnav eval --file evals/golden.versions.jsonl --record-queries evals/queries.versions.json
uv run permitnav export-eval-corpus --out eval-corpus.txt.gz    # after re-indexing
gh release create eval-corpus-YYYY-MM-DD eval-corpus.txt.gz --title "Eval corpus YYYY-MM-DD" \
  --notes "Public source text for the CI retrieval eval; see evals/README.md."
shasum -a 256 eval-corpus.txt.gz                                # into the workflow
```

then update `EVAL_CORPUS_TAG`, `EVAL_CORPUS_SHA256` and, if the numbers moved for a good
reason, `--min-r5` / `--min-mrr`. Check locally first, in an empty database:
`permitnav migrate && permitnav import-eval-corpus eval-corpus.txt.gz && permitnav eval
--queries evals/queries.dev.json`.

## The LLM smoke slice

On every merge to `main`, nightly and on demand, CI asks the 12 questions in `smoke.json`
with the real answer model (job `smoke`; not on pull requests, which never get the
OpenRouter secret). The questions are replayed from `queries.dev.json` and searched in the
eval corpus, so the answer model is the only model called: about $0.02 a run, through a
CI-only OpenRouter key with a $5 monthly credit limit.

Each question must get its expected answer kind, an answer must cite a source, and a reply
must be in the question's language. The questions were chosen as ones the current
configuration got right in all five dev runs. A failing question is asked once more and fails
only if it fails again; one failing question is allowed. Run it locally with
`permitnav smoke` (bge-m3 and translation live) or `permitnav smoke --queries
evals/queries.dev.json`.

When the golden set changes, re-record the queries (`permitnav eval --record-queries
evals/queries.dev.json` records every checked dev question, including those the retrieval
eval skips). When the answer prompt or model changes on purpose, check the slice still
passes locally before merging; replace questions only with ones that are stable over
several runs, never to make a failing change pass.

## Checks for named items and known cases

Averages hide single questions, so two things are checked by name.

- **`permitnav eval --must-find ID …`** (CI runs it for `work-switch-in-sweden-104`): every
  expected group of the named items must have a member among the eight sources an answer is
  given, or the eval fails like a gate (exit 3; exit 2 when a named item is not scored). An expected group lists every source that carries
  the rule, statute sections as well as pages, so the check does not depend on one page or
  one chunk staying as it is.
- **`evals/judge/known_cases.jsonl`**: whole answers kept because the judge once got them
  wrong, each with its counterpart the judge must keep getting right, their labels and a note
  saying what happened. `judge-calibrate` reports them one by one ("known case ok / MISS",
  with the judge's reason for a miss), keeps them out of the agreement bar, since they are
  picked, not sampled, and fails when one is missed. The first
  pair is item 104: an answer that applies the 30-credit rule for research permits to a work
  permit (incorrect; the judge passed such answers on 25 and 26 November 2026 against the
  earlier reference) and one that mentions the rule without applying it (correct). A check on
  the words alone would fail both or pass both; the difference is in the reference answer.
