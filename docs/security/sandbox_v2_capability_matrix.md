# Sandbox v2 Capability Matrix

| Backend | Available Locally | Isolation Strength | Network Control | Filesystem Control | Resource Limits | Windows/WSL Fit | CI Fit | Operational Complexity | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Current Python sandbox | yes | low-medium | low — dangerous modules are blocked statically, but there is no explicit runtime network capability contract yet | low — no explicit read/write allowlist contract yet | medium — timeout and output truncation exist; CPU/memory limits are not explicit | high | high | low | **Local dev:** keep as current baseline only. **CI:** acceptable as the default safe path while Sandbox v2 lands. **Production:** insufficient as a final isolation boundary by itself. |
| Subprocess with policy wrapper | yes | medium | medium — Phase 3.4 policy can deny network by default and expose an explicit capability flag even if enforcement is backend-limited | medium — policy allowlists can define intended read/write scope and deny writes outside explicit paths | medium — timeout and output limits are straightforward; stronger CPU/memory/process limits depend on OS/backend support | high | high | low-medium | **Local dev:** recommended Phase 3.4 baseline. **CI:** recommended because it is testable without privileged runtimes. **Production:** still needs a stronger backend for high-risk workloads. |
| Docker no-network | yes | medium-high | high — `--network none` is a clear and well-understood control | medium-high — mount/volume choices and read-only rootfs are available | high — cgroup and container-level limits are available | medium — workable on Windows via Docker Desktop, but not zero-friction | medium — useful for optional probes or dedicated jobs, but should not be mandatory for default CI | medium | **Local dev:** good optional proof backend when Docker is already present. **CI:** optional/non-blocking only. **Production:** a plausible next-step backend, but still needs hardened policy and image/runtime decisions. |
| gVisor / runsc | no | high | high | high | high | low-medium | low-medium | high | **Local dev:** unavailable on this machine. **CI:** do not require in Phase 3.4. **Production:** promising later candidate if installed and operationally justified. |
| Firecracker | no | high | high | high | high | low | low | very high | **Local dev:** unavailable on this machine. **CI:** do not require in Phase 3.4. **Production:** future-only candidate for stronger isolation, not a Phase 3.4 requirement. |

## Evidence Notes

- `agent/sandbox.py` is the active hot-path sandbox used by `execute_python` through `agent/tools_registry.py`.
- `snapshot-phase-3-4/sandbox_runtime_availability.log` shows:
  - Docker CLI present at `/c/Program Files/Docker/Docker/resources/bin/docker`
  - Docker client/server available locally via Docker Desktop
  - `runsc` unavailable
  - `firecracker` unavailable
- No missing runtime was installed for this evaluation.

## Recommendation Summary

### Local Development

Use the current subprocess sandbox plus an explicit Sandbox v2 policy wrapper as the default local path. Keep Docker no-network as an optional local proof backend because it is present on this machine, but do not make it mandatory for all contributors.

### CI

Keep CI centered on policy-level tests and local proof-of-isolation tests that do not require Docker, gVisor, or Firecracker. Optional Docker probes may run in a non-blocking lane only when the runtime is already available.

### Production

Do not treat the current subprocess sandbox as the final production isolation boundary for higher-risk code execution. A future production decision should compare hardened Docker/container isolation versus stronger runtimes such as gVisor or Firecracker after policy requirements are finalized.
