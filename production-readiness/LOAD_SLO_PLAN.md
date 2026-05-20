# Production Load and SLO Plan

## Status

PENDING EXECUTION

## Current Evidence

Phase 3.3 local baseline:

- smoke requests: `2918`
- smoke failures: `0`
- smoke p50/p95/p99/max: `4.293 ms` / `5.91 ms` / `11.052 ms` / `16.363 ms`
- baseline requests: `6034`
- baseline failures: `0`
- baseline p50/p95/p99/max: `9.331 ms` / `19.076 ms` / `30.519 ms` / `149.195 ms`

Evidence:

- `snapshot-phase-3-3/performance_smoke_summary.md`
- `snapshot-phase-3-3/performance_baseline_summary.md`
- `snapshot-phase-3-3/PHASE_3_3_FINAL_REPORT.md`
- `docs/performance.md`

## Important Limitation

The current baseline is local only and does not define production SLOs.

## Required Production Load Test

- target environment
- workload model
- concurrency profile
- duration
- success criteria
- failure budget
- monitoring required
- rollback trigger

## Proposed Initial SLO Draft

These are **PROPOSED** planning targets only. They are not approved production SLOs.

- **PROPOSED availability:** `99.0%` monthly for control-plane endpoints such as `/status`, excluding planned maintenance.
- **PROPOSED error budget:** `< 1%` unexpected `5xx` responses over a rolling 15-minute window under the approved workload profile.
- **PROPOSED latency target for `/status`:** p95 `< 250 ms`, p99 `< 500 ms` in the selected production environment.
- **PROPOSED `/chat` target:** do not approve a latency SLO until provider mix, timeout budgets, and upstream model/network dependencies are selected and measured in the real production topology.

## Blockers

- No production environment has been selected for load execution.
- No approved production workload profile exists.
- No concurrency target or duration target is committed.
- No production observability backend is approved to capture the load-test evidence.
- No rollback trigger threshold is approved for load-induced degradation.
