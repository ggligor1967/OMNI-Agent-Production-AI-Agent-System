# Phase 3.7 Quality Ratchet Baseline

## Execution Context

- working directory: `/c/Users/gligo/My Projects/OMNI Agent — Production AI Agent System`
- branch: `main`
- head at start: `f7d6b5ae568b4c991e71d2e92bdfc2e54b40c786`
- expected Phase 3.6 closeout commit: `f7d6b5ae568b4c991e71d2e92bdfc2e54b40c786`
- `phase-3.6-complete` target: `f7d6b5ae568b4c991e71d2e92bdfc2e54b40c786`
- available phase tags: `phase-0-complete`, `phase-1-complete`, `phase-2-complete`, `phase-3.1-complete`, `phase-3.2-complete`, `phase-3.3-complete`, `phase-3.4-complete`, `phase-3.5-complete`, `phase-3.6-complete`
- python version: `Python 3.13.3`
- working tree before snapshot creation: clean

## Global Coverage

- fail_under: `58`
- interpretation: baseline guard, not quality target
- current total coverage: `59.93%`

## Priority 1

| Module                  | Current Coverage | Target   | Reason                              |
|-------------------------|------------------|----------|-------------------------------------|
| `main.py`               | `18.73%`         | `>= 30%` | runtime/API entry point             |
| `agent/crypto_utils.py` | `29.18%`         | `>= 50%` | security-sensitive crypto utilities |

## Priority 2

- `agent/ollama_client.py`
- `agent/streaming.py`
- `agent/knowledge_graph.py`

## Rule

Do not chase global average. Improve risk-bearing modules.
