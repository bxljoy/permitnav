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
docker-compose.yml          Postgres + pgvector (the app runs natively on the Mac, for the GPU)
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
- **Pages crowd out the law** (slice 5): an English question matches an English Migrationsverket page far better (cosine ~0.78) than the Swedish statute (~0.57), so with pages indexed, statute sections dropped out of the top 10. Phase 1 interleaves law and guidance results (`RetrievalConfig.mix = "interleave"`); law items recovered only partly (R@5 0.46 → 0.29, and R@1 is now always a page). Phase 2's authority-aware fused ranking (D22) and keyword search replace this.
- **Repeated page sections** (slice 5): Migrationsverket repeats identical sections across many pages (the union paragraph is on ~20). Search keeps one per distinct text, and the eval counts an identical copy of an expected section as a hit. Near-identical variants (artist vs employee pages) still compete; phase 2 should collapse them or prefer the page a golden item names.
- **OpenRouter model IDs** for Claude Haiku 5.5 and the Mistral judge are configuration values, to be confirmed against OpenRouter's model list at setup.
