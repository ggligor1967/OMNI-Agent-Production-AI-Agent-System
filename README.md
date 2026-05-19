# OMNI Agent — Production AI Agent System

A fully modular, production-ready AI agent built in Python.  
Routes intelligently across **27 cloud models**, supports RAG, pipelines, caching, prompt templates, and a rich interactive CLI.

## Documentation Contract

Documentation baseline: phase-2-complete

- Model catalog source of truth: `agent/model_registry.py` (**27 cloud models**)
- Support matrix path: `tests/SUPPORT_MATRIX.md`
- ADR index: `docs/adr/README.md`
- CI release gate: Python 3.12 and 3.13 with `pytest tests/ -q`, `ruff check .`, and `python tools/check_documentation_consistency.py --root .`
- Storage strategy: SQLite for local development and tests; Postgres is the production target (see `docs/adr/ADR-003-db-strategy.md`)

---

## Quick Start

```bash
unzip omni_agent_v4.zip && cd omni_agent
pip install -r requirements.txt
cp .env.example .env          # Edit TELEGRAM_TOKEN, OLLAMA_BASE_URL
ollama serve &
python main.py --mode cli     # Interactive Rich CLI
python main.py --mode api     # REST API on :8000
python main.py --mode telegram
python main.py --mode all     # All modes simultaneously
```

Dashboard: **http://localhost:8000/dashboard**

### One-click local dashboard start (Windows)

If you want to start OMNI with a single click on Windows, use the launchers in the project root:

```powershell
.\start_dashboard.bat
```

Additional launchers:

```powershell
.\stop_dashboard.bat
.\start_cli.bat
.\stop_cli.bat
.\start_telegram.bat
.\stop_telegram.bat
.\start_all.bat
.\stop_all.bat
```

What it does:

- prefers the working interpreter in `.venv-1`
- falls back to `.venv` or global `python` if needed
- disables auth for local dashboard use by setting `AUTH_ENFORCE=false`
- opens the dashboard automatically in your browser
- reuses the existing local dashboard if it is already running

Launcher notes:

- `start_dashboard.bat` starts or reuses OMNI API mode and opens the dashboard
- `stop_dashboard.bat` stops OMNI processes running in `api` or `all` mode because they own the dashboard server
- `start_cli.bat` opens the Rich CLI in a new PowerShell window
- `stop_cli.bat` stops OMNI processes started with `--mode cli`
- `start_telegram.bat` starts Telegram mode and warns if `TELEGRAM_TOKEN` is missing
- `stop_telegram.bat` stops OMNI processes started with `--mode telegram`
- `start_all.bat` starts OMNI in `all` mode and opens the dashboard when the API is ready
- `stop_all.bat` stops OMNI processes started with `--mode all`

---

## Architecture

```
omni_agent/
├── agent/
│   ├── core.py               # Main orchestrator — wires all subsystems
│   ├── multi_model_client.py # Unified async LLM interface (27 models)
│   ├── model_registry.py     # Complete 27-model catalog with metadata
│   ├── model_router.py       # Task classifier + smart routing engine
│   ├── ollama_client.py      # Raw Ollama API client
│   ├── rag.py                # RAG pipeline: ingest → chunk → embed → retrieve
│   ├── cache.py              # Redis cache (memory fallback)
│   ├── prompt_templates.py   # Named prompt templates with variables
│   ├── pipeline.py           # Multi-step agentic pipeline executor
│   ├── summarizer.py         # Conversation auto-compressor
│   ├── cli.py                # Rich interactive CLI
│   ├── memory.py             # SQLite persistent memory (8 tables)
│   ├── database.py           # DB abstraction (SQLite / PostgreSQL)
│   ├── hooks.py              # Async event system (20+ event types)
│   ├── scheduler.py          # Cron scheduler + heartbeat monitor
│   ├── skills_manager.py     # Dynamic skills with trigger matching
│   ├── social.py             # Intent detection + persona system
│   ├── collaboration.py      # Workspaces, tasks, notes
│   ├── doc_generator.py      # Auto Markdown documentation
│   ├── dashboard.py          # Real-time web dashboard
│   ├── telegram_bot.py       # Full Telegram bot (10 commands)
│   └── tools/
│       └── __init__.py       # WebScraper, CodeExecutor, SemanticAnalyzer, SecurityToolkit
├── agent/skills/
│   └── example_skills.py
├── tests/
│   ├── test_suite.py         # Core module tests (50+)
│   ├── test_models.py        # Model registry + router tests (45+)
│   └── test_new_modules.py   # RAG, cache, templates, pipeline, summarizer (80+)
├── config.py
├── main.py
├── requirements.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

---

## The 27 Cloud Models

| Provider | Models | Best For |
|---|---|---|
| **Alibaba** | qwen3-vl:235b-instruct-cloud, qwen3-coder-next:cloud, qwen3-coder:480b-cloud, qwen3-next:80b-cloud, qwen3.5:cloud, qwen3-vl:235b-cloud | Vision, coding, general |
| **Zhipu AI** | glm-5:cloud, glm-4.7:cloud | Chinese/bilingual, reasoning |
| **DeepSeek** | deepseek-v3.1:671b-cloud, deepseek-v3.2:cloud | Math, reasoning, code |
| **OpenAI OSS** | gpt-oss:120b-cloud, gpt-oss:20b-cloud | General, creative, fast |
| **Google** | gemini-3-flash-preview:cloud (1M ctx), gemma3:12b-cloud, gemma3:4b-cloud | Long context, vision, speed |
| **Mistral AI** | mistral-large-3:675b-cloud, ministral-3:8b-cloud, devstral-2:123b-cloud, devstral-small-2:24b-cloud | Multilingual, code, agents |
| **NVIDIA** | nemotron-3-nano:30b-cloud, nemotron-3-super:cloud | Enterprise, structured output |
| **MiniMax** | minimax-m2:cloud, minimax-m2.5:cloud, minimax-m2.7:cloud | 1M context, creative, long docs |
| **Cogito** | cogito-2.1:671b-cloud | Formal reasoning, math proofs |
| **Moonshot AI** | kimi-k2.5:cloud | Long context, Chinese/English |
| **RNJ Labs** | rnj-1:8b-cloud | Fast inference, edge |

### Auto-Routing

Every message is automatically classified and routed to the best model:

```
"Write a Python function"   → qwen3-coder-next:cloud   (code)
"What is ∫x²dx?"           → deepseek-v3.1:671b-cloud  (math)
"Translate to French"       → mistral-large-3:675b-cloud (multilingual)
"Summarize this 500-page doc" → minimax-m2.5:cloud      (long context)
"hi"                        → gemma3:4b-cloud            (fast)
[image attached]            → qwen3-vl:235b-instruct     (vision)
```

---

## REST API

### Core
```
POST /chat                    Chat with the agent
GET  /status                  System health + model stats
GET  /memories?q=query        Search memory
GET  /audit?limit=50          Audit log
```

### Models
```
GET  /models                  All 27 models
GET  /models/{id}             Single model details
POST /route                   Preview routing decision for a prompt
POST /compare                 Run same prompt on multiple models in parallel
```

### RAG
```
POST /rag/ingest              Ingest text into vector store
POST /rag/query               Retrieve relevant chunks
GET  /rag/docs                List ingested documents
DELETE /rag/docs/{id}         Delete a document
```

### Pipelines
```
GET  /pipelines               List registered pipelines
POST /pipelines/{name}/run    Execute a pipeline with context
GET  /pipelines/runs          Recent pipeline run history
```

### Prompt Templates
```
GET  /templates               List all templates
POST /templates/{name}/render Render template with variables
```

### Cache
```
GET  /cache/stats             Cache backend stats
POST /cache/flush             Clear all cached responses
```

---

## CLI Commands

```
/models                       Browse all 27 models
/model <id>                   Pin session to a model
/model auto                   Restore auto-routing
/route <text>                 Preview routing decision
/compare <prompt>             Run on 3 models simultaneously
/load <file>                  Ingest document into RAG
/loaddir <directory>          Ingest all docs in directory
/docs                         List RAG documents
/rag <question>               RAG-augmented answer
/pipelines                    List pipelines
/run <name> {context_json}    Execute a pipeline
/templates                    Browse prompt templates
/use <name> {vars_json}       Use a prompt template
/summarize                    Compress conversation history
/stats                        Model usage statistics
/memory <query>               Search stored memories
/status                       System status
```

---

## Telegram Commands

```
/start    /help    /status    /clear    /memory
/skills   /exec <code>
/models   /model <id>   /route <text>
```

---

## RAG Pipeline

```python
# Ingest
doc = await agent.rag.ingest_file("research_paper.pdf")
doc = await agent.rag.ingest_text("...", title="My Note")
docs = await agent.rag.ingest_directory("./docs", extensions=["md","txt"])

# Retrieve
results = await agent.rag.retrieve("transformer architecture", top_k=5)

# RAG-augmented chat
augmented, results = await agent.rag.augment_prompt("How does attention work?")
response = await agent.llm.chat([{"role":"user","content":augmented}])
```

## Prompt Templates

```python
# Built-in templates: summarize, code_review, translate, rag_answer,
#                    chain_of_thought, write_tests, debug, agent_plan, explain_concept

msgs = agent.templates.render("code_review", {
    "language": "Python",
    "code": "def add(a,b): return a+b",
    "focus": "edge cases"
})
response = await agent.llm.chat(msgs)
```

## Pipelines

```python
from agent.pipeline import Pipeline

pipeline = Pipeline("my_workflow")
pipeline.step("fetch",   fetch_fn,   output_key="data")
pipeline.step("process", process_fn, output_key="result",
              condition=lambda ctx: bool(ctx.get("data")))
pipeline.step("store",   store_fn,   on_error="skip")

run = await agent.pipeline_executor.run(pipeline, {"query": "AI news"})
print(run.status, run.context["result"])
```

## Cache

```python
# Automatic response caching (1h TTL, Redis or in-memory)
# Manual cache use:
await agent.cache.set("key", {"data": ...}, ttl=3600)
val = await agent.cache.get("key")

# Rate limiting
result = await agent.cache.rate_check("user:123", limit=30, window=60)
if not result["allowed"]:
    return f"Rate limited. Retry in {result['retry_after']}s"
```

---

## Environment Variables

See `.env.example` for the complete list. Key variables:

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3-next:80b-cloud
MODEL_AUTO_ROUTE=true

# Override routing per task type:
MODEL_CODE=qwen3-coder-next:cloud
MODEL_MATH=deepseek-v3.1:671b-cloud
MODEL_VISION=qwen3-vl:235b-instruct-cloud
MODEL_REASON=cogito-2.1:671b-cloud
MODEL_FAST=gemma3:4b-cloud

# Exclude models from routing (comma-separated):
# MODEL_EXCLUDE=rnj-1:8b-cloud,gemma3:4b-cloud

TELEGRAM_TOKEN=your_bot_token
DB_PATH=data/omni_agent.db
REDIS_URL=redis://localhost:6379/0
```

---

## Docker

```bash
docker-compose up -d
# Services: omni-agent (:8000), ollama (:11434), searxng (:8080), redis (:6379)
```

---

## Tests

```bash
pytest tests/ -q                         # Active release-gate test command
ruff check .                             # Active release-gate lint command
python tools/check_documentation_consistency.py --root . --report-only
coverage erase && coverage run -m pytest tests/ && coverage report
```

See `tests/SUPPORT_MATRIX.md` for the active suite inventory, CI lane names, and the latest committed verification evidence.
See `docs/testing/coverage.md` for the Phase 3.6 baseline guard and the Phase 3.7 Quality Ratchet results. The latest committed Phase 3.7 ratchet evidence measured `65.34%` coverage across `10462` statements with `3626` missed; `main.py` improved from `18.73%` to `86.93%`, and `agent/crypto_utils.py` improved from `29.18%` to `99.57%`. `fail_under = 58` remains the anti-regression floor, not a quality target.

---

## Performance Baseline

Phase 3.3 adds a loopback-only local performance harness for `GET /status` and a deterministic `POST /chat` fixture route.
It records request count, failure count, error rate, and p50 / p95 / p99 / max latency without requiring real external LLM providers.

```bash
python tools/performance/run_local_baseline.py --smoke
python tools/performance/run_local_baseline.py --baseline
```

Artifacts are written under `snapshot-phase-3-3/`.
This baseline is **local-only** and does **not** define production SLOs. See `docs/performance.md` for workload scope, safety rules, and the latest recorded baseline.

---

## Mutation Testing Baseline

Phase 3.5 adds a local AST-based mutation harness for selected active/hot-path modules:

- `agent/model_router.py`
- `agent/rag.py`
- `agent/sandbox.py`
- `agent/workflow.py`

It runs only against temporary overlay copies, refuses dirty working trees by default, and does **not** make mutation score a blocking CI gate in this phase.

```bash
python tools/mutation/run_mutation_baseline.py --smoke
python tools/mutation/run_mutation_baseline.py --baseline
```

Latest recorded focused baseline (`2026-05-19`, commit `1bb2636fc9e443eae55cebea17cde045c99dabaf`) captured `8` mutants with `0` killed, `8` survived, `0` timeout, `0` incompetent, mutation score `0.0`, and runtime `19.601` seconds.

Artifacts are written under `snapshot-phase-3-5/`. See `docs/testing/mutation_testing.md` for scope, safety rules, smoke/baseline commands, and the recorded mutation metrics.

---

## License

MIT
