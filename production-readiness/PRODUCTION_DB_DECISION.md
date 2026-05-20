# Production Database Decision

## Status

PENDING DECISION

## Current Policy

- SQLite is local/dev only.
- Postgres is production target.

## Required Production Decision

| Area | Decision | Evidence | Status |
| ------ | ---------- | ---------- | ------ |
| Production DB engine | Postgres as the architectural production target | `docs/adr/ADR-003-db-strategy.md` is accepted and explicitly states that Postgres is the production target while SQLite remains the active local/test default | APPROVED |
| Migration strategy | Backend-aware subsystem migration still required | `docs/adr/ADR-003-db-strategy.md` documents that `DB_BACKEND=postgres` is not sufficient today because active runtime services still open SQLite directly | PENDING |
| Backup strategy | Snapshot / logical backup approach not yet approved | No committed production backup policy or backup automation is present in repository evidence | PENDING |
| Restore procedure | Documented and tested restore path not yet approved | No committed production restore drill or restore runbook evidence exists in scope | PENDING |
| Data export contracts | Verified export boundaries still need production sign-off | `agent/export.py` exists, but no committed production DB export contract approval or restore-validation evidence is present | PENDING |

## Blockers

- Active runtime services are still SQLite-first on the hot path; Postgres is a target, not a completed runtime migration.
- No approved production migration plan exists for auth, memory, RAG, knowledge graph, evaluation, config persistence, or notifications.
- No committed production backup and restore policy exists.
- No restore drill evidence exists.
- No approved production data-retention/export contract is recorded for the chosen database system.
