# Sandbox Capability Matrix Review

## Runtime Availability Evidence

Source: `snapshot-phase-3-4/sandbox_runtime_availability.log`

Observed locally:

- Docker CLI available
- Docker Desktop server available
- `runsc` unavailable
- `firecracker` unavailable
- Python 3.13.3 on Git Bash / Windows environment

## Matrix Conclusions

- The current `agent/sandbox.py` subprocess sandbox remains the only active, repo-native execution path today.
- A Sandbox v2 policy wrapper is feasible immediately and is the best Phase 3.4 baseline for local dev and CI because it does not require privileged runtimes.
- Docker no-network is feasible locally on this machine and is worth optional proof probing, but it should remain non-blocking.
- gVisor and Firecracker cannot be recommended as required Phase 3.4 backends because they are not installed locally and Phase 3.4 forbids mandatory runtime installation.

## Phase 3.4 Decision

Recommend:

1. policy-first Sandbox v2 on top of the existing subprocess sandbox for repo-default behavior;
2. optional Docker no-network probing when Docker is already present;
3. defer gVisor/Firecracker production decisions to a later phase.
