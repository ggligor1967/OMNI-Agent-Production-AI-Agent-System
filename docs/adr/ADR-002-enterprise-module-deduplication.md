# ADR-002 — Enterprise Module Deduplication

- Status: Accepted
- Date: 2026-05-18

## Context

Phase 2 Gate 2.1 established that the repository contains several duplicate or overlapping enterprise-style module families, but only a subset of them is actually used by the current runtime path.

The active runtime path is still anchored on `main.py -> agent/core.py`, with active imports and tests centered on:

- `agent.tools_registry`
- `agent.scheduler`
- `agent.cache`
- `agent.tracing`
- `agent.streaming`

Fresh Phase 2 evidence also showed that several sibling modules are either:

- imported only by dormant builder-style code such as `agent/agent_builder.py`, or
- referenced only by archived tests, or
- completely unwired from both the active runtime and the active test suite.

The duplicate families reviewed in this gate are:

| Family | Candidates | Current evidence |
| ------ | ---------- | ---------------- |
| governance | `agent/governance.py`, `agent/governance_engine.py` | `agent.governance` is referenced by `agent/agent_builder.py`; `agent.governance_engine` has no active runtime imports. |
| tool registry | `agent/tool_registry.py`, `agent/tools_registry.py` | `agent.tools_registry` is imported by `main.py`, `agent/core.py`, and `agent/workflow.py`, and is covered by active tests. |
| scheduler | `agent/scheduler.py`, `agent/task_scheduler.py`, `agent/job_scheduler.py`, `agent/agent_scheduler.py` | `agent.scheduler` is used by `agent/core.py`, surfaced by `main.py`, and covered by active tests; the siblings are unwired. |
| secret management | `agent/secret_manager.py`, `agent/secrets_manager.py` | neither module is on the active runtime path; archived tests mention `agent.secret_manager.py`. |
| observability | `agent/observability.py`, `agent/observability_hub.py` | `agent.observability` is only imported by dormant `agent/agent_builder.py`; active runtime observability instead uses `agent.tracing.py` and `agent.streaming.py`. |
| cache family | `agent/cache_manager.py`, `agent/cache_warmer.py`, `agent/cache_warmup_manager.py` | active runtime caching uses `agent/cache.py`; the three enterprise-style siblings are unwired and covered only by archived tests. |

Supporting evidence for these decisions comes from:

- `snapshot-phase-2/import_graph_phase2.log`
- `snapshot-phase-2/ORPHAN_MODULE_CLASSIFICATION.md`
- direct code references in `main.py`, `agent/core.py`, `agent/workflow.py`, and `agent/agent_builder.py`

## Decision

Choose the following canonical modules for Phase 2 and stop adding new imports to their non-canonical siblings.

| Family | Canonical module | Rationale |
| ------ | ---------------- | --------- |
| governance | `agent/governance.py` | It is the only family member with any current code-path reference (`agent/agent_builder.py`) and exposes the broader governance/compliance surface (`GovernanceManager`, policy, consent, audit). |
| tool registry | `agent/tools_registry.py` | It is the active runtime implementation used by `main.py`, `agent/core.py`, and `agent/workflow.py`, and it carries the confirmation-policy and typed-tool contract required by Phase 1 security hardening. |
| scheduler | `agent/scheduler.py` | It is the active runtime scheduler instantiated by `agent/core.py`, exposed by `main.py`, and covered by active tests in `tests/test_suite.py`. |
| secret management | `agent/secret_manager.py` | Neither family member is active, but `agent.secret_manager.py` has stronger historical test evidence and a clearer single-module contract than `agent/secrets_manager.py`. This choice is provisional but becomes the canonical import target for any future work. |
| observability | `agent/observability.py` | It is the only family member with any current importer (`agent/agent_builder.py`) and is a better fit as the canonical metrics/health surface. `agent.observability_hub.py` remains a dormant experimental trace/log hub. |
| cache | `agent/cache.py` | This is the active runtime cache contract used by `agent/core.py`, `main.py`, and active tests. The enterprise-style siblings are not the runtime source of truth. |

Additional family notes:

- `agent/tool_registry.py` is explicitly non-canonical. It represents an older registry API and must not regain runtime ownership.
- `agent/task_scheduler.py`, `agent/job_scheduler.py`, and `agent/agent_scheduler.py` are explicitly non-canonical. They expose richer feature sets, but none is wired into the active runtime.
- `agent/secrets_manager.py` is explicitly non-canonical pending any future secret-storage adoption work.
- `agent/observability_hub.py` is explicitly non-canonical. Any future tracing/logging consolidation should align with the already-active `agent/tracing.py` and `agent/streaming.py` path instead.
- `agent/cache_manager.py`, `agent/cache_warmer.py`, and `agent/cache_warmup_manager.py` are explicitly non-canonical for runtime caching.
- `agent/llm_cache.py` is a separate concern from runtime caching. It is not part of the `cache_manager/cache_warmer/cache_warmup_manager` family decision and must not be conflated with `agent/cache.py`.

## Compatibility Policy

- No broad deletion is approved in this gate.
- Existing module paths remain in place during Gate 2.2.
- New runtime-facing imports must prefer the canonical modules listed above.
- Non-canonical siblings are frozen: no new imports should be introduced unless a later ADR supersedes this one.
- If Gate 2.3 moves canonical modules into subpackages, preserve backwards-compatible wrappers at the old import paths where active code or tests still depend on them.
- Do not attempt automatic aliasing where APIs are materially different, especially for:
  - `agent/tool_registry.py` vs `agent/tools_registry.py`
  - `agent/scheduler.py` vs the richer scheduler siblings
  - `agent/secret_manager.py` vs `agent/secrets_manager.py`
  - `agent/cache.py` vs the warmup/manager siblings

## Non-Goals

- No broad deletion in this gate.
- No full migration of dormant modules into the canonical implementations.
- No attempt to unify materially different APIs behind unsafe shims.
- No reorganization of packages in this ADR; that belongs to Gate 2.3.

## Follow-up

- Gate 2.3 should move only canonical active modules into subpackages and preserve compatibility wrappers.
- Any future secret-management activation should begin from `agent/secret_manager.py` and include a deliberate schema and policy review.
- Any future observability consolidation should reconcile `agent/observability.py` with the already-active `agent/tracing.py` and `agent/streaming.py` path instead of reviving `agent/observability_hub.py` as a parallel root.
- Any future cache warm-up work should treat `agent/cache.py` as the runtime cache contract and evaluate `agent/cache_warmup_manager.py` only as an adjunct subsystem, not as a replacement.
- `agent/agent_builder.py` remains a dormant importer and should not be used as sole evidence that a family is active on the main runtime path.
