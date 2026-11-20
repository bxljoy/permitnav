# Golden set

The questions PermitNav is measured against. The design behind the format is in [`docs/DESIGN.md`](../docs/DESIGN.md) D16–D20.

| File | What | Public? |
|---|---|---|
| `evals/golden.dev.jsonl` | Dev set, 60–80 items. Used for tuning. | Yes |
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
| `expects_not_settled` | The sources don't settle it; the assistant must say so. |
| `expects_out_of_scope` | Asylum, protection or deportation; the assistant must decline and point to Migrationsverket (D18). |
| `authority` | Optional. Which kind of source answers it (D22): `law` (a legal requirement), `agency` (Migrationsverket practice the law doesn't set) or `both`. Results are reported per kind. |
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
| `not-settled` | Discretionary; the sources don't decide it. |
| `out-of-scope` | Asylum, protection, deportation. |

Target for the dev set: at least 6 items of each type except `version-dependent`, plus 6–8 out-of-scope items, with every original in both languages. At least a third of the in-scope items should be `agency` or `both` (D22).

## Unit IDs

The citable units from D2. The validator checks these patterns.

| Source | Pattern | Example |
|---|---|---|
| Statute section with chapters | `sfs:<year>:<number>/<chapter>kap/<section>§` | `sfs:2005:716/5akap/1§` |
| Statute section without chapters | `sfs:<year>:<number>/<section>§` | `sfs:2001:82/11§` |
| Transitional provision | `sfs:<year>:<number>/övergång/<point>` | `sfs:2026:295/övergång/1` |
| Position paper section | `rs:<RS number>/<section>` | `rs:RS/005/2022/3` |
| MIG ruling | `mig:<year>:<number>` | `mig:2023:1` |
| Migrationsverket page section | `web:<sv\|en>/<path>#<anchor>` | `web:sv/arbeta-i-sverige#krav` |

Chapter and section letters are written without spaces: "5 a kap. 3 b §" is `5akap/3b§`.
