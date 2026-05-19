# Mutation Testing Summary

- Timestamp: `2026-05-19T12:46:43.070641+00:00`
- Mode: `baseline`
- Tool: `local-ast-harness`
- Runtime: `19.601` seconds
- Test command: `python -m pytest tests/test_models.py tests/test_model_routing_tracing.py -q ; python -m pytest tests/test_new_modules.py tests/test_sql_injection_sweep.py -q ; python -m pytest tests/test_sandbox_policy.py tests/test_sandbox_isolation_proofs.py tests/test_security_event_audit.py -q ; python -m pytest tests/test_advanced_modules.py tests/test_tool_registry_enforcement.py -q`

## Target Modules

- `agent/model_router.py`
- `agent/rag.py`
- `agent/sandbox.py`
- `agent/workflow.py`

## Metrics

| Metric | Value |
| --- | ---: |
| Total mutants | 8 |
| Killed | 0 |
| Survived | 8 |
| Timeout | 0 |
| Incompetent | 0 |
| Mutation score | 0.0 |

## Limits

Focused baseline is intentionally capped per target to keep Phase 3.5 local and reproducible.
