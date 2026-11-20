# Provider facts for PermitNav: embeddings, answer models, EU processing

All facts checked on **2026-11-20** against primary sources: providers' own pricing pages, docs, data and privacy terms, Hugging Face model cards and API, the raw MTEB results repository, and EU data protection authorities' own sites. Where a primary source did not answer a question, this is marked **unverified**. Prices are list prices in USD per 1M tokens unless stated otherwise. This research feeds DESIGN.md D15 and the phase 1–2 embedding comparison. It does not change either document.

## Summary

| # | Question | Answer | Confidence | What it means for PermitNav |
|---|---|---|---|---|
| 1 | Do cheaper embedding models hurt Swedish retrieval? | **Price does not track quality.** On Swedish and English→Swedish retrieval tasks in MTEB, the open `bge-m3` (free to self-host, $0.01/1M via OpenRouter) scores **at or above `text-embedding-3-large`** ($0.13) and clearly above `text-embedding-3-small` ($0.02). `text-embedding-3-small` is the weakest of the models checked. `Qwen3-Embedding-8B` and `gemini-embedding-001` score highest. See the table in A.1. | High for the benchmark numbers (read from raw results). Medium for how they transfer to Swedish legal text: none of the tasks is legal. | Do not choose `text-embedding-3-small` on price. Compare `bge-m3` with one stronger model on the golden set (D16). |
| 2 | Embedding cost at PermitNav scale | **Under about $6/month for every hosted option checked**, even in a month with five full re-embeds of a 5M-token corpus. Embedding cost is not what decides the choice. See C. | High (arithmetic on verified prices). Volumes are assumptions. | Choose on quality and privacy. Cost only matters for the answer model. |
| 3 | DeepSeek embeddings | **DeepSeek has no embeddings API.** Its docs and pricing page list chat models only (`deepseek-flash`, `deepseek-v4-pro`). | High | The DeepSeek key cannot be used for embeddings. |
| 4 | OpenRouter embeddings | **Yes.** `POST /api/v1/embeddings`, with 38 models listed on 2026-11-20, including `baai/bge-m3`, `qwen/qwen3-embedding-8b` and `-4b`, `intfloat/multilingual-e5-large` (all $0.01–0.02), OpenAI, Voyage 4, Cohere Embed 5, Gemini and Mistral Embed at the providers' own prices. EU-only routing (`eu.openrouter.ai`) needs a **Business or Enterprise** plan. | High | Useful for quick experiments with open models without hosting them. It adds a processor (OpenRouter) plus an unverified-location backend host (DeepInfra, Parasail, Nebius). Not the production path for user queries. |
| 5 | EU processing for embeddings | **Mistral** (EEA by default), **OpenAI** (`eu.api.openai.com`, EU data-residency project, eligibility via sales), **Voyage via MongoDB Atlas** (`eu.ai.mongodb.com`; which models it serves is unverified), and **self-hosting** in an EU VPS. Cohere, Jina and the plain Gemini API document no EU-only option for the standard API (unverified). | High for Mistral and OpenAI. Medium for Voyage. | Self-hosting `bge-m3` or a Qwen3 model removes the question entirely, since query text never leaves PermitNav's server. |
| 6 | Claude with EU-only inference | **Not on the Anthropic API itself:** `inference_geo` accepts only `"us"` and `"global"`, and workspace geo only `"us"`. **Yes on Amazon Bedrock** (EU cross-region inference profile; Sonnet 5.5 and Haiku 5.5 have no single-region EU endpoint) and on **Google Cloud (Google managed)** via the `eu` multi-region (Sonnet 5.5 and Haiku 5.5 GA). Both carry a **10% premium** over global. | High | Use Bedrock (EU profile) or Vertex (`eu`) for Claude. On Bedrock, set `data_retention_mode: none`. |
| 7 | Claude zero data retention | Anthropic API: ZDR is **by arrangement through sales**. Sonnet 5.5 and Haiku 5.5 are **not** "Covered Models", so they are eligible. Default without ZDR: inputs and outputs deleted within **30 days**. Bedrock: Sonnet 5.5 and Haiku 5.5 are not on the retention-required list, and an account can enforce `data_retention_mode: none` itself. | High | Bedrock gives ZDR-equivalent behaviour without a sales contract. |
| 8 | DeepSeek API for user questions | **No.** The privacy policy says data is collected, processed and stored **in the People's Republic of China**. The API terms are governed by PRC law. No DPA is offered. Italy's Garante restricted DeepSeek in January 2025. In June 2025 the Berlin DPA found DeepSeek in breach of Art. 46(1) GDPR (transfers to China) and reported the app to Apple and Google as illegal content. | High for the policy text and the authority actions. | Do not send user questions or any personal data to the DeepSeek API. |
| 9 | Mistral as an EU answer model | Data hosted in the **EEA by default**. ZDR available on request for pay-as-you-go, stateless endpoints. **Mistral Large 4: $0.70 in / $2.10 out**; **Mistral Small 4: $0.15 / $0.60** (docs pricing page). Mistral's marketing page quotes Large at $0.50 / $1.50, which conflicts. | High for hosting. Medium for the Large 4 price. | A credible EU fallback answer model, and a different model family for the judge (D19). |

---

## A. Embedding models

### A.1 Swedish and cross-language retrieval evidence (MTEB raw results)

**Source:** raw per-task result files in the MTEB results repository, https://github.com/embeddings-benchmark/results (`results/<model>/<revision>/<task>.json`), downloaded 2026-11-20. These are the files the MTEB leaderboard (https://huggingface.co/spaces/mteb/leaderboard) is built from. The leaderboard itself is an interactive app that could not be read as text, so the numbers below were extracted from the raw files directly. The Scandinavian Embedding Benchmark has "moved to MTEB" (https://kennethenevoldsen.com/scandinavian-embedding-benchmark/; paper https://arxiv.org/abs/2406.02396). Its Swedish retrieval tasks are `SwednRetrieval` and `SweFaqRetrieval`.

Metric: nDCG@10 × 100 (the tasks' main score). "–" means no result file for that model and task.

| Model | Swedn (sv) | SweFaq (sv) | Wikipedia sv | Belebele sv→sv | **Belebele en query → sv passage** | Belebele sv query → en passage | MIRACL-HN, 18 langs (no sv) |
|---|---|---|---|---|---|---|---|
| gemini-embedding-001 | **85.8** | **84.7** | **95.1** | 96.8 | 96.3 | 96.4 | 70.4 |
| Qwen3-Embedding-8B | 83.9 | 78.1 | 93.6 | **99.1** | **98.8** | **99.3** | **70.6** |
| text-embedding-3-large | 82.2 | 81.6 | 93.5 | 94.0 | 94.1 | 95.9 | 56.9 |
| voyage-multilingual-2 | 81.6 | 83.8 | – | – | – | – | – |
| bge-m3 | 81.0 | 76.6 | 91.8 | 95.0 | 92.9 | 92.8 | 69.6 |
| snowflake-arctic-embed-l-v2.0 | 80.4 | 78.7 | 92.2 | 94.1 | 92.0 | 90.6 | 66.5 |
| Qwen3-Embedding-4B | 80.2 | 72.7 | 92.7 | 95.6 | 93.8 | 95.9 | 69.5 |
| Cohere embed-multilingual-v3.0 | 80.0 | 77.9 | 92.9 | 95.6 | 93.8 | 95.1 | 68.0 |
| jina-embeddings-v3 | 79.8 | 78.3 | 91.0 | 93.6 | 91.8 | 92.5 | 62.6 |
| multilingual-e5-large | 79.2 | 80.0 | – | 96.1 | 93.9 | 94.5 | – (1 lang only) |
| multilingual-e5-large-instruct | 78.1 | 76.5 | 93.4 | 94.8 | 93.5 | 94.2 | 57.7 |
| jina-embeddings-v5-text-small | 77.8 | 74.9 | 92.0 | 94.3 | 93.1 | 94.8 | 66.6 |
| **text-embedding-3-small** | 73.5 | 74.1 | – | 90.8 | 90.0 | 93.9 | – (1 lang only) |
| Qwen3-Embedding-0.6B | 71.2 | 67.7 | 88.6 | 90.4 | 85.8 | 91.2 | 61.2 |
| embeddinggemma-300m | – | – | 90.8 | 91.0 | 89.1 | 93.2 | 66.2 |
| Cohere embed-v5.0-pro / -fast | – | – | – | – | – | – | 65.5 / 64.2 |
| Cohere embed-v4.0 | – | – | – | – | – | – | 64.0 |
| voyage-4-large / voyage-4 / -lite | – | – | – | – | – | – | 62.1 / 60.6 / 57.1 |
| EmbeddingGemma-Scandi-300m (community) | 55.4 | 54.4 | – | – | – | – | – |
| KBLab sentence-bert-swedish-cased | – | – | – | – | – | – | – |

How to read it:
- The **"en query → sv passage"** column is the closest public proxy for PermitNav's main case: an English question against Swedish statute text.
- **Swedish results are missing for the newest hosted models** (Voyage 4, Cohere Embed 4/5, Gemini Embedding 2, Mistral Embed). Their Swedish quality is **unverified**. Mistral Embed has no folder in the results repository at all.
- Results were produced with different MTEB versions (1.12 to 2.26) and, for MIRACL, two dataset revisions. Small differences (about 1 point) are not meaningful.
- MIRACL-HN has no Swedish. It is shown only as a general multilingual-retrieval signal.
- None of these tasks is legal text. They are news summaries (Swedn), FAQ pairs (SweFaq), Wikipedia and reading-comprehension passages (Belebele).

**MMTEB (MTEB Multilingual) aggregate scores**, as reported in the Qwen3-Embedding model card from a leaderboard snapshot of 2025-05-24 (https://huggingface.co/Qwen/Qwen3-Embedding-8B). Mean (Task) / Retrieval:

| Model | Mean (Task) | Retrieval |
|---|---|---|
| Qwen3-Embedding-8B | 70.58 | 70.88 |
| Qwen3-Embedding-4B | 69.45 | 69.60 |
| gemini-embedding-exp-03-07 | 68.37 | 67.71 |
| Qwen3-Embedding-0.6B | 64.33 | 64.64 |
| multilingual-e5-large-instruct | 63.22 | 57.12 |
| Cohere-embed-multilingual-v3.0 | 61.12 | 59.16 |
| bge-m3 | 59.56 | 54.60 |
| text-embedding-3-large | 58.93 | 59.27 |

EmbeddingGemma-300m reports an MTEB Multilingual v2 mean (task) of **61.15** at 768 dimensions (https://huggingface.co/google/embeddinggemma-300m). Current leaderboard positions as of 2026-11-20 are **unverified** (the leaderboard app could not be read).

### A.2 Hosted embedding APIs

| Model | Price /1M tokens | Dimensions | Max input | EU processing option | Retention and training |
|---|---|---|---|---|---|
| OpenAI `text-embedding-3-small` | $0.02 | 1536 (shortenable) | 8,192 (OpenRouter listing) | Yes: `eu.api.openai.com`, EU-region project, eligibility via sales | Abuse-monitoring logs up to 30 days. No training unless opted in. `/v1/embeddings` is ZDR-eligible with approval. |
| OpenAI `text-embedding-3-large` | $0.13 | 3072 (shortenable) | 8,192 | Same | Same |
| Voyage `voyage-4-large` / `voyage-4` / `voyage-4-lite` | $0.12 / $0.06 / $0.02. 200M free tokens per model. Batch 33% off. | 1024 default (256, 512, 2048) | 32,000 | Through MongoDB Atlas `eu.ai.mongodb.com`. Which models the EU endpoint serves is **unverified**. Not documented for `api.voyageai.com`. | Opt-out toggle gives "zero-day retention" and no training. Default is opted in. |
| Cohere `embed-v5.0-pro` / `-fast` | $0.12 / $0.08 (OpenRouter listing; Cohere's own pricing page shows no figure: **unverified** direct) | 2048 default (256–2048) | 128k | **Unverified** for the SaaS API. Private and cloud-marketplace deployments exist. | Logged prompts deleted after 30 days. ZDR only for enterprise customers. Training is opt-out via Data Controls. |
| Cohere `embed-multilingual-v3.0` | **Unverified** | 1024 | 512 | As above | As above |
| Mistral `mistral-embed` | $0.10 | 1024 (OpenRouter and third parties; Mistral's own page does not state it: **unverified**) | 8k | **Yes, EEA by default** | ZDR on request (pay-as-you-go, stateless calls incl. embeddings). No training in pay-as-you-go. |
| Mistral `codestral-embed` | $0.15 | **Unverified** | 8,192 (OpenRouter) | EEA by default | As above. A code model, not suited to legal text. |
| Jina `jina-embeddings-v5-text-small` | **Unverified** (third parties say $0.05) | Matryoshka down to 32 | 32,768 | **Unverified.** Jina is now part of Elastic, whose DPA applies. | "We never use your API requests, inputs, or outputs to train." |
| Google `gemini-embedding-001` | $0.15 (batch $0.075) | 128–3072, default 3072 | 2,048 | Gemini API: no EU option documented. Vertex AI lists the model under the `eu` multi-region (availability checkmarks unreadable: **unverified**). | Free tier data **is** used to improve products. Paid tier data is not. |
| Google `gemini-embedding-2` | $0.20 text (batch $0.10) | 128–3072, default 3072 | 8,192 | As above | As above |

Sources:
- OpenAI prices: https://developers.openai.com/api/docs/pricing. EU residency, including `/v1/embeddings` with the three embedding models processed in the EU, and the 10% uplift only for models released on or after 2026-03-05: https://developers.openai.com/api/docs/guides/data-residency. Retention and ZDR: https://developers.openai.com/api/docs/guides/your-data.
- Voyage prices: https://docs.voyageai.com/docs/pricing. Models: https://docs.voyageai.com/docs/embeddings. Opt-out: https://docs.voyageai.com/docs/faq. EU geography: https://www.mongodb.com/docs/voyageai/geographies/.
- Cohere models: https://docs.cohere.com/docs/models. Data terms: https://cohere.com/enterprise-data-commitments. Pricing page without figures: https://cohere.com/pricing.
- Mistral prices: https://docs.mistral.ai/models/pricing and https://docs.mistral.ai/models/model-cards/mistral-embed-23-12 ($0.1, 8k context). Hosting: https://help.mistral.ai/en/articles/347629-where-do-you-store-my-data-or-my-organization-s-data. ZDR: https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr.
- Jina: https://jina.ai/embeddings/. Elastic DPA note: https://jina.ai/legal.
- Gemini prices and data use: https://aistudio.google.com/docs/pricing. Dimensions and limits: https://aistudio.google.com/docs/embeddings. Vertex locations: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations.

### A.3 DeepSeek and OpenRouter

**DeepSeek:** no embeddings endpoint. The API docs list only chat completions, with OpenAI- and Anthropic-compatible base URLs, and two models (`deepseek-flash`, `deepseek-v4-pro`) (https://api-docs.deepseek.com/). The pricing page lists no embedding model (https://api-docs.deepseek.com/quick_start/pricing).

**OpenRouter:** `POST https://openrouter.ai/api/v1/embeddings`, with model discovery at `GET /api/v1/embeddings/models` (https://openrouter.ai/docs/api-reference/embeddings). Models and prices from that endpoint on 2026-11-20 (prices per 1M tokens, derived from the per-token `prompt` field):

| Model | Price | Context | Backend providers (from `/api/v1/models/<id>/endpoints`) |
|---|---|---|---|
| `baai/bge-m3` | $0.01 | 8,194 | Parasail, DeepInfra |
| `qwen/qwen3-embedding-8b` | $0.01 | 40,960 | Nebius, DeepInfra, SiliconFlow ($0.04) |
| `qwen/qwen3-embedding-4b` | $0.02 | 32,768 | not checked |
| `intfloat/multilingual-e5-large` | $0.01 | 512 | DeepInfra |
| `openai/text-embedding-3-small` / `-large` | $0.02 / $0.13 | 8,192 | not checked |
| `mongodb/voyage-4-lite` / `voyage-4` / `voyage-4-large` | $0.02 / $0.06 / $0.12 | 32,000 | not checked |
| `cohere/embed-5-fast` / `embed-5-pro` | $0.08 / $0.12 | 128,000 | Cohere |
| `google/gemini-embedding-001` / `gemini-embedding-2` | $0.15 / $0.20 | 20,000 / 8,192 | not checked |
| `mistralai/mistral-embed-2312` | $0.10 | 8,192 | Mistral |
| `nvidia/nemotron-3.5-embed-1b:free`, `-8b:free` | $0 | 32,768 | not checked |

The endpoint API returned no data-policy or ZDR fields for these embedding endpoints. Whether each backend host retains data, and where it runs, is **unverified**.

### A.4 Open-weight, self-hostable models

Parameter counts come from the Hugging Face model API (`safetensors.total`) on 2026-11-20. RAM figures are **estimates**: weights at fp32 (4 bytes/param) or bf16 (2 bytes/param), plus roughly 1 GB of runtime overhead. Int8 quantisation roughly halves the bf16 figure.

| Model | Params | Dims | Max input | Licence | CPU RAM (estimate) | On MTEB / SEB tasks? |
|---|---|---|---|---|---|---|
| `BAAI/bge-m3` | ~568M (the HF API does not report it; this is the shared XLM-R-large backbone, cf. `bge-reranker-v2-m3` at 567.8M) | 1024; also sparse and multi-vector | 8,192 | MIT | ~3.5 GB fp32, ~2 GB fp16 | Yes, all Swedish tasks above |
| `intfloat/multilingual-e5-large` | 560M | 1024 | **512** | MIT | ~3.5 GB fp32 | Yes |
| `intfloat/multilingual-e5-large-instruct` | 560M | 1024 | **512**; queries need an `Instruct: …\nQuery: …` prefix | MIT | ~2 GB fp16 | Yes |
| `Qwen/Qwen3-Embedding-0.6B` | 596M | up to 1024 | 32K | Apache-2.0 | ~2.5 GB bf16 | Yes |
| `Qwen/Qwen3-Embedding-4B` | 4.0B | up to 2560 | 32K | Apache-2.0 | ~9 GB bf16 | Yes |
| `Qwen/Qwen3-Embedding-8B` | 7.6B | up to 4096 | 32K | Apache-2.0 | ~16 GB bf16. Slow on CPU, so a GPU or a hosted API is realistic. | Yes, top Swedish results |
| `google/embeddinggemma-300m` | 303M | 768 (MRL 512/256/128) | 2,048 | Gemma licence (gated) | ~2 GB fp32 | Partly (no Swedn/SweFaq file) |
| `KBLab/sentence-bert-swedish-cased` (v2.0) | 125M | 768 | **384** | Apache-2.0 | ~1 GB | No result files found. The model card reports SweFAQ accuracy 0.587. |
| `jinaai/jina-embeddings-v5-text-small` | 596M | Matryoshka | 32,768 | **CC-BY-NC-4.0** (non-commercial) | ~2.5 GB bf16 | Yes |

Sources: model cards at https://huggingface.co/BAAI/bge-m3, https://huggingface.co/intfloat/multilingual-e5-large-instruct, https://huggingface.co/Qwen/Qwen3-Embedding-8B, https://huggingface.co/google/embeddinggemma-300m, https://huggingface.co/KBLab/sentence-bert-swedish-cased, https://huggingface.co/jinaai/jina-embeddings-v5-text-small, and `https://huggingface.co/api/models/<id>` for parameter counts and licences.

**Swedish-specific models.** KBLab's sentence-BERT (2023) is the only established Swedish embedding model found. It is a distilled paraphrase model with a 384-token limit, and it has no Swedish retrieval results in the MTEB repository. Community Swedish fine-tunes exist (for example `oxfrug/bge-m3-swedish-superlim`, which self-reports +0.047 on SwednRetrieval over vanilla bge-m3, https://huggingface.co/oxfrug/bge-m3-swedish-superlim), but none was verified on the leaderboard. No AI Sweden embedding model was found (**unverified**: not searched exhaustively). The one Scandinavian fine-tune in the results repository, `EmbeddingGemma-Scandi-300m`, scores far below the multilingual models on Swedn and SweFaq (55.4 / 54.4). Nothing suggests a Swedish-only model beats `bge-m3` for this task, and none handles English queries.

### A.5 Multilingual rerankers

| Reranker | Price | Data / EU | Source |
|---|---|---|---|
| `BAAI/bge-reranker-v2-m3` | Free to self-host. 567.8M params (~3.5 GB fp32). Apache-2.0. | Runs on PermitNav's own server. | https://huggingface.co/api/models/BAAI/bge-reranker-v2-m3 |
| Cohere `rerank-v5.0-pro` / `-fast` (multilingual, 32k context); `rerank-v3.5`, `rerank-multilingual-v3.0` | Billed per search: "one query with up to 100 documents". Price per search **unverified** (not shown on the pricing page). | Same terms as Cohere Embed: 30-day log deletion, enterprise-only ZDR, EU **unverified**. | https://docs.cohere.com/docs/models, https://cohere.com/pricing |
| Jina reranker (v3 open weights are CC-BY-NC-4.0) | API price **unverified** | Elastic DPA. Location **unverified**. | https://jina.ai/embeddings/, https://huggingface.co/api/models/jinaai/jina-reranker-v3 |
| Voyage `rerank-3` / `rerank-3-lite` | $0.05 / $0.02. 200M free tokens. | Same opt-out as Voyage embeddings. EU via MongoDB Atlas (**unverified** per model). | https://docs.voyageai.com/docs/pricing |

---

## B. Answer-generation LLMs with EU processing

### B.1 Claude

**Anthropic API (first party): no EU-only inference.** `inference_geo` accepts only `"global"` (default) and `"us"`. Workspace geo (storage at rest) is `"us"` only. "Current limitations: Inference geo: Only "us" and "global" are available." (https://platform.claude.com/docs/en/manage-claude/data-residency). The same holds on Claude Platform on AWS and Claude Models on Google Cloud (Anthropic managed), which follow the first-party controls.

**Prices** (https://platform.claude.com/docs/en/about-claude/models/overview): Claude Sonnet 5.5 `claude-sonnet-5-5` costs $2 in / $10 out. Claude Haiku 5.5 `claude-haiku-5-5` costs "from $0.10 / from $0.50" (tiered; see the pricing page). Batch is 50% off.

**Retention on the Anthropic API:**
- Default: inputs and outputs are deleted "within 30 days of receipt or generation". Flagged content may be kept up to 2 years (https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data).
- The API data-retention page says "Conversation content … is not retained by default; the exception is Covered Models". This conflicts with the 30-day statement above. Treat 30 days as the safe assumption until confirmed.
- ZDR is available **by arrangement with Anthropic sales**, per organization. Covered Models (Fable 5.x, Mythos 5.x) require 30-day retention. **Sonnet 5.5 and Haiku 5.5 are not Covered Models**, so ZDR applies to them. Flagged content may still be kept up to 2 years. (https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)

**Amazon Bedrock (EU):**
- Claude Sonnet 5.5 and Claude Haiku 5.5 have **no in-region support in any EU region**. They are available through the **EU geo cross-region inference profile** from eu-central-1, eu-central-2 (Zurich), eu-north-1 (Stockholm), eu-south-1, eu-south-2, eu-west-1, eu-west-2 (London) and eu-west-3 (https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html).
- Note that the "EU" geography includes London and Zurich. Both are outside the EU but covered by EU adequacy decisions (adequacy status not re-checked here).
- Regional and geo endpoints carry a **10% premium** over global. Bedrock IDs are `anthropic.claude-sonnet-5-5` and `anthropic.claude-haiku-5-5`. Access criteria are set per model in the console. (https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock)
- Data on Bedrock: model providers "don't have access to Amazon Bedrock logs or to customer prompts and completions" (https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html).
- An account or project can set `data_retention_mode: none`: "No request or response data is written to durable storage by AWS or shared with the model provider" (https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html).
- Retention is mandatory only for the Fable 5.x models and for classifier-flagged OpenAI traffic (https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html). Sonnet 5.5 and Haiku 5.5 are not on that list.

**Google Cloud (Google managed, formerly Vertex AI):**
- Claude Sonnet 5.5 and Claude Haiku 5.5 are **GA in the `eu` multi-region**, with ML processing in "Europe multi-region" (https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5-5, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/haiku-5-5).
- Regional endpoints (such as `europe-west1`) serve only Sonnet 4.6 and earlier. Multi-region and regional endpoints cost **10% more** than global. (https://platform.claude.com/docs/en/build-with-claude/claude-models-on-google-cloud/google-managed)
- Google does not train on customer data without permission (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance). Abuse-monitoring logging for Claude on Google Cloud, and how to get ZDR for it, is **unverified**: the page covers only OpenAI models.
- One user forum report describes Sonnet 5.5 quota stuck at 0 in the EU multi-region (https://discuss.google.dev/t/claude-sonnet-5-5-quotas-stuck-at-0-despite-active-billing-manual-access-review-requested/412427). This is anecdotal: check quota before relying on it.

### B.2 DeepSeek API

- **Location:** "we directly collect, process and store your Personal Data in People's Republic of China" (Privacy Policy, last updated 2026-02-10, https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html).
- **Retention:** "for as long as you have an account". There is no separate API retention period.
- **Training:** data may be used to "train and improve our technology", with an opt-out right. An EU representative (Prighter) is named.
- **API terms:** governed by "the laws of the People's Republic of China in the mainland", with disputes heard in Hangzhou courts. No data processing agreement is mentioned. (https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)
- **Italy:** the Garante ordered an urgent limitation of processing of Italian users' data on 30 January 2025 (Provvedimento 10098477, https://www.garanteprivacy.it/web/guest/home/docweb/-/docweb-display/docweb/10098477). Whether that investigation has concluded is **unverified**.
- **Berlin:** on 27 June 2025 the Berlin Commissioner for Data Protection reported the DeepSeek app to Apple and Google as illegal content in Germany, citing a breach of Art. 46(1) GDPR (transfers to China without appropriate safeguards) (https://www.datenschutz-berlin.de/fileadmin/user_upload/pdf/pressemitteilungen/2025/20250627-BlnBDI-Press-Release_DeepSeek.pdf). The PDF body could not be text-extracted. The Art. 46(1) finding is taken from the press-release title and search-result summaries of it, so the detail is **partly unverified**.
- **Assessment:** there is no adequacy decision for China, the terms offer no SCCs or DPA, and PRC law governs. Sending user questions, which may reveal nationality, family status or immigration history, to the DeepSeek API would be very hard to justify under GDPR Chapter V. **Not suitable.**
- Mistral's model list includes "DeepSeek V4.1 Flash 1017" (https://docs.mistral.ai/getting-started/models/models_overview/). If a DeepSeek model is wanted, running it on Mistral's EEA infrastructure avoids the transfer problem. Its price on Mistral is **unverified**.

### B.3 OpenRouter

- **OpenRouter's own storage:** it "does not store your prompts or responses" unless you opt in (Input & Output Logging is off by default), or the content is flagged as potential CSAM. It stores per-request metadata (token counts, latency) and may categorise a small sample of prompts anonymously. (https://openrouter.ai/docs/guides/privacy/data-collection)
- **Retention period** for metadata: **unverified**.
- **Provider filtering:** account-level ZDR per model group, or per request with `provider: {"zdr": true}`. Endpoints with unknown policies are treated as retaining and training. Training-provider exclusion is a separate setting. (https://openrouter.ai/docs/guides/privacy/zero-data-retention, https://openrouter.ai/docs/guides/privacy/logging)
- **Screening:** from 2026-10-27 all inference requests, including ZDR traffic, are screened for CSAM.
- **EU-only routing:** `eu.openrouter.ai` "keeps prompts and completions processed within that region", but only on **Business and Enterprise** plans (https://openrouter.ai/docs/guides/privacy/logging).
- **Processor chain:** yes, it adds one. Every request passes through OpenRouter as an extra processor, in addition to the backend provider. Where OpenRouter itself processes requests outside the `eu.` host is **unverified**.

### B.4 Mistral as the answer model

- **Hosting:** data is hosted in the EEA by default. A US endpoint exists but must be chosen explicitly. Some features use non-EU subprocessors under SCCs. (https://help.mistral.ai/en/articles/347629-where-do-you-store-my-data-or-my-organization-s-data)
- **ZDR:** on request for pay-as-you-go, stateless calls only. Labs and Public Preview models are excluded. (https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr)
- **Current models** (https://docs.mistral.ai/getting-started/models/models_overview/): Mistral Large 4 (v26.10, 1M context per the changelog), Mistral Small 4 (v26.03), and Ministral 3 at 14B, 8B and 3B. Hosted third-party models include GLM 5.3 and DeepSeek V4.1 Flash.
- **Prices** (https://docs.mistral.ai/models/pricing):

  | Model | Input | Output |
  |---|---|---|
  | Large 4 | $0.70 | $2.10 |
  | Small 4 | $0.15 | $0.60 |
  | Ministral 3 14B | $0.20 | $0.20 |
  | Ministral 3 8B | $0.15 | $0.15 |

  https://mistral.ai/pricing instead says "Mistral Large costs $0.5 /M tokens in and $1.5 /M tokens out". The conflict is unresolved, so treat the Large 4 price as **unverified** within that range.
- Mistral Medium is not on the pricing page (**unverified**).

---

## C. Cost sanity check

Assumptions (not facts):
- Corpus of 5M tokens (upper end).
- Daily changes up to 200k tokens re-embedded (about 6M/month; real churn is probably much lower).
- 100k queries × 50 tokens = 5M query tokens/month (upper end). Doubling this for query translation (D20) changes little.
- **Steady month:** 6M + 5M = **11M tokens**.
- **Experiment month** (phases 1–2): add five full re-embeds = 25M, so **36M tokens**.

| Option | Price /1M | Steady month (11M) | Experiment month (36M) | Initial 5M embed |
|---|---|---|---|---|
| `bge-m3` self-hosted on an EU VPS | $0 marginal | VPS cost only (CPU, ~4 GB RAM; VPS price not checked) | same | same |
| `bge-m3` or `qwen3-embedding-8b` via OpenRouter | $0.01 | $0.11 | $0.36 | $0.05 |
| `text-embedding-3-small` (OpenAI EU) | $0.02 | $0.22 | $0.72 | $0.10 |
| `voyage-4` | $0.06 | $0.66 (inside the 200M free tokens) | $2.16 (free) | $0.30 |
| `mistral-embed` (EEA) | $0.10 | $1.10 | $3.60 | $0.50 |
| `text-embedding-3-large` (OpenAI EU) | $0.13 | $1.43 | $4.68 | $0.65 |
| `gemini-embedding-001` | $0.15 | $1.65 | $5.40 | $0.75 |

For comparison, the answer model dominates cost. At 100k queries a month with an assumed 4k input and 500 output tokens each, Claude Sonnet 5.5 at $2/$10 would cost about 400M × $2 + 50M × $10 = **about $1,300/month**, plus 10% for an EU endpoint. That is 250 times or more the cost of any embedding option above.

---

## Recommendation for PermitNav

1. **Embeddings: compare `bge-m3` against one stronger model. Drop `text-embedding-3-small` as the default comparison point.**
   - `text-embedding-3-small` is the weakest model checked on every Swedish task (Swedn 73.5 vs 81.0 for `bge-m3`; en→sv 90.0 vs 92.9). It saves at most cents a month.
   - `bge-m3` matches `text-embedding-3-large` on Swedish retrieval. It is MIT-licensed and handles 8k-token inputs, which suits long statute sections and Migrationsverket pages. Run on PermitNav's own EU server, it keeps query text in-house, which removes the processor and retention question for the most sensitive data. It also produces sparse vectors that could complement Postgres full-text search.
   - The stronger contender should be **`Qwen3-Embedding-8B`** (best cross-language Belebele scores, Apache-2.0, $0.01 via OpenRouter for experiments, a GPU for production) or **`gemini-embedding-001`** (best Swedn and SweFaq scores, but no documented EU processing on the Gemini API).
   - If a hosted EU API is preferred for production, **`text-embedding-3-large` via an OpenAI EU-residency project** is the verified-EU hosted option with solid Swedish scores. `mistral-embed` is EU-native but has **no public Swedish benchmark**, so include it only if it is measured on the golden set.
   - Benchmarks are not legal Swedish. The phase 1–2 decision should come from recall@k and MRR on the golden set (D16), reported separately for original and translated questions (D20). Keep hybrid search (pgvector plus full-text) for exact identifiers like "6 kap. 2 §", which no embedding model handles reliably.
2. **OpenRouter: use it for offline experiments only.** It is a cheap way to run `bge-m3`, Qwen3 and others over the public corpus and the sanitised dev set. Do not route real user questions through it unless PermitNav is on a Business plan using `eu.openrouter.ai` with ZDR enforced. Even then it adds a processor.
3. **DeepSeek: do not use it for anything that touches user questions.** It has no embeddings API, processes and stores data in China under PRC-law terms, and has been the subject of EU DPA action. The existing key is useful at most for non-personal offline work, and the Mistral-hosted DeepSeek model is the EU-safe way to try that model family.
4. **Answer model (D15): Claude stays recommended, but not through the Anthropic API directly**, because the Anthropic API cannot keep inference in the EU.
   - **First choice: Amazon Bedrock** with the EU cross-region inference profile for Sonnet 5.5 and Haiku 5.5, and `data_retention_mode: none` set at account level. This meets both D15 criteria (EU processing, abuse-monitoring retention off) without a sales contract. The 10% premium applies, and the EU geography includes London and Zurich.
   - **Alternative:** Google Cloud `eu` multi-region (also +10%). Its Claude abuse-logging terms are unverified.
   - **EU-native fallback, and a judge from a different family (D19):** Mistral Large 4 on La Plateforme with ZDR requested.

Open items to verify before committing:
- Bedrock model access for Sonnet 5.5 and Haiku 5.5 on the account.
- Whether the Vertex Claude abuse-logging terms allow retention to be turned off.
- Whether Voyage 4 is served on MongoDB's `eu.` endpoint.
- Cohere's direct prices.
- `mistral-embed` dimensions, and its Swedish quality (only measurable on the golden set).
