# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

OMNI Agent is a modular, async Python AI agent system that routes requests across 27 cloud LLM models. It supports four run modes: CLI, REST API (aiohttp on :8000), Telegram bot, and all-at-once. The system uses Ollama as its LLM backend with auto-routing to select the best model per task type (code, math, vision, multilingual, etc.).

## Documentation Contract

Documentation baseline: phase-2-complete

- Model catalog source of truth: `agent/model_registry.py` (**27 cloud models**)
- Support matrix path: `tests/SUPPORT_MATRIX.md`
- ADR index: `docs/adr/README.md`
- CI release gate: Python 3.12 and 3.13 with `pytest tests/ -q`, `ruff check .`, and `python tools/check_documentation_consistency.py --root .`
- Storage strategy: SQLite for local development and tests; Postgres is the production target (see `docs/adr/ADR-003-db-strategy.md`)

## Commands

```bash
# Run
python main.py --mode cli          # Interactive Rich CLI
python main.py --mode api          # REST API on :8000
python main.py --mode telegram     # Telegram bot
python main.py --mode all          # All modes

# Tests
pytest tests/ -q                   # Active release-gate suite
ruff check .                       # Active release-gate lint command
python tools/check_documentation_consistency.py --root . --report-only
pytest -k "test_name" -v           # Single test by name

# Docker
docker-compose up -d               # omni-agent(:8000), ollama(:11434), searxng(:8080)
```

Use `tests/SUPPORT_MATRIX.md` as the inventory of active release-gate test files and CI lane names.

## Architecture

### Startup chain
`main.py` → `config.py` (dataclass `CONFIG` singleton, all env vars) → `agent/core.py` (`OmniAgent` class)

### Core orchestrator (`agent/core.py`)
`OmniAgent.__init__()` wires ~30 subsystems. Key flow:
1. `OmniAgent.chat()` — main entry: security check → intent detection → skill triggers → history/summarization → optional RAG augmentation → cache check → LLM call → tool execution → memory extraction
2. Tool calls are parsed from LLM output via `[TOOL: tool_name(args)]` pattern
3. `start()` initializes scheduler, heartbeat, cache, pipelines, config watcher, event bus
4. `stop()` cleanly shuts down all async resources

### LLM subsystem (the "nucleus")
These four modules form the critical model contract — changes must stay consistent across all of them:
- **`agent/model_registry.py`** — Static catalog of 27 cloud models. Exports: `MODELS`, `get_model()`, `ModelCapability`, `summary_table()`
- **`agent/model_router.py`** — Task classifier + routing engine. Exports: `ModelRouter`, `TaskType`, `classify_task()`, `RouteDecision`, `TASK_TO_CAPABILITY`
- **`agent/multi_model_client.py`** — Unified async LLM interface. Uses router for auto-routing, supports `chat()`, `chat_parallel()`, `embed()`
- **`agent/ollama_client.py`** — Raw Ollama HTTP client

### `classify_task()` — Scoring Architecture (v2, March 2026)

`classify_task(text, has_image)` in `agent/model_router.py` uses a **multi-category scoring system** — every category accumulates evidence points independently; the highest score wins.

**Scoring weights:**
| Signal type | Weight |
|---|---|
| Strong pattern match | +2.0 – +2.5 |
| Medium pattern match | +0.8 – +1.0 |
| Short input (≤2 words or greeting) | +3.0 for FAST |

**Priority tie-breaking order:** MATH > CODE > TRANSLATION > VISION > CREATIVE > AGENT > REASONING > FAST > GENERAL

**Confidence formula:** `min(0.5 + score/8.0, 1.0)` — boosted +0.10 if only one category scored. Note: `route()` also adds +0.10 on top of `classify_task()` confidence.

**Category coverage:**
- `MATH` — equation/formula/integral/derivative verbs, unicode symbols (∫ ∑ ∂ ∇), Romanian math terms. **NOTE: "function" intentionally excluded from math_strong** (too ambiguous with code prompts like "write a Python function")
- `CODE` — language-specific write patterns, import statements, framework names, API/DevOps terms
- `TRANSLATION` — translate verbs, `from X to Y` language pairs, "how do you say X in Y"
- `VISION` — image/photo/screenshot + "describe this image", OCR, object detection verbs
- `CREATIVE` — write/compose/make/craft + creative genres (poem, story, haiku, song, etc.)
- `REASONING` — why/because/compare/analyze/pros-and-cons, "X vs Y" comparison phrases
- `AGENT` — web search verbs, API calls, scheduling, monitoring/alerting
- `FAST` — ≤2 words or common greetings (hi/hey/yes/no/thanks)
- `GENERAL` — fallback when no category scores

**Known bug fixed (v2.1, March 2026):** `ModelRouter.route()` was calling `_infer_task_type(best_model, task_type)` which overwrote the correct `classify_task()` result with the selected model's primary capability (e.g., a creative task was returning `vision` because the best-scored model had `ModelCapability.VISION`). Fixed by removing the `_infer_task_type` call in `route()`.

**Validated test suite: 8/8 live API tests (100%)** covering all categories:
- "Write a Python function to sort a list" → `code` (conf 0.95)
- "Solve the integral of x squared" → `math` (conf 1.0)
- "Translate hello from English to French" → `translation` (conf 1.0)
- "What is the capital of France" → `general` (conf 0.60)
- "Make me a haiku about the sea" → `creative` (conf 1.0)
- "Compare pros and cons of React vs Vue" → `reasoning` (conf 1.0)
- "hi" → `fast` (conf 1.0)
- Romanian math text → `math` (conf 1.0)

### Key subsystems
| Module | Purpose |
|--------|---------|
| `agent/memory.py` | SQLite persistent memory (MemoryDB, 8 tables) |
| `agent/rag.py` | RAG pipeline: ingest → chunk → embed → retrieve |
| `agent/cache.py` | Redis cache with in-memory fallback |
| `agent/pipeline.py` | Multi-step agentic pipeline executor |
| `agent/prompt_templates.py` | Named prompt templates with variables |
| `agent/summarizer.py` | Conversation auto-compressor |
| `agent/knowledge_graph.py` | Entity/relationship graph (exports `KnowledgeGraph`, `GraphStore`) |
| `agent/hooks.py` | Async event system (20+ event types) |
| `agent/tools_registry.py` | Formal tool registry with OpenAI/Anthropic schema export |
| `agent/config_manager.py` | Hot-reload config wrapper |
| `agent/sandbox.py` | Secure code execution sandbox |
| `agent/auth.py` | JWT auth with role-based access |
| `agent/streaming.py` | SSE streaming + global event bus |
| `agent/structured_output.py` | Schema-based structured output parsing |
| `agent/cli.py` | Rich interactive CLI |
| `agent/dashboard.py` | Real-time web dashboard at /dashboard |

### Legacy / quarantine
`agent/_legacy/` contains archived duplicate modules from version drift. These are **not** on the runtime hot path. Do not import from `agent/_legacy/` in nucleus modules.

### Test files
The blocking release gate is `pytest tests/ -q`. Use `tests/SUPPORT_MATRIX.md` for the active suite inventory; `tests/_archive/` remains a non-blocking audit path.

## Configuration

All config is in `config.py` as a `@dataclass` `Config` with env var overrides. Key groups:
- Model routing: `MODEL_AUTO_ROUTE`, `MODEL_CODE`, `MODEL_MATH`, `MODEL_VISION`, etc.
- Database: `DB_PATH` (SQLite default), `DB_BACKEND`, `POSTGRES_DSN`, `REDIS_URL`
- Telegram: `TELEGRAM_TOKEN`, `TELEGRAM_ALLOWED_USERS`
- Security: `SECRET_KEY`, `AUTH_ENFORCE`, `RATE_LIMIT_PER_MINUTE`, `ENABLE_SANDBOX`

## Async patterns

- All async uses `asyncio`. pytest uses `asyncio_mode = auto` (see `pytest.ini`).
- `aiohttp` for the REST API server.
- The LLM client, cache, scheduler, and pipeline executor are all async.

## Known issues / caveats

- `agent/core.py` has duplicate import blocks and duplicate component initialization (evaluator, persona, knowledge_graph, config_manager initialized twice). The second set overwrites the first.
- `core.py` creates both `self.config_mgr` and `self.cfg` — both are `ConfigManager` instances; `stop()` calls both.
- ROLE.txt documents a prior repair mission with contract alignment rules — consult it for context on past structural fixes.
