# Remaining Risks

## Operational Risks

- The local handoff package does not prove GitHub-hosted CI behavior because no remote push or GitHub Actions run was performed in this scope.
- Local verification was performed on a Windows workspace path containing a Unicode em dash, which still complicates some tools and required an explicit UTF-8 environment fix for `pip-audit`.
- Streaming coverage is strong, but the latest passing suite still emits non-blocking `response.drain()` deprecation warnings in `agent/streaming.py`.

## Security Risks

- Full-agent Bandit remains a separate audit lane rather than part of the blocking local release-gate surface.
- Local `pip-audit` now passes in this workspace, but only when run with explicit UTF-8 environment overrides and a writable cache directory.
- Security posture is validated locally, but not re-proved on remote CI in this handoff package.

## Quality Risks

- Coverage remains uneven outside the completed ratchet targets; low-coverage active surfaces still include `agent/config_manager.py`, `agent/multimodal.py`, `agent/notifications.py`, `agent/persona.py`, and `agent/evaluation.py`.
- Phase 3.5 mutation evidence remains informational with mutation score `0.0`, not a hard quality gate.
- The global coverage floor remains an anti-regression baseline guard, not a statement that the entire active runtime surface is deeply tested.

## Dependency / Environment Risks

- Dependency audit success in this workspace depends on a documented local execution fix (`PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, and a writable temp cache directory) when the repository lives under the Windows Unicode path.
- The handoff package does not update dependency files or prove behavior in a clean fresh clone on another machine.
- Local SQLite-backed development/test assumptions remain different from the documented Postgres production target.

## Evidence Gaps

- Dedicated `*FINAL_REPORT*` files were not found in filesystem inventory for Phase 0, Phase 1, Phase 2, Phase 3.1, or Phase 3.2.
- The handoff package relies on committed local evidence and does not include remote release artifacts, release binaries, or deployment manifests beyond the existing repository files.
