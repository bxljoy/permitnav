# PermitNav design

The design decisions behind [`ROADMAP.md`](../ROADMAP.md). The roadmap says what gets built and in what order; this file says how the key parts work and why. Facts about the sources (formats, APIs, copyright, crawling rules, the AI Act) are in [`research/source-facts.md`](research/source-facts.md).

Decided 2026-11-19. Each decision has an ID (D1–D21) so code, issues and commits can refer to it. To change a decision, edit it here and add the date and the reason.

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
 └─ unit_version   one row per wording: text, valid_from, valid_to, status, amended_by, evidence
     └─ chunk         built from a unit_version: text, embedding, tsvector
```

- **`unit`:** the stable ID from D2, plus fields that never change: source type, authority level (law / ordinance / position paper / ruling / web page) and language.
- **`unit_version`:** the text as it reads in one period.
  - `valid_from` and `valid_to` (empty while current).
  - `status`: `in_force`, `upcoming` (passed but not yet in force), `repealed` or `expired`.
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

- The answer model's provider is chosen in phase 1, on two criteria: processing in the EU, and whether retention for abuse monitoring can be turned off. No provider's EU options have been checked yet.
- **Recommended:** Claude Sonnet 5.5 for answers, and Claude Haiku 5.5 for cheap steps such as query translation and routing. If Claude can't meet the EU requirement, use another provider with EU hosting.
- The judge comes from a different model family (D19).
- Embeddings are chosen separately, in phases 1–2 (multilingual: `text-embedding-3-small` vs `bge-m3`).

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
