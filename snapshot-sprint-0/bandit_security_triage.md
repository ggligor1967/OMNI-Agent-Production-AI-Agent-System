# Bandit Security Triage — Sprint 0

**Gate 0.8 verdict:** PASS  
**Scope:** medium/high findings from `snapshot-sprint-0/bandit_active.json` and `snapshot-sprint-0/bandit_active_medium_high.log`  
**Hot-path B110 review:** no current `B110` rows required elevation into the active Gate 0.8 set.

| ID | File | Line | Rule | Severity | Classification | Decision | Owner |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| 1 | `agent/tools/__init__.py` | 187 | `B602` | HIGH | fixed | Gate 0.4.5 replaced the raw `shell=True` path with strict tokenization and `shell=False`; negative injection test added. | backend/security |
| 2 | `agent/skills_manager.py` | 93 | `B102` | MEDIUM | fixed | Gate 0.4.5 removed direct DB `exec()` loading and now accepts import references only. | backend/security |
| 3 | `agent/retry_manager.py` | 135 | `B608` | MEDIUM | fixed | Gate 0.4.5 replaced string interpolation with constant parameterized SQL queries and added SQLi negative coverage. | backend |
| 4 | `agent/ab_router.py` | 357 | `B324` | HIGH | false-positive | Sticky A/B bucketing hash over experiment and routing keys only; not auth/password/crypto usage. | backend |
| 5 | `agent/alert_manager.py` | 332 | `B324` | HIGH | false-positive | Alert fingerprint used for deduplication/resolution identity only; not a security decision. | backend |
| 6 | `agent/audit_logger.py` | 157 | `B608` | MEDIUM | false-positive | `WHERE` clauses are assembled from fixed fragments while values remain bound parameters. | backend |
| 7 | `agent/bloom_filter.py` | 52 | `B324` | HIGH | false-positive | MD5 is used as a non-cryptographic Bloom filter hash primitive. | backend |
| 8 | `agent/cost_tracker.py` | 266 | `B608` | MEDIUM | false-positive | `group_by` is allowlisted to fixed column names before interpolation. | backend |
| 9 | `agent/cost_tracker.py` | 301 | `B608` | MEDIUM | false-positive | Clause fragments are fixed and values are parameterized; no raw SQL text injection path. | backend |
| 10 | `agent/crypto_utils.py` | 51 | `B324` | HIGH | orphan-candidate | Real MD5 helper inside a crypto module, but no active runtime imports were found from `main.py`/`agent/core.py`; keep on orphan review radar. | phase-2 |
| 11 | `agent/data_augmentor.py` | 204 | `B324` | HIGH | false-positive | Augmentation dedup hash only; not security-sensitive crypto. | backend |
| 12 | `agent/document_chunker.py` | 148 | `B324` | HIGH | false-positive | Chunk deduplication hash only; not auth/password/crypto. | backend |
| 13 | `agent/document_qa.py` | 64 | `B324` | HIGH | false-positive | Pseudo-embedding/vectorization hash only; not a security control. | backend |
| 14 | `agent/document_qa.py` | 170 | `B608` | MEDIUM | false-positive | Dynamic `IN (?,...)` placeholder count only; values stay parameterized. | backend |
| 15 | `agent/embedding_pipeline.py` | 207 | `B324` | HIGH | false-positive | Mock/deterministic pseudo-embedding seed; non-security use. | backend |
| 16 | `agent/embedding_store.py` | 45 | `B324` | HIGH | false-positive | Deterministic pseudo-embedding seed only; not security-sensitive. | backend |
| 17 | `agent/event_bus.py` | 132 | `B608` | MEDIUM | orphan-candidate | Raw topic text is interpolated into DLQ SQL, but `core.py` uses `agent.streaming.bus`; this module is not on the active runtime path. | phase-2 |
| 18 | `agent/event_sourcing.py` | 121 | `B608` | MEDIUM | false-positive | SQL fragments are fixed and values are parameterized. | backend |
| 19 | `agent/event_store.py` | 122 | `B608` | MEDIUM | false-positive | Fixed clause assembly with bound params only. | backend |
| 20 | `agent/event_store.py` | 138 | `B608` | MEDIUM | false-positive | Only placeholder count varies for `IN (...)`; values stay bound. | backend |
| 21 | `agent/experiment_tracker.py` | 124 | `B608` | MEDIUM | false-positive | `conds` are fixed fragments and all values are bound via `?`. | backend |
| 22 | `agent/feature_flags.py` | 45 | `B324` | HIGH | false-positive | Stable rollout bucket hash only; not crypto-sensitive. | backend |
| 23 | `agent/gateway.py` | 400 | `B104` | MEDIUM | orphan-candidate | Standalone gateway binds broadly, but active repo wiring was not found outside archived tests. | phase-2 |
| 24 | `agent/graph_executor.py` | 347 | `B324` | HIGH | false-positive | Cache key over node id + serialized inputs only. | backend |
| 25 | `agent/health_monitor.py` | 175 | `B310` | MEDIUM | orphan-candidate | `urlopen(check.url)` lacks a scheme allowlist, but current repo wiring appears dormant outside archived tests. | phase-2 |
| 26 | `agent/hot_reloader.py` | 31 | `B324` | HIGH | false-positive | File-content hash for change detection only. | backend |
| 27 | `agent/hot_reloader.py` | 98 | `B102` | MEDIUM | orphan-candidate | Dev-time module reload via `exec`, but not wired into the active runtime bootstrap. | phase-2 |
| 28 | `agent/jobs.py` | 274 | `B608` | MEDIUM | false-positive | `updates` is built from fixed column names selected by internal branches. | backend |
| 29 | `agent/knowledge_base.py` | 217 | `B608` | MEDIUM | false-positive | `IN` placeholder list is generated from internal IDs; values remain parameterized. | backend |
| 30 | `agent/knowledge_base.py` | 221 | `B608` | MEDIUM | false-positive | Same placeholder-expansion pattern as line 217. | backend |
| 31 | `agent/llm_cache.py` | 74 | `B324` | HIGH | false-positive | N-gram hashing for vector index construction only. | backend |
| 32 | `agent/log_aggregator.py` | 131 | `B608` | MEDIUM | false-positive | Search query uses fixed predicates and bound params only. | backend |
| 33 | `agent/log_aggregator.py` | 152 | `B608` | MEDIUM | false-positive | Optional `WHERE source=?`; fully parameterized. | backend |
| 34 | `agent/memory_graph.py` | 89 | `B608` | MEDIUM | false-positive | `WHERE` is assembled from fixed fragments while values stay bound. | backend |
| 35 | `agent/memory_graph.py` | 123 | `B608` | MEDIUM | false-positive | Optional `WHERE namespace=?` only; no raw interpolation. | backend |
| 36 | `agent/memory_graph.py` | 124 | `B608` | MEDIUM | false-positive | Same as line 123 for relations count. | backend |
| 37 | `agent/memory_graph.py` | 143 | `B324` | HIGH | false-positive | Pseudo-embedding hash only; not crypto-sensitive. | backend |
| 38 | `agent/message_queue.py` | 134 | `B608` | MEDIUM | false-positive | `ORDER BY` fragment is derived from an internal enum, not raw user text. | backend |
| 39 | `agent/message_queue.py` | 206 | `B608` | MEDIUM | orphan-candidate | Raw queue name is interpolated into DLQ SQL, but module reachability is limited to archived tests/dormant wiring. | phase-2 |
| 40 | `agent/message_queue.py` | 281 | `B324` | HIGH | false-positive | Dedup-window key only; not auth/password/crypto. | backend |
| 41 | `agent/notification_manager.py` | 148 | `B608` | MEDIUM | false-positive | Channel input is coerced through a trusted enum before SQL execution. | backend |
| 42 | `agent/notification_manager.py` | 210 | `B324` | HIGH | false-positive | Notification dedup hash only. | backend |
| 43 | `agent/object_storage.py` | 69 | `B324` | HIGH | false-positive | ETag compatibility hash over object content; not auth/password/crypto. | backend |
| 44 | `agent/object_storage.py` | 357 | `B324` | HIGH | false-positive | Multipart ETag/checksum compatibility only. | backend |
| 45 | `agent/prompt_library.py` | 178 | `B608` | MEDIUM | false-positive | Search conditions are fixed SQL fragments with bound `LIKE` params. | backend |
| 46 | `agent/rag.py` | 361 | `B608` | MEDIUM | false-positive | `LIKE` terms and `doc_id` are bound; active path uses constant `top_k=4`. | backend |
| 47 | `agent/sandbox_executor.py` | 300 | `B307` | MEDIUM | orphan-candidate | Intentional `eval` in a separate sandbox module that is not the sandbox used by `core.py`. | phase-2 |
| 48 | `agent/sandbox_executor.py` | 302 | `B102` | MEDIUM | orphan-candidate | Intentional `exec` in a separate sandbox module that is not the active runtime sandbox. | phase-2 |
| 49 | `agent/search.py` | 320 | `B608` | MEDIUM | orphan-candidate | Raw `corpus` interpolation exists, but the service is only assembled via unused builder wiring. | phase-2 |
| 50 | `agent/search.py` | 332 | `B608` | MEDIUM | orphan-candidate | Same latent `corpus` interpolation issue as line 320. | phase-2 |
| 51 | `agent/search.py` | 400 | `B608` | MEDIUM | orphan-candidate | Same latent `corpus` interpolation issue as lines 320/332. | phase-2 |
| 52 | `agent/secrets_manager.py` | 219 | `B608` | MEDIUM | orphan-candidate | Raw `secret_name` interpolation exists, but active runtime wiring was not found. | phase-2 |
| 53 | `agent/session.py` | 214 | `B608` | MEDIUM | false-positive | Constant string concatenation only for `include_archived`; values remain parameterized. | backend |
| 54 | `agent/session.py` | 237 | `B608` | MEDIUM | false-positive | Conditions are fixed fragments with bound params. | backend |
| 55 | `agent/session.py` | 269 | `B608` | MEDIUM | orphan-candidate | Raw `user_id` interpolation exists only behind unused builder wiring. | phase-2 |
| 56 | `agent/session.py` | 271 | `B608` | MEDIUM | orphan-candidate | Same latent `user_id` interpolation issue as line 269. | phase-2 |
| 57 | `agent/session.py` | 274 | `B608` | MEDIUM | orphan-candidate | Same latent `user_id` interpolation issue as lines 269/271. | phase-2 |
| 58 | `agent/session_manager.py` | 148 | `B608` | MEDIUM | false-positive | `WHERE` is built from fixed `user_id=?` / `status=?` fragments only. | backend |
| 59 | `agent/telemetry.py` | 179 | `B608` | MEDIUM | false-positive | Constant-clause assembly with bound params only. | backend |
| 60 | `agent/token_budget_manager.py` | 133 | `B608` | MEDIUM | false-positive | `wh` is built from fixed condition fragments while values remain bound. | backend |
| 61 | `agent/token_budget_manager.py` | 134 | `B608` | MEDIUM | false-positive | Same fixed-fragment pattern as line 133. | backend |
| 62 | `agent/token_budget_manager.py` | 135 | `B608` | MEDIUM | false-positive | Same fixed-fragment pattern as line 133. | backend |
| 63 | `agent/token_budget_manager.py` | 136 | `B608` | MEDIUM | false-positive | Same fixed-fragment pattern as line 133. | backend |
| 64 | `agent/workflow.py` | 333 | `B307` | MEDIUM | accepted-risk-temporary | `WorkflowManager` is active, but no built-in workflows use `action: transform` and no external workflow loading is exposed through the current API. Follow-up ticket: `PHASE1-WORKFLOW-TRANSFORM-HARDENING`. | backend/security |

## Gate 0.4.5 rows confirmed fixed

- `agent/tools/__init__.py` — `B602` / prior direct execution path hardened in Gate 0.4.5.
- `agent/skills_manager.py` — `B102` removed from DB skill loading in Gate 0.4.5.
- `agent/retry_manager.py` — `B608` parameterized in Gate 0.4.5.

## Must-fix blockers before Phase 0 can be green

- None identified in the current active Gate 0.8 scope after reachability and code-context review.

## Notes

- `accepted-risk-temporary` requires written justification and a ticket; the active follow-up identifier for `agent/workflow.py:333` is `PHASE1-WORKFLOW-TRANSFORM-HARDENING`.
- `orphan-candidate` rows should be revisited during Gate 0.9 inventory and the Phase 2 architecture/orphan-module audit.
