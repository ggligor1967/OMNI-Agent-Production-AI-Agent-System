# Quality State

## Coverage

- `fail_under`: `58`
- interpretation: baseline guard, not quality target
- latest total coverage: `68.45%` from `release-handoff/evidence/coverage.log`

## Completed Ratchets

Priority 1:

- `main.py`: `18.73%` → `86.93%`
- `agent/crypto_utils.py`: `29.18%` → `99.57%`

Priority 2:

- `agent/ollama_client.py`: final `100.00%`
- `agent/streaming.py`: final `90.00%`
- `agent/knowledge_graph.py`: final `99.00%`

## Test Suite

- latest pytest result: PASS
- latest test count: `510 passed` from `release-handoff/evidence/pytest.log`

## Mutation Baseline

- smoke baseline: `2` total mutants, `0` killed, `2` survived, mutation score `0.0`, runtime `5.652` seconds (`snapshot-phase-3-5/mutation_smoke_summary.json`)
- focused baseline: `8` total mutants, `0` killed, `8` survived, mutation score `0.0`, runtime `19.601` seconds (`snapshot-phase-3-5/mutation_baseline_summary.json`)
- interpretation: informational baseline, not hard gate
