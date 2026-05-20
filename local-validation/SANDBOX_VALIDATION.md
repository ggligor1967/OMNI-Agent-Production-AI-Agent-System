# Local Sandbox Validation

## Status

PASS

## Tests

- sandbox policy: `local-validation/evidence/l4_sandbox_tests_resolved.log` (`14 passed` across policy and isolation cases)
- isolation proofs: included in `local-validation/evidence/l4_sandbox_tests_resolved.log`
- optional Docker no-network probe: `local-validation/evidence/l4_docker_no_network_probe.log` showed `ROUTE_LINE_COUNT=0` and `NETWORK_DISABLED_EVIDENCE=NO_ROUTES`

## Behaviors Checked

- network deny by default
- environment filtering
- filesystem allowlist behavior
- timeout/output control
- subprocess/shell restrictions

## Notes

This validates local behavior only. It does not approve production sandbox backend.

A Windows-specific invocation quirk was observed when targeting the sandbox test files with `pytest.exe` directly; `python -m pytest` produced the correct local validation result and is the evidence-backed outcome for this gate.
