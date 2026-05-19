# Phase 3.8 Priority 2 Quality Ratchet Baseline

## Execution Context

- working directory: `/c/Users/gligo/My Projects/OMNI Agent — Production AI Agent System`
- branch: `main`
- head at start: `c01f592f65ba0268971a32c33be33eedb38cf94c`
- expected Phase 3.7 closeout commit: `c01f592f65ba0268971a32c33be33eedb38cf94c`
- `phase-3.7-complete` target: `c01f592f65ba0268971a32c33be33eedb38cf94c`
- available phase tags: `phase-0-complete`, `phase-1-complete`, `phase-2-complete`, `phase-3.1-complete`, `phase-3.2-complete`, `phase-3.3-complete`, `phase-3.4-complete`, `phase-3.5-complete`, `phase-3.6-complete`, `phase-3.7-complete`
- python version: `Python 3.13.3`
- working tree before snapshot creation: clean

## Baseline Verification

- `python -m compileall agent main.py` → `PASS`
- `pytest tests/ -q` → `490 passed`
- `ruff check .` → `PASS`
- `python tools/check_documentation_consistency.py --root .` → `PASS`
- `coverage run -m pytest tests/ && coverage report` → `PASS`
- active-path `bandit` gate from `.github/workflows/ci.yml` → `PASS`

## Global Coverage

- fail_under: `58`
- interpretation: baseline guard, not quality target
- current total coverage: `65.34%` (`10462` statements, `3626` missed)
- drift vs Phase 3.7 final evidence: none detected at Gate 3.8.0

## Priority 1 Carry-Forward

- `main.py`: `86.93%`
- `agent/crypto_utils.py`: `99.57%`

## Priority 2

| Module | Current Coverage | Target | Reason |
| --- | ---: | ---: | --- |
| `agent/ollama_client.py` | `33.73%` | `>= 55%` | raw Ollama HTTP client, availability probes, chat/generate/embed paths |
| `agent/streaming.py` | `41.43%` | `>= 60%` | SSE framing, event bus delivery, session scoping, heartbeat/live routes |
| `agent/knowledge_graph.py` | `43.14%` | `>= 60%` | graph persistence, traversal/path logic, export semantics, HTTP routes |

## Evidence Files

- `snapshot-phase-3-8/git_status_start.txt`
- `snapshot-phase-3-8/head_start.txt`
- `snapshot-phase-3-8/phase37_tag_target.txt`
- `snapshot-phase-3-8/gate_3_8_0_compile.log`
- `snapshot-phase-3-8/gate_3_8_0_pytest.log`
- `snapshot-phase-3-8/gate_3_8_0_ruff.log`
- `snapshot-phase-3-8/gate_3_8_0_doc_consistency.log`
- `snapshot-phase-3-8/gate_3_8_0_coverage.log`
- `snapshot-phase-3-8/gate_3_8_0_bandit_active_path.log`
- `snapshot-phase-3-8/coverage_start.json`

## Rule

Do not chase global average. Improve risk-bearing modules.
