# Axiom — AI-Powered Academic Due Diligence

> 🌐 **Language note / Nota de idioma:** This README is provided in **English first**. The full **Spanish version (Español)** follows immediately below the English one — scroll down or jump to [§ Versión en Español](#-versión-en-español).

**Axiom** automates end-to-end PRISMA 2020 systematic reviews with a pipeline of 8–10 agents orchestrated on LangGraph: multi-API discovery, cascade screening, structured extraction, semantic clustering with BGE-M3, dual-model consensus/contradiction analysis, research-gap detection and verification, and executive drafting in Markdown + PDF.

Optionally, in **Cochrane mode**, it adds per-paper Risk of Bias 2.0 assessment and per-cluster GRADE certainty rating.

---

## 📑 Table of contents

1. [What problem does it solve?](#-what-problem-does-it-solve)
2. [Architecture](#-architecture)
3. [Agent pipeline](#-agent-pipeline)
4. [AxiomState — data contract](#-axiomstate--data-contract)
5. [Technology stack](#-technology-stack)
6. [Repository structure](#-repository-structure)
7. [Installation](#-installation)
8. [Configuration (`.env`)](#-configuration-env)
9. [Running](#-running)
10. [HTTP API](#-http-api)
11. [Cochrane mode (RoB 2.0 + GRADE)](#-cochrane-mode-rob-20--grade)
12. [Evaluation against gold standards](#-evaluation-against-gold-standards)
13. [Known limitations](#-known-limitations)
14. [Future roadmap](#-future-roadmap)
15. [License and credits](#-license-and-credits)

---

## 🎯 What problem does it solve?

A manual systematic review takes **6 to 24 months** for a team of 2–4 researchers: searching ≥3 databases, dual-blind screening of ≥1000 abstracts, data extraction, bias assessment, narrative synthesis, and manuscript drafting. Axiom compresses that flow to **15–45 minutes per review** while preserving full PRISMA 2020 traceability: every inclusion, exclusion, extraction, and consensus decision is recorded in a per-run `events.jsonl`.

Primary use cases:

- **Academic research** — systematic-review drafts ready for human review, not for direct publication.
- **Pharma / biotech due diligence** — evidence sweeps for investment decisions, in-licensing, or pipeline evaluation.
- **Public-health policy** — fast evidence synthesis for clinical guidelines, especially in LATAM contexts (Scielo integrated).
- **Technology surveillance** — monitoring emerging literature on a clinical or technical question.

---

## 🏗 Architecture

```
┌──────────────────────────────┐
│  Streamlit Frontend          │
│  (axiom_frontend/)           │  ◀── config · progress SSE · results
└────────────┬─────────────────┘
             │ HTTP + SSE (Bearer token)
             ▼
┌──────────────────────────────┐
│  FastAPI (axiom_api.py)      │
│  · in-proc sequential queue  │
│  · per-run events.jsonl      │
│  · SSE replay + cancellation │
│  · final_state adapter       │
└────────────┬─────────────────┘
             │ pipeline.astream()
             ▼
┌──────────────────────────────────────────────────────────────┐
│                  LangGraph (graph.py)                        │
│                                                              │
│  searcher ─► screener_7b ─► screener_32b ─► extractor ──┐    │
│                                                         │    │
│       ┌──── (Cochrane mode: rob_assessor) ◀────────────┘    │
│       ▼                                                      │
│  clusterer ──┬─► analyst_7b ──┐                              │
│              └─► analyst_32b ─┴─► reconciler                 │
│                                       │                      │
│             (Cochrane mode: grade_profiler)                  │
│                                       ▼                      │
│                                  gapfinder                   │
│                                       │                      │
│       writer_synthesis ──► writer_discussion ──► writer_     │
│       limitations ──► writer_tables ──► writer_references    │
│       ──► writer_assembler ──► END                           │
└──────────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│  Featherless API (LLM inference)     │
│  · Qwen2.5-7B-Instruct (cost 1)      │
│  · DeepSeek-R1-Distill-32B (cost 2)  │
│  · Kimi-K2-Instruct (writer)         │
│  · Global cap: 4 concurrent units    │
└──────────────────────────────────────┘
```

**Key decisions:**

- **Featherless instead of self-hosted vLLM.** The AMD MI300X hackathon version ran two local vLLM servers. The current version uses the Featherless API (OpenAI-compatible), eliminating the on-premise GPU dependency. The 4-concurrent-units cap of the Premium plan is protected by a *credit-based* semaphore in `llm_router.py` (Qwen-7B = 1 unit, DeepSeek-R1 = 2 units).
- **In-process sequential queue.** `axiom_api.py` processes runs one at a time (`max_queue_size=10`). Running two pipelines in parallel would saturate the Featherless cap and cause cascading 429s. This requires `--workers 1` for uvicorn.
- **Per-run `events.jsonl`.** If the SSE consumer disconnects, it can reconnect to `/pipeline/stream/{run_id}?since_event_id=N` and full replay from disk.
- **Cooperative cancellation.** `POST /pipeline/{run_id}/cancel` flags the run in a set; the worker checks between `astream` chunks and aborts before the next node (without interrupting in-flight LLM calls).

---

## 🤖 Agent pipeline

| # | Node | Model | Role |
|---|---|---|---|
| 1 | `searcher` | Qwen2.5-7B + `instructor` | Decomposes the question into 5 queries and hits PubMed, OpenAlex, arXiv, Crossref, and Scielo in parallel. Dedupe by DOI + (title, year). Enriches via `check_access_async` (Unpaywall + OpenAlex + Crossref, 2-of-3 rule). |
| 2a | `screener_7b` | Qwen2.5-7B | First PRISMA pass: fast per-abstract decision with `confidence` (high/medium/low). |
| 2b | `screener_32b` | DeepSeek-R1-Distill-32B | Adjudicator: only re-evaluates abstracts where `screener_7b` marked `low confidence` or `uncertain`. If the 32B fails, the 7B verdict is preserved (`route="7b_fallback"`). |
| — | `rob_assessor` *(Cochrane)* | DeepSeek-R1 | Evaluates each paper across the 5 RoB 2.0 domains (Sterne et al., BMJ 2019). Runs only when `cochrane_mode=True`. |
| 3 | `extractor` | Qwen2.5-7B + `instructor` | PyMuPDF downloads and parses the PDF if `is_open=True`; falls back to `abstract_only` for paywalled or scanned ones. Dynamic schema per domain bucket (medicine, oncology, mental health, etc., defined in `domain_ontologies.json`). |
| — | `clusterer` | BGE-M3 + `AgglomerativeClustering` | Dense embeddings (fp16) per extraction → persistent ChromaDB → hierarchical clustering by cosine distance (`threshold=0.7` default). Ensures no cluster exceeds `analyst_max_user_chars=28000` (greedy split in original order if it does). |
| 4a | `analyst_7b` | Qwen2.5-7B | Per-cluster consensus/contradiction analysis, in parallel with `analyst_32b`. Structured output (`supporting_papers`, `contradicting_papers`, `neutral_papers`, `agreement_percentage`). |
| 4b | `analyst_32b` | DeepSeek-R1 | Same task with a `<think>` block for deeper contradiction detection. |
| — | `reconciler` | deterministic | Merges 7B + 32B paper-by-paper. Policy: 32B is canonical; on disagreement, prioritizes the more skeptical verdict (`contradicting` > `neutral` > `supporting`). Emits `consensus_level` ∈ {full, partial, split}. |
| — | `grade_profiler` *(Cochrane)* | DeepSeek-R1 | Computes per-cluster GRADE certainty (High/Moderate/Low/Very Low) using the 5 downgrades (RoB, inconsistency, indirectness, imprecision, publication bias) and 0–3 upgrades. Re-writes `consensus_clusters` enriched. |
| 5 | `gap_finder` | DeepSeek-R1 | Proposes 5 gaps (population, methodological, comparison, temporal, open question) → verifies each in OpenAlex. If `hits < 50` → confirmed; `< 500` → emerging; more → rejected. If all 5 are rejected, rescues the 2 with the fewest hits. |
| 6 | `writer_*` | Kimi-K2-Instruct (LLM) + Python | Bi-phasic: 3 LLM nodes (`synthesis`, `discussion`, `limitations`) + 3 deterministic Python nodes (`tables`, `references`, `assembler`). The `assembler` concatenates everything and renders ONE unified PDF with WeasyPrint. |

**Fan-out / fan-in.** After `clusterer`, `analyst_7b` and `analyst_32b` run concurrently (consuming 2 + 2 = 4 units, exactly the Featherless cap). The `reconciler` waits for both via reducers `Annotated[list, operator.add]` on `synthesis_7b` / `synthesis_32b`.

**Conditional skip.** If the screener rejects every abstract, the graph routes `screener_32b` → `writer_synthesis` directly to deliver a "no eligible studies" report instead of crashing (`check_screening_results` in `graph.py`).

**Conditional Cochrane mode.** Nodes `rob_assessor` and `grade_profiler` are invoked only if `state["cochrane_mode"]=True`. The global kill-switch `settings.cochrane_mode_enabled=False` disables them even if the frontend requests them (useful when Featherless is rate-limited).

---

## 📦 AxiomState — data contract

LangGraph passes a single `TypedDict` between nodes. Each node returns only the fields it modified; LangGraph merges according to the *reducer* declared per field.

```python
class AxiomState(TypedDict, total=False):
    # Inputs
    sr_id: str
    domain: str
    question: str
    prisma_criteria: dict
    cochrane_mode: bool
    output_language: str  # "English" | "Spanish" | "auto"

    # Accumulators (operator.add — concurrent-safe)
    errors:          Annotated[list[dict], operator.add]
    papers_found:    Annotated[list[dict], operator.add]   # searcher
    screened_papers: Annotated[list[dict], operator.add]   # screener (include + uncertain)
    papers_excluded: Annotated[list[dict], operator.add]   # screener (PRISMA audit trail)
    extractions:     Annotated[list[dict], operator.add]   # extractor
    rob_assessments: Annotated[list[dict], operator.add]   # rob_assessor (Cochrane)
    synthesis_7b:    Annotated[list[dict], operator.add]   # analyst_7b
    synthesis_32b:   Annotated[list[dict], operator.add]   # analyst_32b

    # Atomic writes (single producer)
    papers_to_escalate: list[dict]    # screener_7b → screener_32b
    clusters:           list[list[dict]]    # clusterer
    consensus_clusters: list[dict]          # reconciler (re-written by grade_profiler in Cochrane)
    research_gaps:      list[dict]          # gap_finder

    # Writer (6 sequential sub-nodes)
    writer_synthesis_md:   str   # writer_synthesis (LLM)
    writer_discussion_md:  str   # writer_discussion (LLM)
    writer_limitations_md: str   # writer_limitations (LLM)
    writer_tables_md:      str   # writer_tables (Python)
    writer_references_md:  str   # writer_references (Python)

    # Final output
    executive_report_md:        str
    executive_report_pdf_path:  str | None
```

**Errors never abort the graph:** failing nodes append to `errors` and downstream nodes degrade gracefully. This means a searcher that loses 1 of 5 APIs still yields papers; an extractor that fails to parse a PDF falls back to the abstract; an analyst_32b that crashes lets the reconciler use the 7B verdict.

See `axiom_backend/state.py` for full per-field comments.

---

## 🛠 Technology stack

| Layer | Technology | Why |
|---|---|---|
| Orchestration | **LangGraph** 0.2+ | Stateful DAG, reducers for concurrent fan-in, native checkpointing (planned). |
| LLM inference | **Featherless** (OpenAI-compatible) | Cost-by-token, no on-premise GPU, support for open models (Qwen, DeepSeek, Kimi). |
| Fast model | **Qwen2.5-7B-Instruct** | High throughput for mass screening and JSON extraction with `instructor`. |
| Reasoning model | **DeepSeek-R1-Distill-Qwen-32B** | Explicit chain-of-thought (`<think>...`) for contradictions, RoB, GRADE, gap finding. |
| Writer model | **Kimi-K2-Instruct** | Long, coherent narrative (executive report + APA 7) without losing the thread. |
| Embeddings | **BGE-M3** (`BAAI/bge-m3`) via `FlagEmbedding` | Multilingual (relevant for Scielo in Portuguese/Spanish), dense + sparse + multi-vector. |
| Vector store | **ChromaDB** persistent | Simple, embedded, serverless. Persists under `data/chroma_db/`. |
| Clustering | `sklearn.cluster.AgglomerativeClustering` | Hierarchical by cosine distance; no `n_clusters` required a priori. |
| PDF parsing | **PyMuPDF** (fitz) | Fast text extraction; falls back to `abstract_only` for scanned PDFs. |
| Schema validation | **Pydantic v2** + **instructor** | JSON guarantee from LLMs without manual retries. |
| HTTP API | **FastAPI** + **uvicorn** | Native async, SSE streaming, dependency injection (`Depends(require_bearer)`). |
| Frontend | **Streamlit** 1.42 | Fast iteration; manual i18n (ES/EN); SSE consumer in `pipeline_runner.py`. |
| PDF reports | **WeasyPrint** | Markdown → HTML → PDF with modular CSS. |
| Search APIs | PubMed (Entrez), OpenAlex, arXiv, Crossref, Scielo | Biomedical + multidisciplinary + LATAM coverage. |
| Access check | Unpaywall + OpenAlex + Crossref (2-of-3) | Reduces false positives on `is_open`; the extractor only downloads if the majority vote is OA. |

---

## 📂 Repository structure

```
axiom/
├── main.py                          ◀── uvicorn entrypoint (preflight checks + logging)
├── requirements.txt
├── README.md                        ◀── This file
├── .env                             ◀── (not versioned — use .env.example)
├── .gitignore
│
├── axiom_backend/                   ◀── Backend FastAPI + LangGraph
│   ├── __init__.py
│   ├── axiom_api.py                 ◀── FastAPI · queue · SSE · final_state adapter
│   ├── graph.py                     ◀── build_axiom_graph() · conditional routing
│   ├── state.py                     ◀── AxiomState TypedDict + reducers
│   ├── config.py                    ◀── pydantic-settings (loads .env)
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── searcher.py              ◀── 5 APIs · dedupe · access_check
│   │   ├── screener.py              ◀── 7B → 32B cascade (screener_7b_node, screener_32b_node)
│   │   ├── extractor.py             ◀── PyMuPDF + dynamic schema per bucket
│   │   ├── rob_assessor.py          ◀── Cochrane RoB 2.0 (5 domains)
│   │   ├── analyst_7b.py            ◀── per-cluster consensus (fast)
│   │   ├── analyst_32b.py           ◀── per-cluster consensus (with <think>)
│   │   ├── grade_profiler.py        ◀── GRADE certainty per cluster
│   │   ├── gap_finder.py            ◀── 5 categories + OpenAlex verification
│   │   └── writer.py                ◀── 6 sub-nodes: synthesis → discussion → limitations
│   │                                      → tables → references → assembler
│   │
│   ├── tools/
│   │   ├── llm_router.py            ◀── Featherless client · credit semaphore · JSON extractor
│   │   ├── clusterer.py             ◀── BGE-M3 + ChromaDB + AgglomerativeClustering
│   │   ├── reconciler.py            ◀── deterministic 7B↔32B merge
│   │   ├── access_check.py          ◀── Unpaywall + OpenAlex + Crossref (2-of-3)
│   │   └── pdf_parser.py            ◀── PyMuPDF + abstract_only fallback
│   │
│   ├── prompts/                     ◀── *.md loaded at import (fail-loud if missing)
│   │   ├── __init__.py              ◀── _read_text/_read_json + schema validation
│   │   ├── searcher_prompt.md
│   │   ├── screener_prompt_7b.md
│   │   ├── screener_fewshot_7b.md
│   │   ├── screener_prompt_32b.md
│   │   ├── screener_fewshot_32b.md
│   │   ├── extraction_prompt.md
│   │   ├── rob_assessor_prompt.md
│   │   ├── analyst_prompt_v3.md     ◀── analyst_7b
│   │   ├── analyst_prompt_r1.md     ◀── analyst_32b (DeepSeek-R1)
│   │   ├── grade_profiler_prompt.md
│   │   ├── gapfinder_prompt.md
│   │   ├── writer_synthesis_prompt.md
│   │   ├── writer_discussion_prompt.md
│   │   ├── writer_limitations_prompt.md
│   │   ├── writer_apa7_rules.md
│   │   ├── extractor_schema.json
│   │   ├── domain_ontologies.json
│   │   └── prisma_criteria_template.json
│   │
│   └── utils/
│       └── language.py              ◀── resolve_output_language (auto/ES/EN)
│
├── axiom_frontend/                  ◀── Streamlit UI (thin client)
│   ├── app.py                       ◀── 3-screen router (config → progress → results)
│   ├── ui/
│   │   ├── screen_config.py         ◀── PRISMA form + Cochrane toggle
│   │   ├── screen_progress.py       ◀── 9 agent rows + SSE consumer + cancel
│   │   ├── screen_results.py        ◀── tabs: report, gaps, restricted, RoB/GRADE
│   │   └── components.py
│   ├── utils/
│   │   ├── api_client.py            ◀── HTTP + SSE client + PDF download
│   │   ├── pipeline_runner.py       ◀── mock/real event stream
│   │   └── i18n.py                  ◀── ES/EN tables + LOG_PATTERNS
│   └── assets/gifs/                 ◀── per-agent animations
│
├── test_prompts/                    ◀── 10 gold-standard test cases (PICOS prefilled)
│   ├── 01_telemedicine_diabetes.py
│   ├── 02_nlp_health_llm_triage.py
│   ├── 03_salud_mental_tcc_adolescentes.py
│   ├── 04_oncologia_pancreatic_liquid_biopsy.py
│   ├── 05_immunology_mrna_transplant.py
│   ├── 06_surgery_robotic_colorectal.py
│   ├── 07_pharmacology_cre_icu_latam.py
│   ├── 08_geriatria_ejercicio_demencia.py
│   ├── 09_gene_therapy_crispr_sickle_cell.py
│   └── 10_nutrition_mediterranean_microbiome.py
│
└── data/                            ◀── created at runtime (gitignored)
    ├── api_runs/{run_id}/
    │   ├── initial_state.json
    │   ├── events.jsonl
    │   ├── meta.json
    │   └── final_state.json
    ├── results/
    │   └── Axiom_Report_{sr_id}.pdf
    └── chroma_db/                   ◀── BGE-M3 persistence (see § Limitations)
```

> **Historical note:** earlier versions placed the backend under `src/agents/` and `src/tools/`. Any reference to `src/` in old commits maps to the current `axiom_backend/`.

---

## ⚙️ Installation

### 1 · System dependencies (WeasyPrint)

WeasyPrint requires native Pango/Cairo libraries that **are not installed via pip**:

```bash
sudo apt-get update && sudo apt-get install -y \
  libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
  libgdk-pixbuf2.0-0 shared-mime-info
```

Without these libs, `weasyprint` imports but silently fails at render time — the pipeline completes, but `executive_report_pdf_path` ends up `None`.

### 2 · Python ≥ 3.11 + virtual environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3 · BGE-M3 model (pre-download)

`FlagEmbedding` downloads `BAAI/bge-m3` (~2.3 GB) the first time `BGEM3FlagModel("BAAI/bge-m3")` is invoked. To avoid latency on the first run, pre-download at build time:

```bash
python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)"
```

The model is cached under `~/.cache/huggingface/hub/`. To relocate it, export `HF_HOME=/path/to/cache` first.

### 4 · GPU (optional, recommended for BGE-M3)

`clusterer.py` instantiates BGE-M3 with `device="cuda"`. If you're on CPU-only, edit `axiom_backend/tools/clusterer.py:35` to use `device="cpu"` (embeddings will be ~10× slower, but the pipeline still works).

### 5 · Frontend (separate)

The Streamlit frontend runs as an independent process:

```bash
cd axiom_frontend
streamlit run app.py --server.port 8501
```

It points at the backend via the `AXIOM_BACKEND_URL` env var (default `http://localhost:8080`).

---

## 🔐 Configuration (`.env`)

Loaded by `axiom_backend/config.py` via `pydantic-settings`. Unknown variables are ignored.

| Variable | Required | Default | Use |
|---|---|---|---|
| `FEATHERLESS_API_KEY` | ✅ | — | Bearer for Featherless calls. Without it: 401 on every LLM node. |
| `CONTACT_EMAIL` | ✅ | — | User-Agent for Crossref/OpenAlex/Unpaywall polite pools. Without it: startup fails. |
| `AXIOM_BACKEND_API_KEY` | ✅ | — | Bearer the frontend sends in `Authorization`. Protected endpoints reject without it. |
| `FEATHERLESS_BASE_URL` | optional | `https://api.featherless.ai/v1` | OpenAI-compatible endpoint. |
| `FEATHERLESS_MAX_CONCURRENT` | optional | `4` | Cap of the global semaphore. Premium = 4; exceeding it triggers 429s. |
| `MODEL_7B_NAME` | optional | `Qwen/Qwen2.5-7B-Instruct` | ID on featherless.ai/models. |
| `MODEL_32B_NAME` | optional | `deepseek-ai/DeepSeek-R1-Distill-Qwen-32B` | Reasoning model. |
| `MODEL_WRITER_NAME` | optional | `moonshotai/Kimi-K2-Instruct-0905` | Writing model. |
| `MODEL_LIGHT_REASONING_NAME` | optional | `Qwen/Qwen2.5-7B-Instruct` | Reserved for a future analyst_7b. |
| `COCHRANE_MODE_ENABLED` | optional | `true` | Global RoB + GRADE kill-switch (independent of the frontend toggle). |
| `COCHRANE_ROB_TIMEOUT_S` | optional | `120` | Per-paper RoB timeout. |
| `COCHRANE_GRADE_TIMEOUT_S` | optional | `180` | Per-cluster GRADE timeout. |
| `PUBMED_API_KEY` | optional | — | Raises PubMed's rate limit from 3 → 10 req/s. |
| `OPENALEX_API_KEY` | optional | — | OpenAlex premium polite-pool. |
| `CHROMA_PERSIST_DIR` | optional | `./data/chroma_db` | Where ChromaDB persists. |
| `CLUSTER_DISTANCE_THRESHOLD` | optional | `0.7` | Cosine distance (NOT similarity). 0.3–0.4 = near-duplicates; 0.5 = same subtopic; 0.6–0.7 = general domain. |
| `ANALYST_MAX_USER_CHARS` | optional | `28000` | Hard cap on the cluster JSON sent to the analyst. Beyond it, the clusterer splits greedily. |
| `MAX_QUEUE_SIZE` | optional | `10` | Cap on concurrent queued runs. Beyond it: `503`. |
| `AXIOM_MAX_RESULTS_PER_API` | optional | `50` | Cap per academic API. For evaluation against gold standards raise to 200–300 (watch out for arXiv 429s). |
| `AXIOM_LOG_LEVEL` | optional | `INFO` | DEBUG/INFO/WARN/ERROR. |
| `AXIOM_RELOAD` | optional | `0` | Local dev only. NEVER in prod (kills queued runs). |
| `PORT` | optional | `8080` | Cloud Run injects it automatically. |

Example `.env`:

```ini
FEATHERLESS_API_KEY=ftl_xxx...
CONTACT_EMAIL=you@domain.org
AXIOM_BACKEND_API_KEY=$(openssl rand -hex 32)

# Recommended optionals
PUBMED_API_KEY=...
OPENALEX_API_KEY=...
AXIOM_MAX_RESULTS_PER_API=200
```

---

## 🚀 Running

### Backend

```bash
python main.py
```

`main.py` runs `_preflight_check()` before invoking uvicorn: aborts if `FEATHERLESS_API_KEY`, `CONTACT_EMAIL`, or `AXIOM_BACKEND_API_KEY` are missing, with clear messages. This prevents the case where the server starts, accepts requests, and every request fails with obscure errors.

Important flag: **`workers=1` is mandatory.** The asyncio queue, `_current_run`, and the SSE subscriber map are in-process. Multiple workers would split the queue and break SSE replay.

### Frontend

```bash
cd axiom_frontend
AXIOM_BACKEND_URL=http://localhost:8080 \
AXIOM_BACKEND_API_KEY=<same-key-as-backend> \
streamlit run app.py
```

If you want to run the frontend without a backend (offline demo), set `AXIOM_MOCK=1` and `pipeline_runner` replays a deterministic script.

### Smoke test

```bash
# Health
curl http://localhost:8080/healthz

# Submit
curl -X POST http://localhost:8080/pipeline/start \
  -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY" \
  -H "Content-Type: application/json" \
  -d @test_prompts/03_salud_mental_tcc_adolescentes.json

# Stream
curl -N http://localhost:8080/pipeline/stream/<run_id> \
  -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY"
```

---

## 🔌 HTTP API

All endpoints (except `/healthz`) require `Authorization: Bearer ${AXIOM_BACKEND_API_KEY}`.

### `POST /pipeline/start`

Submits a run. Returns immediately; the actual pipeline starts when the sequential worker picks it up.

**Request:**
```json
{
  "sr_id": "abc12345",
  "domain": "salud_mental",
  "question": "What is the effectiveness of mobile CBT apps for reducing depressive symptoms in adolescents aged 12-18?",
  "cochrane_mode": true,
  "output_language": "English",
  "prisma_criteria": { /* see test_prompts/*.py */ }
}
```

Body cap: 100 KB. Queue cap: `MAX_QUEUE_SIZE` (default 10). If the queue is full: `503`.

**Response (202):**
```json
{ "run_id": "abc12345", "status": "queued", "queue_position": 0, "created_at": "2026-05-19T15:00:00Z" }
```

### `GET /pipeline/stream/{run_id}` (SSE)

Server-Sent Events. Replay from `events.jsonl`, then subscribes to live events until `finished` or `error`.

**Query param:** `since_event_id` (int, default 0) for resubscription.

**Emitted types:**
- `agent_start` — `payload={sr_id}`
- `agent_done` — `payload={ui_label, is_internal, stats}`
- `log` — captured from `logging.getLogger("axiom_backend")`
- `finished` — `payload={sr_id, duration_s}` (success) or `{error}` (crash)
- `cancelled` — after `POST /pipeline/{run_id}/cancel`

Heartbeat: SSE comment `: keep-alive` every 15 s.

### `GET /pipeline/result/{run_id}`

After `finished`, returns the final state adapted to the frontend contract:

```json
{
  "report_md": "...",
  "apa_draft": "...",
  "gaps": [...],
  "restricted_papers": [...],
  "stats": { "found": 312, "included": 87, "excluded": 225, "restricted": 14 },
  "kappa": null,
  "sr_id": "abc12345",
  "executive_report_pdf_path": "/data/results/Axiom_Report_abc12345.pdf",
  "cochrane_mode": true,
  "rob_assessments": [...],
  "consensus_clusters": [ /* with grade_* embedded if cochrane */ ]
}
```

The adapter (`_adapt_final_state_for_ui` in `axiom_api.py`) translates internal names (`executive_report_md` → `report_md`) and derives fields not present in the state (`restricted_papers` = filter of `screened_papers` by `is_open=False`; `stats` = counts).

### `GET /pipeline/{run_id}/status`

Lightweight polling. Returns `status` (`queued|running|done|error|cancelled`), `queue_position`, `current_node`, `last_event_id`, timestamps.

### `GET /pipeline/{run_id}/report.pdf`

Returns the unified PDF. `200` (bytes), `202` (in progress), `404` (not generated).

### `POST /pipeline/{run_id}/cancel`

Cooperative cancellation. The current node finishes its in-flight LLM call; the next does not start. Typical latency: seconds for searcher/screener, up to ~3 min for heavy LLM nodes.

### `GET /healthz`

No auth. Returns queue depth, configured models, and `cochrane_mode_enabled`. **Does not ping Featherless** to avoid burning the orchestrator's rate limit.

---

## ⚖️ Cochrane mode (RoB 2.0 + GRADE)

When `cochrane_mode=true` in `POST /pipeline/start`, the graph activates two extra nodes:

**`rob_assessor`** (between `extractor` and `clusterer`):
- Evaluates each paper across the 5 Cochrane RoB 2.0 domains (Sterne et al., *BMJ* 2019).
- Each domain receives a `judgment` ∈ `{low, some, high}` + `rationale`. Domain 1 (randomization) accepts `n/a` for observational studies.
- The `overall` must be mathematically consistent: any domain at `high` implies `overall ≠ low`.

**`grade_profiler`** (between `reconciler` and `gap_finder`):
- Computes per-cluster GRADE certainty (Guyatt et al., *BMJ* 2008).
- Starting certainty: `High` if ≥50% of papers in the cluster are RCTs, `Low` if observational dominates.
- 5 downgrades evaluated: risk_of_bias, inconsistency, indirectness, imprecision, publication_bias.
- 0–3 optional upgrades: large_effect, dose_response, plausible_confounding.
- Final certainty ∈ `{High, Moderate, Low, Very Low}`.

Cost: ~5–10 extra minutes per run, intensive on the reasoning model (DeepSeek-R1). Concurrency is limited to 1 paper / 2 clusters simultaneously to respect the Featherless cap.

**Frontend output:** the RoB & GRADE tab in `screen_results.py` renders a Summary of Findings table with per-cluster certainty and per-paper domain-by-domain detail.

---

## 🧪 Evaluation against gold standards

The `test_prompts/` directory contains 10 pre-built gold-standard cases with full PICOS criteria across distinct domains (telemedicine, NLP in health, mental health, oncology, immunology, robotic surgery, pharmacology, geriatrics, gene therapy, nutrition/microbiome). The `.py` files expose an `initial_state` variable ready for `pipeline.ainvoke(initial_state)`.

Coverage:
- **Mixed languages:** ES + EN + PT (Scielo in LATAM)
- **Designs:** RCT, observational, review, qualitative
- **Time windows:** 5–10 years from 2015–2025
- **Differentiated primary and secondary outcomes** (important for structured extraction)

To run the battery against a running backend:

```bash
# Pseudocode — the real script is smoke_test.sh / evaluate_pipeline.py (gitignored)
for f in test_prompts/*.py; do
  python -c "import json; from $f import initial_state; print(json.dumps(initial_state))" \
    | curl -X POST http://localhost:8080/pipeline/start \
           -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY" \
           -H "Content-Type: application/json" \
           --data-binary @-
done
```

**Metrics we should be capturing (pending):** precision / recall / F1 vs curated DOI lists per test case, inter-rater kappa between screener_7b and screener_32b, total time per node.

---

## ⚠️ Known limitations

Real gaps between the documented contract and current behavior:

- **`kappa` is never computed.** No node writes it to the state; `/pipeline/result/{run_id}` always returns `kappa: null`. The frontend shows `—`. To fix: compute inter-rater agreement between the 7B and the 32B passes inside `screener.py`.

- **Granular SSE events are missing.** The frontend contract mentions `agent_progress`, `stat`, `kappa`. The backend only emits `agent_start` (global, once), `agent_done` (per node), `log`, and `finished`. Per-agent progress bars stay at 0% in real mode (mock mode uses a richer script). Fix: emit `agent_progress` from inside long-running nodes (searcher fetches, screener cascade, writer generation).

- **Ephemeral ChromaDB in `/tmp` / `data/chroma_db`.** Per-run persistence but shared on local disk. Multiple backend instances overwrite the same path. Migrate to managed Pinecone/Weaviate or `pgvector` for multi-tenancy.

- **WeasyPrint fails silently.** If native libs are missing, the pipeline completes but the PDFs come back `None`. Check `data/api_runs/{run_id}/events.jsonl` for warnings.

- **No LangGraph checkpointing.** If the server restarts mid-run, that run is lost. Pending: PostgreSQL Checkpointer (part of the roadmap, phase 2).

- **Paywalled PDFs = `abstract_only`.** Today: if `is_open=False`, the extractor falls back to the abstract. That loses critical methodological detail. Roadmap phase 1 (Bright Data) attacks this directly.

- **Dynamic schemas per domain bucket.** If the user's question doesn't match any known bucket in `domain_ontologies.json`, it falls back to `default`. Current coverage: 8 buckets (medicine, oncology, mental health, immunology, etc.). See `axiom_backend/prompts/domain_ontologies.json`.

---

## 🗺 Future roadmap

### Phase 1 — Bright Data AI Agents Web Data Hackathon (short sprint)

The Achilles' heel of any automated systematic review is access to paywalled / captcha-protected data and expansion into unstructured web content. Today the `extractor` falls back to `abstract_only` whenever `is_open=False`, losing methodology and specific results.

**Key tool:** Bright Data Scraping Browser or Web Unlocker.

**Implementation:**

- When the `searcher` identifies a key paper hosted on strictly restricted platforms (ResearchGate, ScienceDirect, Wiley) that `Unpaywall` or `OpenAlex` flag as blocked, activate Bright Data's Scraping Browser to bypass JS/Captcha blocks and extract full text or download the PDF legally before handing it to the `extractor`.
- **Scope expansion:** don't limit to native academic APIs. Use residential proxies to scrape industry blogs, Google Patents, and government clinical-trial registries (ClinicalTrials.gov, EU CTR, RPCEC in Cuba).

**Code integration points:**
- `axiom_backend/tools/access_check.py` — add a third tier: if `is_open=False`, try Bright Data Web Unlocker before flagging the paper as restricted.
- `axiom_backend/agents/extractor.py:_fetch_and_parse_pdf` — fallback to Scraping Browser when the direct download returns 403/Captcha.
- New `axiom_backend/tools/brightdata_client.py` — async wrapper over Bright Data's API, with retry/backoff and respect for its rate limits.

### Phase 2 — Data robustness and persistence (short term)

- **Real vector persistence.** Replace ephemeral ChromaDB on local disk with:
  - **Cloud SQL with pgvector** (recommended for integration with the LangGraph Checkpointer below)
  - **or Pinecone / Weaviate serverless** (if low latency matters more than control).
  This lets users save their projects and re-query them weeks later without losing the accumulated embeddings.

- **Advanced state management.** PostgreSQL as a persistent **LangGraph Checkpointer**. Benefits:
  - Pause/resume runs if an academic API goes down temporarily
  - Re-evaluate specific branches of the pipeline without re-running everything
  - Full audit: inspect the state after every node

### Phase 3 — Multi-tenant architecture (medium term)

- **Authentication and workspaces.** Integrate Firebase Auth or Auth0 to support multiple users. Create shared "Workspaces" so lab teams or investment analysts can collaborate on a single review.

- **Monetization (billing) with credits.** Featherless' cost-by-token + Bright Data's per-proxy calls enable a transparent credit system:
  - Standard SaaS subscription: N pipeline runs / month on 7B models
  - Heavy DeepSeek-R1 use + premium Bright Data proxies → additional credits
  - Cochrane mode (RoB + GRADE) counts double credits due to reasoning consumption

### Phase 4 — Enterprise-ready (long term)

- **Advanced export.** Today: PDF + Markdown. Roadmap:
  - **`.bib`** and **`.ris`** for Zotero / EndNote / Mendeley
  - **`.docx`** via python-docx (placeholder already disabled in the frontend)
  - **Excel** with editable PRISMA flow table + extractions table

- **Data Provenance Tracker (hallucination audit).** Every claim in the final report should carry a direct link with exact coordinates (page, paragraph) of the original PDF. Implementation:
  - The `extractor` already saves `source_fragments` per extraction (text + page)
  - We still need to propagate those coordinates through `clusterer` → `analyst` → `writer`
  - The `writer_references_node` should emit clickable `[P3§p.4¶2]` links in the final PDF
  - Fully mitigates the corporate hallucination risk: every claim is verifiable against the source document in seconds.

---

## 📜 License and credits

Apache 2.0 — see [LICENSE](LICENSE).

Initially built for the **AMD MI300X Hackathon** (May 2026) and refactored toward serverless infrastructure (Featherless API) for commercial extension.

Stack: Qwen2.5 · DeepSeek-R1 · Kimi-K2 · BGE-M3 · LangGraph · ChromaDB · FastAPI · Streamlit · WeasyPrint · Featherless.


---
---
---

# 🇪🇸 Versión en Español

> Esta es la versión en español del README. La versión en inglés está más arriba.

# Axiom — AI-Powered Academic Due Diligence

**Axiom** automatiza la revisión sistemática PRISMA 2020 end-to-end con un pipeline de 8–10 agentes orquestados sobre LangGraph: descubrimiento multi-API, screening en cascada, extracción estructurada, clustering semántico con BGE-M3, análisis de consensos/contradicciones por modelo dual, detección y verificación de vacíos de investigación, y redacción ejecutiva en Markdown + PDF.

Opcionalmente, en **modo Cochrane**, incorpora evaluación per-paper de Risk of Bias 2.0 y calificación de certeza GRADE por cluster de evidencia.

---

## 📑 Tabla de contenidos

1. [¿Qué resuelve?](#-qué-resuelve)
2. [Arquitectura](#-arquitectura-1)
3. [Pipeline de agentes](#-pipeline-de-agentes)
4. [AxiomState — contrato de datos](#-axiomstate--contrato-de-datos)
5. [Stack tecnológico](#-stack-tecnológico)
6. [Estructura del repositorio](#-estructura-del-repositorio)
7. [Instalación](#-instalación)
8. [Configuración (`.env`)](#-configuración-env)
9. [Ejecución](#-ejecución)
10. [API HTTP](#-api-http)
11. [Modo Cochrane (RoB 2.0 + GRADE)](#-modo-cochrane-rob-20--grade)
12. [Evaluación contra gold standards](#-evaluación-contra-gold-standards)
13. [Limitaciones conocidas](#-limitaciones-conocidas)
14. [Roadmap futuro](#-roadmap-futuro)
15. [Licencia y créditos](#-licencia-y-créditos)

---

## 🎯 ¿Qué resuelve?

Una revisión sistemática manual lleva entre **6 y 24 meses** para un equipo de 2–4 investigadores: búsqueda en ≥3 bases, screening dual ciego de ≥1000 abstracts, extracción de datos, evaluación de sesgos, síntesis narrativa y redacción del manuscrito. Axiom comprime ese flujo a **15–45 minutos por revisión** preservando trazabilidad PRISMA 2020 completa: cada decisión de inclusión, exclusión, extracción y consenso queda registrada en un `events.jsonl` por run.

Casos de uso primarios:

- **Investigación académica** — borradores de revisión sistemática listos para revisión humana, no para publicación directa.
- **Due diligence farmacéutica / biotech** — barrido de evidencia para decisiones de inversión, in-licensing o evaluación de pipelines.
- **Política pública en salud** — síntesis rápida de evidencia para guías clínicas, especialmente en contextos LATAM (Scielo integrado).
- **Vigilancia tecnológica** — monitoreo de literatura emergente sobre una pregunta clínica o técnica.

---

## 🏗 Arquitectura

```
┌──────────────────────────────┐
│  Streamlit Frontend          │
│  (axiom_frontend/)           │  ◀── config · progress SSE · results
└────────────┬─────────────────┘
             │ HTTP + SSE (Bearer token)
             ▼
┌──────────────────────────────┐
│  FastAPI (axiom_api.py)      │
│  · cola secuencial in-proc   │
│  · events.jsonl per-run      │
│  · SSE replay + cancelación  │
│  · adaptador final_state     │
└────────────┬─────────────────┘
             │ pipeline.astream()
             ▼
┌──────────────────────────────────────────────────────────────┐
│                  LangGraph (graph.py)                        │
│                                                              │
│  searcher ─► screener_7b ─► screener_32b ─► extractor ──┐    │
│                                                         │    │
│       ┌──── (modo Cochrane: rob_assessor) ◀────────────┘    │
│       ▼                                                      │
│  clusterer ──┬─► analyst_7b ──┐                              │
│              └─► analyst_32b ─┴─► reconciler                 │
│                                       │                      │
│             (modo Cochrane: grade_profiler)                  │
│                                       ▼                      │
│                                  gapfinder                   │
│                                       │                      │
│       writer_synthesis ──► writer_discussion ──► writer_     │
│       limitations ──► writer_tables ──► writer_references    │
│       ──► writer_assembler ──► END                           │
└──────────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│  Featherless API (LLM inference)     │
│  · Qwen2.5-7B-Instruct (cost 1)      │
│  · DeepSeek-R1-Distill-32B (cost 2)  │
│  · Kimi-K2-Instruct (writer)         │
│  · Cap global: 4 unidades concurr.   │
└──────────────────────────────────────┘
```

**Decisiones clave:**

- **Featherless en vez de vLLM self-hosted.** En la versión hackathon AMD MI300X corríamos dos servidores vLLM locales. La versión actual usa Featherless API (OpenAI-compatible), eliminando la dependencia de GPU on-premise. El cap de 4 unidades concurrentes del plan Premium se protege con un semáforo *credit-based* en `llm_router.py` (Qwen-7B = 1 unidad, DeepSeek-R1 = 2 unidades).
- **Cola secuencial in-process.** `axiom_api.py` procesa los runs uno por uno (`max_queue_size=10`). Correr dos pipelines en paralelo saturaría el cap de Featherless y dispararía 429 en cascada. Esto exige `--workers 1` en uvicorn.
- **`events.jsonl` por run.** Si el consumidor SSE se desconecta, puede reconectarse a `/pipeline/stream/{run_id}?since_event_id=N` y replay completo desde disco.
- **Cancelación cooperativa.** `POST /pipeline/{run_id}/cancel` marca el run en un set; el worker chequea entre chunks de `astream` y aborta antes del próximo nodo (no interrumpe llamadas LLM en vuelo).

---

## 🤖 Pipeline de agentes

| # | Nodo | Modelo | Rol |
|---|---|---|---|
| 1 | `searcher` | Qwen2.5-7B + `instructor` | Descompone la pregunta en 5 queries y consulta PubMed, OpenAlex, arXiv, Crossref y Scielo en paralelo. Dedupe por DOI + (title, year). Enriquece con `check_access_async` (Unpaywall + OpenAlex + Crossref, regla 2-de-3). |
| 2a | `screener_7b` | Qwen2.5-7B | Primer pase PRISMA: decisión rápida por abstract con criterio `confidence` (high/medium/low). |
| 2b | `screener_32b` | DeepSeek-R1-Distill-32B | Adjudicador: solo re-evalúa abstracts donde `screener_7b` marcó `low confidence` o `uncertain`. Si el 32B falla, se conserva el veredicto del 7B (`route="7b_fallback"`). |
| — | `rob_assessor` *(Cochrane)* | DeepSeek-R1 | Evalúa cada paper en los 5 dominios de RoB 2.0 (Sterne et al., BMJ 2019). Solo se ejecuta si `cochrane_mode=True`. |
| 3 | `extractor` | Qwen2.5-7B + `instructor` | PyMuPDF descarga y parsea el PDF si `is_open=True`; cae a `abstract_only` si está paywalled o escaneado. Schema dinámico por bucket de dominio (medicina, oncología, salud mental, etc., definidos en `domain_ontologies.json`). |
| — | `clusterer` | BGE-M3 + `AgglomerativeClustering` | Embeddings densos (fp16) por extracción → ChromaDB persistente → clustering jerárquico por distancia coseno (`threshold=0.7` default). Garantiza que ningún cluster supere `analyst_max_user_chars=28000` (parte greedy en orden original si excede). |
| 4a | `analyst_7b` | Qwen2.5-7B | Análisis de consenso/contradicción por cluster, en paralelo con `analyst_32b`. Salida estructurada (`supporting_papers`, `contradicting_papers`, `neutral_papers`, `agreement_percentage`). |
| 4b | `analyst_32b` | DeepSeek-R1 | Misma tarea con bloque `<think>` para detección de contradicciones más profundas. |
| — | `reconciler` | determinístico | Fusiona 7B + 32B paper-por-paper. Política: 32B canónico; en desacuerdo prioriza el veredicto más escéptico (`contradicting` > `neutral` > `supporting`). Emite `consensus_level` ∈ {full, partial, split}. |
| — | `grade_profiler` *(Cochrane)* | DeepSeek-R1 | Calcula la certeza GRADE por cluster (High/Moderate/Low/Very Low) usando los 5 downgrades (RoB, inconsistency, indirectness, imprecision, publication bias) y 0–3 upgrades. Re-escribe `consensus_clusters` enriquecido. |
| 5 | `gap_finder` | DeepSeek-R1 | Propone 5 gaps (poblacional, metodológico, comparación, temporal, pregunta abierta) → verifica cada uno en OpenAlex. Si `hits < 50` → confirmado; si `< 500` → emergente; si más → rechazado. Si los 5 son rechazados, rescata los 2 con menos hits. |
| 6 | `writer_*` | Kimi-K2-Instruct (LLM) + Python | Bifásico: 3 nodos LLM (`synthesis`, `discussion`, `limitations`) + 3 nodos Python deterministas (`tables`, `references`, `assembler`). El `assembler` concatena todo y renderiza UN PDF unificado con WeasyPrint. |

**Fan-out / fan-in.** Después de `clusterer`, `analyst_7b` y `analyst_32b` corren concurrentes (consumen 2 + 2 = 4 unidades, exactamente el cap de Featherless). El `reconciler` espera a ambos via reducers `Annotated[list, operator.add]` sobre `synthesis_7b` / `synthesis_32b`.

**Skip condicional.** Si el screener rechaza todos los abstracts, el grafo enruta `screener_32b` → `writer_synthesis` directamente para entregar un reporte de "no eligible studies" en vez de crashear (`check_screening_results` en `graph.py`).

**Modo Cochrane condicional.** Los nodos `rob_assessor` y `grade_profiler` solo se invocan si `state["cochrane_mode"]=True`. El kill-switch global `settings.cochrane_mode_enabled=False` los desactiva incluso si el frontend los pide (útil cuando Featherless está rate-limitado).

---

## 📦 AxiomState — contrato de datos

LangGraph propaga un único `TypedDict` entre nodos. Cada nodo devuelve solo las claves que modificó; LangGraph mergea según el *reducer* declarado por campo.

```python
class AxiomState(TypedDict, total=False):
    # Inputs
    sr_id: str
    domain: str
    question: str
    prisma_criteria: dict
    cochrane_mode: bool
    output_language: str  # "English" | "Spanish" | "auto"

    # Acumulables (operator.add — concurrent-safe)
    errors:          Annotated[list[dict], operator.add]
    papers_found:    Annotated[list[dict], operator.add]   # searcher
    screened_papers: Annotated[list[dict], operator.add]   # screener (include + uncertain)
    papers_excluded: Annotated[list[dict], operator.add]   # screener (auditoría PRISMA)
    extractions:     Annotated[list[dict], operator.add]   # extractor
    rob_assessments: Annotated[list[dict], operator.add]   # rob_assessor (Cochrane)
    synthesis_7b:    Annotated[list[dict], operator.add]   # analyst_7b
    synthesis_32b:   Annotated[list[dict], operator.add]   # analyst_32b

    # Escrituras atómicas (único productor)
    papers_to_escalate: list[dict]    # screener_7b → screener_32b
    clusters:           list[list[dict]]    # clusterer
    consensus_clusters: list[dict]          # reconciler (re-escrito por grade_profiler en Cochrane)
    research_gaps:      list[dict]          # gap_finder

    # Writer (6 sub-nodos secuenciales)
    writer_synthesis_md:   str   # writer_synthesis (LLM)
    writer_discussion_md:  str   # writer_discussion (LLM)
    writer_limitations_md: str   # writer_limitations (LLM)
    writer_tables_md:      str   # writer_tables (Python)
    writer_references_md:  str   # writer_references (Python)

    # Output final
    executive_report_md:        str
    executive_report_pdf_path:  str | None
```

**Errores nunca abortan el grafo:** los nodos que fallan apendan a `errors` y los nodos downstream degradan gracefully. Esto significa que un searcher que pierde 1 de 5 APIs sigue produciendo papers; un extractor que falla en parsear un PDF cae al abstract; un analyst_32b que cae deja que el reconciler use el veredicto del 7B.

Ver `axiom_backend/state.py` para los comentarios completos por campo.

---

## 🛠 Stack tecnológico

| Capa | Tecnología | Por qué |
|---|---|---|
| Orquestación | **LangGraph** 0.2+ | Stateful DAG, reducers para fan-in concurrente, checkpointing nativo (a futuro). |
| Inferencia LLM | **Featherless** (OpenAI-compatible) | Cost-by-token, 0 GPU on-premise, soporte de modelos abiertos (Qwen, DeepSeek, Kimi). |
| Modelo rápido | **Qwen2.5-7B-Instruct** | Throughput alto para screening masivo y extracción JSON con `instructor`. |
| Modelo razonador | **DeepSeek-R1-Distill-Qwen-32B** | Chain-of-thought explícito (`<think>...`) para contradicciones, RoB, GRADE, gap finding. |
| Modelo redactor | **Kimi-K2-Instruct** | Narrativa larga coherente (executive report + APA 7) sin perder hilo. |
| Embeddings | **BGE-M3** (`BAAI/bge-m3`) via `FlagEmbedding` | Multilingüe (relevante para Scielo en portugués/español), dense + sparse + multi-vector. |
| Vector store | **ChromaDB** persistente | Simple, embebido, sin servidor. Persiste en `data/chroma_db/`. |
| Clustering | `sklearn.cluster.AgglomerativeClustering` | Jerárquico por distancia coseno; no requiere `n_clusters` a priori. |
| PDF parsing | **PyMuPDF** (fitz) | Texto extraído rápido; fallback a `abstract_only` cuando es escaneado. |
| Validación de schema | **Pydantic v2** + **instructor** | JSON guarantee de LLMs sin reintentos manuales. |
| API HTTP | **FastAPI** + **uvicorn** | Async nativo, SSE streaming, dependency injection (`Depends(require_bearer)`). |
| Frontend | **Streamlit** 1.42 | Iteración rápida; i18n manual (ES/EN); SSE consumer en `pipeline_runner.py`. |
| Reportes PDF | **WeasyPrint** | Markdown → HTML → PDF con CSS modular. |
| Search APIs | PubMed (Entrez), OpenAlex, arXiv, Crossref, Scielo | Cobertura biomédica + multidisciplinar + LATAM. |
| Access check | Unpaywall + OpenAlex + Crossref (2-de-3) | Reduce falsos positivos en `is_open`; el extractor solo descarga si el voto mayoritario es OA. |

---

## 📂 Estructura del repositorio

```
axiom/
├── main.py                          ◀── Entrypoint uvicorn (preflight checks + logging)
├── requirements.txt
├── README.md                        ◀── Este archivo
├── .env                             ◀── (no versionado — usar .env.example)
├── .gitignore
│
├── axiom_backend/                   ◀── Backend FastAPI + LangGraph
│   ├── __init__.py
│   ├── axiom_api.py                 ◀── FastAPI · cola · SSE · adaptador final_state
│   ├── graph.py                     ◀── build_axiom_graph() · routing condicional
│   ├── state.py                     ◀── AxiomState TypedDict + reducers
│   ├── config.py                    ◀── pydantic-settings (carga .env)
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── searcher.py              ◀── 5 APIs · dedupe · access_check
│   │   ├── screener.py              ◀── cascada 7B → 32B (screener_7b_node, screener_32b_node)
│   │   ├── extractor.py             ◀── PyMuPDF + schema dinámico por bucket
│   │   ├── rob_assessor.py          ◀── Cochrane RoB 2.0 (5 dominios)
│   │   ├── analyst_7b.py            ◀── consenso por cluster (rápido)
│   │   ├── analyst_32b.py           ◀── consenso por cluster (con <think>)
│   │   ├── grade_profiler.py        ◀── GRADE certainty per cluster
│   │   ├── gap_finder.py            ◀── 5 categorías + verificación OpenAlex
│   │   └── writer.py                ◀── 6 sub-nodos: synthesis → discussion → limitations
│   │                                      → tables → references → assembler
│   │
│   ├── tools/
│   │   ├── llm_router.py            ◀── Featherless client · credit semaphore · JSON extractor
│   │   ├── clusterer.py             ◀── BGE-M3 + ChromaDB + AgglomerativeClustering
│   │   ├── reconciler.py            ◀── merge determinista 7B↔32B
│   │   ├── access_check.py          ◀── Unpaywall + OpenAlex + Crossref (2-de-3)
│   │   └── pdf_parser.py            ◀── PyMuPDF + abstract_only fallback
│   │
│   ├── prompts/                     ◀── *.md cargados al import (fail-loud si falta)
│   │   ├── __init__.py              ◀── _read_text/_read_json + validación de schema
│   │   ├── searcher_prompt.md
│   │   ├── screener_prompt_7b.md
│   │   ├── screener_fewshot_7b.md
│   │   ├── screener_prompt_32b.md
│   │   ├── screener_fewshot_32b.md
│   │   ├── extraction_prompt.md
│   │   ├── rob_assessor_prompt.md
│   │   ├── analyst_prompt_v3.md     ◀── analyst_7b
│   │   ├── analyst_prompt_r1.md     ◀── analyst_32b (DeepSeek-R1)
│   │   ├── grade_profiler_prompt.md
│   │   ├── gapfinder_prompt.md
│   │   ├── writer_synthesis_prompt.md
│   │   ├── writer_discussion_prompt.md
│   │   ├── writer_limitations_prompt.md
│   │   ├── writer_apa7_rules.md
│   │   ├── extractor_schema.json
│   │   ├── domain_ontologies.json
│   │   └── prisma_criteria_template.json
│   │
│   └── utils/
│       └── language.py              ◀── resolve_output_language (auto/ES/EN)
│
├── axiom_frontend/                  ◀── Streamlit UI (thin client)
│   ├── app.py                       ◀── 3-screen router (config → progress → results)
│   ├── ui/
│   │   ├── screen_config.py         ◀── form PRISMA + Cochrane toggle
│   │   ├── screen_progress.py       ◀── 9 agent rows + SSE consumer + cancel
│   │   ├── screen_results.py        ◀── tabs: report, gaps, restricted, RoB/GRADE
│   │   └── components.py
│   ├── utils/
│   │   ├── api_client.py            ◀── HTTP + SSE client + PDF download
│   │   ├── pipeline_runner.py       ◀── mock/real event stream
│   │   └── i18n.py                  ◀── tablas ES/EN + LOG_PATTERNS
│   └── assets/gifs/                 ◀── animaciones por agente
│
├── test_prompts/                    ◀── 10 gold-standard test cases (PICOS prefilled)
│   ├── 01_telemedicine_diabetes.py
│   ├── 02_nlp_health_llm_triage.py
│   ├── 03_salud_mental_tcc_adolescentes.py
│   ├── 04_oncologia_pancreatic_liquid_biopsy.py
│   ├── 05_immunology_mrna_transplant.py
│   ├── 06_surgery_robotic_colorectal.py
│   ├── 07_pharmacology_cre_icu_latam.py
│   ├── 08_geriatria_ejercicio_demencia.py
│   ├── 09_gene_therapy_crispr_sickle_cell.py
│   └── 10_nutrition_mediterranean_microbiome.py
│
└── data/                            ◀── creado en runtime (gitignored)
    ├── api_runs/{run_id}/
    │   ├── initial_state.json
    │   ├── events.jsonl
    │   ├── meta.json
    │   └── final_state.json
    ├── results/
    │   └── Axiom_Report_{sr_id}.pdf
    └── chroma_db/                   ◀── persistencia BGE-M3 (ver § Limitaciones)
```

> **Nota histórica:** versiones anteriores ubicaban el backend bajo `src/agents/` y `src/tools/`. Toda referencia a `src/` en commits viejos corresponde al actual `axiom_backend/`.

---

## ⚙️ Instalación

### 1 · Dependencias de sistema (WeasyPrint)

WeasyPrint requiere librerías nativas de Pango/Cairo que **no se instalan con pip**:

```bash
sudo apt-get update && sudo apt-get install -y \
  libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
  libgdk-pixbuf2.0-0 shared-mime-info
```

Sin estas libs, `weasyprint` importa pero falla silenciosamente al renderizar — el pipeline termina, pero `executive_report_pdf_path` queda en `None`.

### 2 · Python ≥ 3.11 + entorno virtual

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3 · Modelo BGE-M3 (pre-descarga)

`FlagEmbedding` descarga `BAAI/bge-m3` (~2.3 GB) la primera vez que se invoca `BGEM3FlagModel("BAAI/bge-m3")`. Para evitar latencia en el primer run, pre-descargá en build time:

```bash
python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)"
```

El modelo se cachea en `~/.cache/huggingface/hub/`. Si querés moverlo, exportá `HF_HOME=/path/to/cache` antes.

### 4 · GPU (opcional, recomendado para BGE-M3)

`clusterer.py` instancia BGE-M3 con `device="cuda"`. Si corrés en CPU-only, editá `axiom_backend/tools/clusterer.py:35` para usar `device="cpu"` (los embeddings serán ~10× más lentos pero el pipeline funciona).

### 5 · Frontend (separado)

El frontend Streamlit corre como proceso independiente:

```bash
cd axiom_frontend
streamlit run app.py --server.port 8501
```

Apunta al backend via la env var `AXIOM_BACKEND_URL` (default `http://localhost:8080`).

---

## 🔐 Configuración (`.env`)

Cargado por `axiom_backend/config.py` vía `pydantic-settings`. Variables desconocidas se ignoran.

| Variable | Requerido | Default | Uso |
|---|---|---|---|
| `FEATHERLESS_API_KEY` | ✅ | — | Bearer para llamadas a Featherless. Sin esto: 401 en cada nodo LLM. |
| `CONTACT_EMAIL` | ✅ | — | User-Agent para polite-pools de Crossref/OpenAlex/Unpaywall. Sin esto: startup falla. |
| `AXIOM_BACKEND_API_KEY` | ✅ | — | Bearer que el frontend envía en `Authorization`. Endpoints protegidos rechazan sin esto. |
| `FEATHERLESS_BASE_URL` | optional | `https://api.featherless.ai/v1` | Endpoint OpenAI-compatible. |
| `FEATHERLESS_MAX_CONCURRENT` | optional | `4` | Cap del semáforo global. Premium = 4; excederlo dispara 429s. |
| `MODEL_7B_NAME` | optional | `Qwen/Qwen2.5-7B-Instruct` | ID en featherless.ai/models. |
| `MODEL_32B_NAME` | optional | `deepseek-ai/DeepSeek-R1-Distill-Qwen-32B` | Modelo razonador. |
| `MODEL_WRITER_NAME` | optional | `moonshotai/Kimi-K2-Instruct-0905` | Modelo de redacción. |
| `MODEL_LIGHT_REASONING_NAME` | optional | `Qwen/Qwen2.5-7B-Instruct` | Reservado para futuro analyst_7b. |
| `COCHRANE_MODE_ENABLED` | optional | `true` | Kill-switch global de RoB + GRADE (independiente del toggle del frontend). |
| `COCHRANE_ROB_TIMEOUT_S` | optional | `120` | Timeout por paper en RoB. |
| `COCHRANE_GRADE_TIMEOUT_S` | optional | `180` | Timeout por cluster en GRADE. |
| `PUBMED_API_KEY` | optional | — | Sube rate limit de PubMed de 3 → 10 req/s. |
| `OPENALEX_API_KEY` | optional | — | Polite-pool premium de OpenAlex. |
| `CHROMA_PERSIST_DIR` | optional | `./data/chroma_db` | Dónde persiste ChromaDB. |
| `CLUSTER_DISTANCE_THRESHOLD` | optional | `0.7` | Distancia coseno (NO similitud). 0.3–0.4 = near-duplicates; 0.5 = mismo subtopic; 0.6–0.7 = dominio general. |
| `ANALYST_MAX_USER_CHARS` | optional | `28000` | Tope duro del JSON de cluster enviado al analyst. Sobre eso, el clusterer parte greedy. |
| `MAX_QUEUE_SIZE` | optional | `10` | Cap de runs en cola simultánea. Más allá: `503`. |
| `AXIOM_MAX_RESULTS_PER_API` | optional | `50` | Cap por API académica. Para evaluación contra gold standards subir a 200–300 (cuidado con 429 de arXiv). |
| `AXIOM_LOG_LEVEL` | optional | `INFO` | DEBUG/INFO/WARN/ERROR. |
| `AXIOM_RELOAD` | optional | `0` | Solo desarrollo local. NUNCA en prod (mata runs en cola). |
| `PORT` | optional | `8080` | Cloud Run lo inyecta automáticamente. |

Ejemplo de `.env`:

```ini
FEATHERLESS_API_KEY=ftl_xxx...
CONTACT_EMAIL=tuemail@dominio.org
AXIOM_BACKEND_API_KEY=$(openssl rand -hex 32)

# Opcionales recomendados
PUBMED_API_KEY=...
OPENALEX_API_KEY=...
AXIOM_MAX_RESULTS_PER_API=200
```

---

## 🚀 Ejecución

### Backend

```bash
python main.py
```

`main.py` corre `_preflight_check()` antes de invocar uvicorn: aborta si `FEATHERLESS_API_KEY`, `CONTACT_EMAIL` o `AXIOM_BACKEND_API_KEY` faltan, con mensajes claros. Esto evita el caso en que el server arranca, acepta requests y todas fallan con errores oscuros.

Bandera importante: **`workers=1` es obligatorio.** La cola asyncio, el `_current_run` y el mapa de subscriptores SSE son in-process. Múltiples workers fragmentarían la cola y romperían SSE replay.

### Frontend

```bash
cd axiom_frontend
AXIOM_BACKEND_URL=http://localhost:8080 \
AXIOM_BACKEND_API_KEY=<misma-key-que-backend> \
streamlit run app.py
```

Si querés correr el frontend sin backend (demo offline), seteá `AXIOM_MOCK=1` y el `pipeline_runner` reproduce un script determinista.

### Smoke test

```bash
# Health
curl http://localhost:8080/healthz

# Submit
curl -X POST http://localhost:8080/pipeline/start \
  -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY" \
  -H "Content-Type: application/json" \
  -d @test_prompts/03_salud_mental_tcc_adolescentes.json

# Stream
curl -N http://localhost:8080/pipeline/stream/<run_id> \
  -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY"
```

---

## 🔌 API HTTP

Todos los endpoints (excepto `/healthz`) requieren `Authorization: Bearer ${AXIOM_BACKEND_API_KEY}`.

### `POST /pipeline/start`

Submite un run. Devuelve inmediatamente; el pipeline real arranca cuando el worker secuencial lo toma.

**Request:**
```json
{
  "sr_id": "abc12345",
  "domain": "salud_mental",
  "question": "¿Cuál es la efectividad de las aplicaciones móviles de TCC para reducir síntomas depresivos en adolescentes 12-18 años?",
  "cochrane_mode": true,
  "output_language": "Spanish",
  "prisma_criteria": { /* ver test_prompts/*.py */ }
}
```

Body cap: 100 KB. Cola cap: `MAX_QUEUE_SIZE` (default 10). Si la cola está llena: `503`.

**Response (202):**
```json
{ "run_id": "abc12345", "status": "queued", "queue_position": 0, "created_at": "2026-05-19T15:00:00Z" }
```

### `GET /pipeline/stream/{run_id}` (SSE)

Server-Sent Events. Replay desde `events.jsonl`, luego subscribe a eventos live hasta `finished` o `error`.

**Query param:** `since_event_id` (int, default 0) para resubscripción.

**Tipos emitidos:**
- `agent_start` — `payload={sr_id}`
- `agent_done` — `payload={ui_label, is_internal, stats}`
- `log` — captura de `logging.getLogger("axiom_backend")`
- `finished` — `payload={sr_id, duration_s}` (éxito) o `{error}` (crash)
- `cancelled` — tras `POST /pipeline/{run_id}/cancel`

Heartbeat: comentario SSE `: keep-alive` cada 15 s.

### `GET /pipeline/result/{run_id}`

Tras `finished`, devuelve el state final adaptado al contrato del frontend:

```json
{
  "report_md": "...",
  "apa_draft": "...",
  "gaps": [...],
  "restricted_papers": [...],
  "stats": { "found": 312, "included": 87, "excluded": 225, "restricted": 14 },
  "kappa": null,
  "sr_id": "abc12345",
  "executive_report_pdf_path": "/data/results/Axiom_Report_abc12345.pdf",
  "cochrane_mode": true,
  "rob_assessments": [...],
  "consensus_clusters": [ /* con grade_* embebidos si cochrane */ ]
}
```

El adaptador (`_adapt_final_state_for_ui` en `axiom_api.py`) traduce nombres internos (`executive_report_md` → `report_md`) y deriva campos no presentes en el state (`restricted_papers` = filtro de `screened_papers` por `is_open=False`; `stats` = counts).

### `GET /pipeline/{run_id}/status`

Polling liviano. Devuelve `status` (`queued|running|done|error|cancelled`), `queue_position`, `current_node`, `last_event_id`, timestamps.

### `GET /pipeline/{run_id}/report.pdf`

Devuelve el PDF unificado. `200` (bytes), `202` (en progreso), `404` (no generado).

### `POST /pipeline/{run_id}/cancel`

Cancelación cooperativa. El nodo en curso termina su llamada actual al LLM; el siguiente no arranca. Latencia típica: segundos para searcher/screener, hasta ~3 min para nodos LLM pesados.

### `GET /healthz`

Sin auth. Devuelve queue depth, modelos configurados y `cochrane_mode_enabled`. **No pinga Featherless** para no consumir rate limit del orquestador.

---

## ⚖️ Modo Cochrane (RoB 2.0 + GRADE)

Cuando `cochrane_mode=true` en `POST /pipeline/start`, el grafo activa dos nodos adicionales:

**`rob_assessor`** (entre `extractor` y `clusterer`):
- Evalúa cada paper en los 5 dominios de Cochrane RoB 2.0 (Sterne et al., *BMJ* 2019).
- Cada dominio recibe `judgment` ∈ `{low, some, high}` + `rationale`. Domain 1 (randomization) acepta `n/a` para estudios observacionales.
- El `overall` debe ser matemáticamente consistente: cualquier dominio en `high` implica `overall ≠ low`.

**`grade_profiler`** (entre `reconciler` y `gap_finder`):
- Calcula la certeza GRADE por cluster (Guyatt et al., *BMJ* 2008).
- Starting certainty: `High` si ≥50% de papers del cluster son RCT, `Low` si predominan observacionales.
- 5 downgrades evaluados: risk_of_bias, inconsistency, indirectness, imprecision, publication_bias.
- 0–3 upgrades opcionales: large_effect, dose_response, plausible_confounding.
- Final certainty ∈ `{High, Moderate, Low, Very Low}`.

Costo: ~5–10 min adicionales por run, consume el modelo razonador (DeepSeek-R1) intensivamente. Concurrencia limitada a 1 paper / 2 clusters simultáneos para respetar el cap de Featherless.

**Salida al frontend:** la pestaña RoB & GRADE en `screen_results.py` renderiza una Summary of Findings table con la certeza por cluster y el detalle dominio-por-dominio por paper.

---

## 🧪 Evaluación contra gold standards

El directorio `test_prompts/` contiene 10 casos de oro pre-armados con criterios PICOS completos en distintos dominios (telemedicina, NLP en salud, salud mental, oncología, inmunología, cirugía robótica, farmacología, geriatría, terapia génica, nutrición/microbioma). Los archivos `.py` exponen una variable `initial_state` lista para `pipeline.ainvoke(initial_state)`.

Cobertura:
- **Idiomas mixtos:** ES + EN + PT (Scielo en LATAM)
- **Diseños:** RCT, observacional, revisión, cualitativo
- **Ventanas temporales:** 5–10 años desde 2015–2025
- **Outcomes primarios y secundarios diferenciados** (importante para extracción estructurada)

Para correr la batería contra el backend levantado:

```bash
# Pseudocódigo — el script real es smoke_test.sh / evaluate_pipeline.py (gitignored)
for f in test_prompts/*.py; do
  python -c "import json; from $f import initial_state; print(json.dumps(initial_state))" \
    | curl -X POST http://localhost:8080/pipeline/start \
           -H "Authorization: Bearer $AXIOM_BACKEND_API_KEY" \
           -H "Content-Type: application/json" \
           --data-binary @-
done
```

**Métricas que deberíamos estar capturando (pendiente):** precision / recall / F1 vs lista curada de DOIs por test case, kappa inter-rater entre screener_7b y screener_32b, tiempo total por nodo.

---

## ⚠️ Limitaciones conocidas

Gaps reales entre el contrato documentado y el comportamiento actual:

- **`kappa` nunca se computa.** Ningún nodo lo escribe en el state; `/pipeline/result/{run_id}` siempre devuelve `kappa: null`. El frontend muestra `—`. Para fixearlo: computar acuerdo inter-rater entre el pase 7B y el pase 32B dentro de `screener.py`.

- **Eventos SSE granulares ausentes.** El contrato del frontend menciona `agent_progress`, `stat`, `kappa`. El backend solo emite `agent_start` (global, único), `agent_done` (por nodo), `log` y `finished`. Las barras de progreso por agente quedan en 0% en real mode (mock mode usa un script más rico). Fix: emitir `agent_progress` desde dentro de nodos largos (searcher fetches, screener cascade, writer generation).

- **ChromaDB efímera en `/tmp` / `data/chroma_db`.** Persistencia por run pero compartida en disco local. Múltiples instancias del backend pisan el mismo path. Migrar a Pinecone/Weaviate gestionado o `pgvector` para multi-tenancy.

- **WeasyPrint falla silenciosa.** Si faltan libs nativas, el pipeline completa pero los PDFs salen en `None`. Chequear `data/api_runs/{run_id}/events.jsonl` para warnings.

- **Sin checkpointing de LangGraph.** Si el server reinicia mid-run, ese run se pierde. Pendiente: PostgreSQL Checkpointer (parte del roadmap, fase 2).

- **PDFs paywalled = `abstract_only`.** Hoy: si `is_open=False`, el extractor cae al abstract. Eso pierde detalles metodológicos críticos. Roadmap fase 1 (Bright Data) ataca esto.

- **Schemas dinámicos por bucket de dominio.** Si la pregunta del usuario no matchea ningún bucket conocido en `domain_ontologies.json`, cae a `default`. Cobertura actual: 8 buckets (medicina, oncología, salud mental, inmunología, etc.). Ver `axiom_backend/prompts/domain_ontologies.json`.

---

## 🗺 Roadmap futuro

### Fase 1 — Bright Data AI Agents Web Data Hackathon (sprint corto)

El talón de Aquiles de cualquier revisión sistemática automatizada es el acceso a datos protegidos por paywalls/captchas y la expansión a web no estructurada. Hoy el `extractor` cae a `abstract_only` cuando `is_open=False`, perdiendo metodología y resultados específicos.

**Herramienta clave:** Bright Data Scraping Browser o Web Unlocker.

**Implementación:**

- Cuando el `searcher` identifica un paper clave en plataformas con restricciones estrictas (ResearchGate, ScienceDirect, Wiley) que `Unpaywall` o `OpenAlex` marquen como bloqueado, activar el Scraping Browser de Bright Data para saltar los bloqueos JS/Captcha y extraer el texto completo o descargar el PDF de manera legal antes de pasarlo al `extractor`.
- **Expansión del scope:** no limitarse a APIs académicas nativas. Usar proxies residenciales para scrapear blogs de la industria, patentes de Google Patents y registros de ensayos clínicos gubernamentales (ClinicalTrials.gov, EU CTR, RPCEC en Cuba).

**Puntos de integración en código:**
- `axiom_backend/tools/access_check.py` — agregar tercer-tier: si `is_open=False`, intentar Bright Data Web Unlocker antes de marcar el paper como restringido.
- `axiom_backend/agents/extractor.py:_fetch_and_parse_pdf` — fallback a Scraping Browser cuando la descarga directa devuelve 403/Captcha.
- Nuevo `axiom_backend/tools/brightdata_client.py` — wrapper async sobre la API de Bright Data, con retry/backoff y respeto a sus rate limits.

### Fase 2 — Robustez de datos y persistencia (corto plazo)

- **Persistencia vectorial real.** Cambiar ChromaDB efímera en disco local por:
  - **Cloud SQL con pgvector** (recomendado para integración con LangGraph Checkpointer abajo)
  - **o Pinecone / Weaviate serverless** (si priorizamos baja latencia sobre control).
  Permite que los usuarios guarden sus proyectos y los re-consulten semanas después sin perder los embeddings acumulados.

- **Gestión de estados avanzada.** PostgreSQL como **LangGraph Checkpointer** persistente. Beneficios:
  - Pausar/reanudar runs si una API académica cae temporalmente
  - Re-evaluar ramas específicas del pipeline sin re-correr todo
  - Auditoría completa: revisar el state después de cada nodo

### Fase 3 — Arquitectura multi-tenant (mediano plazo)

- **Autenticación y workspaces.** Integrar Firebase Auth o Auth0 para soportar múltiples usuarios. Crear "Workspaces" compartidos para que equipos de laboratorios o analistas de inversión colaboren en una misma revisión.

- **Monetización (billing) con créditos.** El modelo de cost-by-token de Featherless + llamadas por proxy de Bright Data permite un sistema de créditos transparente:
  - Suscripción SaaS estándar: N ejecuciones / mes con modelos 7B
  - Uso intensivo de DeepSeek-R1 + Bright Data premium proxies → créditos adicionales
  - Modo Cochrane (RoB + GRADE) cuenta el doble de créditos por su consumo de razonamiento

### Fase 4 — Enterprise-ready (largo plazo)

- **Exportación avanzada.** Hoy: PDF + Markdown. Roadmap:
  - **`.bib`** y **`.ris`** para Zotero / EndNote / Mendeley
  - **`.docx`** vía python-docx (ya hay placeholder deshabilitado en el frontend)
  - **Excel** con tabla PRISMA flow + extractions table editable

- **Data Provenance Tracker (auditoría de alucinaciones).** Cada afirmación del reporte final debe contener un enlace directo con coordenadas exactas (página, párrafo) del PDF original. Implementación:
  - El `extractor` ya guarda `source_fragments` por extracción (texto + página)
  - Falta propagar esas coordenadas a través del `clusterer` → `analyst` → `writer`
  - El `writer_references_node` debe emitir enlaces `[P3§p.4¶2]` clickables en el PDF final
  - Mitiga por completo el riesgo corporativo de alucinación: cada claim es verificable en segundos contra el documento fuente

---

## 📜 Licencia y créditos

Apache 2.0 — ver [LICENSE](LICENSE).

Construido inicialmente para el **AMD MI300X Hackathon** (mayo 2026) y refactorizado hacia infraestructura serverless (Featherless API) para extensión comercial.

Stack: Qwen2.5 · DeepSeek-R1 · Kimi-K2 · BGE-M3 · LangGraph · ChromaDB · FastAPI · Streamlit · WeasyPrint · Featherless.
