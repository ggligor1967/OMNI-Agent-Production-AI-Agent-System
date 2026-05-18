# Orphan Module Inventory — Sprint 0

Evidence sources:

- `snapshot-sprint-0/import_linter.log` (`lint-imports` unavailable in the local environment)
- `snapshot-sprint-0/import_graph.log` (AST-based inbound import baseline)
- Gate 0.8 Bandit triage reachability review

| Module | Status | Decision |
| ------ | ------ | -------- |
| `ab_router` | orphan-candidate | Zero inbound imports in the active graph; inspect in Phase 2 before any deletion. |
| `alert_manager` | standalone-candidate | Not wired into `core.py`, but coherent operational subsystem; keep pending wiring audit. |
| `audit_logger` | standalone-candidate | No active inbound imports, but self-contained and potentially useful; keep for later integration review. |
| `crypto_utils` | orphan-candidate | Security-adjacent helper surface with no active inbound imports; inspect carefully in Phase 2. |
| `event_bus` | legacy-in-disguise | Active runtime uses `agent.streaming.bus`; this parallel event bus looks superseded and should be reviewed as replacement/deprecation candidate. |
| `gateway` | orphan-candidate | Broad-surface network gateway with zero active inbound imports; do not expose or delete before dedicated review. |
| `health_monitor` | orphan-candidate | No active inbound imports and has SSRF-shaped surface; keep quarantined from active wiring until reviewed. |
| `hot_reloader` | standalone-candidate | Dev-tool style subsystem with no active wiring; keep as a tooling candidate, not runtime. |
| `message_queue` | standalone-candidate | No active inbound imports, but substantial subsystem with its own persistence and semantics; keep pending architecture decision. |
| `retry_manager` | standalone-candidate | Currently unreferenced by the active runtime but now covered by active tests; keep as reusable utility. |
| `search` | orphan-candidate | Appears reachable only via unused builder wiring; inspect in Phase 2 before any enablement. |
| `secrets_manager` | orphan-candidate | Security-sensitive module with no active inbound imports; review separately before adopting or deleting. |
| `session` | orphan-candidate | Unused in current runtime graph and partially overlapped by other session abstractions; classify for deeper consolidation review. |
| `token_budget_manager` | orphan-candidate | No active inbound imports; keep for later cost/token governance audit. |
| `workflow` | active | Imported by `agent.core` and used by the API surface; keep on the active path. |
| `auth` | active | Imported by `agent.core` and exposed in runtime startup; keep. |
| `model_registry` | active | Imported by routing/client/runtime entry points; keep as source of truth. |
| `config_manager` | active-support | Imported by `agent.core` and used for runtime config routes/watcher; keep. |
| `rag` | active-support | Imported by `agent.core` and used in active chat/runtime flows; keep. |
| `notifications` | active-support | Imported by `agent.core` and `main.py`; keep. |

## Notes

- Zero inbound imports do **not** mean deletable; this inventory is for classification only.
- `standalone-candidate` means the module may still be valuable, but it is not currently wired into the active `main.py` → `agent/core.py` path.
- `legacy-in-disguise` marks modules that appear functionally superseded by another active implementation.
