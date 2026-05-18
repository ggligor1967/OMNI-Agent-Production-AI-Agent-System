# Orphan Module Classification — Phase 2

| Module | Classification | Evidence | Decision |
| -------- | ---------------- | ---------- | ---------- |
| `agent/core.py` | active | `snapshot-phase-2/import_graph_phase2.log` shows runtime inbound imports from `main`, `agent.cli`, and `agent.dashboard`; it is the runtime orchestrator. | keep |
| `agent/auth.py` | active | `snapshot-phase-2/import_graph_phase2.log` shows runtime inbound imports from `agent.core`, `agent.export`, `agent.streaming`, and `main`. | keep |
| `agent/model_registry.py` | active | Runtime inbound imports from `main`, `agent.cli`, `agent.model_router`, `agent.multi_model_client`, and `agent.telegram_bot`. | keep |
| `agent/model_router.py` | active | Runtime inbound imports from `agent.cli`, `agent.multi_model_client`, and `agent.telegram_bot`. | keep |
| `agent/multi_model_client.py` | active | Runtime inbound import from `agent.core`; test coverage also present. | keep |
| `agent/memory.py` | active-support | Runtime inbound imports from `agent.core`, `agent.collaboration`, `agent.prompt_templates`, `agent.skills_manager`, and `agent.telegram_bot`. | keep |
| `agent/rag.py` | active-support | Runtime inbound import from `agent.core`; multiple active tests cover retrieval and SQL hardening. | keep |
| `agent/cache.py` | active-support | Runtime inbound import from `agent.core`; active tests cover cache behavior. | keep |
| `agent/tools_registry.py` | active | Runtime inbound imports from `agent.core`, `agent.workflow`, and `main`; active tests cover enforcement and audit behavior. | keep |
| `agent/workflow.py` | active | Runtime inbound import from `agent.core` plus active test coverage. | keep |
| `agent/streaming.py` | active-support | Runtime inbound imports from `agent.core` and `main`; active tests and runtime registration prove usage. | keep |
| `agent/dashboard.py` | active-support | Runtime inbound import from `main` and active dashboard tests. | keep |
| `agent/security_audit.py` | active-support | Runtime inbound imports from `agent.core`, `agent.sandbox`, `agent.tools_registry`, and `main`. | keep |
| `agent/ssrf.py` | active-support | Runtime inbound imports from `agent.multimodal` and `agent.tools`; active SSRF tests cover it. | keep |
| `agent/cli.py` | active-support | Runtime inbound import from `main`; used by the CLI run mode. | keep |
| `agent/telegram_bot.py` | active-support | Runtime inbound import from `main`; used by telegram mode but not on the core API hot path. | keep |
| `agent/config_manager.py` | active-support | Runtime inbound import from `agent.core`; config routes and watcher are active. | keep |
| `agent/knowledge_graph.py` | active-support | Runtime inbound import from `agent.core`; API routes expose KG operations through the active runtime. | keep |
| `agent/ab_router.py` | orphan-candidate | `snapshot-phase-2/import_graph_phase2.log` shows `0` runtime inbound imports and `0` test imports; `snapshot-sprint-0/orphan_module_inventory.md` already flagged it. | do not delete yet |
| `agent/agent_builder.py` | standalone-candidate | `0` runtime inbound imports, but `9` outgoing dependencies into orchestration-style modules indicate a coherent subsystem rather than dead leaf code. | inspect wiring later |
| `agent/alert_manager.py` | standalone-candidate | `0` runtime inbound imports in Phase 2 graph; Sprint 0 inventory already marked it operationally coherent but unwired. | keep pending wiring audit |
| `agent/audit_logger.py` | standalone-candidate | `0` runtime inbound imports; separate audit subsystem exists but is not wired into the active `main -> core` runtime. | keep pending architecture review |
| `agent/cache_manager.py` | legacy-in-disguise | `0` runtime inbound imports while active runtime uses `agent.cache`; overlapping naming suggests a deferred consolidation target. | review in Gate 2.2, do not delete now |
| `agent/cache_warmer.py` | legacy-in-disguise | `0` runtime inbound imports and overlapping cache family naming with active `agent.cache`. | review in Gate 2.2, do not delete now |
| `agent/cache_warmup_manager.py` | legacy-in-disguise | `0` runtime inbound imports and overlapping cache warm-up family naming. | review in Gate 2.2, do not delete now |
| `agent/config_validator.py` | standalone-candidate | `0` runtime inbound imports and `0` tests; validation helper exists but is not wired into active startup/config flow. | inspect for future config hardening, do not delete |
| `agent/crypto_utils.py` | orphan-candidate | `0` runtime inbound imports; only test-only inbound import from `tests.test_md5_sweep`. Sprint 0 already flagged it as security-adjacent but unwired. | keep isolated; no deletion decision yet |
| `agent/event_bus.py` | legacy-in-disguise | `0` runtime inbound imports, while `agent.core` imports `bus` from `agent.streaming`; overlapping event-bus semantics indicate supersession risk. | review as replacement/deprecation candidate |
| `agent/gateway.py` | orphan-candidate | `0` runtime inbound imports in Phase 2 graph; Sprint 0 inventory already marked it broad-surface and unused. | do not expose or delete yet |
| `agent/governance.py` | standalone-candidate | Imported only by orphaned `agent.agent_builder`; not on the active runtime path but part of a coherent enterprise-style family. | defer to Gate 2.2 canonicalization |
| `agent/governance_engine.py` | orphan-candidate | `0` runtime inbound imports and no test imports; overlaps with `agent.governance.py` naming but lacks evidence for safe removal. | document now, decide later |
| `agent/health_monitor.py` | orphan-candidate | `0` runtime inbound imports; Sprint 0 inventory already flagged it as unwired with potential network-facing risk. | keep quarantined from active wiring |
| `agent/hot_reloader.py` | standalone-candidate | `0` runtime inbound imports, but behavior suggests dev-tooling support rather than dead code. | keep as tooling candidate |
| `agent/message_queue.py` | standalone-candidate | `0` runtime inbound imports; substantial subsystem with its own persistence semantics and dedup logic. | keep pending architecture decision |
| `agent/observability_hub.py` | legacy-in-disguise | `0` runtime inbound imports; overlaps with tracing/streaming/observability surfaces already in use elsewhere. | review in Gate 2.2, do not delete now |
| `agent/retry_manager.py` | standalone-candidate | `0` runtime inbound imports but active test-only inbound from `tests.test_suite`; reusable utility rather than proven dead code. | keep as tested utility |
| `agent/search.py` | orphan-candidate | Imported only by orphaned `agent.agent_builder`; not referenced from `main` or `agent.core`. Sprint 0 inventory already flagged it. | inspect later before any enablement or deletion |
| `agent/secret_manager.py` | orphan-candidate | `0` runtime inbound imports and no test imports; overlaps with `agent.secrets_manager.py` but evidence is insufficient for removal. | defer to Gate 2.2 |
| `agent/secrets_manager.py` | orphan-candidate | `0` runtime inbound imports; Sprint 0 inventory flagged it as sensitive and unwired. | review separately before adoption or deletion |
| `agent/session.py` | orphan-candidate | Imported only by orphaned `agent.agent_builder`; not wired into current runtime path. Sprint 0 inventory already flagged overlap with other session abstractions. | classify for deeper consolidation review |
| `agent/token_budget_manager.py` | orphan-candidate | `0` runtime inbound imports and `0` test imports; no evidence of active runtime use. | keep for later cost/token governance audit |
| `agent/tool_registry.py` | legacy-in-disguise | `0` runtime inbound imports while `agent.tools_registry.py` has active runtime imports from `agent.core`, `agent.workflow`, and `main`. | preserve for analysis only; canonicalize later |

## Notes

- `lint-imports` was unavailable locally in this environment; fresh AST evidence was generated by `tools/phase2_import_inventory.py` and captured in `snapshot-phase-2/import_graph_phase2.log`.
- `snapshot-sprint-0/orphan_module_inventory.md` was used as historical evidence only; this file is the Phase 2 refresh.
- `orphan-candidate` and `legacy-in-disguise` are classification labels, **not** deletion approvals.
- No product modules were deleted in this gate.
