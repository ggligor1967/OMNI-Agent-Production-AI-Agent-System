# Sandbox v2 Evaluation

## Status

Gate 3.4 evaluation completed with a safe local proof-of-isolation baseline.

- Active sandbox hot path reviewed: `execute_python` -> `agent/sandbox.py`
- Threat model documented: `docs/security/sandbox_v2_threat_model.md`
- Capability matrix documented: `docs/security/sandbox_v2_capability_matrix.md`
- Policy interface implemented in the active sandbox runtime
- Safe local proof tests added and passing
- Optional Docker `--network none` probe executed locally without requiring image pulls or external connectivity

## Evaluated Backends

| Backend | Local availability | Phase 3.4 role | Notes |
| --- | --- | --- | --- |
| Current Python sandbox | Available | Active baseline | Real hot path; now uses explicit `SandboxPolicy` controls |
| Subprocess with policy wrapper | Available | Recommended baseline | Works in local dev and CI without privileged runtime requirements |
| Docker `--network none` | Available locally | Optional supporting evidence | Safe local probe succeeded; should remain non-blocking in CI |
| gVisor / `runsc` | Unavailable | Not required | Excluded from Phase 3.4 acceptance |
| Firecracker | Unavailable | Not required | Excluded from Phase 3.4 acceptance |

## Implemented Isolation Baseline

The Phase 3.4 baseline is policy-first rather than VM-first.

Implemented controls in `agent/sandbox.py`:

- explicit `SandboxPolicy`
- deny-by-default environment inheritance with filtered subprocess env
- explicit network allowance flag
- explicit read/write allowlist helpers
- explicit timeout and max-output controls
- explicit audit events for allow/deny decisions
- shell execution still disabled unless explicitly enabled

This keeps the active path safe for local execution and CI while avoiding mandatory Docker, gVisor, Firecracker, privileged containers, or host kernel changes.

## Safe Proof-of-Isolation Coverage

`tests/test_sandbox_isolation_proofs.py` adds safe local proofs for:

1. secret-bearing environment variables are not inherited by default
2. filesystem writes outside the configured allowlist are denied at the policy layer
3. allowlisted temp writes are permitted by policy when a backend supports them
4. network access is denied by default
5. subprocess / shell execution is denied unless explicitly enabled
6. oversized output is truncated according to policy
7. timeout policy is represented and enforced by the current backend

Validation result:

- `snapshot-phase-3-4/gate_3_4_4_isolation_tests.log` -> `7 passed in 0.70s`
- `snapshot-phase-3-4/gate_3_4_4_pytest.log` -> `459 passed in 9.54s`
- `snapshot-phase-3-4/gate_3_4_4_ruff.log` -> `All checks passed!`
- `snapshot-phase-3-4/gate_3_4_4_doc_consistency.log` -> `Status: PASS`
- `snapshot-phase-3-4/gate_3_4_4_bandit_active_path.log` -> `No issues identified.`

## Optional Docker No-Network Probe

Local probe artifact: `snapshot-phase-3-4/docker_no_network_probe.log`

Observed result:

- local image used: `debian:bookworm-slim`
- interface list showed only `lo`
- `/proc/net/route` contained no active routes
- probe conclusion: `NETWORK_DISABLED_EVIDENCE=NO_ROUTES`

This probe is supporting evidence only. It is intentionally non-blocking and relies solely on local Docker availability.

## Recommendation

Phase 3.4 should adopt the following Sandbox v2 baseline:

- keep the active subprocess sandbox as the required cross-platform baseline
- enforce policy-driven env, path, timeout, output, and shell controls in-process
- treat Docker no-network as optional local hardening evidence, not as a CI requirement
- defer gVisor / Firecracker decisions to a later phase with explicit platform and operational prerequisites

## Evidence Bundle

- `docs/security/sandbox_v2_threat_model.md`
- `docs/security/sandbox_v2_capability_matrix.md`
- `snapshot-phase-3-4/SANDBOX_THREAT_MODEL_REVIEW.md`
- `snapshot-phase-3-4/SANDBOX_CAPABILITY_MATRIX_REVIEW.md`
- `snapshot-phase-3-4/SANDBOX_V2_EVALUATION.md`
- `snapshot-phase-3-4/sandbox_runtime_availability.log`
- `snapshot-phase-3-4/docker_no_network_probe.log`
- `snapshot-phase-3-4/gate_3_4_4_isolation_tests.log`
- `snapshot-phase-3-4/gate_3_4_4_pytest.log`
- `snapshot-phase-3-4/gate_3_4_4_ruff.log`
- `snapshot-phase-3-4/gate_3_4_4_doc_consistency.log`
- `snapshot-phase-3-4/gate_3_4_4_bandit_active_path.log`
