# Sandbox Threat Model Review

## Reviewed Inputs

- `agent/sandbox.py`
- `agent/sandbox_executor.py`
- `agent/tools_registry.py`
- `agent/core.py`
- `agent/security_audit.py`
- `tests/test_security_event_audit.py`
- `tests/test_security_auth_tools.py`
- `snapshot-phase-3-2/`

## Findings

- The active OMNI hot path already routes `execute_python` through `agent.sandbox.Sandbox`.
- Security audit callbacks already exist and are sanitized before persistence.
- Existing controls are useful but primarily coarse-grained: blocked imports, subprocess execution, timeout, and output truncation.
- There is no explicit Sandbox v2 policy contract yet for network, environment inheritance, filesystem allowlists, or backend selection.
- This makes the threat model documentation a prerequisite for any safe Sandbox v2 evaluation or proof testing.

## Phase 3.4 Decision

Document Sandbox v2 as a deny-by-default policy problem first, then evaluate isolation backends and add proof tests without claiming production deployment.
