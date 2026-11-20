# PermitNav roadmap

**PermitNav (working name) is a question-answering assistant over Swedish immigration rules.** The first version covers work permits (including the EU Blue Card), family members of work-permit holders, permanent residence (PUT) and EU long-term resident status. Study permits and citizenship come later; asylum, protection and deportation are out of scope entirely. Every answer cites the law section or Migrationsverket page it came from, with the date that source was last checked and the date the rule took effect.

How the key parts work, and why, is in [`docs/DESIGN.md`](docs/DESIGN.md) (decisions D1–D23). Facts about the sources are in [`docs/research/source-facts.md`](docs/research/source-facts.md).

The work is split into **8 phases**, done in order at whatever pace suits. Each phase builds on the one before and ends with a "done when" check; move on when the check passes. Phases 1–7 end in a free public beta (roughly 70–90 hours in total). Phase 8, fine-tuning, starts once the beta has produced real questions to train and test on. The hour figures are rough estimates for spotting a phase that is growing out of hand, not deadlines.

## Why this corpus

- **The test questions come from real cases.** Real questions make wrong answers easy to notice and rule changes easy to spot, so the test set is real instead of invented.
- It is harder than a static corpus in four ways worth showing: **the rules change often**, **which version applies can depend on when you applied**, **the best sources are in Swedish**, and **the sources have a hierarchy** for legal requirements (law beats position paper beats web page), while much of the actual process (advertising, union statements, required insurances, processing times) is set by Migrationsverket's own guidance rather than by law (D22).

**What it will not do:** predict the outcome of an individual case. Where the rules leave the decision to Migrationsverket's judgment, no system can know the result. It can find the exact section, position paper and ruling that apply, and say plainly when the sources don't settle the question. It carries a visible "not legal advice" notice and tells users they are talking to an AI.

## Sources

| Source | Language | Authority | Where |
|---|---|---|---|
| Utlänningslagen (2005:716) | Swedish | Law (highest) | riksdagen.se open data API: `https://data.riksdagen.se/dokument/sfs-2005-716.{text,json,xml,html}`; list of statutes at `https://data.riksdagen.se/dokumentlista/?doktyp=sfs&utformat=json`. The consolidated text comes from Regeringskansliet |
| Utlänningsförordningen (2006:97) | Swedish | Ordinance | riksdagen.se open data API (`sfs-2006-97`) |
| Lag om svenskt medborgarskap (2001:82) | Swedish | Law | riksdagen.se open data API (`sfs-2001-82`) |
| Amending laws as published (SFS) | Swedish | Law; gives in-force dates and transitional provisions | svenskforfattningssamling.se, the official and authentic SFS from 1 April 2018. One sealed PDF per act (the HTML page is metadata only). RSS feed at `/regulations/rss.xml/rss.xml`. No API. Acts before April 2018: printed-SFS PDFs at rkrattsdb.gov.se/sfspdf |
| SFSR amendment register | Swedish | Register (no rule text); in-force date per amending act | `https://rkrattsbaser.gov.se/sfsr?bet=2005:716` (Regeringskansliet). Lists every amending act with the sections affected, its in-force date ("Ikraft") and a transitional-provisions flag. The cleanest source for `valid_from` per amendment. HTML only |
| Migrationsverket's regulations (MIGRFS) | Swedish | Binding regulations, below ordinances (optional) | Listed at migrationsverket.se/om-migrationsverket/styrning-och-uppfoljning/lagar-och-forordningar.html |
| Migrationsverket's legal position papers (rättsliga ställningstaganden) | Swedish | How the agency interprets the law | Still in Lifos (lifos.migrationsverket.se). HTML metadata page (`/dokument?documentSummaryId=…`) plus a text-based PDF. Versioned (e.g. RS/005/2022 version 10.0), so key on RS number and version. New papers come through the RSS feed "Aktuellt från Migrationsverkets rättsavdelning". No API |
| Migrationsöverdomstolen rulings (MIG cases) | Swedish | Precedent | Sök rättspraxis (rattspraxis.etjanst.domstol.se): open JSON API (OpenAPI spec at `/openapi/puh-openapi.yaml`), court code `MIOD`, CC0 licence. Full text as HTML, plus cited SFS sections and cited rulings. Results appear capped at 100 per page. The old rattsinfosok database is retired and blocks all crawling |
| Migrationsverket web pages | Swedish + English | Simplified guidance (lowest) | migrationsverket.se |
| EU directives: long-term residents (2003/109/EC), family reunification (2003/86/EC), Blue Card ((EU) 2021/1883), single permit | English + Swedish | Background for some rules | EUR-Lex |

Start small: Utlänningslagen, Utlänningsförordningen and the English + Swedish Migrationsverket pages for the in-scope topics, including the employer pages, FAQs and processing times (D22). Add position papers and rulings in phase 3, the rest later.

The consolidated statute text on riksdagen.se is a convenience version, not the official one. Use it for the current wording, and the published amending laws for dates and transitional provisions. What the API text already contains, and what the parser must handle:
- **Versions not yet in force** ("/Träder i kraft I:YYYY-MM-DD/") and **versions about to lapse** ("/Upphör att gälla U:YYYY-MM-DD/") appear inline, so one section number can appear twice.
- **Transitional provisions** sit at the end, grouped by amending SFS number.
- Each section ends with the amending law that last changed it, e.g. "Lag (2026:385)".
- It does **not** give past in-force dates per amendment. Take those from SFSR or the SFS PDF.

So `valid_from` and `valid_to` (phase 1) and upcoming changes (phase 4) can be filled largely from these markers, with SFSR for the history.

Crawling manners:
- **Prefer APIs and feeds over crawling:** the riksdagen API, the SFS RSS feed, the Lifos RSS feed and the Sök rättspraxis API.
- **1 request per second or slower, everywhere.** riksdagen.se's robots.txt asks Claude-based agents for `crawl-delay: 1`. No API publishes a rate limit.
- **Respect the blocked paths.** migrationsverket.se disallows `/e-tjanster/`, `/webbfunktion/`, `/lankade-moduler*/`, `/ovrigt/sok*` and `*.html.printable`; use its sitemap at `/sitemapindex.xml` instead. Lifos disallows `/sokning*`, so discover new position papers through RSS, not search pages.
- Cache what you fetch. In the app, show short excerpts with a link to the original instead of republishing whole pages.

Attribution, in the source panel and the app footer:
- "Källa: Sveriges riksdag" for riksdagen open data, and "Källa: Migrationsverket" for Migrationsverket text.
- Never use the Riksdag's or Migrationsverket's names or logos in a way that implies they endorse or run the service. The riksdagen terms explicitly forbid using "Sveriges riksdag" in a service's name or implying it is behind the service.

Details and sources for all of the above: `docs/research/source-facts.md` (checked 2026-11-19).

## Repo rules (set up before the first commit)

- **`git init` with `.gitignore` in place first.** `private/` holds the private eval hold-out and unsanitised questions, and is never committed. Back it up to separate private storage, never to this repo.
- **Open core.** The project is built so a paid service can be added later without relicensing anything:

  | Part | Where | Licence |
  |---|---|---|
  | Crawler, parsers, retrieval, agent, eval harness, sanitised dev set, docs | This repo, **public** | **AGPL-3.0** |
  | Accounts, billing, employer features, change alerts | A separate **private** repo, created only if the paid product goes ahead | Proprietary |
  | Rebuilt statute history, the "what changed" log, the hold-out set, beta questions | The service's database and storage, **never in any repo** | — |

  - **Why AGPL, not MIT:** the core stays open source and readable by anyone, but whoever runs a modified version as a hosted service must publish their changes. That deters a hosted copy competing with the paid service. MIT would allow it, and a licence can't be withdrawn from versions already published, so this is decided before the first push.
  - **Why the data stays out:** the law is public, but the rebuilt history and the change log are the parts that took real effort to produce. They are served through the app, not published as datasets.
  - **The public repo must not depend on the private one.** It builds, runs and passes its evals on its own; the paid parts plug in through the same interfaces the core already uses.
  - An open-source licence does not exempt the app from the AI Act's transparency duties (Article 50; see phase 6).
- **Contributions need a contributor licence agreement (CLA)** from the first outside pull request, e.g. via the CLA Assistant GitHub app. Without one, every contributor's agreement would be needed to offer the core under a commercial licence later.
- **Ship the crawler, not the crawled content.** Raw HTML, PDFs and cached pages stay out of the repo for size, freshness and privacy, even though most of the content is free to reuse:
  - Statutes, MIGRFS, court rulings and authority decisions have no copyright (upphovsrättslagen 1 kap. 9 §). Position papers are formal agency statements and very likely covered too.
  - Migrationsverket's own terms make all **text** on migrationsverket.se free to reuse with attribution. Photos and illustrations are not.
  - Third-party documents in Lifos (other organisations' country reports) are protected. Don't ingest them.
  - Test fixtures may include excerpts of statutes, rulings and Migrationsverket text, with the source and fetch date recorded.
- **No secrets, ever.** `.env.example` only, plus a secret scanner (e.g. gitleaks) as a pre-commit hook and in CI.
- **Runs without a cloud account.** `docker compose up` starts Postgres with pgvector, the API and the front end. Production runs the same compose file.
- **Check the name.** Search for existing products and trademarks called "PermitNav" before publishing; rename if there is a collision. The name is what a hosted copy of the code cannot use, so register it as a trademark if the paid product goes ahead.

## Stack

- **Python + FastAPI**, **PostgreSQL with pgvector** for vectors and full-text search, **React** front end, **Docker Compose** for local and production.
- **LangGraph** for the agent layer (phase 5). Phases 1–2 use no framework on purpose.
- **OpenTelemetry** for tracing, sent to **Langfuse Cloud (EU region)**. Nothing extra to host.
- **MLflow** with a local file store for experiment tracking: every eval run and every trained model. It is a development tool and is not deployed.
- **Prompts live in git**, versioned with the code. One source of truth; MLflow runs record the commit.
- **PyTorch + Hugging Face PEFT (LoRA)** and **scikit-learn** for phase 8 only.
- The LLM, the embedding model and the retriever each sit behind a small interface, so a model or backend can be swapped and every eval re-run unchanged.

---

## Phase 1: Baseline RAG from scratch, with the eval set first

Effort: about 10–12 hours.

Goal: understand every moving part before a framework hides it.

1. **Golden set before code (day 1).** Each item has a question, its expected sources as required groups of alternative units, a short reference answer, and its own `as_of` date. The item format is in DESIGN.md D16–D17, D20. Two files:
   - **Dev set, public, 60–80 items** (`evals/golden.dev.jsonl`). Used for tuning.
   - **Hold-out, private, 30–40 items** (`private/golden.holdout.jsonl`). Never tuned on, never published. Final numbers come from here. It is run **once per milestone** (end of phases 2, 4, 5 and 8), only on a configuration already chosen on the dev set.
   - **Sanitise before anything goes in the public file.** Questions drawn from personal experience are rewritten as generic cases: no real dates, employers, nationalities or family details.
   - Mix the types:
     - direct lookups ("What are the requirements for a work permit?")
     - exact identifiers ("What does 6 kap. 2 § utlänningslagen say?")
     - multi-source ("Can my spouse work while on a family permit tied to my work permit?")
     - **time-dependent** ("What is the salary requirement for a work permit today?")
     - **version-dependent** ("I applied before the rule changed. Which requirement applies to me?"), answered from transitional provisions
     - **needs follow-up** ("Can I apply for PUT?"), where the right first response is a question back: which permit, how long, any gaps
     - **not settled by the sources** (discretionary decisions, individual edge cases). The assistant must say so
     - **out of scope** (6–8 items on asylum, protection and deportation, including spårbyte near-misses), where the assistant must decline and point to Migrationsverket (D18)
     - the same question in English and in Swedish, to test cross-language retrieval. Record which language is the original (D20)
2. **Ingestion:** fetch the statutes from riksdagen.se and the in-scope Migrationsverket pages, for applicants and for employers, in English and Swedish (D22). Store the raw HTML locally with the fetch date and a content hash.
3. **Data model: unit → unit version → chunk** (DESIGN.md D2–D3). A source is a citable unit (a statute section, a heading section, a ruling), each wording of it is a `unit_version`, and chunks are built from versions. The fields below live on the unit or the version, not copied onto every chunk:
   - source type and **authority level** (law / ordinance / position paper / ruling / web page)
   - law number, chapter and section (kap., §) where it applies
   - language
   - URL and **fetched_at**
   - **Time fields:** `valid_from`, `valid_to` (empty while current), `amended_by` (the SFS number shown after each section in consolidated statutes, e.g. "Lag (2023:…)"). Whether a wording is upcoming, applies or no longer applies is computed from these dates, not stored (see DESIGN.md, Phase 1 internals)
   - **Transitional provisions** (övergångsbestämmelser) stored as their own units, linked automatically to both the versions their act introduced and the versions it ended (D4)
4. **Chunking, two strategies to compare:**
   - fixed size (e.g. 500 tokens, 15% overlap) as the baseline
   - **structure-aware:** one chunk per section (§) for laws, one per heading for web pages, with the chapter and page title prepended. Expect this to win
5. **Embeddings + vector store:** **pgvector** with **`bge-m3`**, self-hosted through a Python library behind the embedding interface (D15). It runs natively on the developer's machine (Docker on a Mac can't use the GPU, so only Postgres runs in Docker during development).
6. **Generation:** answer in the user's language, cite the source per claim (section number or page title, with the fetch date), quote the Swedish original with an unofficial translation (D12), state from when the cited rule applies, and refuse when the context doesn't contain the answer. What text goes to the model is in D11. The baseline prompt already allows one follow-up question, "not settled" and declining out-of-scope questions (D21).
7. **Set up the answer model through OpenRouter:** Claude Haiku 5.5 to start (D15). Only your own questions and the golden set are sent during development.

✅ Done when: a question in English gets an answer cited to Swedish law text, `docker compose up` works from a clean clone, and both golden files exist.

## Phase 2: Retrieval evaluation, hybrid search, reranking

Effort: about 12–15 hours.

Goal: a results table that holds up to scrutiny.

1. **Retrieval metrics** (no LLM needed, cheap, deterministic): **recall@k**, **MRR**, **nDCG@k**, using the expected sources from the golden set. Count citable units, not chunks, scored by required groups (D16). Report English and Swedish questions separately, and original and translated questions separately (D20).
2. **Honest statistics.** With 60–80 items, a per-type, per-language cell has about five questions. So:
   - report **bootstrap confidence intervals** on every headline number
   - report per-type results only as counts ("4 of 6"), not as percentages
   - tune on the dev set, then run the chosen configuration **once** on the hold-out and publish that number. The hold-out confirms a choice; it is never used to make one. Later milestones get one run each on the same terms
3. **Hybrid retrieval:** add keyword search (Postgres full-text search with the `swedish` configuration, or `rank_bm25`) and fuse it with the vector results using **Reciprocal Rank Fusion**. Watch "6 kap. 2 §"-style questions improve.
4. **Cross-language experiments:** keyword search can't match an English question to Swedish text. Try translating the query to Swedish first, and compare with vector-only for English questions. Swedish compound words ("arbetstillstånd", "uppehållstillstånd") are a second thing to test.
5. **Reranking:** retrieve top 30–50, then rerank to top 5 with a multilingual reranker (`bge-reranker-v2-m3` locally, or Cohere's multilingual rerank). Measure the gain *and* the added latency. Measure both ways of serving it, because the choice sizes the phase 3 VM: a hosted rerank API (no memory cost, per-call price) or the local model (a few GB of memory, and possibly seconds per query on CPU).
6. **Authority-aware ranking (D22):** when a law section and a web page cover the same legal requirement, prefer the law for the citation and use the web page for the plain-language explanation. For agency-practice questions (steps, union statement, insurances, processing times) the Migrationsverket page is the primary source. Add test questions of both kinds, including ones where the web page is simplified and the law has the full condition, and report results per kind.
7. **Generation evals:** faithfulness (is every claim supported by the retrieved text?), answer correctness, citation accuracy, refusal rate on the "not settled" questions, and whether it asks a follow-up when it should. Use RAGAS *or* a custom LLM judge from a different model family than the answer model, calibrated on ~30 hand-labelled answers, with at least 85% agreement before its scores count (D19).
8. **Answer-model comparison (D15):** run the golden set through Claude Haiku 5.5, Claude Sonnet 5.5 and one Mistral model, judged by a model from a different family. Report per question type, plus cost and latency, and decide which model handles which step (e.g. Haiku for simple steps, Sonnet for the verifier or multi-source questions).
9. **Experiment grid:** chunking (2) × retrieval (vector / hybrid / hybrid+rerank). Log every run to **MLflow** (local file store): parameters (chunking, retriever, k, model versions), metrics with confidence intervals, and the golden-set version as a tag.

✅ Done when: a table like "structure-aware + hybrid + rerank: recall@5 0.62 → 0.91 (95% CI …)", with real numbers, split by question language, plus one hold-out row.

## Phase 3: Deploy it, make it observable, widen the corpus

Effort: about 6–8 hours.

Goal: the same system running on a public URL, traceable and reproducible from code. This phase is small on purpose; if phase 2 ran long, this is where the time comes back.

1. **Hosting:** one small VM in an **EU region** running the same Docker Compose file, with a reverse proxy for TLS (e.g. Caddy). Fixed, low monthly cost and nothing new to learn. It also runs the self-hosted `bge-m3` (about 2–3 GB of memory), so plan for **8 GB of RAM**; add more if the phase 2 reranker runs locally too. No GPU is needed at this volume. Avoid serverless hosting, which would reload the model on every cold start. Provision it from code (Terraform or a short setup script).
2. **Database:** Postgres with pgvector on the same VM, with a nightly dump to object storage. Raw HTML goes to the same storage, one version per fetch.
3. **CI/CD (GitHub Actions):** merging to main deploys. Evals run at two levels:
   - **Every pull request, no API key needed:** unit tests and the retrieval evals, using query embeddings cached in the repo. This works for pull requests from forks, which cannot read secrets.
   - **On merge to main and nightly, with the LLM:** a smoke slice of 10–15 golden cases that must pass (refuses on "not settled" questions, asks a follow-up when it should, cites a section, answers in the user's language). This is the prompt regression test; a prompt change that breaks it is reverted.
4. **Secrets:** in the host environment and GitHub secrets only. Nothing in the repo.
5. **Observability:** an OpenTelemetry trace per request (retrieve → rerank → generate) with tokens, latency and cost per query, sent to Langfuse Cloud in the EU region. Two modes:
   - **Production: metadata only.** No question or answer text; log lengths, timings, token counts and retrieved chunk IDs. This sets up the privacy rules in phase 6.
   - **Eval runs on the golden set: full text.** There is no user data in them, and reading prompts and outputs is how failures get diagnosed.
6. **Spend limit:** set a monthly cap with the LLM provider on day 1.
7. **Add position papers and MIG rulings** to the index and re-run the evals. Check whether edge-case questions improve.

✅ Done when: a deployed URL, a trace for each query, no secrets in the repo, a measured cost per query, and a "with position papers and rulings" row in the results table.

## Phase 4: Freshness pipeline and time handling

Effort: about 16–20 hours, including 6–8 for rebuilding history.

Goal: answers that stay right when the rules change, and that say which version of a rule they describe.

Freshness:
1. A scheduled job (cron on the VM, or a scheduled GitHub Action) checks every source, on this schedule:

   | Source | Daily | Weekly |
   |---|---|---|
   | Statutes (riksdagen API) | Re-fetch the in-scope laws and compare content hashes | — |
   | Amending acts (SFS) | Read the SFS RSS feed; fetch only new acts | Check the SFSR register for in-force dates |
   | Position papers (Lifos) | Read the Lifos RSS feed; fetch only new versions | — |
   | Rulings (Sök rättspraxis) | Ask the API for new MIOD rulings | — |
   | Migrationsverket pages | Re-fetch pages whose sitemap "last modified" date changed | Re-fetch all in-scope pages, in case the sitemap missed a change |

   Daily is enough: amending acts are published weeks or months before they take effect and are stored as upcoming versions (D9), so a change is usually indexed before it applies. A typical daily run reads feeds and compares hashes, downloading only a handful of documents at 1 request per second; more frequent runs would add load on government sites for little gain.
2. Changed documents are re-parsed and compared **per unit**: only units whose text changed get a new version, new chunks and new embeddings. The old version is kept with its dates and gets a `valid_to`.
3. A **"what changed" log** records which page or section changed, when, and a short diff.
4. The eval suite runs after each re-index. A drop in scores raises an alert.
5. **Crawler failures raise an alert too** (a site down, a changed format, a parser error). The index keeps serving the last good versions, and answers still show their fetch dates.
6. **Drift check:** re-ask every golden item as of today and list those whose expected units have a newer version since the item's `as_of`. Reviewed items get a fresh copy dated today; the old copy stays as a historical test (D17).

Time handling:
1. **`as_of` per conversation** (D8), defaulting to today. Retrieval returns only the versions that apply on `as_of`, so repealed text is never cited as current.
2. **Upcoming changes and future dates:** ingest amending laws that are passed but not yet in force, with their `valid_from`. Answers on an affected topic mention the coming change and its date. A future `as_of` up to 2 years ahead uses these versions, with a "more changes may still be decided" note (D9).
3. **Transitional provisions:** linked both ways (D4) and always included in the context when either version is retrieved.
4. **Version selection:** when a transitional provision depends on the application or decision date, the assistant asks for that date, then answers from the version that applies and says so.
5. **Rebuild history back to 1 January 2023** for the in-scope sections of utlänningslagen and utlänningsförordningen, walking the SFSR register and the amending acts backwards from today's text. Test rebuilt versions against lagen.nu; never cite it. For an earlier `as_of`, say the period isn't covered (D10).
6. **Non-statute dates:** position papers by decision date and version; web pages by `first_seen` / `last_seen`, excluded before they were first seen (D5).
7. Run the version-dependent golden questions as their own eval slice.

✅ Done when: one real source change is caught by the re-crawl and shown in the log, history back to 2023 is rebuilt and passes the lagen.nu check, and the version-dependent questions are answered from the right version with the date stated.

## Phase 5: Agentic layer with a named framework

Effort: about 12–15 hours.

Goal: tool use, memory and a verifier, with evidence of when they help.

Use **LangGraph**. (LlamaIndex is the alternative if its retrieval abstractions appeal more.)

1. **Tools:** `search_rules(query, filters)`, `get_section(law, chapter, section, as_of)`, `get_position_paper(id)`, `get_ruling(id)`, `what_changed(topic, since)`. All deterministic.
2. **Intake step:** before answering a situation-dependent question, the agent fills a fixed "your situation" structure (permit type, start date, application date, employer changes, family members, `as_of`). It asks only for fields the retrieved rules depend on, at most 3 questions (D14).
3. **Agent loop:** plan → retrieve → check whether the evidence is sufficient → retrieve again or answer. Cap the steps.
4. **Memory:** the "your situation" structure, shown to the user as an editable panel, so follow-ups work ("and for my spouse?"). Session only, never persisted (D14).
5. **Verifier (the only second agent):** checks every claim against the Swedish text it cites, at the version that applies on `as_of`; flags when a web page and the law disagree (D6) and when a ruling interpreted an older wording (D7). A failed claim gets one retry, then is removed; if nothing is left, the answer says the sources don't settle it (D13). No router or separate researcher agent; add them only if the evals show a need.
6. **Evaluate against phase 2 plain RAG** (which has the same prompt, D21) on the same golden set, especially the multi-source, version-dependent and needs-follow-up questions. Report cost and latency too, e.g. "agentic helped multi-source questions by X but cost 3× and added 4 s".

✅ Done when: the results table has a "plain RAG vs agentic" row, and the graph is drawn in the README.

## Phase 6: Front end, feedback loop, privacy and safety

Effort: about 10–12 hours.

1. **Front end: one small React chat UI** (no separate Streamlit prototype). Streaming answers, clickable citations that open the source text with its fetch date and in-force date, a "show retrieved chunks" debug panel, and the "what changed" log as its own page.
2. **Feedback → eval set, with consent.** The default is that nothing a user types is stored. Thumbs down opens a form that:
   - asks explicitly: "Save this question and answer to help improve the assistant?"
   - shows the exact text that will be saved and lets the user edit it first
   - warns against including names, case numbers or dates that identify them
   
   Saved cases go to a private review queue. A case enters the golden set only after manual review and rewriting into a generic form; the raw submission is then deleted.
3. **Optional question donation, off by default.** A separate switch: "Let my questions be used to improve the assistant." When on, question text only (no answers, no session link, no IP) goes to the same private review queue. This is the only source of real questions for phase 8.
4. **Privacy:**
   - immigration questions can reveal sensitive personal data (origin, family, health, asylum grounds). No accounts, no question text in logs or traces, no stored situations beyond the session, unless the user saved feedback or switched on question donation
   - **Move the answer models off OpenRouter** to a path that meets D15's criteria before anyone else uses the service: Claude through Amazon Bedrock's EU profile with retention off, or Mistral's own EU-hosted API. Check what the provider retains for abuse monitoring and turn it off where possible
   - a plain-language privacy page that says all of this
5. **Safety:**
   - a clear "not legal advice; check with Migrationsverket" notice, an "you are talking to an AI" notice, and the source date on every answer
   - no outcome predictions for individual cases
   - prompt injection in crawled pages: treat retrieved text as data, and test with planted instructions
   - rate limiting per IP and a **hard daily spend cap** that switches the service to a "back tomorrow" page
6. **EU AI Act (requirements, not just notices):**
   - PermitNav is **not high-risk** under Annex III point 7, which covers only systems used by or on behalf of public authorities. Record this reasoning in the README.
   - **Article 50(1)** has applied since 2 August 2026: show the "you are talking to an AI" notice clearly, in an accessible way, at the first interaction.
   - **Article 50(2)** requires machine-readable marking of generated answers, and the Digital Omnibus did not postpone it for new systems. Decide how before the public beta, e.g. a flag in every API response plus page metadata. How this applies to an app built on a third-party LLM is unverified; check for Commission guidance first.

✅ Done when: the UI works end to end, a saved feedback case has travelled through consent → review → golden set, an injection test passes, and the AI notice and machine-readable marking are in place.

## Phase 7: Public beta and write-up

Effort: about 6–8 hours.

1. **Free public beta.** No sign-up, hard spend cap, rate limits. Share it where immigrants to Sweden already ask questions. Real questions provide usage numbers and, through the consented feedback path, new eval cases.
2. **README** structured like a client delivery: problem → architecture diagram → eval method → results table with confidence intervals and the hold-out number → failure modes found → freshness and time handling → cost per 1,000 queries → what comes next.
3. **Blog post:** "RAG over rules that keep changing, in a language the user doesn't ask in".
4. Once the beta has run for a while, publish the usage numbers and a write-up of notable rule changes it caught. The "what changed" log itself stays a page in the app, not a downloadable dataset (see open core in the repo rules).

✅ Done when: the repo is public, the beta is live, and someone other than the author has used it.

---

## Diagnosing retrieval problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Right section never in top-k | chunk splits the answer; embeddings miss section numbers | structure-aware chunking; hybrid keyword search |
| English questions do worse than Swedish | keyword search can't cross languages | translate the query; multilingual embeddings and reranker |
| Right section retrieved but ranked low | no reranker; top-k too small for the reranker | retrieve 50, rerank to 5 |
| Answer cites a web page where the law says more | no authority ranking | authority level in metadata; prefer law for citations |
| Answer was right last month, wrong now | stale index | scheduled re-crawl, content hashes, dated answers |
| Answer gives the new rule to someone covered by the old one | no version selection; transitional provisions not retrieved | time fields, linked transitional chunks, ask for the application date |
| Confident answer to a discretionary question | prompt doesn't permit "not settled"; no evidence check | refusal instruction + verifier |
| Generic answer to a personal question | no intake | ask for permit type and dates first |
| Good retrieval, bad answer | too much context; lost-in-the-middle | fewer, better chunks; order by relevance |
| Scores jump between runs of the same config | eval set too small; judge noise | confidence intervals; repeat judge runs; grow the set |

## Phase 8 (after the beta): When fine-tuning beats prompting

Effort: about 16–20 hours.

Goal: a measured answer to "when is classical ML or fine-tuning the right tool here, and when isn't it?" A negative result is a good result if it is measured.

**Start condition:** about 200 donated or feedback questions from the beta, reviewed and rewritten into generic form. With fewer, run the phase on synthetic and hand-written questions and say so in the write-up; expect weaker conclusions.

1. **Build the data, in three separate parts:**
   - **Training set:** (question, relevant chunk, hard-negative chunk) triples from real beta questions, plus synthetic questions an LLM writes from statute sections, in Swedish and English. Synthetic questions echo the section's wording, so paraphrase them and keep the real-to-synthetic ratio as a logged parameter.
   - **Fine-tuning test set, 200–300 items, hand-checked,** real questions first. The 30–40 item hold-out is too small to detect a gain of a few points, so this set carries the comparison. Never trained on.
   - **Routing labels:** 200–300 questions hand-labelled "needs follow-up before answering: yes / no". Budget an evening or two for labelling.
   - Check for near-duplicates between the training set and both test sets. The golden hold-out stays untouched until the final milestone run.
2. **Fine-tune the reranker with LoRA (PEFT, PyTorch).** Start from `bge-reranker-v2-m3`; train adapters only, on a laptop GPU or a short rented GPU session. Compare with the base reranker on the fine-tuning test set: nDCG@5 and recall@5 with confidence intervals, split by question language, plus latency and model size.
3. **Classical ML for one routing decision.** Train a logistic regression (scikit-learn) on question embeddings to predict "needs follow-up". Compare with asking the LLM to decide: accuracy, latency and cost per 1,000 questions. Optionally try a second label: question type (lookup / multi-source / version-dependent).
4. **Track and ship it simply.** Log training runs and models to MLflow. A model that wins is baked into the Docker image and tagged with its MLflow run ID; production does not depend on an MLflow server.
5. **Decision write-up.** One page: what fine-tuning gained and where (e.g. Swedish legal terms vs English questions), what it cost to build and run, when prompting or an off-the-shelf model was good enough, and what would change the decision (more traffic, a new language, a corpus shift). Two costs to state plainly: a tuned reranker **cannot use a hosted rerank API**, so it commits the project to serving the model itself, and it needs retraining when the sources shift. Include when you would *not* fine-tune: little labelled data, fast-changing sources, no one to retrain it.

✅ Done when: the results table has "base vs LoRA reranker" and "LLM vs classifier routing" rows with confidence intervals, one hold-out milestone run confirms the chosen configuration, and the decision write-up is in the README.

## After the beta: is there a paid product?

Decide only after seeing real usage. The open-core split in the repo rules means nothing has to be relicensed or moved if the answer is yes: the paid features go in a new private repo on top of the public core. Open questions to answer first:

- **Who pays.** Individuals have a one-off need and free alternatives. Employers, HR teams and relocation firms hiring from outside the EU have a recurring one. The feature most likely to be worth paying for is **change alerts** ("a rule affecting your employees changed"), built on the "what changed" log, not the chat.
- **Liability.** A "not legal advice" notice gives limited protection when consumers act on a wrong answer. Get a lawyer's view before charging anyone.
- **GDPR.** A paid product needs accounts, which means stored personal data: a legal basis, processor agreements, retention rules and probably a data protection impact assessment.
- **EU AI Act.** Not high-risk, and the transparency duties are already met from phase 6 (see `docs/research/source-facts.md`). Re-check if the product starts serving public authorities, or if the rules change.
- **Missing pieces**, all in the private repo: authentication, per-customer cost caps, abuse handling, billing, terms of service, support.
- **Trademark.** Register the name before launch (see repo rules).
- **AGPL obligations for yourself.** As the copyright holder you can run the core in the paid service without publishing the private parts, provided the CLA covers every outside contribution. Check this with the lawyer above.
- **Running cost.** Price it from the measured cost per 1,000 queries.

## Later, optional

- More sources: citizenship rules, study permits, EU directives.
- A third question language (Chinese) to push the cross-language tests.
- A managed vector search service, if pgvector stops being enough at the corpus size reached.
- **Applicants' experience reports** (after the beta). Much of what users care about (real processing times, what case officers ask for, how edge cases are handled) is practice that no official source publishes. Collect it from users of the app, not from social media:
  - **Not scraped from social media.** Posts reveal sensitive personal data (nationality, family, health, asylum history), platform terms restrict collecting them, their accuracy can't be checked, and quoting them republishes other people's writing.
  - **Opt-in and structured.** After using the assistant, a user may share their outcome in fixed fields: permit type, month applied, month decided, outcome, whether more documents were requested and about what. No names, no free text.
  - **Shown only as aggregates**, e.g. "reported by 23 users: median 5 months; 40% were asked about insurances", and only once there are enough reports (e.g. at least 10) that no one can be identified.
  - **Kept apart from the rules.** A separate, clearly labelled "What other applicants reported" section, dated and marked unofficial. Reports are never retrieved as sources for legal or practice claims, so the verifier's guarantees are unchanged. This is the "reported practice" source that D23 defers.
  - **Expiring.** Reports older than one to two years drop out, since practice changes.
  - **Before building:** a lawyer's view on GDPR (legal basis, retention, anonymisation). The aggregated reports stay out of the public repo, like the rest of the service data (see open core in the repo rules).
