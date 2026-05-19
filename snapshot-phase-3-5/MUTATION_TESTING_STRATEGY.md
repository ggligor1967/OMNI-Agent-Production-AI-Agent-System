# Mutation Testing Strategy Review

## Selected Target Modules

Phase 3.5 focuses on explicit active/hot-path modules with strong local test coverage:

- `agent/model_router.py`
- `agent/rag.py`
- `agent/sandbox.py`
- `agent/workflow.py`

## Selected Test Commands

Initial target-to-test mapping:

- `agent/model_router.py`
  - `python -m pytest tests/test_models.py tests/test_model_routing_tracing.py -q`
- `agent/rag.py`
  - `python -m pytest tests/test_new_modules.py tests/test_sql_injection_sweep.py -q`
- `agent/sandbox.py`
  - `python -m pytest tests/test_sandbox_policy.py tests/test_sandbox_isolation_proofs.py tests/test_security_event_audit.py -q`
- `agent/workflow.py`
  - `python -m pytest tests/test_advanced_modules.py tests/test_tool_registry_enforcement.py -q`

## Chosen Tool

Selected implementation: local lightweight mutation harness under `tools/mutation/`.

Reasoning:

- explicit target list is easier to keep bounded
- no existing mutation framework is already wired into the repo
- no `pyproject.toml` exists for standardized mutation-tool config
- temporary copies are safer than in-place source mutation for this phase
- smoke/baseline split can be enforced directly by the harness

## Runtime Budget

Target runtime budgets for local execution:

- smoke run: roughly 1 to 3 minutes on a developer workstation
- focused baseline: roughly 3 to 10 minutes depending on machine speed and selected targets

If the focused baseline exceeds the practical local budget, the run may be reduced and explicitly documented in the baseline summary.

## Known Exclusions

Phase 3.5 baseline excludes:

- repository-wide mutation sweeps
- CI-enforced mutation score thresholds
- tests, docs, snapshots, and `.git` content as mutation targets
- external APIs, real LLM calls, Docker, and network-dependent execution
- broad refactors or production behavior changes to chase score improvements
