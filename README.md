# PermitNav (working name)

A question-answering assistant over Swedish immigration rules. Every answer cites the law section, position paper, court ruling or Migrationsverket page it came from, with the date the source was checked and the date the rule took effect.

**Status:** phase 1 (baseline RAG) in progress. The design is done; the code is just starting.

The first version covers work permits (including the EU Blue Card), family members of work-permit holders, permanent residence (PUT) and EU long-term resident status. Asylum, protection and deportation are out of scope.

> **Not legal advice.** PermitNav will be an AI assistant that points to the rules; it cannot predict the outcome of an individual case. Always check with [Migrationsverket](https://www.migrationsverket.se).

## Documents

- [`ROADMAP.md`](ROADMAP.md): what gets built, in 8 phases, each with a "done when" check.
- [`docs/DESIGN.md`](docs/DESIGN.md): the design decisions (D1–D23) and why.
- [`docs/research/source-facts.md`](docs/research/source-facts.md): facts about the sources (APIs, formats, copyright, crawling rules, the EU AI Act), checked against primary sources.

## Development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```
cp .env.example .env               # then fill in the keys
docker compose up -d --wait db     # Postgres + pgvector on localhost:5432
uv sync --extra embed --extra api  # Python 3.12 and dependencies
set -a; source .env; set +a        # load the environment
uv run permitnav migrate           # create the schema
uv run pytest                      # tests (database tests need DATABASE_URL)
uv run ruff check . && uv run pyright
```

For development the CLI runs natively, not in Docker, so the embedding model can use a Mac's GPU.

### Run the API

```
docker compose up -d --wait        # Postgres + the API on localhost:8000 (first start
                                   # downloads bge-m3, ~2.3 GB, into a volume)
curl -X POST localhost:8000/ask \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"question": "What salary do I need for a Swedish work permit?"}'
```

`POST /ask` takes `{"question": …, "as_of": "YYYY-MM-DD"}` (the date defaults to today) and
returns the answer kind, text, citations with dates and links, source credits and the AI
notice; a failed model call or an unreadable reply is a 502. `GET /health` checks the database. Every `/ask` needs the `API_TOKEN` from `.env`;
the service is private until the public beta. The corpus must be ingested and indexed first
(`permitnav fetch`, `ingest`, `index`).

## Licence

[AGPL-3.0](LICENSE). Contributions require agreeing to a contributor licence agreement; see the repo rules in the roadmap.
