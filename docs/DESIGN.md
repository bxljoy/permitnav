# PermitNav design

The design decisions behind [`ROADMAP.md`](../ROADMAP.md). The roadmap says what gets built and in what order; this file says how the key parts work and why. Facts about the sources (formats, APIs, copyright, crawling rules, the AI Act) are in [`research/source-facts.md`](research/source-facts.md).

Decided 2026-11-19. Each decision has an ID (D1–D23) so code, issues and commits can refer to it. To change a decision, edit it here and add the date and the reason.

---

## Scope

### D1. What the first version covers

- **In:** work permits (including the EU Blue Card), family members of work-permit holders, permanent residence (PUT) and EU long-term resident status.
- **Later:** study permits and citizenship.
- **Out entirely:** asylum, protection status and deportation. The assistant says it doesn't cover them and points to Migrationsverket.

Why: every topic in scope needs golden questions, sources and time handling. Asylum is the most sensitive area and the one most driven by discretion, which is where the assistant would help least and could harm most.

---

## Data model

### D2. A "source" is a citable unit

The unit of retrieval, citation, verification and evaluation is the **citable unit**, independent of chunking:

| Source type | Citable unit | Example ID |
|---|---|---|
| Statute or ordinance | One section (§) | `sfs:2005:716/6kap/2§` |
| Transitional provision | One numbered point in an amending act | `sfs:2026:385/övergång/1` |
| Position paper | One heading section | `rs:RS/005/2022/3` |
| MIG ruling | The whole ruling | `mig:2023:1` |
| Migrationsverket web page | One heading section | `web:sv/arbeta-i-sverige/…#krav` |

A web section's anchor is the site's own heading anchor (`h-Krav…`, made from the heading text). Ids that page components generate (`card-header-4Y2vfY9CK1nvwnbX`) can change on any publish, so for those, and for headings with no id, the anchor is made from the heading text instead. The link still uses the element id the page has today.

Chunks record which unit they belong to. Metrics, citations and the golden set use unit IDs, never chunk IDs, so the chunking strategy can change without invalidating the evals.

### D3. Three layers: unit, version, chunk

```
unit            stable identity: id, source_type, authority_level, law/chapter/section, language
 └─ unit_version   one row per wording: text, valid_from, valid_to, amended_by, evidence
     └─ chunk         built from a unit_version: text, embedding, tsvector
```

- **`unit`:** the stable ID from D2, plus fields that never change: source type, authority level (law / ordinance / position paper / ruling / web page) and language.
- **`unit_version`:** the text as it reads in one period.
  - `valid_from` and `valid_to` (empty while current).
  - No stored status: whether a wording is upcoming, applies or no longer applies depends on the date asked about, so it is computed from `valid_from` / `valid_to` (changed 2026-11-20 after the time-model prototype; see Phase 1 internals).
  - `amended_by`: the SFS number of the act that introduced this wording.
  - `evidence`: a reference to the raw fetch it came from.
- **`chunk`:** always built from a `unit_version`, never from a unit directly. When a unit gets a new version, its new chunks are built and the old ones remain attached to the old version.
- **Raw fetches** are kept separately in object storage, one per fetch with a content hash, as the evidence each version came from.

This keeps two kinds of time apart:
- **Valid time:** when a wording applied.
- **Fetch time:** when we saw it.

Retrieval filters on valid time, using the conversation's `as_of` (D8).

### D4. Transitional provisions link automatically, both ways

A transitional provision is its own unit (D2), owned by its amending act. It links to:

1. **Every version the act introduced:** versions whose `amended_by` is the act, cross-checked against the amendment register's (SFSR) list of sections changed.
2. **Every version the act ended:** versions of the same sections whose `valid_to` equals the act's in-force date.

The second link answers "I applied before the change". When either version is retrieved, its linked transitional provisions are always added to the context (D11).

A small manual override list covers provisions that refer to sections outside their own act.

### D5. Dates for sources without legal in-force dates

- **Position papers:** `valid_from` is the decision date. `valid_to` is the decision date of the next version (they are versioned, e.g. RS/005/2022 version 10.0). Key on RS number plus version.
- **Web pages:** only the dates we saw them are known, recorded as `first_seen` and `last_seen`.
  - Answers say "as shown on migrationsverket.se on [date]".
  - A page version is excluded when `as_of` is before its `first_seen`: it may have said something else then, and we can't know what.

### D6. English and Swedish pages are linked translations

- English and Swedish Migrationsverket pages are separate units, linked as translations where the site pairs them.
- Retrieval may return either language.
- For citations, the Swedish page wins on a disagreement, and the verifier flags any disagreement it sees (D13).
- In the golden set, either language counts as a hit (D16).

### D7. Rulings record what they interpreted

- Each MIG ruling stores the units it cites (from the Sök rättspraxis API) and its decision date.
- The verifier compares each cited section's version on the ruling date with its version on the conversation's `as_of`.
- If they differ, the answer says "this ruling interpreted an earlier wording of the section" instead of citing it as current law.

---

## Time

### D8. One `as_of` date per conversation

- Every conversation has one `as_of` date. It defaults to today.
- It changes only when the user sets it, or when the assistant asks for it because a retrieved transitional provision depends on a date (e.g. the application date).
- The transitional provisions decide which version applies. The system never invents its own rule for which date matters.
- Retrieval returns only unit versions that apply on `as_of`, plus `upcoming` versions as "coming change" notes when `as_of` is today.

### D9. Future dates are allowed, up to 2 years ahead

- When `as_of` is in the future, retrieval uses `upcoming` versions that will be in force on that date.
- Every such answer adds: "based on changes known on [today]; more changes may still be decided".

### D10. History is rebuilt back to 1 January 2023, for in-scope sections only

No official source publishes past consolidated wordings (see `research/source-facts.md`). History is rebuilt from the amending acts:

1. Start from today's consolidated text (riksdagen API).
2. Walk the SFSR amendment register newest-first, using each act's in-force date as `valid_from`.
3. For each changed section, take the previous wording from the act named in the footnote "Senaste lydelse …", or from the original act if there is no footnote.
4. Undo renumberings, and take split or later in-force dates from the separate in-force ordinances.

Limits:
- **Only the sections in scope (D1):** the work, family, PUT and long-term resident chapters of utlänningslagen and utlänningsförordningen.
- **Only back to 1 January 2023.** For an `as_of` before that, the assistant says the period isn't covered.
- **Tested, not trusted:** rebuilt versions are checked against lagen.nu in tests. lagen.nu is unofficial and never cited.

This is a phase 4 task.

---

## Answering

### D11. What text goes to the model

- **Short unit versions** (under about 800 tokens; most statute sections and web page headings) are sent whole.
- **Longer ones** (rulings, long position paper sections) are sent as their matched chunks plus a header: title, unit ID, `valid_from` / `valid_to` or `first_seen`, and authority level.
- **Linked transitional provisions** are always added (D4).
- Context is ordered by relevance after reranking, with each unit's ID visible so citations can refer to it.

### D12. Quotes show the Swedish original plus an unofficial translation

- Answers are written in the user's language.
- A cited quote shows the Swedish original and, for non-Swedish users, a translation clearly labelled "unofficial translation".
- The verifier checks claims against the Swedish original only, never against a translation.

### D13. The verifier: one retry, then remove

The verifier checks every claim against the Swedish text it cites, at the version that applies on `as_of`. It also flags web-page/law disagreements (D6) and rulings on older wordings (D7).

1. A claim that fails is sent back to the generator once, with the reason.
2. If it still fails, it is removed from the answer.
3. If removing failed claims leaves no answer, the assistant says the sources don't settle the question.

A claim that hasn't passed the verifier is never shown.

### D14. Session memory is a fixed "your situation" structure

| Field | Example |
|---|---|
| `permit_type` | work permit |
| `permit_start_date` | 2024-03-01 |
| `application_date` | 2026-02-10 |
| `employer_changes` | 1, in 2025 |
| `family_members` | spouse, 1 child |
| `as_of` | 2026-11-19 (D8) |

- Intake fills it from what the user said. A missing field the rules depend on becomes a stated branch in the answer; the assistant asks at most one question, and only when the answer splits into more than two or three branches (revised 2026-11-23 from "at most 3 questions": see "Phase 5 plan").
- The user sees it as a small editable panel.
- It lives on the client and is sent with each request, together with the last several turns of the conversation: never stored on the server. The front end keeps the conversation in the browser's local storage (phase 6), with a delete button, so it survives closing the tab on that device. Because the fields are fixed, the privacy page can say exactly what is held.

### D15. Model choice

Updated 2026-11-20 from [`research/providers.md`](research/providers.md).

**Criteria for any provider that receives other people's questions** (from the public beta, phase 7, onwards; until then the only questions are the author's own and the golden set):
- **Must:** a data processing agreement, no training on API data, retention of 30 days or less, and a valid transfer basis (processing in the EU, or a provider certified under the EU–US Data Privacy Framework). No provider that processes or stores data in China.
- **Preferred now, required before a paid launch:** processing in the EU, and zero retention.

**Answer model, during development (phases 1–6):**
- **Through OpenRouter,** one account for every candidate model. Only the author's questions and the golden set are sent, so the criteria above don't apply yet.
- **Claude Haiku 5.5 is the starting answer model.** It is the cheapest candidate ($0.10 / $0.50 per million tokens, slightly below Mistral Small 4) and good enough to build the pipeline with. Its likely weak spots are multi-source reasoning, transitional provisions, recognising "not settled" questions, and verification.
- **Phase 2 compares Haiku 5.5, Sonnet 5.5 and one Mistral model** on the golden set, reported per question type, not just overall. A likely result is a mix: Haiku for cheap steps (query translation, intake, simple answers) and Sonnet only where Haiku measurably fails, such as the verifier (D13) or multi-source questions.
- Switching models is a configuration change, because the LLM client sits behind an interface.

**Answer model, from the public beta (phase 7):** the chosen models move to a path that meets the criteria above:
- **Claude through Amazon Bedrock's EU cross-region profile,** with model-invocation logging and retention turned off in the account. Anthropic's own API has no EU-only option. Google Cloud's `eu` region is the alternative (its abuse-logging terms for Claude are unverified).
- **Mistral's own API** (EU-hosted by default) if a Mistral model wins.
- **OpenRouter stays for development and evals only.**

**Never DeepSeek** for anything that touches user data: it processes and stores data in China and offers no data processing agreement.

**Judge:** a different model family from the answer model (D19). While Claude answers, a Mistral model judges, through OpenRouter; if Mistral answers, Claude judges.

**Embeddings:**
- **`bge-m3`, self-hosted with a Python library** (`sentence-transformers` or `FlagEmbedding`) behind the embedding interface. It runs on the developer's machine and on the phase 3 VM, so user questions never leave the server. On public Swedish benchmarks it matches `text-embedding-3-large`; `text-embedding-3-small` is the weakest model checked and is dropped.
- **Phase 2 compares it with one stronger model** on the golden set, e.g. `Qwen3-Embedding-8B` through OpenRouter or `text-embedding-3-large`, and keeps `bge-m3` unless the gain is clear.
- **OpenRouter only for offline experiments** on the corpus, never in the path of user questions once the beta starts.
- Embedding cost is negligible for any option (under $6 a month at 100k questions); the decision rests on quality and privacy.

---

## Evaluation

### D16. Golden items: required groups of alternative sources

Each golden item lists one or more **required groups**. Each group is a set of acceptable alternative units.

- A group counts as found if **any** of its units is in the top k.
- **recall@k** is the share of groups found. MRR and nDCG use the rank of each group's first hit.
- Linked translations (D6) are listed as alternatives in the same group.

Example item (`evals/golden.dev.jsonl`, one line):

```json
{
  "id": "work-salary-001",
  "question": "What is the minimum salary for a work permit?",
  "original_language": "en",
  "translation_of": null,
  "type": "time-dependent",
  "as_of": "2026-11-19",
  "expected": [
    ["sfs:2006:97/5kap/X§"],
    ["web:sv/arbeta-i-sverige/…#lon", "web:en/working-in-sweden/…#salary"]
  ],
  "reference_answer": "…",
  "expects_follow_up": false,
  "expects_not_settled": false,
  "expects_out_of_scope": false
}
```

(The section number and URLs are placeholders.)

### D17. Every golden item has its own `as_of`

- Items written as "today" get the date they were written.
- Version-dependent items get the past or future date they test. Past dates must be on or after 1 January 2023 (D10).
- Evals always run each item with its own `as_of`, so items don't go wrong when the rules change.
- **Drift check:** a separate job re-asks every item as of today and lists those whose expected units have a newer version since the item's `as_of`. Those items get a fresh copy dated today, after review. The old copy stays as a historical test.

### D18. Out-of-scope items

- 6–8 items in both languages ask about asylum, protection and deportation. They are scored on whether the assistant declines and points to Migrationsverket.
- They include near-misses about switching from an asylum track to a work permit (spårbyte). That rule used to exist, which makes these the hardest boundary.
- A few items ask about topics unrelated to immigration (driving licences, taxes, housing). Nothing in the corpus answers them, which tests the relevance floor and that the assistant labels them out of scope, not "not settled".

### D19. Trusting the LLM judge

- The judge is from a different model family than the answer model (D15).
- It is calibrated on about 30 hand-labelled answers. It must agree with the labels on at least 85% before its scores go in the results table.
- It is re-calibrated whenever the judge model or the judge prompt changes.

### D20. Questions in two languages

- Each question is written in whichever language comes naturally, translated by an LLM, and the translation is checked by hand.
- Each item records `original_language` and `translation_of`.
- Results are reported separately for original and translated questions, because translated questions tend to use the source's own wording, which makes them easier.

### D21. Plain RAG gets the same prompt as the agent

The phase 2 plain-RAG baseline is given the same instructions as the phase 5 agent:
- it may ask one follow-up question
- it may say "the sources don't settle this"
- it may decline out-of-scope questions

The phase 5 comparison then measures only what the agent adds: the loop, the tools, session memory and the verifier.

---

## Sources and authority

Added 2026-11-20, after working through the first real case for the golden set: most of a first work-permit application is governed by Migrationsverket's own requirements, not by the law.

### D22. Legal requirements and agency practice are different kinds of claims

Every claim in an answer is one of two kinds:

| Kind | Examples | Authority | On a disagreement |
|---|---|---|---|
| **Legal requirement** | the salary requirement, 4 of the last 7 years for PUT, applying before entry | Law, then ordinance, then MIGRFS, then rulings and position papers | The law wins; the verifier flags the web page (D13) |
| **Agency practice** | the application steps, advertising the job, the union statement, the list of required insurances, what happens when the union doesn't answer, processing times | Migrationsverket's published guidance (web pages, position papers) | The more specific and more recent Migrationsverket source wins; the answer says the guidance has changed if it can see that |

Consequences:

- **Answers say which kind a claim is.** Agency practice is stated as "Migrationsverket requires…", not as law. When the law is silent, the answer says so rather than inventing a legal basis.
- **Authority-aware ranking prefers the law only when the law and a web page cover the same claim.** For practice questions the web page is the primary source, not a fallback.
- **Migrationsverket's site is indexed broadly for the in-scope topics** from phase 1: the pages for applicants *and* for employers, FAQs and processing times, in both English and Swedish. English-speaking users read the English pages, so they matter even where the Swedish page wins on a disagreement (D6).
- **The golden set covers both kinds.** Items record which kind of source answers them (`authority`: `law`, `agency` or `both`; see `evals/README.md`), and results are reported per kind.

### D23. What people learn from Migrationsverket's phone service and emails

Answers from Migrationsverket's phone service, emails and individual case officers are real but unpublished, so a user can't check them.

- **They are used only to write test items:** in golden-set `notes` and reference answers, as "what actually happens". They are never retrieved, quoted or shown as a source.
- **Revisit after the beta.** A "reported practice" source (written by the project, labelled unofficial, dated, lowest authority) may be worth adding once several independent reports agree on a claim. Until then, a wrong or outdated note shown to users is exactly the harm the verifier exists to prevent.

---

### D24. English-primary; answers in the question's language

PermitNav's primary users are people who move to Sweden for work: applicants for a first permit, people changing employer or extending, and those heading for permanent residence. Many of them do not read Swedish well, and English is the language they most often share. Swedish speakers (employers, HR staff, long-term residents) are served too, and Swedish is the language of every source.

- **Priorities and evaluation are English-first.** When a change is judged, the English slice is the headline number (D20 already reports both languages); a change that helps Swedish but hurts English needs a strong reason. Cross-language work (an English question, Swedish law) gets priority because it is the hardest path and the main one.
- **Answers are in the language of the question,** not English by default: Swedish questions get Swedish answers, English ones English. When the language is unclear (very short, mixed, another language), no language is imposed and the model follows the question (forcing English on unclear questions answered German and Arabic in English).
- **English is the default around the answer:** the interface, PermitNav's fixed texts, and the unofficial translation of Swedish quotes for every non-Swedish question (D12), which for English users is part of the product, not a courtesy.
- **Swedish stays first-class in the pipeline:** the sources are Swedish, the Swedish page wins on a disagreement (D6), and answers quote the Swedish text.
- **V1 supports English and Swedish.** Other languages may work (the translator turns any language into Swedish for keyword search, and the model answers in the question's language) but nothing measures them; which ones to add is decided from real usage after V1.
- **The golden set keeps its English/Swedish pairs,** which keep the comparison fair; new items can lean on questions written in English.

## Phase 1 internals

Added 2026-11-20. How the concepts above become code for phase 1 (baseline RAG), designed so that phases 2–5 add behaviour behind the same interfaces instead of changing them.

### Modules and seams

Each module hides a lot behind a small interface. A **seam** (a swappable dependency with more than one implementation) exists only where something really varies; everything else is a plain module.

| Module | Interface (what callers use) | What it hides | Depends on | Seam? |
|---|---|---|---|---|
| `sources` | `fetch(ref) -> RawFetch` | Per-source discovery and URLs, robots rules, rate limit (1 req/s), user agent, caching, content hash | HTTP | **Yes: `Http`.** Live HTTP, and a fixture adapter that serves saved files, so tests never touch the network. |
| `parsing` | `parse(raw) -> list[ParsedUnit]` | Statute parser (chapters, sections, "Lag (…)" markers, duplicated sections with "/Träder i kraft/" and "/Upphör att gälla/", transitional provisions); web page parser (split by heading, strip menus) | Nothing (pure) | No. Tested directly on real files. |
| `corpus` | `ingest(raw, units) -> IngestReport`, `rebuild_chunks(strategy)` | Versioning per unit (D3): compare text hashes, open new versions, close old ones; transitional links (D4); chunking; embedding new chunks | Postgres, `Embedder` | Postgres: no (tested against a real Postgres in Docker). |
| `embedding` | `embed_documents(texts)`, `embed_query(text)`, `model_id`, `dims` | Model loading, device (Mac GPU or CPU), batching, input length cap, fp16 | `bge-m3` weights | **Yes: `Embedder`.** `bge-m3` (local), a deterministic fake for tests; phase 2 adds a second real model for comparison. |
| `retrieval` | `search(query, as_of, k, filters) -> list[Hit]` | Query embedding, `as_of` filtering (D8, D5), vector search; from phase 2 also keyword search, fusion (RRF), reranking and authority-aware ranking (D22) | Postgres, `Embedder` | No. One backend; phase 2's variants are a config setting (`mode`), not separate implementations. |
| `llm` | `complete(request) -> Completion` | Provider HTTP, retries, token and cost accounting | OpenRouter | **Yes: `LLM`.** OpenRouter, and a scripted fake for tests; from the beta, Bedrock EU or Mistral (D15). |
| `answering` | `answer(question, as_of=None) -> Answer` | Context assembly (D11), the prompt (D21), translation of quotes (D12), parsing citations and checking they point at retrieved units, mapping the outcome to answered / follow-up / not settled / out of scope | `retrieval`, `llm` | No. |
| `evals` | `run_retrieval(golden, retriever)`, later `run_generation(golden, answerer, judge)` | Loading and validating golden files, the metrics (recall@k, MRR, nDCG by required groups, D16), bootstrap intervals, MLflow logging | `retrieval`, `answering`, `llm` | No. |
| `api` | `POST /ask` | FastAPI wiring | `answering` | No. Thin by design. |

What this buys:
- **The eval harness and the API call the same two interfaces** (`Retriever.search`, `Answerer.answer`), so what is measured is what users get.
- **Phase 2 changes configuration, not callers:** chunking strategy, retrieval mode, embedding model and answer model are all settings.
- **The hard logic is testable without a network:** parsers on saved files, versioning against a local Postgres, answering with the fake LLM.

### Key types

```python
@dataclass(frozen=True)
class RawFetch:            # one downloaded document, stored as evidence
    source: str            # "riksdagen" | "migrationsverket" | ...
    url: str
    fetched_at: datetime
    content: bytes
    content_hash: str      # sha256 of content

@dataclass(frozen=True)
class ParsedUnit:          # one citable unit as read from one fetch (D2)
    unit_id: str           # e.g. "sfs:2005:716/6kap/2§"
    source_type: str       # statute | ordinance | transitional | position_paper | ruling | web_page
    authority: str         # law | ordinance | regulation | ruling | position_paper | web_page
    language: str          # sv | en
    heading: str           # chapter and section title, or page heading path
    text: str
    time_basis: str        # valid | observed
    valid_from: date | None
    valid_to: date | None
    amended_by: str | None # "2026:295"
    links: list[tuple[str, str]]   # (kind, target unit_id), e.g. ("transitional_for", ...)

@dataclass(frozen=True)
class Hit:                 # one retrieved chunk, with what citations and the verifier need
    unit_id: str
    version_id: int
    chunk_id: int
    text: str
    score: float
    authority: str
    heading: str
    url: str
    valid_from: date | None
    valid_to: date | None
    fetched_at: datetime

@dataclass(frozen=True)
class Answer:
    kind: str              # answered | follow_up | not_settled | out_of_scope
    text: str
    citations: list[Citation]   # unit_id, version_id, Swedish quote, valid_from, fetched_at
    usage: Usage           # tokens and cost, for the evals and the spend cap
```

```python
class Embedder(Protocol):
    model_id: str
    dims: int
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    # Two methods because several candidate models (e5, Qwen3) embed queries
    # differently from documents; bge-m3 treats them the same.

class LLM(Protocol):
    def complete(self, request: CompletionRequest) -> Completion: ...
    # CompletionRequest: model, system, messages, max_tokens.
    # Completion: text, model, input_tokens, output_tokens, cost_usd.
    # No sampling parameters: current Claude models reject them.
```

### Database schema

The three layers from D3 plus the evidence table (`download`). Two kinds of time are kept apart: **valid time** (`valid_from` / `valid_to`, when a wording applied in law) and **observed time** (`observed_from` / `observed_to`, when the crawler saw it), which is all a web page has (D5).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Evidence: every download, raw bytes in object storage (data/raw locally).
-- (Not called "fetch": that is a reserved word in Postgres.)
CREATE TABLE download (
  id            bigserial PRIMARY KEY,
  source        text        NOT NULL,
  url           text        NOT NULL,
  fetched_at    timestamptz NOT NULL,
  content_hash  text        NOT NULL,
  storage_path  text        NOT NULL,
  http_status   int         NOT NULL
);

-- Stable identity of a citable unit (D2).
CREATE TABLE unit (
  id              text PRIMARY KEY,        -- 'sfs:2005:716/6kap/2§'
  source_type     text NOT NULL,
  authority       text NOT NULL,
  language        text NOT NULL,           -- 'sv' | 'en'
  document        text NOT NULL,           -- 'sfs:2005:716', page URL, 'mig:2023:1'
  heading         text NOT NULL,
  url             text NOT NULL,
  translation_of  text REFERENCES unit(id) -- linked translations (D6)
);

-- One row per wording of a unit (D3).
CREATE TABLE unit_version (
  id             bigserial PRIMARY KEY,
  unit_id        text NOT NULL REFERENCES unit(id),
  text           text NOT NULL,
  text_hash      text NOT NULL,
  time_basis     text NOT NULL CHECK (time_basis IN ('valid', 'observed')),
                                           -- 'valid': legal sources, dated by valid_from/valid_to
                                           -- 'observed': web pages, dated only by when they were seen (D5)
  valid_from     date,                     -- NULL = start not known (before rebuilt history, D10)
  valid_to       date,                     -- NULL = still applies
  amended_by     text,                     -- '2026:295'
  observed_from  timestamptz NOT NULL,     -- first download that showed this wording
  observed_to    timestamptz,              -- NULL = still seen
  download_id    bigint NOT NULL REFERENCES download(id),
  -- No two wordings of a legal unit may apply on the same day. An unknown
  -- start counts as "since forever", so it is checked too.
  CONSTRAINT one_wording_per_day
    EXCLUDE USING gist (unit_id WITH =, daterange(valid_from, valid_to) WITH &&)
    WHERE (time_basis = 'valid')
);
-- No stored status: "upcoming", "applies" and "no longer applies" depend on
-- the date asked about, so they are computed from the dates at query time.

-- Links between units: transitional provisions both ways (D4), rulings' cited units (D7).
CREATE TABLE unit_link (
  from_unit_id    text   NOT NULL REFERENCES unit(id),
  to_version_id   bigint NOT NULL REFERENCES unit_version(id),
  kind            text   NOT NULL,         -- transitional_introduced | transitional_ended | cites
  PRIMARY KEY (from_unit_id, to_version_id, kind)
);

-- Chunks are built from a version, per chunking strategy (phase 2 compares two).
CREATE TABLE chunk (
  id          bigserial PRIMARY KEY,
  version_id  bigint   NOT NULL REFERENCES unit_version(id),
  strategy    text     NOT NULL,           -- 'structure' | 'fixed500'
  ordinal     int      NOT NULL,
  text        text     NOT NULL,           -- with heading prepended
  ts_config   regconfig NOT NULL,          -- 'swedish' | 'english'
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, text)) STORED,
  UNIQUE (version_id, strategy, ordinal)
);
CREATE INDEX ON chunk USING gin (tsv);     -- keyword half of hybrid search (phase 2)

-- Embeddings per model, so phase 2 can compare models side by side.
CREATE TABLE chunk_embedding_bge_m3 (
  chunk_id   bigint PRIMARY KEY REFERENCES chunk(id),
  embedding  vector(1024) NOT NULL
);
CREATE INDEX ON chunk_embedding_bge_m3 USING hnsw (embedding vector_cosine_ops);
```

Notes:
- **One embedding table per model,** because models have different dimensions and pgvector indexes are per column. A larger model in phase 2 (e.g. 4096 dimensions) needs `halfvec`, since pgvector's HNSW index covers `vector` only up to 2,000 dimensions; check this against the pgvector version used.
- **The `as_of` filter** (D8) for legal sources: `valid_from <= as_of` (or unknown) and `valid_to` empty or after `as_of`. For web pages: `observed_from <= as_of` and `observed_to` empty or after `as_of` (D5).
- **Context for a version also follows its transitional provisions back** (D4, D11): add the provisions linked to the version that applies, *and* the older version each of them ended. Transitional provisions usually say "the older wording still applies to…", so the answer needs that older text, not just the provision.
- **The golden set stays in JSONL files,** versioned in git, not in the database.
- **Migrations** are plain SQL files applied in order; no ORM.

### Layout

```
pyproject.toml              Python 3.12, managed with uv
docker-compose.yml          Postgres + pgvector, and the API (phase 3); the CLI runs natively on the Mac, for the GPU
.env.example                DATABASE_URL, OPENROUTER_API_KEY, ANSWER_MODEL, EMBEDDING_MODEL
migrations/                 001_schema.sql, …
src/permitnav/
  sources/                  http.py (Http seam), riksdagen.py, migrationsverket.py
  parsing/                  statute.py, webpage.py, types.py
  corpus/                   store.py, versioning.py, chunking.py
  embedding/                port.py, bge_m3.py, fake.py
  retrieval/                retriever.py
  llm/                      port.py, openrouter.py, fake.py
  answering/                answerer.py, context.py, citations.py
  evals/                    golden.py, retrieval_metrics.py, run.py
  api/                      app.py
  prompts/                  answer.md, … (in git, shipped with the package, D15)
  cli.py                    permitnav fetch | ingest | index | ask | eval
tests/
  fixtures/                 short statute and page excerpts, with source and fetch date
  …                         tests per module, through its interface
evals/                      golden.dev.jsonl, README.md
scripts/validate_golden.py  moves into evals/golden.py once the package exists
```

### Build order for phase 1

1. **Statute parser,** test-first on the fetched utlänningslagen and utlänningsförordningen text: units, headings, amendment markers, duplicated sections, transitional provisions.
2. **Schema and `corpus.ingest`:** versions, links, re-ingesting the same fetch changes nothing, a changed section gets a new version.
3. **`bge-m3` embedder and chunking;** measure memory and speed on the Mac.
4. **`Retriever.search`** (vector only) with `as_of` filtering, and **the retrieval eval** on the golden items that cite statutes only. This gives the first recall@k number, before any LLM is involved.
5. **Migrationsverket page fetcher and parser,** so the `web:tbd/…` placeholders in the golden set get real unit IDs.
6. **`Answerer` with the OpenRouter `LLM`** and the D21 prompt; `permitnav ask` on the command line; the API last.

### What the time-model prototype showed

Tested 2026-11-20 with a throwaway script against Postgres 17 + pgvector, using real text from the riksdagen API: two wordings of 6 kap. 4 § (one in force, one from 2028-01-01), the current 6 kap. 2 § (changed by SFS 2026:295 from 2026-06-01), and the transitional provisions of 2026:295 and 2026:1687.

| Check | Result |
|---|---|
| The `as_of` filter returns the right wording on 2026-05-01, 2026-11-20 and 2028-02-01 | Works |
| A second wording overlapping a known one is rejected | Works |
| A second wording with an *unknown* start date is rejected | **Failed** in the first schema (the rule skipped unknown starts); fixed with `time_basis` |
| Several versions of a web page, dated only by when they were seen, are accepted | Works after the fix |
| A stored `status` stays correct over time | **Failed**: an "upcoming" wording was still "upcoming" after its start date; status is now computed |
| A version-dependent question ("extension registered before 1 Dec 2026 for a permit granted before 1 June 2026") gets the right context | Works only when context also follows transitional provisions back to the older wording; now part of the design |
| Table named `fetch` | **Failed**: reserved word; renamed `download` |

One more finding: the older wording of 6 kap. 2 § (before 2026-06-01) is **not** in the riksdagen API text, yet transitional provision 2026:295 punkt 2 says it still governs some extensions decided now. So rebuilding history (D10, phase 4) matters for current questions too, not only historical ones. For 6 kap. 2 § in particular, consider rebuilding its older wording already in phase 1.

### Known open points

- **LangGraph (phase 5)** works with its own chat-model wrappers. The `LLM` interface either gets a LangGraph adapter then, or the agent calls `Retriever` and `Answerer` as tools and keeps the `LLM` interface for the plain-RAG baseline. Decide in phase 5; nothing in phase 1 depends on it.
- **Changes "on the day the government decides"** (found by the statute parser): resolved in slice 2 (migration 002). `unit_version.pending` is `starts` or `ends`; a `starts` wording never applies and is left out of the overlap rule until the source gives a date, when ingest updates it in place.
- **Undated changes** (slice 2): when a stored wording disappears and the source gives no date for the change, ingest ends it, and starts its replacement, on the day the crawler saw the change, with `dates_estimated = true`. Rebuilding history from the amending acts (phase 4) replaces these estimates.
- **Transitional links are broad** (slice 2; handled in slice 6): every point of an amending act links to every section that act amended. Context assembly now keeps a point only for the sections it names ("6 kap. 2 §"), keeps points that name no section (up to three per hit), and drops points that name only other sections. Ranges ("10 kap. 1–17 §§") are not expanded yet.
- **Inconsistent consolidated text:** the riksdagen text sometimes gives one section two different upcoming wordings from the same day (seen once: 10 kap. 14 § utlänningslagen, 2027-12-01). The parser keeps the later one and reports a conflict; check such cases against the SFS PDF.
- **Swedish keyword stemming is incomplete** (slice 3): Postgres's `swedish` configuration maps "medianlönen" to "medianlön" but leaves "anställningsavtalet" and "arbetstillståndet" unchanged, so keyword search misses some definite forms. Phase 2's hybrid search should test this on the golden set and consider a better Swedish dictionary (e.g. a Hunspell `sv_SE` dictionary) or relying on the vector half for inflected forms.
- **Pages crowd out the law** (slice 5; much improved in phase 2: law items R@5 0.29 → 0.69 after slices 1 and 4): an English question matches an English Migrationsverket page far better (cosine ~0.78) than the Swedish statute (~0.57), so with pages indexed, statute sections dropped out of the top 10. Phase 1 interleaves law and guidance results (`RetrievalConfig.mix = "interleave"`); law items recovered only partly (R@5 0.46 → 0.29, and R@1 is now always a page). Phase 2's authority-aware fused ranking (D22) and keyword search replace this.
- **Repeated page sections** (slice 5; near-identical variants handled in phase 2 slice 5): Migrationsverket repeats identical sections across many pages (the union paragraph is on ~20). Search keeps one per distinct text, and the eval counts an identical copy of an expected section as a hit. Near-identical variants (the same section on the employee, artist, seasonal-worker… pages) are collapsed to one slot by `retrieval/variants.py`, showing the variant the question names or else the employee page. Still open: a question about a variant family the keyword list misses gets the employee page's version.
- **Cross-references are not followed** (found with `permitnav ask`, 2026-11-21; handled in phase 2 slice 3): 6 kap. 4 a § says "det som föreskrivs i 2 § andra stycket" also applies to a new employment, but 6 kap. 2 § was not retrieved, so the model could only say a requirement exists that its sources don't show. Phase 2: when a retrieved section refers to another section of the same statute, add that section to the context (reusing `section_refs`), within a limit.
- **Search has no relevance floor** (same run; handled in phase 2 slice 2, as a safety net only): search always returns the k nearest sections, even for a question nothing in the corpus answers. "How do I exchange my foreign driving licence?" sent 11 unrelated sources (4,814 tokens) to the model, which declined correctly but at full cost. Phase 2: set a minimum similarity score from the golden set (in-scope versus out-of-scope items); when nothing passes it, skip the answer model or tell it that no relevant sources were found.
- **Unrelated topics get the wrong label** (same run; handled in phase 2 slice 2): the prompt defines out of scope only as asylum, protection and deportation, so the driving-licence question came back as `not_settled` ("the rules don't settle this"). Phase 2: widen rule 7 of `prompts/answer.md` to any topic outside work permits, their family members and permanent residence, and add unrelated-topic items to the golden set (D18).
- **OpenRouter model IDs** for Claude Haiku 5.5 and the Mistral judge are configuration values, to be confirmed against OpenRouter's model list at setup.

## Phase 2

### Build order

Each slice ends with the retrieval eval (dev set, drafts included, so development numbers only) and a code review. Retrieval slices come first because they are measurable without an LLM; the judge needs your hand-labelled answers.

1. **Hybrid search:** BM25 keyword search over `chunk.tsv`, Reciprocal Rank Fusion, and a direct lookup of sections the question names. *(Done; results below.)*
2. **Questions the corpus can't answer:** a relevance floor calibrated on the golden set, the wider out-of-scope rule in the prompt, unrelated-topic golden items (D18), and a behaviour eval. *(Done; results below.)*
3. **Cross-references:** add sections a retrieved section refers to ("2 § andra stycket") to the context. *(Done; results below.)*
4. **Query translation for keyword search:** English questions translated to Swedish before BM25; then re-measure the keyword weight. *(Done; results below.)*
5. **Authority-aware ranking (D22):** replaces the law/guidance interleaving. *(Done, re-scoped by measurement: interleaving stays; near-duplicate page variants are collapsed. Results below.)*
6. **Reranker:** `bge-reranker-v2-m3` over the top 30–50; gain and latency. *(Done; results below.)*
7. **LLM judge:** calibrated on ~30 answers you label, ≥85% agreement (D19). *(Done; results below.)*
8. **Answer-model comparison (D15):** Haiku 5.5, Sonnet 5.5, one Mistral model.
9. **Experiment grid and the hold-out run:** MLflow; your golden-set review (`checked`), then one hold-out run. *(Dev-set review done 2026-11-21: all 64 items checked, no changes.)*

### Slice 1 results: hybrid search

`RetrievalConfig.mode` is `vector`, `keyword` (BM25 only) or `hybrid` (the default). Hybrid fuses the vector and keyword rankings with RRF (1 / (60 + rank), the keyword ranking weighted by `keyword_weight`), within each source group, and puts up to three sections the question names ("6 kap. 2 a § utlänningslagen", "chapter 6, section 2 of the Aliens Act") first, in the question's order. BM25 is computed in SQL: document frequencies from the GIN index, each chunk matched with its own text-search configuration.

Dev set, 48 items, structure chunks:

| Configuration | R@1 | R@5 | R@10 | MRR | Identifier (8) |
|---|---|---|---|---|---|
| vector (phase 1) | 0.06 | 0.32 [0.21, 0.44] | 0.45 | 0.18 | 0.12 |
| keyword only | 0.03 | 0.10 | 0.18 | 0.06 | 0.00 |
| hybrid, keyword weight 1.0 | 0.24 | 0.40 | 0.57 | 0.32 | 1.00 |
| hybrid, keyword weight 0.3 | 0.25 | 0.46 | 0.61 | 0.35 | 1.00 |
| **hybrid, keyword weight 0 (default)** | 0.23 | **0.47 [0.34, 0.59]** | 0.57 | 0.33 | 1.00 |

- **The gain comes from the named-section lookup.** Identifier questions went from 0.12 to 1.00; nothing else changed at weight 0.
- **BM25 helps Swedish questions and hurts English ones.** Swedish R@10 rose from 0.56 to 0.69 at weight 0.3, but English R@5 fell, because an English question's words, stemmed as Swedish, match noise in the law. The default stays at 0 until query translation (slice 4) gives keyword search Swedish words to match; the differences between 0, 0.3 and 0.5 are one or two items, inside the noise.
- **Performance:** BM25's per-term document counts must be computed once per query (`MATERIALIZED`); inlined, Postgres recomputed them for every matching row, about 1 s per search instead of ~50 ms.

### Slice 2 results: questions the corpus can't answer

- **Golden items:** three unrelated-topic pairs in the dev set (driving licence, income tax return, rental housing queue) and one in the hold-out (bank account), 16 out-of-scope items in the dev set in all.
- **Relevance floor (`AnswerConfig.min_score`, `BGE_M3_FLOOR = 0.55`):** when every source scores below it and the question names no section, the answerer declines without a model call. Calibration on the dev set (best source's cosine score): in-scope 0.589–0.809, near misses (asylum, deportation) 0.640–0.734, unrelated 0.491–0.641. The ranges overlap: unrelated English questions mention Sweden and taxes, which the work-permit pages also discuss. So the floor sits below every in-scope question with a margin and is only a safety net (1 of 6 unrelated questions, and nonsense input). A higher floor would refuse real questions, which is worse than a wasted model call.
- **Prompt rule 7** now names the service's scope and declines any other topic, not only asylum and deportation; "not_settled" is reserved for in-scope questions the sources leave open.
- **Behaviour eval (`permitnav eval-answers`):** each item expects one answer kind from its `expects_*` flags; the eval compares the answerer's `kind` with it. No judge, about $0.08 for the 64 dev items with Haiku 5.5.

Out-of-scope items (16), Haiku 5.5:

| Configuration | Right | Wrong |
|---|---|---|
| Old prompt, no floor | 9 | 7 "not settled" (all six unrelated, one deportation) |
| New prompt, no floor | 14 | 2 follow-ups (deportation) |
| New prompt, floor 0.55 | 14 | 1 "not settled", 1 follow-up (asylum, deportation); 1 declined without a model call |

All 64 dev items, new prompt and floor: 44 right. No in-scope question was declined. answered 20/28 (8 follow-ups instead), follow_up 3/4, not_settled 7/16 (7 follow-ups, 2 answered), out_of_scope 14/16.

Found by the behaviour eval, for later slices:

- **Too many follow-up questions:** 15 items that expect an answer or "not settled" got a follow-up question. Some golden labels are debatable (whether applying for permanent residence together with an extension "depends on your situation"), so the golden-set review comes before any prompt tuning.
- **The near-miss boundary is unstable:** which asylum and deportation questions fail changes from run to run. One of them (a final deportation decision and a work-permit extension) is partly covered by Migrationsverket's employer pages, so its out-of-scope label should be checked in the review.
- **Language:** some Swedish questions got English answers (put-apply-with-extension-302-sv, family-spouse-put-income-401-sv, work-extension-insurance-history-204-sv), against rule 8.

### Slice 3 results: cross-references

- **Parsing (`parsing/references.py`, `cited_units`):** references with a chapter ("7 kap. 7 e §") point into the same statute, or the other one when it is named ("5 kap. 3 § utlänningslagen" from the ordinance); chapterless ones ("2 § andra stycket", "3 eller 3 a §", "9–11 §§") into the citing section's chapter. References to any other statute ("brottsbalken", "lagen (2016:752)") are left out. In the stored statutes there are about 480 chapterless and 410 chaptered references across 922 sections.
- **Context (`answering/context.py`):** for each retrieved law section, the sections it cites, in citing order and in the wording that applies on `as_of`, with the role `referenced`: at most 2 per section and 4 per answer, skipping sections already in the context. A long referenced section is cut at a paragraph and marked […]. Prompt rule 10 tells the model what `referenced` means.
- **Effect on the context (dev set, no model calls):** 61 of 64 items get references, 3.1 on average, adding about 700 tokens to a mean of 2,200. In 11 items a reference brings in an expected section that search had missed (e.g. 6 kap. 2 § for the employer-steps questions, 7 kap. 7 e § for the insurance-history ones).
- **The question that found the gap** ("Can I start working for a new employer while my extension is processed?") now cites 6 kap. 2 § and says what a new job must meet, instead of only saying that a requirement exists.
- **Behaviour eval:** no change beyond noise. Not-settled items scored 8 and 8 of 16 with references, 9 and 8 without; a single run of all 64 items gave 4 of 16, so one run swings by four or five items. **Compare behaviour-eval results across repeated runs, never one run against one.**
- **A retrieval metric for the context** would show this kind of gain directly: the share of expected sections that reach the model (search plus references and transitional provisions), next to recall@k. Worth adding with the judge (slice 7).

### Slice 4 results: query translation

- **Translation (`retrieval/translate.py`):** a question is recognised as Swedish without a model call (Swedish letters outside statute names, or more Swedish than English function words; right for all 64 dev questions). An English question is translated by the answer model (or `TRANSLATE_MODEL`) with `prompts/translate.md`, which asks for the law's own terms ("make, maka eller sambo", "status som varaktigt bosatt") and keeps section references. Translations are cached per (model, question) in `data/cache/translations.json`, so repeated evals are free and repeatable. About $0.0001 and a second per English question.
- **Keyword search per language:** Swedish chunks are matched with the Swedish text, English pages with the original question. Before, an English question stemmed as Swedish matched noise in the law.
- **Defaults:** the CLI (`ask`, `eval`, `eval-answers`) translates and uses keyword weight 0.5; `--no-translate` turns both off (weight 0). `RetrievalConfig`'s own defaults stay offline (no translation, weight 0), as do the tests.

Dev set, 48 items:

| Configuration | R@1 | R@5 [95% CI] | R@10 | MRR | English R@5 | Law R@5 |
|---|---|---|---|---|---|---|
| slice 1 default (no translation, weight 0) | 0.23 | 0.47 [0.34, 0.59] | 0.57 | 0.33 | 0.44 | 0.56 |
| translate, weight 0.3 | 0.25 | 0.52 | 0.63 | 0.37 | 0.54 | 0.65 |
| **translate, weight 0.5 (default)** | 0.25 | **0.56 [0.44, 0.68]** | 0.66 | 0.38 | **0.58** | **0.69** |
| translate, weight 1.0 | 0.25 | 0.49 | 0.62 | 0.37 | 0.52 | 0.65 |
| translate + law vector, weight 0 | 0.22 | 0.50 | 0.61 | 0.35 | 0.50 | 0.62 |
| translate + law vector, weight 0.5 | 0.28 | 0.53 | 0.66 | 0.40 | 0.52 | 0.62 |

- **English questions gain the most** (0.44 → 0.58): keyword search can now reach the Swedish law. Law items: 0.56 → 0.69 (0.29 in phase 1).
- **Searching the law with the translated question's vector adds nothing** on top; it stays an option (`--translated-vector`), off by default.
- **Translations vary a little between runs:** a fresh set of translations (after the cache key began to include the prompt) gave R@5 0.55 instead of 0.56, one item. The cache keeps a run repeatable; a new prompt or model means new translations and a slightly different number.
- **The weight is not tuned finely:** 0.3–1.0 differ by one to three items; 0.5 is the middle of that range, and the hold-out run will show whether it carries over.
- **Behaviour eval:** 44, 42 and 44 of 64 in three runs, the same as before. Across all runs so far, about 3 of ~380 answers came back `failed`, intermittently; `eval-answers --out` now keeps the model's raw reply and finish reason for failed answers, so the next one can be diagnosed.

### Slice 5 results: authority and page variants

The plan was to replace the law/guidance interleaving with an authority-aware ranking. Measuring first changed the slice:

| Items (dev set) | Interleaved | One ranking over everything |
|---|---|---|
| law (26) | **0.69** | 0.37 |
| agency (8) | 0.38 | 0.38 |
| both (14) | 0.39 | 0.29 |

- **Interleaving is the authority-aware ranking that works,** so it stays: without it, pages push law items from 0.69 to 0.37. It already does what D22 asks of ranking: the law is never crowded out, and for practice questions the pages get half the slots, not leftovers. Preferring the law *for the citation* when both cover a claim is the answer's job (prompt rule 3) and the verifier's (D13), not ranking's.
- **Agency items do not fail because the law takes their slots:** with no law in the results at all they still score 0.38. They fail inside the page ranking. The expected sections were at ranks 23–39, so this is ranking quality, which the reranker (slice 6) addresses. Some expected sections are also debatable (an applicant's "prepare your application" section versus the employer's version of it), which the golden-set review should settle.
- **Near-duplicate page variants are collapsed (`retrieval/variants.py`, `RetrievalConfig.collapse_variants`, on by default).** Migrationsverket publishes the same section on parallel pages for employees, artists, athletes, ICT permits, seasonal workers, seafarers, researchers and others: 456 sections appear on two or more sibling pages. Embeddings cannot separate them from different sections (variants 0.85–0.96; different sections on one page up to 0.95), but the structure can: the same section anchor, compared whole, on sibling pages of the known variant families (general and specialised pages), excluding intros. Other sibling pages that share a heading, such as the pages for a first application and for an extension, differ in content and stay apart. A group takes one slot where its best member ranked, with that member's score (so the relevance floor and the group order are unaffected); it shows the variant the question names ("I'm an artist", whole unambiguous words only), else the general page (employees), else the best-ranked one.
- **Effect:** recall unchanged (R@5 0.55 with and without), because the missed sections ranked far below the freed slots. But without collapsing, 36 of the 512 top-8 slots on the dev set (8 sources × 64 questions) held a section with the same anchor as one on a sibling page already shown, in 21 of 64 questions; with it, 5, all on sibling pages outside the variant families, kept apart on purpose. The answer model sees more distinct material.

### Slice 6 results: reranking

- **Reranker (`reranking/`):** a port (`score(question, texts)`), `BgeReranker` (`BAAI/bge-reranker-v2-m3` with sentence-transformers, Mac GPU in half precision, pairs capped at 512 tokens) and a fake for tests. It reorders each source group's top `rerank_depth` candidates before variant collapsing; named sections still come first. Opt-in: `--rerank` (`RetrievalConfig.rerank`).
- **Cost:** a one-time 2.3 GB download, 4.6 s to load, about 2.6 GB of memory. Per question, about 4 s more on the Mac GPU at depth 20 (5.5 s with a 1,024-token cap; 2.4 s at depth 10); the eval goes from 20 s to about 220 s for 48 questions. A CPU-only server would be several times slower.

Dev set, 48 items (hybrid, translation, keyword weight 0.5, variants collapsed):

| Configuration | R@5 [95% CI] | R@10 | MRR | Law R@5 | Agency R@5 | Time per question added |
|---|---|---|---|---|---|---|
| no reranker | 0.55 [0.43, 0.68] | 0.67 | 0.38 | 0.69 | 0.38 | – |
| rerank top 10 | 0.58 | 0.68 | 0.40 | 0.67 | 0.50 | ~2.4 s |
| **rerank top 20** (512 tokens) | **0.61 [0.49, 0.73]** | 0.72 | 0.41 | 0.67 | **0.75** | **~4 s** |
| rerank top 20 (1,024 tokens)* | 0.61 | 0.76 | 0.42 | 0.71 | 0.62 | ~5.5 s |
| rerank top 30* | 0.59 | 0.76 | 0.41 | 0.67 | 0.62 | ~7 s |
| one ranking by reranker score (`--mix rerank`)* | 0.45 | 0.56 | 0.33 | 0.38 | 1.00 | ~7 s |

\* Measured before the code review's fixes (sigmoid scores; candidates past the rerank depth dropped instead of kept after the reranked ones). The rows without a star were measured after them; rerank top 20 gave the same figures before and after.

- **The gain is where slice 5 pointed:** agency items, whose right sections ranked 23–39, go from 0.38 to 0.62–0.75 (R@10 to 1.00). Overall R@5 0.55 → 0.61; multi-source items 0.12 → 0.25.
- **Interleaving still matters:** one ranking by reranker score, which unlike cosine compares across groups, lets the plain-language pages push law items from 0.69 to 0.38. The cross-encoder prefers explanations to statute text, just as cosine does.
- **Deeper is not better:** 30 candidates add time without gain; 20 is the best on every measure.
- **For phase 3 (hosting):** measured on CPU (the M3's cores, full precision), one question at depth 20 (40 pairs) takes 8.8 s with 2 threads and 7.4 s with 4; a typical cloud vCPU is slower. So the local model on the planned 8 GB CPU server would add 8 s or more to every answer. A hosted rerank API was not measured: it needs an account with a provider that offers one (OpenRouter does not). The options are a hosted rerank API (no memory cost, a per-call price, and EU data processing to check), a server with a GPU, or depth 10 on a bigger CPU server (R@5 0.58 instead of 0.61). Until that decision, reranking stays off by default; evals that use it say so in their header.

### Golden-set review (dev set)

Reviewed on 2026-11-21 with a review page showing each item's expected sources (with their text on the item's date), reference answer and expected behaviour; deliberately not what PermitNav retrieved or answered. All 64 items were marked correct, with no changes, and are now `checked`, so dev-set results are reported without `--include-drafts`. The review took about 4.5 minutes in all; the owner confirmed it as a full review.

It settles the questions the evals raised, as labelled:

- the deportation item (oos-deportation-work-permit-602) stays out of scope;
- put-apply-with-extension-302, family-spouse-put-income-401, family-spouse-stays-403 and work-new-or-extension-202 expect a direct answer, not a follow-up question;
- work-extension-insurance-history-204, work-update-pending-application-207 and work-switch-in-sweden-104 expect "not settled", not a follow-up;
- the expected Migrationsverket sections for the agency items stand.

So the behaviour eval's main finding, too many follow-up questions (15 items), is now a prompt problem to fix, not a labelling question. The hold-out set (24 items, private) is still unreviewed; it gets its own review before the hold-out run.

### Follow-up questions and unreadable replies

After the golden-set review confirmed the labels, the behaviour eval's main error (the model asking a follow-up question where the item expects an answer or "not settled") became a prompt problem. Rules 5 and 6 of `prompts/answer.md` were rewritten three ways, each measured over three full runs of the 64 dev items with Haiku 5.5 (one run swings by four or five items):

| Prompt | Runs (right of 64) | Mean | Real follow-ups asked (of 4) | Failed replies (3 runs) |
|---|---|---|---|---|
| original ("ask when it depends on facts you do not have") | 44, 42, 44 | 43.3 | 3–4 | – |
| A: answer by default, rarely ask | 53, 54, 50 | 52.3 | 0 | 0 |
| B: ask when the question doesn't say which permit, or for how long | 46, 48, 47 | 47.0 | 3 | 3 |
| C: answer by default; a missing date or duration is a condition to state; ask only when the question says nothing about which permit | 52, 48, 47 | 49.0 | 2 | 6 |
| **C, with unreadable replies asked for again (kept)** | **54, 52, 49** | **51.7** | 2–3 | **0** |

- **C is kept:** as good as A within the noise, and it keeps the follow-up behaviour the labels and D14 call for, which A drops. Rule 6 now also sends a question that names no permit, where knowing the permit would settle the outcome, to rule 5 instead of "not settled" (after the code review, worded to match rule 5's condition; one run afterwards: 54 of 64, none failed). The prompt's example of a broad question is deliberately not a golden question.
- **Unreadable replies:** two causes. The parser rejected replies with real line breaks inside JSON strings (strict JSON) and then picked up an inner quote object instead, which was a bug (fixed: `strict=False`) and behind most of the intermittent `failed` answers since slice 3. Separately, the model sometimes answers in prose or writes invalid JSON (unescaped quotes); the answerer now shows it its reply once and asks for the JSON object only (`REASK`), which costs one extra call on 1–3% of answers. An empty reply is repeated as the same request (an empty assistant turn is rejected); if the second call fails, the first answer and its cost are kept. Costs reported by `ask` and `eval-answers` include every call, and `eval-answers --out` keeps the earlier raw replies of a failed answer.
- **Is it the model's size?** Sonnet 5.5 (20× Haiku's price) was run once on the 20 contested items (follow-up and not-settled) with the same prompt and retrieval: 13 of 20 right, against 12–15 for Haiku, and it misses the same items. The broad questions (put-work-003, work-working-while-pending-105) get an answer with conditions instead of a question, and 204, 207 and 304 get a caveated answer instead of "not settled". So the remaining errors sit on a judgement boundary (sources that partly answer: an answer with conditions, or "not settled"?), not on model capability. Haiku stays the answer model; the full comparison, with the judge measuring content, is slice 8.

### Slice 7 results: the LLM judge

- **The judge (`evals/judge.py`)** gives two verdicts per answer, in two separate calls so neither can leak into the other: **correctness** against the reference answer (correct / partly / incorrect; `prompts/judge_correctness.md`, which sees no sources) and **faithfulness** to the sources the answer model was shown (every claim supported, or a list of the unsupported ones; `prompts/judge_faithfulness.md`, which never sees the reference). Statements about the sources ("the sources do not say …") need no support; a claim counts as supported if any source shown states it; a number or date needs a source that gives it.
- **Calibration set:** 30 answers from the current configuration, one per in-scope question pair plus six in the other language (`evals/judge/answers.jsonl`, `judge-sample`), labelled by the owner on a review page that hid the judge (`labels.jsonl`). Real answers are mostly right, so a judge that approves everything would agree with them; the set therefore also has **12 seeded errors** (`seeded.jsonl`): copies of real answers with one mistake planted (five wrong numbers, one wrong condition, two flipped conclusions, three invented requirements, one unsupported detail in an otherwise right answer), labelled by construction. Correctness agreement is gated on correct vs not correct; the exact three-level agreement is reported too.
- **Adjudication:** where the best candidate disagreed with a label, the owner looked again with the evidence (`adjudications.jsonl`). Five labels changed where the sources or the reference showed the label was wrong (202 had already been caught by the first judge run: its answer says "apply for a new permit, not an extension", against the reference and the owner's own experience); four changes the owner had first accepted were reverted because the cited source does contain the claim, or the claim needs no support under the judge's rules. Accepting a judge's view without checking the source is the easy path on such a page, and would make the calibration confirm itself. The five changed items make the agreement below somewhat optimistic; the independent check is agreement on answers the judge has not been calibrated on.
- **Candidates** (42 answers, two calls each):

| Judge | Correctness | Faithfulness | Real answers c / f | Seeded errors c / f | Per run | Notes |
|---|---|---|---|---|---|---|
| Mistral Large 4 | – | – | – | – | ~$0.45, ~85 min | reasoning cannot be turned off; ~6,000 thinking tokens and ~3 min a verdict at "low" |
| Mistral Large 2512 | 76% | 40% | 73% / 20% | 83% / 92% | $0.13 | over-flags faithfulness (wrong in 2 of 3 spot-checked claims); judged a flipped conclusion correct; upstream rate limits |
| GPT-6 Luna (low) | 55% | 40% | 43% / 17% | 83% / 100% | $0.04 | far too harsh: 15 of 30 good answers "partly" |
| Mistral Medium 3.1 | – | – | – | – | – | retired upstream ("invalid model") although still listed |
| Gemini 3.1 Flash Lite (low) | 86% | 83% | 93% / 80% | 67% / 92% | $0.12 | missed planted wrong numbers (90→80 %, 3→1 years); flipped its own verdicts between runs |
| **Gemini 3.5 Flash Lite (low), kept** | **90%, 88%** | **88%, 86%** | 93/93%, 90/90% | 83% / 75% (both runs) | **$0.10–0.13** | two runs; 4 of 84 verdicts changed between them |

- **Kept: `google/gemini-3.5-flash-lite` with low reasoning effort** (`JUDGE_MODEL`, `JUDGE_REASONING_EFFORT`). It catches every planted wrong number and flipped conclusion. **Its blind spot:** plausible added requirements ("you must also pass a Swedish B1 test [S12]") and small unsupported details; it missed 3 of 12 planted errors on faithfulness. Judged scores are more trustworthy for wrong content than for added content.
- **`eval-answers --judge`** judges every answered, follow-up and not-settled answer and reports correctness and faithfulness per expected kind; `--out` keeps each verdict and its unsupported claims. About $0.15 for the 64 dev items on top of the answers.
- **First judged run** (64 dev items, Haiku 5.5, default retrieval): **correct 39 of 48 items that should be answered (81%)** (answered 23/28, follow-up 3/4, not settled 13/16; 9 partly or wrong), and no unsupported claims detected in the 48 answers judged. Given the judge's blind spot, read faithfulness as an upper bound. The wrong answers are the list to work through, the way 202 was. Denominators are all items of a kind: a declined, failed or unreadable one counts as not correct, so declining more can never look better.
- **Production (D15):** offline, the judge sees only golden-set questions. If it later checks samples of real answers (online monitoring, phase 3), it sees user data and must run on an EU-compliant route; Gemini has one through Vertex AI in an EU region. Check the provider's data-processing terms before then.
- **Re-calibrate** whenever the judge model or a judge prompt changes (D19): `permitnav judge-calibrate`.

### Working through the judged errors

The second judged run (saved with `--out`) flagged 12 answers. Each was traced to its cause before anything was changed:

| Cause | Answers | Finding | Action |
|---|---|---|---|
| Conditions applied loosely | family-spouse-stays-403-en, family-spouse-put-income-401-en | 403 applied the "lived together abroad / well-established relationship" exception to someone it does not cover: 5 kap. 3 j § grants it only when the sponsor is a Swedish, EEA or Swiss citizen. 401 added a way to meet the support requirement ("including through your income") that no source gives. | Prompt rule 11: apply conditions exactly as the sources state them, check whom an exception is for, invent no ways of meeting a requirement. Both answers correct and faithful in both runs since. |
| Not combining sources | work-extension-insurance-history-204-sv | 6 kap. 4 a § point 4 makes refusal depend on 7 kap. 7 e §; both were in the context, but the answer stopped at "the sources do not say". | Rule 10 extended: combine a section with the one its rule depends on. Right in one of two runs since: not reliably fixed. |
| Answer language | 13 of 24 Swedish questions | Answered in English despite rule 8, with an English prompt and mixed sources. | The answerer detects the question's language (`language_of`) and tells the model ("Answer in Swedish, the language of the question"); when the question is neither clearly Swedish nor clearly English (another language, a tie, fewer than two words of evidence) no language is imposed. `eval-answers` reports "in the question's language" over every reply the model wrote, declines included, ignoring quoted passages and leaving out replies whose language cannot be told: 49 of 63 before, 63 of 63 in both runs since. |
| Retrieval depth | work-employer-steps-101-sv | The employer-preparation section ranks 14th: below page intros and a partly relevant section; not a collapsing or translation problem, and the reranker does not lift it into the top 8. | Not tuned for one question; the depth was measured as a whole (below) and stays at 8. |
| Missing source | work-union-statement-102-en/sv | No crawled section said what happens if the union does not reply. The owner found it: Migrationsverket's page for trade unions (`/andra-aktorer/fackforbund.html`, outside the crawl rules): "Om fackförbundet inte har yttrat sig inom 14 dagar kan arbetsgivaren gå vidare i processen utan att vänta in yttrandet", and otherwise Migrationsverket checks the collective agreement and wage statistics. | That page added to the crawl scope (only it, not all of `/andra-aktorer/`); 102 gets a second required group and a precise reference, its `as_of` moved to the day the page was first crawled (pages are dated by observation, D5). The Swedish answer now gives the 14-day rule; the English one does not reach the page (below). |
| Two words for one application | work-new-or-extension-202-en/sv | Migrationsverket's page says "submit a new application" for a new job; the law (6 kap. 4 a §) and the e-service ("Extend the existing work permit") treat it as an extension. The owner's experience settles it: "new application" means a new decision, because the permit is tied to an employer; the application category for someone with a valid permit is the extension. | References updated to lead with the extension and explain the page's wording; the phone service's advice is in the notes (D23). A prompt sentence for such cases (lead with the law's term, explain the page's) changed nothing in two tries and was removed. 6 kap. 4 a § is in the context (rank 4), yet the answer follows the page and adds "not an extension", which no source says. No crawled section names the e-service option. |
| Known kind boundary | work-working-while-pending-105-en/sv, oos-deportation-work-permit-602-en | Follow-up vs not settled; an out-of-scope question answered. | Unchanged (measured in the follow-up tuning). |

Two judged runs with rules 10–11 and the language fix: correct 41 and 39 of 48 (85%, 81%), the same as before within the noise; faithful 46 and 48 of 48; behaviour 49 and 53 of 64; language 63 of 63. The fixes repair the answers they target without moving the total, as expected for changes aimed at 2–3 of 48 items. Both new prompt examples were first written from golden cases (204, 403) and replaced with neutral ones before measuring.

Open points from this round:

- **Swedish-only pages and English questions (handled):** the trade-union page is the only one of 114 Swedish pages with no English version. For the English 102 question its 14-day section ranked 59th by meaning (cosine 0.56 against 0.66 for English union sections) though 2nd by keyword search, so it missed the top 8. Fix (`RetrievalConfig.translated_page_weight`, `translated_pages_untranslated_only`): for English questions, a third ranking in the page group, ordered by the Swedish translation's vector (scores stay against the question), fused with RRF.

| Configuration | R@5 | R@10 | 102-en: 14-day section | English questions' page sources in English |
|---|---|---|---|---|
| without | 0.53 | 0.66 | not found | 73% |
| A: all pages, weight 1 | 0.53 | 0.68 | rank 5 | 41% |
| **B: only pages with no English version, weight 1 (CLI default)** | 0.53 | 0.67 | **rank 1** | **70%** |
| B, weight 2 | 0.53 | 0.66 | rank 1 | 69% (crowds out 102-en's other requirement) |

  Over all pages the translated ranking pushed English readers' sources from English to Swedish twins (D6), for no recall gain; restricted to pages without an English version it reaches the Swedish-only guidance and leaves the rest alone. The English 102 answer now gives the 14-day rule. The page-language share is reported by `permitnav eval`.
- **A page's plain wording over the law's term:** in 202 the answer model prefers Migrationsverket's "new application" to the law's "extension" even with 6 kap. 4 a § in the context, and invents the contrast. A candidate for the answer-model comparison (slice 8): whether a stronger model reads the two together.
- **The language instruction can still slip:** one `ask` of a Swedish question (102-sv) answered in English, although both judged runs had every reply in the question's language.
- **More sources outside the crawl rules:** the trade-union page shows that useful guidance can live outside the applicant and employer sections. When an answer says "the sources do not say", check the site before treating it as a gap in the rules.

### Retrieval depth: 8 sources stay

Does giving the answer model more sources (`AnswerConfig.k`, `--k`) help? First without model calls, the share of expected sources that reach the model (search, transitional provisions and references together):

| Sources | Expected sources reaching the model | English | Swedish | Context |
|---|---|---|---|---|
| **8 (kept)** | 49 of 72 (68%) | 25/36 | 24/36 | ~3,100 tokens |
| 10 | 53 of 72 (74%) | 25/36 | 28/36 | ~3,800 |
| 12 | 55 of 72 (76%) | 26/36 | 29/36 | ~4,400 |

Then judged, two runs each:

| | k = 8 | k = 12 |
|---|---|---|
| Correct (of 48) | 41, 40 | 44, 43 |
| English correct (of 24) | 22, 20 | 22, 21 |
| Faithful (of 48 judged) | 48, 48 | 46, 45 |
| Behaviour (of 64) | 52, 55 | 51, 51 |
| Answer cost, 64 questions | $0.104 | $0.12 |

- **More sources help Swedish questions, not English ones:** about three more correct answers per run, nearly all Swedish; English unchanged.
- **More sources add unsupported claims, in both languages:** two or three per run, against none at 8. Of the five, three were substantive: content from the artists' page answering a general question (a near-miss variant slipping in), an inference no source states, and a claim about which trips count towards residence time that the cited source does not support.
- **Kept at 8 (owner's decision, D24):** the English slice is the headline, and there k = 12 adds unsupported claims without making answers more correct; in a legal answer an unsupported confident claim is worse than a missing detail. Not tried: k = 10, which may keep most of the gain with less dilution, and more sources for Swedish questions only (easy, but tuned to a 24-item slice).

### The hold-out run (phase 2 result)

The 24 private hold-out items were reviewed on 2026-11-22 (eight references made more precise, each new fact checked against the sources first; one cited court ruling, MIG 2017:24, kept in the item's notes because rulings are not crawled yet, D7) and then run **once** with the default configuration (hybrid search with translation and keyword weight 0.5, variant collapsing, the Swedish-only page ranking, k = 8, Haiku 5.5, the calibrated judge). Aggregate numbers only; the items stay private.

| | Dev set (tuned on) | **Hold-out (run once)** |
|---|---|---|
| Retrieval R@5 [95% CI] | 0.53 [0.41, 0.65] | **0.31** [0.14, 0.50] (18 scored) |
| Law items R@5 | 0.69 | 0.40 |
| English R@5 | 0.54 | 0.25 |
| Behaviour (right kind of answer) | 49–55 of 64 (~81%) | **14 of 24 (58%)** |
| Judged correct (items that should be answered) | 39–44 of 48 (81–85% at k = 8) | **11 of 18 (61%)** |
| Faithful (judged) | 43–48 of 48 | 18 of 18 |
| Answers in the question's language | 63 of 63 | 24 of 24 |

- **The dev numbers were optimistic.** Every phase 2 choice was tuned on the dev set (prompt wording, fusion weights, variant rules, the follow-up line), and part of the gain was specific to those 64 questions: about 20 points of correctness and 23 of behaviour do not carry over. The hold-out is also small (CIs overlap) and somewhat harder (more multi-source items; references just made more precise).
- **What carried over:** faithfulness (no unsupported claims detected) and the answer-language fix. Changes to *how* the system answers generalised; tuning of *what* it retrieves, and of when it answers rather than says "not settled", generalised less: four answerable hold-out questions got "not settled".
- **Phase 2's headline is therefore:** dev 81–85% correct, hold-out 61%; retrieval R@5 0.53 dev, 0.31 hold-out.
- **This hold-out is now spent.** Nothing may be tuned on it. Its failures can be read to find general causes (multi-source retrieval, over-caution), but every fix must be shown on new questions: a fresh hold-out of 20–30 items, reviewed the same way, before the next round of changes is judged.

### Why the hold-out scored lower (diagnosis, read-only)

The hold-out's failures were read to find general causes; nothing was tuned on them, and any fix must be shown on a fresh hold-out. Counts only; the items stay private.

1. **Situational questions do not reach the law, and the dev set hid it.** Of the 20 required sources the hold-out missed (outside the top 10), 17 are law sections. Most were near misses: of 30 required sources, 10 ranked in the top 10, 15 at 11–40, 3 at 41–100 and 2 beyond 100. The dev set's law score (R@5 0.69) was flattered by identifier questions ("What does 6 kap. 2 a § say?"), which the named-section lookup answers perfectly: 8 of 48 dev items, 2 of 18 hold-out items. A question that describes a situation in everyday words ("my family, after I lose my job") misses statute wording ("anknytning", "anställningen har upphört"); Swedish questions miss too, so this is vocabulary, not language.
2. **Over-caution follows.** With the deciding law section missing, the answer honestly says the sources do not say, which scores as "not settled" or a follow-up where an answer was expected. In one case the right section was retrieved and the answer still hedged: the "not settled" line from the dev tuning is too strong.
3. **The hold-out is harder in composition:** multi-source items are 6 of 18 (33%) against 8 of 48 (17%) in the dev set.
4. **Minor:** the waiting-times page ranks low for processing-time questions; the temporary-protection boundary is still unstable.

Candidates for the next round, each to be validated on a fresh hold-out, not this one:

- **Law near misses:** the reranker, since most misses sit at ranks 11–40, exactly the range it reorders (on the dev set it did not help law, 0.69 → 0.67, but there the law items were dominated by identifier questions it cannot improve); and legal-vocabulary query expansion, extending the existing translation step to restate a situation in statute terms.
- **Over-caution:** reserve "not settled" for questions the sources address but leave open.
- **Golden sets:** the fresh hold-out should look like real users' questions (mostly situational, few identifier ones), and the dev set should gain situational items so that dev numbers stop flattering law retrieval.

### Law retrieval for situational questions: what did not work (dev set)

Following the hold-out diagnosis, three fixes were tried on the dev set with throwaway prototypes (nothing was added to the code). Split by question kind, the dev set shows the same problem: law sources for situational questions reach the top 5 in 43% of cases (44 groups), identifier questions in 100% (8 groups).

| Try | Situational law in top 5 | Note |
|---|---|---|
| Baseline | 0.43 (0.57 in top 8) | |
| Reranker (built, opt-in) | 0.45 (0.52 in top 8) | Helps pages (0.35 → 0.50), not the law |
| Statute-style restatement of the question by Haiku (HyDE for the law), as extra vector and keyword rankings in the law group, weight 1 and 2 | 0.41–0.45 | No effect |
| Leave out the EU/EEA chapters (3 a and 3 b kap.), which never apply to the service's users | unchanged | Missed sections move up 1–6 places |
| A model names the likely sections, fetched by the named-section lookup | — | Haiku names 11 of 44 required sections and 4 of the 19 misses; Sonnet 22 of 44 and 8 of the 19, at about $0.012 a question and with 149 of 171 proposals not needed |

- **Why:** inside the law group, 24 of the 44 sources rank in the top 3, 5 rank 4–10 (cut only by interleaving with the other groups) and 15 rank below 10, often very deep (6 kap. 2 § at 99 for an insurance question, 7 kap. 3 § at 52 for a job loss). The missed sections are the long, general central provisions; their wording is too general for word or meaning similarity to pick them out. Only knowledge of the law finds them.
- **Decision:** law retrieval is parked. Section naming by a strong model is the only lever that worked and is a later design decision on cost (about 8× the answer's cost per question). The interleaving loss (5 of 44) is small and cheaper to address later.
- **Gotcha found on the way:** Haiku 5.5 spends its token budget on hidden reasoning first; with `max_tokens=300` it returned empty text. The query translator (400 tokens) was checked: every English dev and hold-out question has a cached translation.

### A softer "not settled" rule: tried and reverted

The spent hold-out was used as private development data for this fix (with the owner's agreement), because the dev set barely shows the over-caution: 0–1 answerable questions got "not settled" per dev run, against 4 of 14 on the hold-out. Hold-out 2 stays untouched.

Rule 6 of `prompts/answer.md` was rewritten to reserve "not settled" for questions whose main point the sources leave open, and to answer when a source states the deciding rule even if it does not mention the person's exact circumstances. Three runs each, with the judge (about $1):

| | Dev, right kind | Dev, judged correct | Hold-out, right kind | Hold-out, answerable → "not settled" |
|---|---|---|---|---|
| Old rule 6 (2 dev runs, 3 hold-out runs) | 52, 55 of 64 | 40–41 of 50 | 13–14 of 24 | 4, 4, 4 |
| New rule 6 | 48, 48, 50 of 64 | 41–43 of 50 | 13–15 of 24 | 3, 4, 4 |

- **No gain where intended, a loss elsewhere:** the hold-out's over-caution barely moved, while dev questions that really are open were answered as settled 7–9 times per run (2–3 before), and an out-of-scope question was answered. Reverted.
- **The diagnosis overstated this cause.** The hold-out's over-caution is two topics (each in two languages), identical in every run: one whose required sources were not retrieved at all (an honest "the sources do not say", so cause 1 again), and one borderline item where the answer gives the right rule but declines to draw the conclusion the reference draws from it (that a break does not reset a total counted over a period). A general prompt change was the wrong tool for one borderline item.
- **Where the hold-out findings stand:** law retrieval for situational questions is the real problem; over-caution is mostly its consequence. The one lever that worked for retrieval (a strong model naming the sections) needs a cost decision.

### Hold-out 2 and the close of phase 2

Hold-out 2 (22 private items, 11 topics in English and Swedish, drafted from Migrationsverket's own Q&A and rephrased as situations, human-reviewed, never used before) was run once against the frozen commit `71eb06a` (prompt hashes and full config printed in each report). Retrieval once; answers three times with the judge (about $0.30). Aggregate numbers only.

| | Dev set (tuned on) | Hold-out 1 (spent) | **Hold-out 2 (fresh)** |
|---|---|---|---|
| Retrieval R@5 [95% CI] | 0.53 [0.41, 0.65] | 0.31 [0.14, 0.50] | **0.61** [0.43, 0.80] |
| Right kind of answer | ~81% | 58% | **19–20 of 22 (86–91%)** |
| Judged correct | 81–85% | 61% | **20 of 22 in each run (91%)** |
| Faithful (judged) | 43–48 of 48 | 18 of 18 | 20–22 of 22 |
| In the question's language | 63 of 63 | 24 of 24 | 22 of 22 |

- **Not comparable to hold-out 1 as "better":** every hold-out 2 item expects a direct answer, and Migrationsverket's Q&A sections hold their answer in one place, so the set is easier by construction. It measures the answered path on realistic wording; follow-ups, "not settled" and out-of-scope questions are not covered by it.
- **The law problem shows here too:** items that need a law section (multi-source) have R@5 0.31, against 0.79 for page-only items — the same weakness the hold-out 1 diagnosis found.
- **Failures are concentrated:** the only incorrect answers are one topic (both languages, every run), answered as "not settled". Three answers in total were judged unfaithful, spread over two topics in single runs.
- **Hold-out 2 is now spent** once its failures are acted on; the next independent check needs a hold-out 3, which should include follow-up, not-settled and out-of-scope items and questions from real cases.

**Phase 2 closes here.** Its honest results: on fresh questions that should be answered, about 90% judged correct and faithful, in the question's language; on harder situational questions (hold-out 1) about 60%. The main open problem is law retrieval for situational questions (only a strong model naming the sections helped, a cost decision for later); over-caution turned out to be mostly a consequence of it.

---

## Phase 3

Build order (agreed 2026-11-22): widen the corpus first (MIG rulings, then position papers), because it is the only part of phase 3 that can change answer quality; then the HTTP API and container, tracing, CI eval levels and deployment.

**Position papers move to phase 4** (decided 2026-11-22): Lifos has no API, its search is blocked by robots.txt and Migrationsverket's pages do not link to the papers, so they need a curated list kept fresh through the Lifos RSS feed. They are versioned documents ("version 10.0"), which is what phase 4's freshness pipeline handles, and rulings showed that a new source type brings a small gain here. Phase 3's "with position papers and rulings" row is therefore "with rulings" only.

### Corpus widening 1: MIG rulings

- **Source:** the courts' open data API (Sök rättspraxis, PUH API, CC0), `GET /api/v1/publiceringar?domstolkod=MIOD`, 100 records a page, 1 request/s. One fetch (all 535 Migration Court of Appeal publications, with full text and cited sections) is saved as one raw download (`sources/domstol.py`).
- **Scope (`parsing/ruling.py`):** a ruling is kept if it cites sections on work permits, family ties, permanent residence or long-term residents (5 kap. 3–3 l, 5, 7, 8, 10 with 6 kap., 17, 18 §§, 5 a, 6, 6 a, 6 b kap., 7 kap. 3 and 7–7 f §§ of utlänningslagen; 4 kap. 4–4 f §§, 5 and 6 kap. of the ordinance) and none on protection, exceptional grounds, spårbyte, enforcement or studies, and if it was decided from 2009 (after the labour-migration reform of 2008-12-15). **97 rulings kept**; 406 out of scope, 32 older. One (MIG 2024:13) has no full text in the source and is kept with its summary.
- **Unit (D2):** `mig:2017:24`, the whole ruling: summary, cited sections, then the court's text. Dated by the decision date (`valid_from`, no end). A corrected text (or full text added later) withdraws the old one rather than ending it, since both start on the decision date. 1,644 chunks.
- **Citations (D7):** `cites` links to each cited section's wording on the decision date. Only 71 of 191 citations link: the others cite sections whose only stored wording starts after the ruling (6 kap. 2 § as amended 2026-06-01, for example). Until history is rebuilt (D10, phase 4), D7's check ("this ruling interpreted an earlier wording") cannot be relied on, so answers state each ruling's date and the prompt (rule 3) says to note that the law may have changed since.
- **Retrieval:** rulings form the "other" group. With an equal share in the interleaving they took about a third of the places: dev R@5 0.53 → 0.46, law 0.69 → 0.62, hold-out 1 0.31 → 0.19, with a ruling in the top 3 for 46 of 48 dev items. Now the group gets **one place, at rank 8** (`RetrievalConfig.other_slots`, `other_rank`; the same rank whatever k a caller asks for, and never in place of a section the question names): dev R@5 0.52, law 0.69, hold-out 1 0.31.
- **Answers (3 judged runs on dev and hold-out 1 each, against the same day's baseline):**

| | Baseline | With rulings |
|---|---|---|
| Dev, judged correct | 41, 40 of 50 (82%, 80%) | 41, 43, 44 of 50–51 (82%, 86%, 86%) |
| Dev, right kind | 52, 55 of 64 | 52, 53, 52 of 64 |
| Dev, faithful | 50, 50 | 50, 48, 47 |
| Hold-out 1, judged correct | 61%, 61%, 50% | 72%, 67%, 50% |
| Answer cost per dev run | ~$0.105 | ~$0.115 |

  - A small gain (3–4 points on dev, within the run-to-run noise) and no new risk: answers citing a ruling (67 of 208 judged) were 84% correct with 1 unfaithful, against 75% and 7 for the rest; none of the unfaithful claims came from a ruling.
  - The case that motivated it works: the insurance-history question now cites the precedent the reviewer named, but still labels the question "not settled".
  - **Kept.** Rulings are the right sources for edge cases at little cost, but they are not a fix for law retrieval on situational questions.

### The HTTP API and container

- **`POST /ask`, `GET /health`** (FastAPI, `api.py`) over the same pipeline and default settings as the CLI and the evals (`service.py`). Every `/ask` needs a bearer token; without `API_TOKEN` the app refuses to start unless local use is allowed explicitly. Private until the public beta (D15).
- **Concurrency:** requests run on worker threads. The embedder and reranker sit behind one lock (Hugging Face tokenizers are not thread-safe); each request gets its own translator with no disk cache, so no question text is stored.
- **Container:** CPU-only PyTorch on Linux (no CUDA libraries; image 3.3 GB), non-root, migrations on start, bge-m3 weights in a volume. `docker compose up` starts Postgres and the API. Locally: ~1.1 GB of memory, three concurrent questions answered in 10–13 s each.

### Tracing

- **OpenTelemetry, exported to Langfuse Cloud (EU)** over OTLP/HTTP (`tracing.py`); no vendor SDK, so the backend is a configuration choice (LangSmith, for instance, also accepts OTLP). Exporters come from the environment (Langfuse keys, or a local JSON-lines file); with neither, nothing is recorded.
- **Spans:** `ask` > `answer` > `retrieve` (> `embed`, `translate`, `embed`, `rerank`) and `generate`. Model calls are Langfuse generations with model, tokens and cost.
- **Only questions are traced** (added 2026-11-23). FastAPI (0.165) traces every request once a tracer provider exists, adding `fastapi.dependencies/endpoint/serialization` spans under each. In the first day on the VM, health checks (the uptime check every minute, Docker, deploys) and internet scanners made ~19K observations, ~97% of the total, and used 49% of the Langfuse Hobby plan's 50K monthly units. A sampler (`keep_trace`, parent-based) now keeps a trace only if its root is `POST /ask` or not an HTTP request at all (CLI, evals, the refresh's retrieval check); a dropped root drops its children. It decides on the start attributes (`http.request.method`, `url.path`), because FastAPI names the span only `POST` until routing has run. The tests' tracer uses the same sampler.
- **Eval runs are traced only on request** (added 2026-11-24). The day after the sampler fix, local eval runs (~10 observations per judged question: answer, retrieve, embeds, translate, generate, evidence check, two judge calls) used ~5,700 of the Hobby plan's 50,000 monthly units, and the nightly retrieval check another ~150 a night, while their per-item `--out` files and Cloud Logging already hold what was needed. The CLI now installs a tracer only for `ask` (one question, metadata) and for `eval`, `eval-answers`, `smoke`, `judge-sample` and `judge-calibrate` given `--trace` (e.g. for phase 5's Langfuse experiments). Without it no exporter is installed and spans cost nothing. Production (`POST /ask`) is unchanged.
- **Privacy:** the content mode is fixed in code, not configuration: the API and `ask` record metadata only (lengths, timings, tokens, costs, answer kind, unit ids); eval commands, which ask golden-set questions only, record full text. Exceptions are recorded by type, never by message (provider errors can hold account details or echo the question). Tests trace into memory only.
- **Measured (one question, Haiku 5.5):** about 8 s and **$0.0012 per question**: `generate` 6.0 s and $0.0012 (6,368 in / 1,056 out tokens), `translate` 1.0 s and $0.00005, the two embeddings 0.18 s, the database searches ~0.7 s. About $1.25 per 1,000 questions.

### CI eval levels

- **Every push and pull request, no model, no secret (job `retrieval-eval`):** the retrieval eval on the dev set against the eval corpus, a public release asset (`eval-corpus-2026-11-22`, 25.8 MB: every stored wording, links, page translations, `structure` chunks with bge-m3 embeddings; public source text with attribution, never rebuilt history), with the questions' vectors and translations replayed from `evals/queries.dev.json`. Deterministic: a restored database reproduces the local numbers exactly (R@5 0.5208, MRR 0.3537), so the job fails below R@5 0.52 or MRR 0.35. Works for pull requests from forks.
- **On merge to main, nightly and on demand, with the answer model (job `smoke`):** 12 dev questions covering every answer kind and both languages, chosen from the 35 that were right in all five dev runs of the current configuration. Checks: the answer kind, a citation for an answer, the reply's language. A failing question is asked once more; one failing question is allowed. Measured: 11, 12 and 12 of 12 in three local runs (one random flip in 36 answers), $0.019 a run. Runs on a CI-only OpenRouter key with a $5 monthly credit limit and an expiry.

### Deployment (Google Cloud, free trial)

- **One VM in Stockholm** (`europe-north2`, `e2-standard-2`: 2 vCPU, 8 GB), created with Terraform (`infra/`): static IP, firewall, the VM's own service account, an EU backup bucket. Docker Compose with Caddy in front (`deploy/`); the API and Postgres have no host ports. Live at a subdomain of the owner's domain (DNS only, no proxy: a proxy would see questions in plain text). The domain stays in the VM's secrets, not in the repo.
- **Deliberately minimal** (phase 3 is "small on purpose"): no managed database, load balancer, autoscaling or Secret Manager. These become relevant at the public beta, together with the EU model provider (D15); Compose keeps each upgrade separate.
- **Security:** from outside only TCP 443, TCP 80 (redirects to HTTPS) and ping (GCP's default ICMP rule) are reachable (verified: 22, 3389, 5432 and 8000 closed); SSH only through Google's IAP tunnel and OS Login; shielded VM; daily automatic security updates; the VM may create, list and read backups but not delete or overwrite them; a production Postgres password; secrets only in a root-owned `/opt/permitnav/.env`, never in Terraform, metadata or the repo; a server-only OpenRouter key with its own limit.
- **Backups:** nightly `pg_dump` to the EU bucket, versioned, kept 30 days. First run: a 24.7 MB dump.
- **The corpus** is loaded from the public eval-corpus snapshot; phase 4 adds re-crawling on the server.
- **Measured live (HTTPS, from Stockholm):** 7.7 s for an answer (17.1 s for the very first request after start), 4.1 s for an out-of-scope decline; API ~0.97 GB, Postgres ~0.18 GB, Caddy ~0.03 GB of memory; 9.5 GB of the 30 GB disk.
- **Found and fixed while deploying:** the VM first had `objectAdmin` on the backups (could delete them; now `objectCreator`, plus `objectViewer` because `gcloud storage cp` reads the destination first: the first backup run failed with 403 until then; verified that the VM still cannot delete a backup); GCP's default network allows SSH and RDP from anywhere (outranked by a deny rule for this VM; the default rules are the project's, not the module's); a token appended to a `.env` without a final newline merged into the previous line (the API's start-up check refused the empty token, as designed).
- **Trial deadline:** the $300 credit runs for 90 days; about $55–60 a month is used. Before it ends, upgrade the billing account or move (a database dump, a new VM, a DNS change).

### Monitoring

- **Cloud Monitoring and Logging** (`infra/monitoring.tf`), within their free allowances, alerts by email (the address only in the git-ignored `permitnav.tfvars`).
- **Is it up?** An uptime check on `/health` every 5 minutes from six regions (HTTP 200, `"ok"` in the body, a valid certificate); "PermitNav is down" when two or more regions fail (Google's 1200 s alignment recipe).
- **Did the nightly backup fail?** `backup.sh` writes INFO or ERROR to Cloud Logging: an `EXIT` trap reports every non-zero exit (also a missing variable, which skips an `ERR` trap), and a systemd `OnFailure=` unit reports a killed run. "PermitNav backup failed" fires on any ERROR.
- **Not:** a "backup missing" absence alert (its window is shorter than a day, so it would fire before every nightly run; a VM that is down is caught by the uptime check), and container log shipping (Docker's `gcplogs` driver can stop containers starting when Cloud Logging is unreachable; logs stay on the VM).
- **Tested live, end to end:** a real backup logged INFO; a forced failure (a bucket that does not exist) logged ERROR and the alert email arrived; all six regions' uptime checks passed.
- The API logs only the first part of a provider error (`OpenRouter HTTP 402`), never its body, which can carry account details.

### Deploy on merge (pull-based)

- **The VM pulls, GitHub does not push** (`deploy/update.sh`, a systemd timer every 5 minutes): GitHub needs no credentials for the VM, and nothing outside can reach in; the VM only reads the public repository.
- **Only checked commits:** the ruleset on `main` already requires *Secret scan*, *Lint, types and tests*, *Retrieval eval (no models)* and (since the image slice) *Build image*; the VM checks them again for the exact commit (GitHub's public check-runs API), against a push that bypassed the ruleset. A pending check means "try again later".
- **Safe switch:** pull the image CI built (since the image slice; before, the VM built it), restart, wait up to 5 minutes for the health check; if the new version is not healthy, roll back to the previous commit. A failed commit is remembered and skipped until a newer one arrives, so a broken merge cannot restart the service every 5 minutes. A commit whose check failed is reported once and not remembered, so a re-run that passes is deployed; a required check missing after an hour (a renamed job) is reported once.
- **Database migrations:** when `migrations/` changed, a backup is taken before the deploy, and a failed deploy is **not** rolled back automatically (old code on a migrated schema can pass the health check and still fail); the alert says so and points to the restore.
- **One at a time:** a lock shared with the boot-time build; the timer waits for the startup script; the script runs from a private copy, since a deploy may change the script itself. A changed Caddyfile recreates Caddy (a single-file bind mount keeps the old file).
- **Alerting:** each deploy logs INFO or ERROR (`permitnav-deploy`); every failure path reports (explicit reports, an `EXIT` trap, an `OnFailure=` unit for a killed run); "PermitNav deploy failed" fires on ERROR.
- **Not automatic:** changes to `infra/` (Terraform, the startup script) are applied by hand. A deploy reaches the server up to 5 minutes after the merge's checks finish.
- **First run (2026-11-22):** installing the timer on a VM whose checkout predated `update.sh` made the timer's first run fail at once ("No such file"); the `OnFailure` unit logged an ERROR and the "deploy failed" alert fired, as designed. A VM created from scratch clones the current `main`, so this only affects the move from manual updates. The first deploy (`a1a3969`) was then started by hand and logged `INFO deployed`; the next merge was deployed by the timer alone.

### Images and the supply chain

- **Order of the rollout:** the VM's startup script (Terraform metadata) is updated before the first image-based deploy, so a reboot runs the recorded image instead of trying to build it; the GHCR package turned out public on CI's first push (it inherits the repository's visibility through the image's source label; checked with an anonymous pull), so no manual step was needed.
- **Build once, run that:** CI's "Build image" job builds the API image on every pull request (a broken Dockerfile fails it) and, on `main`, pushes it to GHCR as `ghcr.io/bxljoy/permitnav-api:sha-<commit>`. The VM pulls that image and runs it **by digest**; it no longer builds. What runs is what CI built, a deploy takes seconds instead of a build on 2 vCPUs, and a rollback runs the previous digest. GHCR, not a cloud registry: the image holds only the public code (no secrets, data or model weights), so a public package is free, needs no credentials to pull and works unchanged on any provider (the platform after the GCP trial is open: GCP, AWS or another).
- **Push or pull is left for the platform decision:** pull keeps working on any provider; a push deploy's authentication is provider-specific (GCP: Workload Identity Federation and IAP; AWS: GitHub OIDC to an IAM role and SSM).
- **Dependencies:** Dependabot (`.github/dependabot.yml`) opens weekly pull requests for Python (`uv.lock`, minor and patch updates grouped), GitHub Actions and the Docker base image, each gated by CI. `pip-audit` checks the hashed lock file (all extras) on every CI run; not a required check, since a new advisory can turn it red without any change here. First run: no known vulnerabilities in 243 locked packages.
- **Actions pinned to commit SHAs** (with the version as a comment, which Dependabot keeps current): a moved or compromised tag cannot change what runs.

---

## Phase 4

Build order (agreed 2026-11-23): 1. a daily re-crawl on the server with a "what changed" log and alerts; 2. amending acts (SFS RSS) and in-force dates (SFSR); 3. rebuilding history back to 2023 (checked against lagen.nu); 4. version selection (asking for the application date when a transitional provision depends on it) with a version-dependent eval slice; 5. position papers (curated list + Lifos RSS) and the golden-set drift check.

### Slice 1: the daily refresh

- **`permitnav refresh`:** fetch each source separately (statutes, Migrationsverket, rulings: one site being down does not stop the others; a failing page or statute is recorded, not fatal), ingest, build and embed `structure` chunks (the strategy production searches; only changed sections get new chunks), then summarise the change log for the run.
- **On the VM** (`deploy/refresh.sh`, systemd timer at 02:30 UTC, before the 03:30 backup): a one-off container from the running image, sharing the deploy lock; raw downloads kept as evidence in a volume. After the refresh, the retrieval eval runs on the **live** corpus with the recorded dev questions (the image carries the public dev set): below the CI gate, or a golden item's source gone, is an alert. Results go to Cloud Logging ("PermitNav refresh failed" on any ERROR; kills and crashes through an `OnFailure` unit).
- **The "what changed" log** (`change_log`, migration 004): one row per section added, changed, removed or re-dated, with a unified diff for changes; a page that disappears logs its sections as removed; a corrected ruling as changed. Replaced wordings are paired with the most similar new text (a download can also announce a future wording, which is "added"). It is the service's own data: not in the public eval-corpus snapshot.
- **Unchanged downloads are not stored again** (~100 MB a day otherwise): the existing file is reused, with this fetch's time, so what is seen again is observed from today.
- **A bug found on the way:** a section whose text changed without a new amending act (a published correction, or a parser change) kept its start date, and ingest crashed on the overlap rule: every daily run would have failed. A new wording with the same start date now replaces the old one for its whole period, and the old one is withdrawn.
- **Snapshots from an older schema import:** migrations only add, and COPY names its columns, so the eval corpus need not be republished after each migration; a snapshot from a schema the database has not reached is refused.
- **First real run (local, 2026-11-23):** statutes, 227 Migrationsverket pages (3,072 sections) and 97 rulings all unchanged since 2026-11-20: "no changes". The phase's first check (a real source change caught and logged) waits for a real change; the mechanics are covered by tests that edit a page, a statute section and a ruling.

### Slice 2: in-force dates from the amendment register

- **The gap:** the consolidated text marks an amended section with its act ("Lag (2026:385)") but gives no date for changes already in force, so 546 of 981 current statute wordings had no start date. An undated wording counts as applying on any earlier date, so a question dated before an amendment got the newer wording, and the rulings' D7 check could not compare wordings.
- **Sources** (3 requests a day in the refresh): the amendment register SFSR (rkrattsbaser.gov.se, Regeringskansliet; one page per statute: every amending act with its scope, in-force date and a transitional-provisions flag) and the SFS publication feed (svenskforfattningssamling.se, the authentic publication: new acts as issued, usually before the register and the consolidated text include them). Parsed in `parsing/sfsr.py`; acts kept in `amending_act` (migration 005).
- **Dates applied conservatively** (`corpus/dates.py`): only wordings with no start yet; an act's in-force date for its sections and transitional provisions, the statute's own in-force date (2006-03-31) for sections never amended; acts partly "on a day the government decides", undated in the register or with several dates are skipped (the register does not say which sections start when); a date that would overlap another wording is skipped and reported; each date is a `dates` row in the change log naming its source.
- **Result (2026-11-23):** 520 wordings dated (418 by their act, 88 original, 14 transitional), no conflicts; **546 → 40** undated. The rest belong to partly pending acts (resolved by ikraftträdande ordinances, which the feed announces) or acts the register gives no date for (the act's PDF has it): later refinements.
- **Early notice:** a feed act not yet in the register is logged once as a new amending act. The feed is optional: an unreadable feed is reported and the dating still runs.
- **Links follow the dates:** after dating, the transitional links and (for `--source sfsr`) the rulings' citation links are rebuilt, since both are chosen by start dates.
- **Retrieval unchanged** (dev R@5 0.5208, MRR 0.3537): every golden question is dated after these starts. The gain is for dated questions before an amendment and for answers stating when a rule took effect.
- **Code review (high): 3 important, 5 nits, all fixed with regression tests (308 tests).** An unreadable feed crashed the whole ingest (now reported, dating continues); an Ikraft with several dates gave every section the first one (now no single date: skipped); dating alone left ruling and transitional links on the old wordings (now rebuilt); the overlap check is deferred to commit, so the savepoint never caught it (now immediate during dating); dates were logged at the register page's first fetch time, missing from the refresh summary (now this run's time); wordings in force until a government decision were never dated (now dated: 44 → 40 undated); `fetch/ingest <sfs>` ignored the given statutes; a feed item's guessed statute was never corrected by the register.

### Slice 3: older wordings rebuilt from the amending acts

- **The gap:** the corpus held only today's wording of each section (plus changes seen since November 2026), so a question dated before an amendment got no wording of that section at all. On 2024-01-01, 305 of the law's sections and 163 of the ordinance's had a wording that applied.
- **Method** (`corpus/history.py`): every amending act prints the new wording of each section it changes, with a footnote naming the act whose wording it replaced ("Senaste lydelse 2018:319"). From each current wording that started after 2023-01-01, walk back: the wording before act A is that section's text in the act A's footnote names, from that act's in-force date (slice 2's register) until A's. Stop at a wording in force on 2023-01-01, at the statute as enacted (2006), or at a step that cannot be taken, each counted with its reason.
- **No footnote is not proof of no amendment:** an act replacing a whole chapter ("upph. 4 kap.; nya 4 kap.", 2026:1142, 2026:1425) prints its sections without footnotes. Taking the 2006 wording there was wrong (caught by the lagen.nu check), so the register decides: the latest earlier act whose scope ("Omfattning", parsed in `parsing/sfsr.py`) names the section or its chapter gave the wording before; if it repealed the section, there was none; if no act names it, the statute as enacted.
- **Renumbered sections** ("Senaste lydelse av tidigare 9 § 2013:606") keep their earlier wordings under their former number, which is what the section was called then.
- **Sources:** the act PDFs, fetched once each (an act never changes): svenskforfattningssamling.se from April 2018 (HTML page → sealed PDF), rkrattsdb.gov.se `/SFSdoc/<yy>/<yy><nnnn>.PDF` before (TLS valid again; no robots.txt on either). 162 PDFs (≈30 MB); the fetch plans which acts the rebuild reads and follows the acts they name.
- **PDF text** (`parsing/sfs_act.py`): pypdf for words and order; it splits ≈0.6% of words in the printed SFS ("arbetstillstå nd"), mended using pdfminer.six's text of the same act as a dictionary only (pdfminer keeps those words whole but drops and reorders words in the newer PDFs: 8 self-check failures against pypdf's 3). Layout handled: page furniture, headers glued to the next line ("SFS 2017:2212 kap."), footnote numbers glued or spaced ("18 a §4", "8 c § 3", "beslut 1"), footnotes resynced after a miss, wrapped headings, chapter headings without a full stop or absent (one-chapter acts take it from the enacting clause), broken words at line ends.
- **Verified two ways:**
  - *Self-check against today's text:* where an act is still the latest for a section, its printed wording must equal the consolidated text: **608 of 611** match; the 3 are faults in the consolidated text (an ordinance section marked "Lag (2026:1260)", two sections holding two wordings).
  - *Independent check against lagen.nu* (its consolidation after act K shows act B's wording when B ≤ K < the act that replaced it): **309 of 318** rebuilt wordings match (2 consolidations, 2023 and 2025); the other 9 are lagen.nu's own formatting (spaces before punctuation, dash lists shown as numbers, appended headings, pending markers).
- **Result:** 338 older wordings, as ordinary dated versions with the act PDF as their download (the consolidated-text ingest never treats them as its own). Sections with a wording on a past date: 2023-01-01 law 303 → **483**, ordinance 159 → **222**; 2025-01-01 333 → **500**, 179 → **235**; today unchanged. Transitional provisions and rulings link to them (610 links): a ruling from 2024 is compared with the wording of 2024 (D7). Example: the EU Blue Card salary threshold dated 2024-06-01 now retrieves the 2013 wording ("en och en halv gånger" the average salary), dated 2025-06-01 the current one (1,25 gånger).
- **Repeatable nightly:** a rerun stores nothing new; a wording read differently after a parser fix replaces the earlier rebuild (the old row withdrawn, kept as evidence) and only it is re-embedded. Retrieval eval unchanged (R@5 0.5208, MRR 0.3537): every golden question is dated November 2026.
- **Known limits** (each a counted stop, never a guessed wording): sections changed only by a word swap ("ordet … ska bytas ut", e.g. 2014:655) or a pure renumbering, whose new wording no act prints (28); acts with no single in-force date (5); wordings of sections in a replaced chapter whose earlier act is not dated. Sections repealed since 2023 and not re-added are not rebuilt (no current unit to walk from). A pre-existing parser fault found on the way: the consolidated text's `21 kap. 10 §` unit held all of chapter 22 (Tribunalvittnen); fixed below.
- **Code review (high): 7 important, 3 nits, all fixed with regression tests (329 tests).** A page body could be cut at an arbitrary line after a footnote mark (my resync change had detached a check), and a footnote glued to its number ("14Ändringen") was not one: both fixed, and an earlier act's section whose footnote was not found is no longer used; a heading item in the register scope hid every section after it ("rubr. närmast före 21 §, 12 kap. 14 b …"), and "5 kap. den nya 2 c §" read as the whole chapter; an unreadable register or act PDF crashed `fetch`/`ingest` (now reported, the rest runs); transitional links were not rebuilt after the rebuild; a rebuilt wording whose dates change on a later run (a corrected in-force date) stayed stale (earlier runs' overlapping rebuilds are now replaced); the fetch planner and the walk disagreed on footnotes without "Senaste lydelse"; wordings ending on a government decision were not walked; act PDFs were parsed once per statute (now once per process); a stopped walk left an empty unit for a former number.

### Slice 4: version selection, with a version-dependent eval slice

- **What transitional provisions actually depend on** (the 34 substantive points of acts in force since 2023): not one "application date" but several facts — when a **permit was granted** (2026:457/459, 2026:1221 p2, 2026:1686 p3), when an **extension was registered** (2026:295 p2 and 2026:605 p2: extensions of work permits granted before 2026-06-01 keep the old salary rule if registered by **2026-12-01**; 2026:1746 p3: by 2027-10-01), when an appealed **decision** was made (2026:295 p3 and others), when an **application came in** (2026:1686 p2), and for crime-related ones when an offence was committed. The live example: until 2026-05-31 a work permit needed "god försörjning" = a salary of at least **80 %** of the median (6 kap. 2 § första stycket 1, ordinance 5 kap. 8 a § in SFS 2023:570); from 2026-06-01 at least **90 %** (SFS 2026:295).
- **Eval slice** (`evals/golden.versions.jsonl`, 12 items): the transition case (granted February 2026, registered this week → 80 %), too late (registered January 2027 → 90 %), granted after the change (→ 90 %), no dates given (→ both branches), and historical `as_of` 2024-06-01 (work permit → 80 %; EU Blue Card → 1.5 × the average salary, the rebuilt 2013:606 wording).
- **Measured before changing anything:** retrieval R@5 0.67; right kind 10 of 12; judged correct 11 of 12, faithful 12 of 12. The transition case, the no-dates case (both branches, in both languages) and the historical questions were already right: slices 2–3 supplied the dates and older wordings, and context assembly already followed a retrieved *section* to its transitional points and the older wording they keep (D4, D11).
- **The gap:** the other direction. A retrieved *transitional point* ("… gäller 6 kap. 2 § första stycket 1 i den äldre lydelsen") came without the section it names, so for the late extension the model said the new rule applied but that the sources did not say what it requires ("not settled"). Fix (`answering/context.py`): a retrieved point that names sections brings each (up to two, only sections its act changed, so the statute is right) in the wording that applies on the date (`referenced`) and in the older wording the point keeps alive (`older_wording`); a point naming no section brings nothing.
- **After:** the late-extension case answers 90 % with the reason (registered after 1 December 2026); slice judged correct 12 of 12 (third run; right kind 11 of 12). The dev set does not regress: right kind 51 of 64 (before 49–55), judged correct 44 of 48 (92 %; before 81–85 %), faithful 45 of 48 — single runs, within the noise.
- **D8 in practice: answer both branches rather than ask.** D8 says the assistant asks for the date a transitional provision depends on; answer rule 5 (phase 2) says a missing date is a condition to state, never a reason to ask. Measured: with no dates given, the answer states both branches correctly in every run and language ("if granted before 1 June 2026 and registered by 1 December 2026: 80 %; otherwise 90 %"). That is complete, faithful and spares a round trip, so rule 5 stands and D8's "asks" is read as "makes the answer depend on it". The transitional provisions still decide which wording applies; the system adds no date rule of its own.
- **Eval lessons:** a historical reference must not require knowledge from after its `as_of` (the Blue Card reference first required the 1.25 × threshold, enacted in December 2024, for a question dated June 2024). Two "unfaithful" flags were judge blind spots: the date the system gives the model ("today is 23 November 2026") and Migrationsverket's SEK 34 470 (its own figure, on a dozen pages).
- **Open (the CI part is done, see below):** a generic historical question ("what salary for a work permit?", as of 2024) sometimes gets a follow-up about the permit type (2 of 6 answers) — the phase 2 follow-up behaviour, not a version issue. The version slice needs the rebuilt history, which the CI corpus snapshot (exported before phase 4) lacks: it runs against a live corpus until the snapshot is re-exported with the history.
- **Code review (high): 3 important, 4 nits, all fixed with regression tests (334 tests).** A named section that was itself a later hit was added first as a shortened `referenced` source and the hit then dropped (now skipped: it comes in whole, as the hit); a point naming its statute after the section ("5 kap. 8 a § utlänningsförordningen") matched nothing because references were read without a statute (now each linked section is matched in its own statute's terms); named sections had no budget and their older wordings were not shortened (now at most 4 sources in all, shortened like references); several named sections were cut in id order, not the order named; the slice's items now use the README's `version-dependent` type; a test picked "today's wording" by `valid_to IS NULL`. Chapterless statutes are not handled (none in scope). After the fixes: slice judged correct 12 of 12, faithful 12 of 12, right kind 11 of 12.

### The version slice in CI (eval corpus 2026-11-23)

- **Decision (2026-11-23): the rebuilt statute history is published** in the public eval corpus, reversing a phase 3 note that kept it "served by the app, not published". It is derived only from official SFS acts (no copyright), and the code that rebuilds it is public in this repository, so the data gives little away; publishing it lets CI test version-dependent answers on every pull request. The change log stays unpublished.
- **New corpus** `eval-corpus-2026-11-23` (27.4 MB; 5,297 wordings with the rebuilt history, 6,250 `structure` chunks with embeddings), checked in an empty database the way CI loads it: the dev set replays unchanged (R@5 0.5208, MRR 0.3537).
- **New CI step:** the version-dependent slice with its recorded queries (`queries.versions.json`, no model): R@5 0.6667, MRR 0.3056 → minimums 0.66 and 0.30 (a minimum above the score fails, exit 3).

### Fix: chapter 22 read into 21 kap. 10 §

- **Cause:** the consolidated text prints chapter 22 as "22 kap. " / blank line / "Tribunalvittnen"; every other chapter has its title on the heading line ("21 kap. Tillfälligt skydd"). The parser's chapter pattern wants the title on the same line, so chapter 22 was never seen and its 7 sections were read as the end of 21 kap. 10 §. Fix (`parsing/statute.py`): a bare "N kap." block followed by a short one-line title is joined into one heading. Checked on both statutes: the only differences are the 7 sections of chapter 22 added and 21 kap. 10 § back to its own text; the ordinance is unchanged.
- **The same source read differently is a correction, not a change in the law** (`corpus/store.py`): the first local ingest ended the bloated 21 kap. 10 § on the crawl date and started the corrected one then (an estimate), so a question dated before today would have kept the bloated text. The law only changes with new source text: when a download has the same bytes as the previous one from that URL and one wording of a section is replaced by one undated (or same-start) reading, the new reading takes its place for its whole period and the old reading is withdrawn. Its start carries over only if it still names the same amending act (otherwise the register dates it again); its end is what the source says now. 21 kap. 10 § now reads correctly from 2006-03-31; the change log keeps the diff.
- Chapter 22's sections are dated by the register (2006-03-31, and 22 kap. 2 § by its act) and get their history rebuilt; both CI gates unchanged (dev R@5 0.5208, versions 0.6667).
- **Code review (high): 3 important, 2 nits fixed with regression tests (339 tests); 1 pre-existing already handled.** The correction rule compared the source with the download the wording was first read from, so any change to the source since then defeated it (now: the previous download of the same URL); a corrected reading inherited a start that came from an act it no longer names, and an end date it no longer has (both fixed); a chapter title on its own lines that wraps was still missed, silently (now joined, and a heading that still cannot be joined is a parser warning, which stops that statute's ingest instead of misreading it); several wordings of one section changed at once still take the estimated path (documented, not seen). Links to a withdrawn reading: the statute ingest rebuilds transitional links at its end, ruling links are rebuilt by the riksdagen/domstol steps.

### Slice 5: Migrationsverket's legal position papers

- **Discovery within the rules.** Lifos's search, its result pages and "Nya RÄS och dokument" are closed to crawlers (`robots.txt`: `Disallow: /sokning*`, added by Migrationsverket on 2026-04-15); the news archive has no index page; only single document pages, their PDFs and the legal department's RSS feed (the latest 5 announcements, about a month) are open. So a person runs the search in a browser ("Rättsligt ställningstagande" in the title: 96 results on 10 pages) and saves the result pages; `permitnav import-lifos-results <folder>` reads them (title, RS number, version, date, document id), deduplicates by RS number (5 papers had an older version in the results), suggests relevance from the title (include / exclude / review), and writes the review file `curation/position_papers.csv`, where a person's decisions are kept across re-imports. Result: 91 papers, **15 in scope** (work, extensions, family, maintenance, permanent residence, several grounds, the Turkish association agreement), 76 out (asylum, detention, return, procedure, EU free movement). The import caught a stale version web search had found (RS/084/2021 v6.0 of 2023; current: v8.0 of 2026-07-12).
- **Keeping it current without crawling the search:** the nightly refresh reads the feed. A newer version of an in-scope paper is followed (its announcement page links the document) and remembered in the raw volume (`lifos/latest.json`, which survives container restarts); a paper not in the review file is reported (`NEW POSITION PAPER …`) for a person to decide on in a pull request. Each version is fetched once.
- **TLS:** Lifos chains to Let's Encrypt's "Root YR", not yet in Mozilla's list (certifi, Debian). The cross-signed copy (`root-yr-by-x1.pem`, signed by ISRG Root X1, checked against certifi and against the self-signed Root YR's key) completes the chain to a root already trusted; no new root of trust.
- **Parsing** (`parsing/position_paper.py`, pypdf with the slice 3 word mending): one unit per numbered section (`rs:RS/084/2021/2.1.3`, D2), found by its title from the table of contents (a contents list can be stale after a revision: the body's numbers win; older papers have unnumbered contents, numbered 1, 2, 3 in order). Page furniture and footnotes are left out; footnote blocks are found by their number and a mark earlier on the page glued to a word, a punctuation mark after a word ("utlänningslagen.6", "utsatta:16"), or ")." after a number, never by a heading that looks like a footnote ("2 Gällande rätt") or a number ("MIG 2018:5"). Checked: 228 sections, no furniture or footnote text left, and the words not kept are fewer than the headings' words in every paper (nothing of the body lost).
- **Dates:** each section starts on the paper's last revision that changed it or a section above it (the revision table on page 1: "2026-07-12 8.0 2, 2.1, 2.2 …"), else on the paper's own start; a new version's revised sections end the old wording on its date, unchanged ones stay one wording (stored by document, since each version is its own download).
- **A retrieval fix the papers exposed:** BM25's collection statistics (document count, average length, document frequencies) were computed over all sources, although each group is ranked on its own. 636 chunks of position-paper prose made "permanent uppehållstillstånd" common and reordered law sections no paper competed with (dev R@5 0.5208 → 0.5104). Each group's BM25 now uses its own collection: with or without the papers the law and guidance rankings are identical, dev R@5 stays 0.5208 and dev MRR rises from 0.3537 to **0.3728**; the version slice: R@5 0.6667, MRR 0.3056 → 0.3021 (one item). The CI snapshot gives the same numbers.
- **Answers:** position papers are an `other` source (with the rulings); the prompt says what they are: Migrationsverket's interpretation, binding for its staff but not for courts, above its web pages and below the law and the court's rulings, cited as "RS/number/year".
- **Effect on answers (dev set, judged): none measurable, either way.** Three judged runs with the papers against two without (one with the old global statistics): correct 38, 39 of 48 with; 44 (global statistics) and 42 without; right kind 54, 53 with; 51, 53 without; faithful 43, 47 with; 45, 46 without. Split by whether the context changed: on the 30 questions where the model saw exactly the same sources with or without papers, the scores still moved by 1.5–2 points (pure run-to-run variation); on the 20 where a paper came in, by 0.5–1. The dev questions that point to position papers (permanent residence: conduct, gaps between jobs) were right without them and stay right. Answers use a paper in their context rarely (2–3 of 19 answers per run): on these questions the law and Migrationsverket's pages carry the answer. To measure what the papers add, the golden set needs questions whose answer is the agency's interpretation (e.g. how conduct is weighed for permanent residence, the maintenance calculation in family cases, the burden of proof in family cases): a next step, not a reason to hold the papers back. Retrieval with the papers: dev R@5 0.5208, MRR 0.3728; versions R@5 0.6667, MRR 0.3021.
- **Code review (high): 8 important, 2 nits, all fixed with regression tests (356 tests).** The refresh never fetched Lifos but now ingested it, so every nightly ingest would have failed (the refresh steps now come from one source list, and a test checks that every source the ingest reads is fetched); a new version moved an unchanged section's start forward when a section above it was revised (history lost), backdated a changed section the revision table does not name, and ended a dropped section on the download day (now, against what is stored: unchanged sections keep their start, changed ones start no earlier than the new version, dropped ones end when it starts); importing older saved results could move a row back to an older version; a feed announcement's first document link was trusted (now the linked document must name the RS number and version) and one failing item stopped the rest; feed text was not unescaped or safely decoded; a page without its main PDF crashed the fetch on an `assert`, and an appendix could be taken for the main document; a paper taken out of scope stayed searchable (its sections are now withdrawn); answers citing only a paper had no "Källa: Migrationsverket".

### Slice 5: golden-set drift check (D17)

- **What drift is:** a golden item is written against the sources as they read on its `as_of`; evals keep running it with that date, so it never turns wrong by itself, but it can stop describing the rules people ask about now. `permitnav golden-drift` (`corpus/drift.py`) checks each expected unit of each item written "as of today" (version-dependent items test another date on purpose and are left out): a **newer wording** in force since the item's date, a Migrationsverket **page that reads differently** today, a **corrected** reading of the wording the item was written against (e.g. the chapter 22 fix), or a source **gone**. Those need review; a dated **upcoming** change is reported for information (the item stays valid until then).
- **In the nightly refresh:** after the live-corpus eval. The items needing review are remembered in the raw volume (per set of golden files, and only once the warning is logged, so an alert that could not be sent comes back the next night); items new since the last run are a WARNING (`refresh drift: … (new: …)`) and their own alert, "PermitNav golden items need review" (`infra/monitoring.tf`): once per item, a review rather than a failure. A run that cannot check is an ERROR (the refresh alert).
- **Review (D17):** `permitnav golden-drift --draft drafts.jsonl` writes a fresh copy of each item that needs review, dated today, `review: draft`, with what changed in its notes; a person checks it against today's sources and adds it to the golden set. The old item stays as a historical test: an item with a fresh copy (`<id>-YYYYMMDD`) in the same file is no longer followed for drift, only the newest copy is. `--today` runs a what-if without touching the saved state.
- **Today (2026-11-23):** dev set: no item needs review; 4 items (5 kap. 18 §: switching to a work permit in Sweden, working while an application is pending) have a dated change from 2028-01-01 (SFS 2026:1687). As if on 2028-02-01, those 4 need review. Holdout 1: none; holdout 2: 4 upcoming.
- **Code review (high): 5 important, 3 nits, all fixed with regression tests (363 tests).** A statute section that ends with nothing in its place (repealed, or dropped) was not reported (now `gone`, or `upcoming` when it ends later); an item stayed "needs review" forever after its fresh copy was added (an item with a `<id>-YYYYMMDD` copy is now historical); the record of reported items was one list for every golden file (now per set of files) and was saved before the warning was logged (now saved only after the warning is written, so a lost alert comes back); a drafted translation could point at an original that was not drafted; a failing drift run kept no output for diagnosis; "today" now follows Swedish time, like the corpus's page dates.

### Golden questions about Migrationsverket's interpretations

- **Why:** the dev set could not tell whether position papers help (its questions are answered by the law and the web pages). `evals/golden.interpretation.jsonl`: 4 scenarios × EN/SV whose answer is the agency's legal position, each expecting the paper section and the law section it interprets (`multi-source`): which incomes count for permanent residence (RS/084/2021 3.1.1: only the applicant's own taxed income from employment or business; not a spouse's income, savings or benefits); whether unpaid taxes or fines can stop permanent residence (RS/084/2021 3.2: yes, they can be enough on their own); an extension applied for after the permit expired (RS/082/2021 2.3–2.4: a new application, 5 kap. 18 § decides; as a rule no work while it is processed, RS/084/2021 3.1.1); and whether the employer matters in family maintenance (RS/008/2026 2.3.3.1: its seriousness and ability to keep paying are assessed). Drafted from the papers' own text, reviewed and checked 2026-11-23. A fifth topic (applying on several grounds, RS/083/2021) was dropped: the paper's text is abstract procedure without a clear answer to a realistic question.
- **Baseline (2026-11-23):** retrieval R@5 0.12, MRR 0.08; right kind 3 of 8; judged correct 4 of 8, faithful 8 of 8. **When the paper reached the context, the answer used it and was correct** (both conduct items, the paper at rank 8 in the "other" slot); mostly it did not reach the context at all: papers share the single "other" slot (rank 8) with the rulings. So the limit is retrieval of position papers, not the model: the next experiment (and phase 5's `get_position_paper` tool) is measured on this slice.
- **Not in CI yet:** the CI corpus snapshot (2026-11-23) predates the papers; the recorded queries (`queries.interpretation.json`) are ready for when it is re-exported with them.

### Experiment: retrieving position papers (measured on the interpretation slice)

- **Question:** papers reach the model only through the single "other" slot (rank 8), shared with rulings; on the interpretation slice the expected paper section was among the answer's 8 sources in 3 of 8 items. Two variants, both options, **off by default**:
  - (a) `paper_slots` (`--paper-slots`): papers get a group and a slot of their own, at rank 7, before the "other" slot;
  - (b) `paper_follow_law` (`--paper-follow-law N`): with a paper slot, prefer the paper sections that cite one of the top N law sections found (a paper interprets the law it cites; citations read with the reference parser, once per wording) over the best match among all papers.
- **Results** (recorded queries, no model):

| Variant | Paper section among the 8 sources (interpretation slice) | Dev R@5 | Dev R@10 |
|---|---|---|---|
| shared "other" slot (default) | 3 of 8 | 0.52 | 0.67 |
| (a) own slot | 4 of 8 | 0.52 | 0.63 |
| (a) + (b), following the top 3 or 5 law sections | 4 of 8 | 0.52 | 0.63 |
| two paper slots + (b) | 4 of 8 | 0.52 | 0.60 |

- **Why (b) changed nothing:** for the items that miss, the law search itself misses the law the paper interprets (the family-employer question retrieves 5 kap. 18 §, 9 kap. 17 §, 6 kap. 6 c § instead of 5 kap. 3 j §; the late-extension question 6 b kap. 7 §, 5 kap. 3 § instead of 5 kap. 18 §), the known phase 2 weakness for situational questions. The citations are right (RS/008/2026 2.3.3.1 cites 5 kap. 3 j §; RS/082/2021 2.3 cites 5 kap. 18 §). On these questions the papers are not the bottleneck; one-pass law retrieval is. That is phase 5's job: an agent that searches, judges the evidence and searches again (for instance a paper first, then the law it cites).
- **Decision:** neither variant becomes the default (one more item, against dev R@10 0.67 → 0.63); both stay as options, like the reranker, for phase 5 to reuse.
- **Found on the way:** recorded queries were refused whenever *any* retrieval option was added (the recording kept the whole config), which would have failed CI the next time an option appeared. Replay now compares only the settings that decide which texts are embedded or translated (the golden file, drafts, mode, translation settings, keyword weight); a text that was never recorded still fails the replay. The smoke command had its own copy of the check and failed on main after the merge (CI's PR checks don't run the smoke slice); it now uses the same comparison, checked with a real smoke run on the recorded queries.

## Phase 5 plan (revised 2026-11-23)

The roadmap's phase 5 was written before anything was measured. Five changes, each from a result:

- **A fixed workflow with one flexible step, not a free agent loop.** The measured failure is specific: one search pass misses the right law section for situational questions (phase 2: recall about 0.43; the paper experiment: the family-employer question retrieves 5 kap. 18 § instead of 5 kap. 3 j §). So: search → sufficient? → at most two targeted extra searches → answer → verify. A free plan-and-act loop costs more and varies more, and judged runs already vary by about 3 answers in 48.
- **Fewer tools, and the missing one.** `get_position_paper` and `get_ruling` become `get_unit`; `follow_citations` is new (a paper or page → the law it cites, and back: the hop the paper experiment showed is missing; the citations themselves are right); `what_changed` moves to phase 6.
- **Intake extracts; at most one question.** Phase 2 (answer rule 5) and phase 4 slice 4 both measured that stating the branches beats asking. This narrows D14's "at most 3 questions" to one, asked only when the answer splits into more than two or three branches.
- **Memory on the client.** The API is stateless; the client sends the "your situation" structure back with each request, so nothing is stored on the server (D14's "session only, never persisted", kept by construction).
- **Verifier cheap-first.** Faithfulness is already 43–48 of 48, so the gain may be small: deterministic checks first (numbers, dates and sections in the answer appear in the cited source, in the wording valid on `as_of`), an LLM claim check only if they miss real errors.

- **Conversation and learning (added 2026-11-23).** A narrow assistant is fine; what makes one feel bad is a clumsy refusal, not feeling like a conversation, and slow answers. So: the client sends the last several turns, not one (sources are still searched fresh each turn); the conversation is saved in the browser with delete and export, and progress is shown while answering (both phase 6). Learning without storing what people ask: production traces already hold the answer kind, the sections retrieved and cited, the top score, timing and cost, but no text; a coarse topic label is added to them, and the question text comes only through the two consent paths of phase 6 (the thumbs-down form and the off-by-default question donation). This learns more slowly than logging everything, which is the accepted price for questions about people's immigration situations.
- **Comparisons in Langfuse (added 2026-11-23).** Tracing so far records production metadata and full-text eval traces, but no scores, so variants are compared in local tables only. Phase 5 adds: the judge's scores attached to eval traces (Langfuse's scores API, from eval commands only; production traces stay metadata-only and unscored, since a real question has no reference answer), and each variant as an experiment over the same golden items, so a per-question side-by-side comes with cost and latency. Only the public golden sets go to Langfuse; holdouts stay private (D17). Plus a production dashboard (p50/p95 latency, cost per question, answer-kind shares; a rising "not settled" share is a warning). Free-plan note: each eval run of 48 questions is a few hundred observations, so repeated runs (three per variant) need watching against the 50K monthly units.
- **The reranker is re-decided after the second search.** Phase 2 measured +0.06 R@5 at ~4 s on a GPU, mostly on agency pages, and no gain on situational law recall; on the VM's 2 CPUs it would likely break the 20 s budget (an estimate). Re-measure it on the current pipeline and time it on 2 CPUs once the second search exists; on only if it still helps within the budget.

**What the eval lacks for an agent:** 4 follow-up items, no multi-turn items, 8 interpretation items. Before building: about 10 multi-turn items and more situational-law items, three runs per variant, a fresh holdout for the final number.

**Budget:** at most about $0.01 and 20 s per question (plain RAG: $0.0012, 8 s). **Stop rule:** if the second search does not beat plain RAG on situational law retrieval, the rest is not built as an agent.

**Build order:** 1. failure analysis by question type and the new eval items; 2. the second-search workflow with `follow_citations`; 3. intake and client-side memory; 4. the verifier; 5. the comparison table, as Langfuse experiments with judge scores; then the reranker re-measured.

### Step 1: where plain RAG fails (2026-11-24)

One run each on the dev, version and interpretation sets (68 questions with sources; the 16 out-of-scope ones left out), at main `b4c9ea6`, local corpus equal to production (history, papers). Answers by Haiku 5.5, judged by Gemini 3.5 Flash-Lite (D19). Retrieval: R@5 dev 0.52, versions 0.67, interpretation 0.12. Answers: dev 40/48 correct, versions 12/12, interpretation 5/8.

**Retrieval decides most of it.** When every expected source reached the answer's context, 35 of 42 answers were good (right kind and judged correct); when one was missing, 11 of 26.

| Question type | n | good | all expected sources in context |
|---|---|---|---|
| identifier (names a section) | 8 | 7 | 8 |
| lookup | 12 | 8 | 5 |
| multi-source | 16 | 9 | 5 |
| not-settled | 14 | 9 | 6 |
| needs-follow-up | 4 | 2 | 4 |
| version-dependent | 12 | 9 | 12 |
| time-dependent | 2 | 2 | 2 |

**Expected source groups in the top 8, by source kind:** law 0.58 (identifier 1.00, multi-source **0.21**), pages 0.35, papers 0.38.

**The 22 answers that were not good fall in three groups:**
- **Retrieval missed a source (10):** a law section, page or paper the answer needed never reached the context, and the answer was wrong, partly right or "not settled" (employer steps 101, union statement 102, new-or-extension 202, spouse income for PUT 401, late extension 803, family employer 804). This is what a second search can fix.
- **Answered where the sources don't settle it (5):** right facts, wrong kind (205, 207, 301, 304 ×2); in 4 a page that shows the open point was missing. Partly retrieval, partly the answer kind.
- **Cautious with every source present (6):** asked a follow-up or said "not settled" although the context held the answer: the 2024 version items 705 ×2 and 706 (the date is in the question; a known generic follow-up behaviour), working while pending 105 and 302-sv (follow-up kinds), plus one partly right (504-sv). Intake that fills in the dates, and the answer prompt, not search.

**What a second search could reach.** Of 33 missed law groups, **14 are cited by a source already in the top 8** (a page, ruling, transitional provision or another section): one citation hop finds them. But following every citation adds a median of 9 new sections per question (p90 26, max 32), so the hop must be selective (the model picks, or code ranks the cited sections against the question). The other **19 are cited by nothing retrieved** (e.g. 5 kap. 7 § for permanent residence, 5 kap. 3 j § for the family employer, 7 kap. 3 § for job loss): they need a different query, the model's rewrite of the search.

**Consequences for the build:** the second search targets the 10 retrieval failures (~15% of questions) and should be measured on them plus no loss elsewhere; `follow_citations` needs a selection step; the 6 over-cautious answers are for intake and the prompt, not the agent loop. One run only: differences of 1–2 answers are within run-to-run variation (phase 4 slice 5).

**New eval items (drafts, for review):** `golden.multiturn.jsonl` (6 scenarios × EN/SV; new optional field `turns`, the earlier questions; plain RAG asks the last question alone, the judge sees all) and `golden.situational.jsonl` (4 × EN/SV, aimed at sections no retrieved source cited). Plain-RAG baseline, one run:

| Slice | R@5 | R@10 | right kind | judged correct |
|---|---|---|---|---|
| multi-turn (12) | 0.04 | 0.15 | 5/12 (5 follow-up questions instead) | 7/12 |
| situational (8) | 0.12 | 0.12 | 5/8 (3 "not settled") | 7/8 |

Without memory the model mostly asks back, which is the right behaviour for a question it cannot place; memory should turn those into answers. Situational answers were often right from Migrationsverket's pages although the law section was missed: retrieval misses there cost citations more than correctness.

**A golden-set error found while drafting.** A work-permit holder's family is granted permits under **4 kap. 4 a § utlänningsförordningen** (a maintenance requirement, its level in 4 c §, no housing-size requirement, the same validity as the worker's permit), not under 5 kap. 3 a § utlänningslagen, which is about families of people resident with permanent residence or a permit with well-founded prospects of long-term residence; 5 kap. 3 j § (housing of sufficient size and standard) applies to the latter. The draft housing item first expected 3 j §; the answer model, faithful to Migrationsverket's page, said a one-room flat is not in itself a problem, and was right. The checked dev item `family-spouse-004` expected 5 kap. 3 a § for a work-permit holder's spouse (its notes had asked to check the ordinance; never done): corrected to 4 kap. 4 a § utlänningsförordningen (dev R@5 unchanged, 0.52; neither section is retrieved for it, Migrationsverket's family pages are). `family-spouse-put-income-401` (5 kap. 3 m § fjärde stycket for such a spouse's permanent residence) was re-checked and holds: three years with any temporary permit, and the 3 a § conditions apply once the worker has permanent residence; its reference answer now gives Migrationsverket's order (the worker first) and the 3 j § requirement.


### Step 2: a second search, measured (2026-11-24) — off by default

**What was built** (`agent/second_search.py`, `prompts/evidence_check.md`; `--second-search` on `ask` and `eval-answers`, `--check-model` to choose the check's model): the agent loop of the plan, without a framework. After the first search (unchanged), a model reads short excerpts of the sources and decides whether they contain the rule that decides the question; if not, it names at most two steps, carried out by deterministic tools that all take `as_of`: `get_section(statute, chapter, section)`, `follow_citations(from)` (the cited sections closest to the question, at most 2, since following all adds a median of 9) and `search_rules(query, sources)` (at most 3 new hits in one kind of source). New sources go after the first search's; any failure (model error, unreadable reply, nothing found) leaves the first search as it was. The second version adds a **table of contents** of the covered chapters (utlänningslagen 5, 6, 6 a–c, 7; utlänningsförordningen 4–6; the law's own subheadings, ~6,000 characters) so `get_section` names real sections, and a stricter rule for "enough" (work out the deciding section first; a page describing the same requirement, or a neighbouring section, does not count).

**Result** (88 questions with sources across dev, versions, interpretation, situational and multi-turn; one judged run each; answers by Haiku 5.5, judge Gemini 3.5 Flash-Lite):

| | Plain RAG | Check: Haiku | Haiku + contents | Sonnet 5.5 + contents |
|---|---|---|---|---|
| Expected law groups in the context | 67/112 | 75 | 85 | **87** |
| Every expected source in the context | 45 | 50 | 53 | 53 |
| Right kind of answer | 62 | 60 | **65** | 60 |
| Judged correct | **71** | 66 | 70 | 66 |
| Steps taken | 0 | 42 | 61 | 102 |
| Cost per question | **$0.0019** | $0.0027 | $0.0031 | $0.0163 |

Haiku as the check model needs reasoning effort `low` and 2,000 tokens: at 800 it spent them all reasoning and replied with nothing. Excerpts are cut short on purpose; without saying so, the check asked for sources it already had.

**Why answers did not follow the search.** With the table of contents, the second search fixed 7 answers and broke 8 (Haiku). The fixes added the deciding section (5 kap. 7 § for permanent residence, 5 kap. 18 § for working while an application is pending, Migrationsverket's page on updating an application). Most breaks are one trap: for a work-permit holder's family the check added the law's general family rules, 5 kap. 3 a §, 3 j §, 3 l § or 3 m § — their headings fit the question exactly — where the ordinance's 4 kap. 4 a § ("Uppehållstillstånd för familjemedlemmar i vissa fall") applies; the answer model then trusted them (housing-size requirements that do not apply, 951; 004; 901; 952). Sonnet as the check model fell into the same trap, at 8.5 times the cost of plain RAG and over the $0.01 budget. The limit is not the model's strength: the law's structure points to a neighbouring rule, and no search step can tell which of several similar rules applies.

**Decision: the agent loop is not built further.** The stop rule's letter (situational law retrieval) was met — +18 law groups — but its purpose was better answers, and those stayed flat. The second search stays as an option, off by default, like the reranker and the paper slots, so it can be re-measured with one flag when something changes (intake, a verifier, a stronger answer model). Phase 5 continues with what does not depend on searching better: intake and memory for follow-on questions (asked alone, 5 of 12 got the right kind of answer), then a verifier that checks that each cited rule fits the situation described — the new bottleneck.

### Step 3: intake and memory for follow-on questions (2026-11-24)

**What was built** (`agent/intake.py`, `prompts/intake.md`): when a question comes with the user's earlier questions (memory, kept by the client: the server stores nothing), one model call (the answer model, reasoning effort low) rewrites it into a self-contained question in the same language, carrying over only facts the user stated and never adding assumptions, and records the user's situation (permit, dates, employer change, family, other; only fields the user mentioned). Search and answer then use the rewrite, exactly as plain RAG does. Without history there is no call, so single questions cost what they did; any failure (model error, unreadable reply, empty rewrite) falls back to the question as asked. At most the last 6 earlier questions are used.

**API:** `POST /ask` takes an optional `history` (at most 6 earlier questions, each validated like `question`: stripped, 1–2,000 characters; more is a 422). The response adds `understood_as` (the rewrite, when there was one) and `situation`, returned only to the asker so they can see and correct what was understood (the editable panel in phase 6). Traces record the number of earlier turns and which situation fields were stated, never the text. Eval: `eval-answers` passes each item's `turns` as history (`--no-memory` asks them alone, the plain-RAG baseline); `ask --earlier "<question>"` (repeatable) does the same from the command line.

**Result** (the 12 multi-turn items, two judged runs each, same corpus and answer model):

| | Plain RAG (asked alone) | With memory |
|---|---|---|
| Expected source groups in the context | 2/30 | **10/30** |
| Right kind of answer | 5, 6 | **9, 8** |
| Judged correct | 4, 6 | **6, 7** |
| Asked back what the user meant | 6, 4 | 0, 1 |
| Cost per question | $0.0016 | $0.0020 |

Fixed: 902 (en and sv; plain RAG asked back, or declined the Swedish one as out of scope), 903-sv, 905 (from asking back to a partly right answer). Unchanged: 903-en and 904, whose wording already carried enough. Worse: 901 (both) and 906-en — without memory the model asked back (judged acceptable); with the full question it tried to answer for a work-permit holder's spouse, was pulled between the law's general family rules and the ordinance's, and said "not settled". The same rule-choice trap as in step 2, now made reachable: the case for the verifier (step 4).

**Decision:** memory on (it costs a call only when there is history). The client sends the history from phase 6.

**Code review (high), all fixed with regression tests:**
- **The answer's language was taken from the rewrite**, which can carry the earlier turns' language into a follow-on asked in another one (a Swedish "och min make?" after English questions could get an English answer). It is now taken from the question as asked.
- **History was formatted into the system prompt**, so a newline in a client-written question could pose as an instruction. The conversation now goes in the user message as JSON (each question on one line), and the prompt says it is data.
- **A rewrite had no length limit.** One longer than any question the API accepts (2,000 characters) is not used; the question as asked is.
- **Model calls were under-counted:** the answer span's `permitnav.model_calls` now counts the intake and second-search calls; `ask` prints how a follow-on was understood and the rewrite's cost; a failed answer in an eval keeps the rewrite it paid for.
- **Placeholder situation values** ("None", "unknown", "N/A", "okänt"…) were shown as if the user had said them; they are dropped, and the prompt asks to leave unmentioned fields out.

A third memory run after the fixes: right kind 11/12, judged correct 8 (within the earlier runs' spread or slightly better).

**Second review (`/ship`, high), fixed:** a client may send a whole conversation (up to 50 earlier questions; only the last 6 are used) instead of getting a 422 from the 8th question; a question echoed back unchanged except for its whitespace no longer counts as rewritten (it was shown back as "understood as"); when memory is skipped, the result and the intake span say why (`model error`, or the reply's finish reason such as `length`), and the rewrite gets 2,500 tokens instead of 1,500; `eval-answers` now uses memory by default, as production does, with `--no-memory` for the plain-RAG baseline (single questions are unaffected: no history, no call); the service builds its rewriter once. Not changed: during a provider outage a follow-on waits for two failing calls (intake, then the answer) before the error, which only matters while everything else fails too.

### Experiment: plain-language descriptions of law sections (2026-11-24) — off by default

**Why the family rule is not found.** For the work-permit spouse questions (004, 901, 951, the production follow-on), utlänningsförordningen 4 kap. 4 a § ranked among law sections: by vector **outside the top 200** (86 once), by keyword 10–12, hybrid 119–141. Its text says whom it is for only through section numbers — "a person granted a permit under 5 kap. 10 § utlänningslagen in cases under 6 kap. 2 § första stycket … a family member under 5 kap. 3 a §" — never "work permit" or "spouse"; its heading ("Uppehållstillstånd för familjemedlemmar i vissa fall") is indexed but does not say *work*. Meaning-based search cannot follow numbers.

**What was tried.** Adding the cited sections' headings ("reference glosses") moved its similarity to the questions only 0.40 → 0.41 (the top law hit scored 0.55–0.62). A model-written plain-language description ("applies when someone who has a temporary residence permit, for example for work, wants to bring close family…") scored **0.56–0.61**, level with the top hit. So: `permitnav describe` (`corpus/descriptions.py`, `prompts/describe.md`, migration 006) describes every statute and transitional wording in Swedish and English with the answer model (1,404 of 1,405, $0.39 once; a reply with notes or a second version is rejected and retried), stores it in `unit_description` (model, prompt hash) and indexes it as chunks of kind `description`. A section found through one is always given to the answer model with its own text (the SQL returns the wording's first text chunk), never the description.

**Result (retrieval, no model cost):**

| | dev R@5 | versions R@5 | situational R@5 | multi-turn (alone) R@5 |
|---|---|---|---|---|
| without descriptions | 0.52 | 0.67 | 0.12 | 0.04 |
| descriptions in the same pool as the text | **0.47** | 0.62 | **0.25** | 0.15 |
| a separate description ranking, fused at weight 0.25–2.0 | 0.49–0.53 | 0.62–0.67 | 0.12 | 0.04–0.10 |

In the same pool the descriptions find the hidden sections (004-sv: 4 kap. 4 a § at rank 3) but make every section sound like an everyday answer — short plain text scores higher against any question than legal text — so general and procedural sections (utlänningsförordningen 3 a kap. 5 §, 5 kap. 1–3 a §; utlänningslagen 14 kap. 3 a §) pushed precise ones out of dev's top 5 (8 items lost, 3 gained), below the CI gate. As a separate fused ranking, dev is safe, but a section found only by its description never gets enough score to reach the top 5, so the situational gain disappears.

**Decision:** off by default (`description_weight = 0`; `--description-weight` to re-measure). The diagnosis stands — law that names its subject only by section number is invisible to embeddings — but making it visible does not tell the system which visible rule to prefer: the same rule-choice problem as the second search, one step earlier. Descriptions are not generated in production. They may still serve the verifier (checking whether a cited rule concerns the person's situation).

### Step 4: a verifier, probed (2026-11-24) — not built

**Question:** can a model, shown the question, the answer and the cited sources exactly as the answer model saw them, tell when a cited rule does not apply to the person's situation or does not say what the answer attributes to it? Probed on 22 answers (memory on; answered, judged and verified in one process so all three see the same sources): 7 judged not good, 15 good. Prompt: check each cited source's own scope (whom and which case it covers) and that it states what is attributed to it; flag only what the sources themselves show.

| Verifier model | Flags on bad answers (want 7) | Flags on good answers (want 0 of 15) | Cost for 22 |
|---|---|---|---|
| Haiku 5.5 | 2 | 6 | $0.016 |
| Sonnet 5.5 | 2 | 9 | $0.23 |

A first round that rebuilt the sources from the database flagged 11 and 13 of 16 good answers: the older wordings and the dates the answer model sees were missing, so a verifier must get exactly the answer's context.

**Why not build it:** the bad answers in this run mostly did not cite a wrong rule; they declined ("not settled") or asked back where the sources answered (804, 953, 954, 906-en, 901-en) — a claim checker cannot fix an answer that makes too few claims. The family questions that cited a neighbouring rule earlier (004, 951, 901-sv) were correct this time (memory, and run-to-run variation). As a gate, a verifier that flags 40–60% of good answers would damage more answers than it fixes. Some of its flags are real mis-citations the judge tolerates (a statement cited to the wrong source in 701, a ruling summarised too loosely in 902), so it may serve later as an offline citation check in evals.

### Phase 5 outcome (2026-11-24)

Built and on: intake and memory for follow-on questions (API `history`, `understood_as`, `situation`). Built, measured and off by default, each re-measurable with one option: the second search (`--second-search`, `--check-model`), plain-language law descriptions (`describe`, `--description-weight`). Probed and not built: the verifier. Measured against plain RAG: memory raised follow-on questions from about half to about three quarters with the right kind of answer; nothing raised judged correctness on single questions. The remaining failures are (1) choosing the right rule among similar ones and (2) the answer kind — declining or asking back when the sources answer. (2) is an answer-prompt calibration problem best tuned on real questions (phase 6 feedback) rather than on this eval set. The agent framework (LangGraph) was not needed: the workflow that stayed on is fixed code (intake → search → answer).


## Phase 6

### Slice 1: the chat UI (2026-11-24)

**What it is.** A small React + TypeScript app (`web/`, Vite) built in the Dockerfile's first stage and served by the API itself at `/` (FastAPI `StaticFiles`, mounted after every API route, only when a build exists). Same origin as `/ask`, so no CORS and no change to Caddy's routing.

- **Access until the beta:** a one-time "access token" screen, checked with `GET /auth-check` (no question, no model call); the token is kept in the browser's local storage and sent with each request. A refused token returns to the screen; "Sign out" also deletes the conversation.
- **Conversation:** kept in local storage only (survives closing the tab on that device; "Delete conversation" removes it); the last 6 earlier questions go to `/ask` as `history`, so follow-ons use phase 5's intake. A failed request puts the question back in the box.
- **Answers:** the answer kind as a label (a question back, not settled, out of scope), paragraphs and lists with clickable `[S1]` markers, the sources listed with their dates, "understood as" for a follow-on and "your situation, as understood" (collapsed), the rules' date and credits, and "Copy answer" (text, sources with dates and links, the rules' date, the answer date and the notice).
- **Sources:** a side panel (over the page on phones) with the quote, its unofficial translation, the full text of the exact wording cited (`POST /source` by `version_id`, which every citation carries: an older wording shares its unit with the current one) and a link to the original.
- **Progress:** a pending line with seconds elapsed ("usually 8–15 s"); real stages come with streaming (slice 2).
- **Notices:** "You are talking to an AI … not legal advice" above the conversation from the first view, and the API's notice in every copied answer.

**Privacy and safety choices.** The page loads nothing from other sites (system fonts, no CDN, no analytics), so no third party learns that someone opened an immigration question; Caddy sends a Content-Security-Policy of `'self'` only, no framing. Caddy's request limit rose from 16 to 64 KB for the history (6 questions of up to 2,000 characters, JSON-escaped). The UI renders model text as text (React escapes it); markers become buttons only when the answer has a citation for them.

**Checked in a real browser before review** (three bugs no unit test caught): a visually-hidden label stretched by the composer's layout rule made the page scroll sideways; an effect written as `useEffect(() => el.scrollIntoView(…))` returned the Promise that `scrollIntoView` now returns, which React later called as a cleanup — the page went blank when an answer arrived (effects now use block bodies); the model's `**bold**` showed as literal asterisks (now rendered, stray ones dropped).

**Code review (high), all fixed:**
- **An older-wording citation opened today's wording** in the panel: `/source` looked a source up by unit and date, but an older wording (kept alive by a transitional provision) shares its unit with the current one. Citations now carry `version_id` and the panel fetches exactly that wording (checked in the browser: 6 kap. 2 § "god försörjning", 2023-11-01 to 2026-06-01, next to its own quote).
- **`/source` is a POST:** with the unit and date in the URL, Caddy's access log would have recorded which provisions each person opened.
- **A conversation deleted while an answer was loading came back** when the answer arrived; a late answer to a deleted conversation is now dropped.
- **An unanswered question stayed in the saved conversation** (token refused, or the tab reloaded while waiting) and was sent as history afterwards; it is taken out on every failure and dropped on load.
- **An intro line straight before its bullets** rendered as one paragraph with literal dashes; lines are now split into paragraph and list runs.
- **Sign out keeps nothing:** signing out on purpose deletes the conversation from the browser (a shared computer); a refused token keeps it.
- Smaller: examples use the chosen "rules as of" date; the source panel has its own error messages; Enter while an input method is composing (Chinese, Japanese, Korean) no longer sends; the UI is served by explicit GET routes (`/`, `/favicon.svg`, `/assets`) instead of a catch-all mount, so unknown API paths still 404 and trailing-slash redirects still work.

**CI:** the web app's type-check and unit tests run inside "Lint, types and tests", its build inside "Build image" (both already required checks, so `deploy/update.sh` and the ruleset need no change); `npm audit` of the runtime dependencies joins the dependency audit.

### Slice 1b: a UX pass after the first real use (2026-11-24)

The first use on production showed the "Rules as of" control as a bare browser date field ("yyyy-mm-dd" next to the word "today") right where questions are typed. Changed, and checked in a browser at 1,288 px (the author's screen), at iPhone 14 width (390 px) and in dark mode:

- **The date is a chip inside the composer:** "Rules as of today", or, once set, a highlighted "Rules as of 15 February 2026 ×"; clicking it opens a small popover that explains what the date does, with the date field opening on today rather than empty. Dates read as people read them ("1 May 2026") in the chip, on questions and in answer footers; the API keeps ISO dates.
- **One composer field:** the text box grows with the text (up to a third of the screen), with the chip and a round send button inside it; "Enter to ask · Shift+Enter for a new line" below it, hidden on touch devices.
- **Phone:** the header keeps one line ("Delete" instead of "Delete conversation"); a shorter placeholder that fits.
- **Reading:** a slightly narrower column (720 px) and more space between paragraphs.

The edges that looked cut off in the first screenshots were the screenshot's crop: the page fits the window exactly at that width.

### Slice 2: progress as the answer is prepared (2026-11-24)

**What:** `POST /ask/stream` returns the same answer as `/ask` as server-sent events: a `stage` event as each step starts — `understanding` (only with earlier questions), `searching`, `checking` (only with the second search), `writing` — then one `answer` event (the AskResponse) or one `error` event with the status and message `/ask` would have returned (no provider text). The answerer reports stages through an optional `progress` callback; `/ask` is unchanged for the CLI, evals and the smoke slice. The UI shows the steps with a check mark per finished step, a spinner on the current one and the seconds elapsed; deleting the conversation cancels the request.

**Stages, not partial text:** the answer model replies in JSON (kind, answer, quotes), and citations and quotes are checked only once the reply is complete; streaming text would show unchecked citations. Searching takes a few seconds and writing most of the rest, so the steps show real progress.

**Production details:**
- The answer runs in anyio's bounded worker threads (as `/ask`'s do) and sends its stages through a memory stream; the request's OpenTelemetry context is attached in the worker, so the streamed question is one trace (`POST` root → `ask` → `answer` …), and the sampler keeps `/ask/stream` as it keeps `/ask`.
- **A client that leaves stops the answer:** the stream is cancelled, which closes the memory stream, so the worker's next stage report fails and it stops before the next model call (a watcher also asks the connection a few times a second, for servers that notice a disconnect only when a write fails). Tested against a real uvicorn server: the test client never reports disconnects.
- The stream is sent with `Cache-Control: no-cache, no-transform` (and `X-Accel-Buffering: no`): proxies must pass events on as they come rather than compress or buffer them; checked on production with a timed request.
- The browser reads the stream with `fetch` and parses the events itself: `EventSource` can neither POST nor send the access token.

**Code review (high), all fixed:** each stream had started its own unbounded thread (now anyio's bounded pool, and no second thread blocked on a queue); a client that left kept the answer running and paying for model calls (now stopped at the next stage); `progress` was stored on the answerer instance, so overlapping calls could cross (now passed through the call); the page jumped to the bottom on every stage; the stream client mapped 404 and error events coarsely and did not release the connection after the answer; the event parser did not accept CRLF or `data:` without a space; `Content-Encoding: identity` is reserved by RFC 9110 (replaced by `no-transform`); the unused non-streaming client call was removed; screen readers heard the seconds counter every second (now only the current step is announced).

Tests: stage order in the answerer (with and without history), the event stream (stages, then the answer; an error as an event without provider text; 401 and 422 before streaming; one trace), and the browser-side parser on a stream split mid-event. Checked in a browser on a live follow-on.

### Conclusion first (2026-11-25)

**Problem (first real use):** answers were correct but read as one flowing text; the conclusion was spread over several sentences of a long first paragraph (median 1,500 characters per answer, first paragraphs up to 1,300), so nothing could be set off as "the answer".

**Considered and dropped:** separate `short_answer` and `next_step` fields. Two texts can contradict each other (and the judge reads the answer as a whole); a field that opens with yes/no pushes against rule 6 (never predict an individual outcome); the citation and quote checks would have to cover every new field; `next_step` would mostly repeat the notice.

**Done instead:** answer prompt rule 9 — the first paragraph is the conclusion for the person's situation, at most two sentences, cited, a paragraph of its own; the details follow. The UI sets the first paragraph off (larger, a rule under it) when it has the shape of a conclusion: at most 350 characters, with details after it and not an intro line before a list (`hasLead`); no label. (As first shipped, any first paragraph was set off, so an old or non-conforming answer did not keep the plain look as claimed here; found in review, fixed.) The eval reports "conclusion first" (first paragraph ≤ 350 characters, cited when the kind is answered; `lead_first`).

**Measured** (old prompt from a clean checkout of main; two judged runs each on dev, multi-turn, situational):

| | Old prompt | v1 | v2 | v3 (kept; dev) |
|---|---|---|---|---|
| Conclusion first | 35% | 85% | 88% | 40/47, 46/47 |
| Right kind (dev) | 53, 54 /64 | 53, 53 | 51, 53 | 54, 54 |
| Not-settled replies (all sets) | 42 | **60** | 38 | — |
| Out-of-scope declined (dev) | 14, 14 /16 | 13, 14 | 12, 13 | 14, 14 |
| Judged correct / wrong (dev) | 43, 43 / 3, 3 | 44, 42 / 4, 4 | 42, 41 / 6, 7 | 41, 43 / 3, 4 |
| Faithful (dev) | 47/50, 47/50 | 46/51, 47/50 | 47/52, 50/51 | 47/50, 46/50 |

- **v1** described what the conclusion says when the sources leave part open and named `not_settled`: the model then chose that kind far more often (42 → 60 replies; right kind on multi-turn 10 → 7–9, situational 5 → 3). A rule about wording moved the kind.
- **v2** made the rule about order only ("rules 5–7 alone decide kind") and asked for a conclusion "as definitely as the sources allow": kinds returned, but out-of-scope questions were declined a little less often.
- **v3** scopes the rule to `answered` and `not_settled` replies (a follow-up keeps rule 5's form, a decline rule 7's): dev matches the old prompt on every measure, with the conclusion first in ~91% of answers. One run on multi-turn (right kind 10/12, correct 7, conclusion first 11/12) and situational (6/8, 8/8, 8/8); smoke slice 12 of 12.

**Known cost:** a wrong conclusion is now more visible than it was inside a paragraph (e.g. 202, where the answer follows Migrationsverket's "new application" wording against the law's "extension"). The phase 5 baselines were measured on the old prompt; comparisons from here on need the new one on both sides.

### Conclusion first: a wording refinement, measured and not adopted (2026-11-25)

An outside review of ten openings found four weak ones — an opening hedge ("Possibly yes."), a follow-on that restated the earlier answer, "The sources do not say…" where the rule is known and only the outcome is Migrationsverket's assessment, and "a well-behaved life" for *vandel* — and asked for labelled sections and a visible supporting passage.

**Tried in rule 9 (wording only):** answer what is asked now; open with the substance, not a hedge or "According to…"; different wording for an individual assessment and for silent sources; short bold labels on detail paragraphs; "conduct requirement".

**Measured** against the live prompt, two judged runs per set (168 answers per column); the final wording differs from the first by two clarifications from code review:

| | Live prompt | Refined, first | Refined, final |
|---|---|---|---|
| Right kind | 140 | 140 | 134 |
| Judged correct / wrong | 117 / 11 | 116 / 12 | 113 / 11 |
| Faithful | 133/140 | 136/142 | 132/141 |
| Conclusion first | 124/134 (93%) | 116 (87%) | 108 (81%) |
| Opens with a bare hedge | 6 | 1 | 2 |
| Opens with "According to / The sources…" | 35 | 26 | 26 |
| Answers with bold labels | 0 | 114 | 116 |
| Cost per question | $0.0020 | $0.0024 | $0.0026 |

**Not adopted.** Some openings read better (the traffic fine, the salary cut, the follow-on) and details gained labels, but the conclusion-first rate — the point of the rule — fell with each added instruction, correctness did not rise, and cost rose 25–30%. A recommendation to ship made after the first run did not survive the second. The lesson of three rounds on rule 9: every sentence added to it moved something else (kinds in v1, declines in v2, the lead rate here), and wording was being judged by reading ten openings while the judge measures only correctness. A further attempt needs a judged "directness" measure first, calibrated against hand labels as the judge was (D19).

**Not adopted from the review, with reasons:** a direct "No" on the one-room flat (the deciding rule, 4 kap. 4 a § utlänningsförordningen, is not among the answer's sources; asserting it would break rule 1); "retrieve again when evidence is thin" (the second search, measured in phase 5: no gain); claim-to-evidence checking as a gate (the verifier probe flagged 40–60% of good answers). The new-employer answer (202) stays wrong for a retrieval reason.

**Kept from this round (code, no prompt change):** the source panel marks the cited quote inside the full text (`locateQuote`: whitespace- and case-tolerant, as the server accepts quotes; only when the quote occurs exactly once), with a pinned heading and a "Go to the cited passage" button instead of an automatic scroll, and is rebuilt per citation. From a late code review of the conclusion-first change (it had been merged unreviewed): the first paragraph is set off only when it has a conclusion's shape (`hasLead`); "Copy answer" and the CLI drop `**`; the eval's `lead_first` splits paragraphs as the UI does. Known and left: the prompt's format note says the conclusion is "always" its own paragraph while rule 9 limits itself to answered and not-settled replies; it was measured with that wording (out-of-scope declines unchanged).

### Slice 3: feedback with consent (2026-11-25)

**The default is unchanged: nothing a person types is stored.** "Report a problem" under an answer opens a form in the page; opening it sends nothing. The form asks "Save this question and answer to help improve the assistant?" and shows everything that would be saved: the question and answer text (both editable), an optional "What was wrong?", the date the rules were asked for (editable, with "Use today's date instead": a person may have set it to the day their permit was granted), the type of answer and the sources it cited. It warns against names, case numbers and identifying dates. Only "Save for review" sends it (`POST /feedback`, which refuses a body without `consent: true`).

**What is stored** (`migrations/007_feedback.sql`, `src/permitnav/feedback.py`): the three texts as edited, the answer's kind, that date, and the ids of the cited units (only ids that exist in the corpus are kept, so the field cannot carry free text). Nothing about the person: no address, token, user agent, conversation or trace id.

**Nothing ties a row to a request.** The day is stored, not the moment; the id is random, not a counter (arrival order would pair rows with log lines); `/feedback` and `/feedback/withdraw` are left out of both access logs (Caddy `log_skip`, a filter on uvicorn's) and write no log line of their own; a failed request too (Caddy writes errors to a second log whatever `log_skip` says, checked against 2.10.2, so `handle_errors` now answers them and the cause goes into the access log line of the other paths); they are not traced (the sampler keeps only questions). Tests assert each of these.

**A guard behind the warning.** Text containing an email address or a Swedish personal number is refused, by the form while typing and again by the server, with a message naming what to remove. Only those two: they can be recognised precisely, and a false alarm stops someone who is trying to help. A personal number must have a real date of birth and a correct check digit, so an organisation number passes, and so do ten bare digits starting 07, the way a mobile number is written (a test holds examples). Names cannot be detected reliably; that is what the review step is for.

**Withdrawal** (beyond the roadmap; consent should be revocable). The browser makes the withdrawal code (32 random bytes) before anything is sent and sends it with the save; the server keeps only its SHA-256. So a save whose reply was lost can still be undone, and saving again with the same code replaces the row instead of adding one: the form never has to claim "nothing was saved" when it cannot know, and says so when the outcome is unknown. The code is written to the browser's storage before the request leaves and removed only when the save or a withdrawal is confirmed. Leaving the form after an unconfirmed save, or deleting the conversation while a save is on its way, withdraws it; if that fails (offline), or the tab was closed first, the next visit withdraws what is still unconfirmed after two minutes. Codes are kept in the browser under their own key, changed one entry at a time and followed across tabs, apart from the stored conversation (which each tab rewrites whole). Deleting the conversation or signing out discards them; the confirmation and both prompts say that the submission can then no longer be withdrawn from there.

**The review queue** is the private `feedback` table: not in the public corpus snapshot (a test pins that), capped at 500 unreviewed rows (checked under a lock). `permitnav feedback list | export --out private/feedback.jsonl | delete <ids>` is the maintainer's side (on the server, `export --out -` and redirect). A case enters the golden set only rewritten into a generic question; the raw row is then deleted.

**From a second review of those fixes (8 findings, all fixed):** an unconfirmed save lost its code on reload, on a failed Cancel, or after a refused retry; Caddy's error log; `consent: 1` was accepted; export had nowhere to write on the server (`--out -` prints it); mobile numbers refused as personal numbers; a misleading "shorten it" message.

**From the first code review (10 findings, all fixed before the first commit):** the unshown date; the counter id and the access logs; "nothing was saved" after a lost reply; a save finishing after the conversation was deleted; a second tab or a refused token erasing the code; free text in `cited`; the cap under concurrent saves; sign-out missing from the confirmation; organisation numbers refused as personal numbers.

**Known limits.** Database backups keep a deleted or withdrawn row for up to the bucket's retention (30 days); the privacy page (slice 4) must say so. Anyone with the access token can submit; rate limiting is slice 5, and with the access-log lines gone it has to count requests itself. The roadmap's separate question-donation switch is not part of this slice.

### What a person rules out is kept out of the search (2026-11-25)

**From the first saved feedback case.** After "What salary do I need to get a work permit?", the follow-on "Just an ordinary work permit, not seasonal or EU Blue Card…" was answered "the sources do not state the salary for a first permit". The law does (6 kap. 2 § utlänningslagen) and the corpus has it; it was not retrieved. Neither half of the search reads "not": BM25 counts the words, and the embedding of "not an EU Blue Card" lies next to "an EU Blue Card". Naming three permit types retrieved the rules of all three (6 a and 6 c kap.), and the two that were ruled out crowded out the one that was meant. The answer model was honest about bad sources.

**Measured before fixing** (a probe, not a golden set: 8 dev questions, each also with exclusions added in one turn and as a follow-on; expected source groups among the top k): control 6/12, one turn 2/12, follow-on 3/12. A pattern, and single questions (which skipped intake) had it too.

**The fix.** Intake's call now returns two questions: the self-contained one, which keeps what the person ruled out and goes to the answer model, and a search question without the alternatives they set aside, which only the search uses (`Intake.search`, `Answerer._answer(search_with=…)`). It is used only when something was ruled out in the conversation (`rules_out`: a word list in English and Swedish). A single question gets the call only then, and is still answered exactly as asked.

**Three versions of the instruction, each measured:**

| | Probe: one turn / follow-on (control 6/12) | Existing golden questions | Follow-ons rewritten (14 items × 6) |
|---|---|---|---|
| 1. "leave out everything they ruled out" | 6/12, 7/12 | two lost an expected source: "never convicted" and "my spouse doesn't work" were stripped as well | — |
| 2. only set-aside alternatives; "keep every other word" | 6/12, 7/12 | none changed | 78/84: the new case was never rewritten (that sentence leaked into the rewrite) |
| 3. two explicit steps: rewrite, then derive the search question from it | 6/12, 6/12 | search changed only for the new case | 84/84 |

Negative facts are the situation ("no job", "didn't take out insurance") and stay; alternatives set aside are what search must not see. Version 3 is what ships.

**Answer quality, before and after, two runs each** (multi-turn with the new case as two draft items, the 14 single questions that contain a ruling-out word, situational; 72 judged answers per arm): right kind 50 → 58, correct 53 → 63, faithful 65 → 68, expected source groups 57 → 62, cost per question $0.0024 → $0.0025. The new case: not settled and judged incorrect in 3 of 4 runs before; answered and correct, with 6 kap. 2 § among the sources, in 4 of 4 after. The rest of the difference is within run-to-run noise (the same arm moved by up to 5 between runs); the claim is that the case is fixed and nothing measured got worse.

**From the code review (8 findings, all fixed): the search question is trusted only as "the question with words taken out".** It is not used when it adds more than two words to the question (a paraphrase, or something put in place of what was removed); a question that names a section is searched in full; when the search question finds nothing above the relevance floor the full question is searched before anything is declined; an unreadable reply to a single question contributes nothing; the reply has room for the question twice (4,000 tokens); a single question reports a stage before its intake call; the second search ranks its candidates by the search question; and the word list covers missing and accented apostrophes ("dont", "don´t"), "cannot", "non-", "apart from". The probe was repeated after these guards: unchanged (6/12, 6/12). The judged runs above were made before them; the search used for every golden question is the same.

**Costs and limits.** A single question containing "not", "no", "without", "inte", "utan"… now makes one more small model call (about $0.0001 and a second) even when nothing was set aside; 14 of the 72 dev and interpretation questions do. Phrasings outside the word list are not covered. The golden items `multi-salary-ruled-out-907` were reviewed and marked checked the same day; the raw submission behind them was then deleted. `permitnav ask` now uses intake for single questions too, as production does.


### Where an answer's time goes, and a faster search (2026-11-25)

**Measured first, on the live site** (five questions through `/ask/stream`, timed on the VM, 2 vCPUs): the search step takes about 3 s for an English question and 1 s for a Swedish one; writing the answer takes 6–8 s; the whole answer 9–11.5 s. So search is about a third of the wait, not most of it. Inside the search step (timed in the API container): translating the question 0.8–1.9 s (one model call), embedding the question and its translation 0.3–0.4 s each, and seven database queries 1.4–1.7 s together.

**The database part had three avoidable costs:**

- **No planner statistics for `chunk.kind`.** The column was added by a migration with a default, which changes no rows, so autovacuum never analysed it. Postgres then guessed that 0.5% of chunks have kind `text` (all do), expected 33 rows instead of 6,564 and joined row by row. After `ANALYZE` on the server the law's vector ranking went from 95 ms to 27 ms and the "other sources" one from 81 ms to 20 ms. `db.migrate` now runs `ANALYZE` whenever it applied a migration.
- **The "Swedish page with no English version" check ran once per page section** (a subquery scanning the unit table 1,500 times): 0.4–0.9 s, the slowest query. The pages that have a live English twin are now worked out once per query.
- **Each keyword ranking joined chunk, wording and unit again for every word of the question** to count how many chunks contain it. The group's chunks are now worked out once and the counts read that.

**Result so far, measured locally** (the 64 dev questions replayed without models, 17,891 chunks): the hit lists are identical before and after (unit, chunk and score for every question), and the time in the database fell from 34 s to 21 s. On the server after the deploy: the database part of an English question's search went from 1.4–1.7 s to about 0.6 s (Swedish: 0.8 s to 0.44 s), the slowest query from 0.4–0.9 s to 0.08 s, and the search step from 3.1–3.9 s to 2.0–2.5 s.

**Not done:** running the translation call and the embedding at the same time would save about 0.3 s and needs threads inside the retriever; left for now. The larger part of the wait is the answer model writing about 1,500 tokens.

**From the code review (5 findings, all fixed; none in the rewritten search SQL's results):** migrations were not each in their own transaction as documented (a bare SELECT opened one around them all, so a failing migration would undo the ones before it); `ANALYZE` is now an optimisation that reports a failure instead of failing the migration and keeping the API from starting; a snapshot import analyses too, since a bulk load after migrating an empty database left the same missing statistics; the per-word counts are a join, not an `IN` that is fast only while the set fits in `work_mem`; and the twin check has tests for a pairing recorded from the Swedish side only, from the English side only, and for a twin that is gone from the site.

### The answer model's writing time: measured, default kept (2026-11-25)

**Question:** writing is 5–9 s of a 7–11 s answer. What decides it, and can it be shortened without worse answers?

**Measured** with the real answer requests of four questions (7–12K tokens in), streamed, so the first visible token is timed:

- **The provider route does not matter.** Five routes for Claude Haiku 5.5 on OpenRouter (Anthropic, Vertex, Bedrock, Azure, Claude Platform on AWS) and OpenRouter's throughput and latency sorting: medians of 6.3–7.6 s for the whole call, within run-to-run spread (single calls ranged 4.6–10.2 s).
- **Hidden reasoning is most of it.** The model thinks before it writes: 700–1,400 of about 1,500 output tokens are reasoning, and the first visible token arrives after about 4.5–5 s. Whole call, median of 20 calls: default 7.6 s, effort `low` 5.8 s, `minimal` 5.4 s; with reasoning off 2.6 s (8 calls), first token at 0.7 s.

**Quality, judged, two runs each on the 64 dev questions** (right kind of 64 / correct of 48 / cost of the run):

| reasoning | right kind | correct | not-settled items declined (of 16) | cost |
|---|---|---|---|---|
| default | 52, 50 | 42, 42 | 12, 11 | $0.125, $0.129 |
| low | 51, 54 | 42, 41 | 11, 13 | $0.113, $0.115 |
| minimal | 53, 51 | 42, 42 | 11, 10 | $0.114, $0.119 |
| off | 48, 44 | 39, 38 | 5, 3 | $0.087, $0.091 |

- **Off is rejected:** it answers most of the questions the sources do not settle, the failure this project is built to avoid, and puts the conclusion first less often (25–31 of about 45 answers, against 39–44).
- **Low and minimal match the default on the dev set.** On the multi-turn set, five runs each: `low` gave the right kind in 59 of 70 answers against 65 of 70 (it declined the two spouse scenarios more often, and one reply was malformed JSON), with the same judged correctness (55 against 54 of 70). On the situational set (two runs) no difference beyond noise.

**Decision: the default stays.** `low` would save about 1.8 s of roughly 9 (and 10% of cost), and what it costs is more "the sources don't settle this" on follow-on questions, the outcome a real user had just reported as a problem. The setting exists (`ANSWER_REASONING_EFFORT`: `none`, `minimal` or `low`, the measured values; unset in production; eval headers and the generation span name it when set) so the question can be asked again with another model or prompt. Showing the text as it is written would not help much either: with reasoning on, the first visible word comes about 4.5 s into the call.

### "Answer not confirmed" replaces "The sources don't settle this" (2026-11-25)

The label for a `not_settled` answer read as a statement about the law, while the kind covers two situations: the rules leave the outcome to the individual case, or the sources PermitNav found are silent, which can also mean the search missed the rule (the case reported on 2026-11-25). The label is now **Answer not confirmed**, with one sentence under it: "PermitNav couldn't confirm a definite answer from the sources it found. The information below may still help, but please verify it with Migrationsverket before relying on it." It is shown as information (the accent colour), not as a warning. The feedback form's "Type of answer" uses the same label. A copied answer starts with the label and the sentence, and `permitnav ask` prints the same label. Wording only: the prompt, the kind's name in the API and the evals are unchanged, so the model's own first sentence (shown as the conclusion) may still say that the sources do not settle the question; that sentence is the model's account of what it found, and changing it is a prompt change. Separate labels for "depends on the individual case" and "not found in the sources" are left until that distinction can be evaluated reliably.

### A daily spend cap for the site (2026-11-25)

**The risk it covers:** a leaked access token. Whoever holds it could run up model costs until someone noticed. Per-person quotas and rate limits wait for accounts (they need someone to count against); this is one limit for the whole site and stays as the backstop behind them.

**What:** every model call the API's service makes (answer, intake, translation) is added to a row per Stockholm calendar day in `spend` (migration 008: dollars, calls, calls without a reported cost, questions declined; nothing about a question or who asked). Before a question's first model call the service compares the day's total with `DAILY_SPEND_CAP_USD` (5 in production, set in `deploy/compose.prod.yml`; unset in development: no cap). At or over it, the API answers 429 with "PermitNav has reached its limit for today. Try again tomorrow.", which the page shows in place of its general error. Questions already in progress finish (the server runs at most 40 at a time), so a day can end that much over. A call whose cost the provider does not report, or reports as zero, negative or not a number, counts as $0.01, so missing prices cannot switch the cap off and a nonsense one cannot close the site; a call that ends in an error counts as $0.03 (up to three attempts, which can still be billed). An amount that could not be written (the database was away) is kept in memory and written with the next call.

**Not counted:** evals and `permitnav ask` (the maintainer's own use; they never go through the service), and feedback requests (no model call).

**Knowing it happened:** the API logs an error for each declined question, and the daily refresh, before anything else, reports every finished day on which questions were declined as an error in `permitnav-refresh` (`permitnav spend --unreported`; a day is marked reported only once its error is logged, so a refresh that failed or never ran does not lose it). An alert of its own mails it ("PermitNav reached its daily spend cap", `infra/monitoring.tf`; until that is applied the line raises "refresh failed"). That is up to a day late, by which time the loss is bounded by the cap. `permitnav spend` shows the last days.

**What it does not cover:** a leaked OpenRouter key, whose holder never calls this server. The server, the maintainer's machine and CI each have their own key (checked by comparing hashes on 2026-11-25); each needs its own credit limit at OpenRouter. `infra/README.md` says how to replace a leaked token or key.

**From the code review (9 findings, all fixed):** a capped day could go unreported when the refresh failed or did not run; failed calls and zero or non-numeric costs were not counted safely; an unwritten amount was dropped; the bound on overshoot was understated; the alert mail would have said "refresh failed".

**Found while building:** the first version raised from inside the transaction that counted the declined question, so the count was rolled back; a test for the count caught it. Tried with the real model and a cap of $0.003: two questions answered, the third declined on `/ask` and as an error event on `/ask/stream`.

### The questions that fail every time: a tabulation and two fixes (2026-11-25)

**Tabulated from the runs already made that day** (reasoning on; 6 runs per dev question, 10 per multi-turn, 4 per situational): 23 of 86 golden questions got the wrong kind or were not judged correct in at least half their runs; 48 never failed. Five groups:

1. **A follow-on dragged the earlier question into its rewrite** (multi-spouse-work-901-en: not correct in 10 of 10). "What salary does my job need?" then "And can my spouse come here and work too?" came out as "…I must know what salary my job needs to pay. Can my spouse come…", and the search returned salary rules only. The intake prompt listed "what they asked before" among the facts to bring in.
2. **Rules the law names only by section number** (the family and maintenance rules of utlänningsförordningen 4 kap. 4 a, 4 c, 4 e §§; 5 questions): rank 75–175 among law sections. The known limit of 2026-11-24 (second search, descriptions: measured, not adopted). Unchanged here.
3. **A page section not found** (employer steps 101, new-or-extension 202, 905; 5 questions).
4. **Judged correct, but another kind than expected** (8 questions, e.g. "can my permit be revoked after a salary cut": a correct answer labelled not settled because the outcome depends on the case; or a page now answers what the item expects to be unsettled). For the maintainer's review of the expectations, not a code fix.
5. **Scope and asking back** (refugee family reunification answered in 4 of 6 Swedish runs; deportation with a work permit; "my first application" answered where a follow-up is expected).

**Fix 1, intake prompt.** A latest question that asks something new comes out as that question with the facts the person stated about themselves, without the earlier question; a question that narrows an earlier one still comes out as the earlier question with the detail (the 907 case). Checked on every multi-turn item's rewrite.

**Fix 1 alone moved the miss, it did not remove it** (multi-turn, 3 runs): the English spouse question now found family pages (right kind 2 of 5 → 3 of 3) but the Swedish one fell from 4 of 5 correct to 0 of 3: the search showed the family section of the *researchers'* page (the English one: the *self-employed* page, with its savings amounts), which does not say that a spouse may work. The rule "show the variant the question names, else the general page" (variants.py) was applied only among the candidates found, and the variants of a section score within a few hundredths of each other, so which of them is a candidate is chance.

**Fix 2, the page to show is looked up.** When a variant group among the candidates has no member of the page to show (the named variant, else the employees' page), that page's section with the same anchor is fetched (one small query, only then) and shown in the group's place, if it matches the question about as well as the section found (cosine at most 0.05 lower): most variants differ by a few words, but some are different content under the same heading (the athletes' "requirements to get an extended work permit" is thirty times the employees' text), and there the found section stays. A variant the question names only to rule it out is not fetched. On the dev set 10 of 64 questions change in the top 8, each a specialised page's section replaced by the employees' one; the retrieval gate is unchanged (R@5 0.52, MRR 0.37).

**Both fixes, judged** (before: the day's default runs; after: 2 runs of dev and situational, 3 of multi-turn, made after the review fixes below):

| set | right kind | correct | judged wrong | faithful |
|---|---|---|---|---|
| dev (128 answers each) | 80% → 84% | 84% → 85% | 9% → 6% | 95% → 97% |
| multi-turn (70, 42) | 93% → 98% | 77% → 76% | 17% → 5% | 94% → 93% |
| situational (16 each) | 56% → 69% | 100% → 100% | 0% → 0% | 88% → 94% |

The target: multi-spouse-work-901-en right kind 2 of 5 → 3 of 3 and correct 0 of 5 → 2 of 3; its Swedish twin unchanged (good). The dev and situational differences are within what two runs can show; the claim is that the target is fixed and nothing measured got worse. Multi-turn "correct" is flat because multi-new-employer-905-en went from 4 of 5 to 0 of 3 (all "partly") with the same rewrite, the same sources and the same answer in substance ("a new work permit, not an extension"): the judge reads that sentence against the reference's nuance differently from run to run. That item belongs to group 3, and its verdicts are not evidence either way.

**From the code review (7 findings, all fixed):** the general page was substituted unconditionally (now only within the margin above); a named variant whose page lacks the section fell back to chance (the general page is asked for too); a variant named to rule it out could be fetched; a follow-on that points back at the earlier question ("How long does it take?") could lose its subject under the new prompt wording (the prompt now names that case); identical text on two pages could make a lookup return nothing; a group's score could come from a section the search did not find; and the prompt's example quoted the golden item word for word (replaced; the rewrites were then checked on all 14 multi-turn items and five probes outside the golden set). `rules_out` moved to `permitnav/negation.py`, since the search uses it too.

### Group 4 reviewed: three expectations changed, four kept (2026-11-26)

The maintainer reviewed the seven questions (both languages each) where the kind of answer differed from the expected one while the judge called the answer correct, with the evidence per question (four runs per language, the passages the answers rest on).

- **Changed, because Migrationsverket's pages now cover what the items had left to practice:**
  - `put-refused-extension-304`: not-settled → lookup (answered). The extension page ("if you are not granted a permanent residence permit, you will keep your temporary residence permit") and the page on a decision about permanent residence are expected sources; the reference leads with them and keeps the legal basis.
  - `work-update-pending-application-207`: not-settled → needs-follow-up. A page covers an extension application (change jobs before the decision, send in the new contract) but not a first one, and the question does not say which.
  - `put-apply-with-extension-302`: kind unchanged; the reference follows the page's wording and that section is an accepted source.
- **Kept, as weaknesses of the system, not of the items:**
  - `sit-salary-cut-954`, `sit-family-housing-951`: correct content labelled "not settled" because the individual outcome is discretionary, although the question asked ("can it be revoked", "is that a problem") is answered by the sources. A reader sees "Answer not confirmed" on a confirmed answer. A candidate for the answer prompt's rule 6.
  - `sit-put-unemployment-benefit-953`: the English answer cannot confirm, because the deciding section (utlänningsförordningen 4 kap. 4 e §) is never retrieved: group 2.
  - `work-extension-unknown-insurance-205`: three of four Swedish answers advise not to submit until the details are found, which no source says and which is risky for an application that must be made before the permit expires.

**Effect on the retrieval gate:** dev R@5 0.52 → 0.58 and MRR 0.37 → 0.41 with no change to the search, because the added sources are ones it already found. The CI thresholds (0.52, 0.35) are left where they are for now: they are floors, and raising them belongs with the next retrieval change.
