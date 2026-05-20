# Sandbox Runtime Decision

## Status

PENDING DECISION

## Current State

- `SandboxPolicy` exists.
- Local isolation proof tests exist.
- Docker no-network probe evidence exists.
- Production sandbox backend is not automatically approved.

Evidence:

- `agent/sandbox.py` defines `SandboxPolicy` and the active sandbox implementation.
- `tests/test_sandbox_policy.py` and `tests/test_sandbox_isolation_proofs.py` verify deny-by-default network behavior, environment filtering, filesystem allowlists, timeout enforcement, and shell execution restrictions.
- `docs/security/sandbox_v2_capability_matrix.md` records that Docker is available locally while `runsc` and `firecracker` are unavailable.
- `snapshot-phase-3-4/sandbox_runtime_availability.log` records Docker Desktop availability and the absence of `runsc` and `firecracker` on this machine.

## Candidate Backends

| Backend | Security Fit | Operational Cost | Current Evidence | Status |
| ------ | -------------- | ------------------ | ------------------ | ------ |
| Current policy wrapper | low / medium | low | Active hot-path sandbox with policy controls and local proof tests, but explicitly documented as insufficient as a final production isolation boundary | dev/local only |
| Docker no-network | medium | medium | Local Docker availability is proven and the Phase 3.4 capability matrix calls it a plausible next-step backend | candidate |
| gVisor | high | medium / high | Capability matrix treats it as a promising later candidate, but `runsc` is unavailable locally and no repo-level operational proof exists | pending |
| Firecracker | high | high | Capability matrix treats it as a future-only stronger isolation option, but runtime availability and operations evidence are absent | pending |

## Required Decision

Select the production sandbox backend or keep this gate pending until the deployment owner chooses and validates a runtime with explicit network, filesystem, environment, timeout, and resource-control guarantees appropriate for untrusted code execution.

## Blockers

- No production sandbox backend has been selected and approved.
- No production image/runtime hardening policy is committed for Docker no-network execution.
- No operational proof exists for gVisor or Firecracker in this repository scope.
- No production environment-filtering, egress-control, or filesystem-allowlist acceptance criteria are signed off.
- No deployment-specific resource-limit policy is approved for untrusted code execution.
