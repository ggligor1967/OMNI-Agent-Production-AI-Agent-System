# Coverage Baseline Analysis

## Runs

| Run | Total Coverage | Tests Passed | Notes |
|-----|----------------|--------------|-------|
| 1 | 59.5963% | 469 | Normalized release-gate scope |
| 2 | 59.5963% | 469 | Normalized release-gate scope |
| 3 | 59.5963% | 469 | Normalized release-gate scope |

## Stability

Stable. Max-min variation across the three runs was 0.0000 percentage points, which is within the 1.0 point limit.

## Measurement Scope

The baseline was measured with the Phase 3.6 normalized release-gate coverage scope:

- included runtime surfaces: `agent/*`, `main.py`, `config.py`, `job_search_*.py`, `tools/check_documentation_consistency.py`
- omitted non-runtime or non-comparable paths: `tests/*`, `agent/_legacy/*`, `snapshot-*`, `tools/mutation/*`, `tools/performance/*`, environment and stdlib leakage paths

This keeps the threshold comparable to the gate it will enforce and avoids widening Phase 3.6 into a full-repository audit lane.

## Proposed Threshold

`58`

## Rationale

Threshold is based on the measured normalized baseline and rounded down conservatively. The minimum measured total coverage was 59.5963%, so the proposed fail-under value is 58%.
No production code or tests were changed to inflate the score; the denominator was normalized so the enforced gate matches the measured scope.
