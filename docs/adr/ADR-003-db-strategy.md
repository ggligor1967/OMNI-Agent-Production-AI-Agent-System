# ADR-003 — Database Strategy

- Status: Accepted
- Date: 2026-05-18

## Decision

SQLite is supported for local development, test runs, and single-node evaluation.

Postgres is the production target for shared-state, multi-instance, or operationally durable deployments.

This decision is directional and architectural, not a claim that the live runtime is already fully migrated to Postgres.

## Current State

The active runtime is still SQLite-first.

Evidence on the current `main.py -> agent/core.py` path:

- `agent.memory.MemoryDB` uses `sqlite3.connect` directly and is wired from `CONFIG.DB_PATH`.
- `agent.rag.VectorStore` uses SQLite directly and is instantiated with `data/rag.db`.
- `agent.knowledge_graph.KnowledgeGraph` uses SQLite directly and is instantiated with `data/knowledge_graph.db`.
- `agent.auth.AuthStore` uses SQLite directly and is instantiated with `data/auth.db` from both `agent/core.py` and `main.py`.
- `agent.evaluation.Evaluator` persists to SQLite (`data/eval_results.db`).
- `agent.config_manager.ConfigManager` persists to SQLite (`data/config.db`).
- `agent.notifications.Notifier` persists to SQLite (`data/notifications.db`).

Repository configuration already exposes:

- `DB_PATH`
- `DB_BACKEND`
- `POSTGRES_DSN`
- `REDIS_URL`

However, only `DB_PATH` is clearly consumed on the live runtime path today.

`agent/database.py` contains a real `asyncpg`-backed `PostgresBackend` and `create_backend()` factory, but that abstraction is not wired into the active orchestrator path. In practice, this means the repository has Postgres groundwork without end-to-end Postgres adoption.

## Production Policy

- Local development and tests may continue to use SQLite as the supported default.
- Production architecture should target Postgres as the system-of-record database once active subsystems are migrated.
- Setting `DB_BACKEND=postgres` is **not** sufficient today to move the live runtime off SQLite.
- Until runtime services stop opening SQLite files directly, Postgres support must be treated as partial and not yet authoritative for production storage.
- Public deployment guidance must not claim that auth, memory, RAG, knowledge graph, evaluation, config persistence, or notifications already run on Postgres.

## Migration Plan

1. **Memory**
   - Make `MemoryDB` backend-aware or introduce a shared storage adapter.
   - Route conversation history, memories, state, and audit storage through the active backend decision.

2. **Auth**
   - Replace direct SQLite use in `AuthStore` with a backend abstraction that respects `DB_BACKEND` and `POSTGRES_DSN`.
   - Keep bootstrap and ownership-binding behavior unchanged during storage migration.

3. **RAG / Knowledge Graph**
   - Decouple `VectorStore` and `KnowledgeGraph` from direct SQLite connections.
   - Define the production persistence model for chunk storage, metadata, and graph state before declaring Postgres support complete.

4. **Evaluator / Config / Notifications**
   - Remove hardcoded per-subsystem SQLite database paths.
   - Consolidate these stores behind a consistent environment-driven backend policy.

5. **Export Path**
   - Keep `agent.export` above storage details.
   - Let export consume migrated memory/KG APIs rather than assuming SQLite-backed implementations.

6. **Tests and rollout**
   - Keep SQLite as the default test backend initially.
   - Add backend-specific tests only after one subsystem at a time is truly Postgres-ready.
   - Avoid a repo-wide "flip everything to postgres" change in one gate.

## Non-Goals

- Do not complete full Postgres migration in this gate.
- Do not claim that `DB_BACKEND=postgres` is already authoritative across the active runtime.
- Do not rewrite all storage modules in a single refactor.
- Do not remove SQLite support for local development or tests.

## Notes

This ADR makes the intended boundary explicit:

- **today's implementation reality:** SQLite-first runtime
- **production direction:** Postgres target
- **remaining work:** migrate active subsystems until the runtime backend decision is actually authoritative
