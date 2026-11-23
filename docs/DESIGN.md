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

- The agent fills it during intake. Intake asks only for fields the retrieved rules actually depend on, and at most 3 questions.
- The user sees it as a small editable panel.
- It lives for the session only and is never persisted. Because the fields are fixed, the privacy page can say exactly what is held.

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

