# Mutation Testing Summary

- Timestamp: `2026-05-19T12:45:29.395995+00:00`
- Mode: `smoke`
- Tool: `local-ast-harness`
- Runtime: `5.652` seconds
- Test command: `python -m pytest tests/test_models.py tests/test_model_routing_tracing.py -q ; python -m pytest tests/test_sandbox_policy.py tests/test_sandbox_isolation_proofs.py tests/test_security_event_audit.py -q`

## Target Modules

- `agent/model_router.py`
- `agent/sandbox.py`

## Metrics

| Metric | Value |
| --- | ---: |
| Total mutants | 2 |
| Killed | 0 |
| Survived | 2 |
| Timeout | 0 |
| Incompetent | 0 |
| Mutation score | 0.0 |

## Limits

Smoke mode uses only a small subset of targets and mutants.
