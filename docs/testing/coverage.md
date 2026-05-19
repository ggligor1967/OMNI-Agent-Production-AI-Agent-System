# Coverage Policy

## Baseline Guard

Phase 3.6 introduces a numeric global coverage floor in `.coveragerc`:

- `fail_under = 58`
- derived from the stable measured Phase 3.6 baseline rather than guessed values
- used as a baseline guard and anti-regression floor for the active release-gate runtime surface
- **not a quality target**

The floor exists to prevent large regressions while the project still has several active modules with visibly uneven test depth.

## Current Measured State

The threshold policy is anchored to the committed Phase 3.6 threshold-reference evidence in `snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log`:

- total coverage: `59.65%`
- total statements: `10338`
- missed statements: `4171`

The latest committed Phase 3.8 Priority 2 ratchet evidence in `snapshot-phase-3-8/gate_3_8_3_coverage.log` records the current local state after the knowledge-graph gate:

- total coverage: `68.45%`
- total statements: `10462`
- missed statements: `3301`

This measurement follows the normalized `.coveragerc` scope for active runtime code and should stay comparable to the stable Phase 3.6 baseline analysis.

## Quality Ratchet Policy

The global floor catches regressions, but improvement work should follow a module-level quality ratchet.

### Priority 1

| Module | Start | Target | Current | Status | Rationale |
| --- | --- | --- | --- | --- | --- |
| `main.py` | `18.73%` | `>= 30%` | `86.93%` | `PASS` | Entry-point and orchestration surface with high operational risk if behavior drifts unnoticed. |
| `agent/crypto_utils.py` | `29.18%` | `>= 50%` | `99.57%` | `PASS` | Security-sensitive helper surface; better regression depth matters more than cosmetic average gains. |

### Priority 2

| Module | Start | Target | Current | Status | Rationale |
| --- | --- | --- | --- | --- | --- |
| `agent/ollama_client.py` | `33.73%` | `>= 55%` | `100.00%` | `PASS` | Active provider integration surface with direct request/response and streaming risk. |
| `agent/streaming.py` | `41.43%` | `>= 60%` | `90.00%` | `PASS` | Runtime event delivery and SSE response streaming surface. |
| `agent/knowledge_graph.py` | `43.14%` | `>= 60%` | `99.00%` | `PASS` | Active storage/query/traversal surface with persistence and route behavior. |
| Other active low-coverage runtime modules | varies | later phase | varies | pending | Continue ratcheting in later phases using the release-gate coverage report as evidence. |

## Interpretation Rules

- the global average catches regression across the active runtime surface
- the module-level ratchet controls operational and security risk more directly than the global average alone
- the module-level ratchet is the quality mechanism for this phase
- the global average alone does not define quality
- **no artificial tests** created only to raise the percentage
- do not exclude active runtime code to improve the percentage
- no per-file hard threshold yet unless a later phase introduces it explicitly

## Evidence References

- Coverage configuration: `.coveragerc`
- Stable baseline rationale: `snapshot-phase-3-6/COVERAGE_BASELINE_ANALYSIS.md`
- Threshold-reference evidence: `snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log`
- Phase 3.7 crypto ratchet evidence: `snapshot-phase-3-7/CRYPTO_UTILS_RATCHET.md`
- Phase 3.7 main entrypoint ratchet evidence: `snapshot-phase-3-7/MAIN_ENTRYPOINT_RATCHET.md`
- Phase 3.8 Ollama client ratchet evidence: `snapshot-phase-3-8/OLLAMA_CLIENT_RATCHET.md`
- Phase 3.8 streaming ratchet evidence: `snapshot-phase-3-8/STREAMING_RATCHET.md`
- Phase 3.8 knowledge graph ratchet evidence: `snapshot-phase-3-8/KNOWLEDGE_GRAPH_RATCHET.md`
- Latest committed ratchet coverage evidence: `snapshot-phase-3-8/gate_3_8_3_coverage.log`
