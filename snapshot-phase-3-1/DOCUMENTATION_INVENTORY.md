# Gate 3.1.1 Documentation Inventory

Date: 2026-05-18

## Evidence Used

- `snapshot-phase-3-1/documentation_files.txt`
- `snapshot-phase-3-1/documentation_claims_scan.log`
- `snapshot-phase-3-1/pytest_start.log`
- `snapshot-phase-3-1/git_status_start.txt`
- `.github/workflows/ci.yml`
- `agent/model_registry.py`
- `bandit.yaml`
- `pytest.ini`
- `ruff.toml`
- `docs/adr/ADR-001-model-registry.md`
- `docs/adr/ADR-002-enterprise-module-deduplication.md`
- `docs/adr/ADR-003-db-strategy.md`

## Contract Documents in Synchronization Scope

| File | Role | Evidence-backed claim surface | Priority |
| --- | --- | --- | --- |
| `README.md` | Primary user-facing repository documentation | model count, startup paths, dashboard behavior, RAG/API surface, storage wording, test commands | High |
| `AGENTS.md` | Versioned agent-operator guidance | architecture summary, model contract, test guidance, legacy-path wording | High |
| `CLAUDE.md` | Versioned agent-operator guidance | architecture summary, model contract, test guidance, legacy-path wording | High |
| `tests/SUPPORT_MATRIX.md` | Existing support-matrix document (actual location; no root `SUPPORT_MATRIX.md`) | active-suite counts, release-gate command, model contract, Python notes | High |
| `docs/adr/ADR-001-model-registry.md` | ADR for model-registry contract | 27-model decision, provider normalization | High |
| `docs/adr/ADR-002-enterprise-module-deduplication.md` | ADR for Phase 2 canonical modules | canonical vs non-canonical module families | High |
| `docs/adr/ADR-003-db-strategy.md` | ADR for storage/runtime policy | SQLite-first runtime, Postgres production target, no premature migration claims | High |

## Supporting Sources of Truth

These files should drive documentation claims even when they are not themselves prose documents:

- `agent/model_registry.py` — runtime source of truth for the **27-model** catalog.
- `.github/workflows/ci.yml` — source of truth for the release-gate matrix (`3.12`, `3.13`), `pytest tests/ -q`, `ruff check .`, active-path Bandit, `coverage`, `full-agent-bandit-audit`, and `legacy-audit`.
- `bandit.yaml` — active Bandit policy; excludes `agent/_legacy` and `tests`, with `skips: []`.
- `pytest.ini` — active suite path is `tests`, with `tests/_archive` and `agent/_legacy` excluded.
- `ruff.toml` — current lint policy is narrow and correctness-focused, not a broad style contract.
- phase tags `phase-0-complete`, `phase-1-complete`, `phase-2-complete` — version anchors for historical claims.

## Observed Drift and Documentation Risks

1. `tests/SUPPORT_MATRIX.md` is stale against current repository evidence.
   - It says `Active Suite Status: PASSING (325/325 tests)`.
   - Gate 3.1.0 baseline evidence recorded `410 passed` in `snapshot-phase-3-1/pytest_start.log`.

2. `AGENTS.md` and `CLAUDE.md` use `agent/legacy/` in the quarantine note.
   - Current policy files and CI use `agent/_legacy`.
   - This is a verified path drift, not a hypothetical one.

3. `README.md`, `AGENTS.md`, and `CLAUDE.md` still present narrow test guidance centered on older “canonical” subsets.
   - The current blocking release gate is `pytest tests/ -q` from `.github/workflows/ci.yml`.
   - The active suite now includes more than the three older headline files.

4. Storage wording must be synchronized with ADR-003.
   - `README.md` references SQLite/PostgreSQL at a high level.
   - ADR-003 explicitly states the active runtime is still SQLite-first and that Postgres is the production target, not a completed migration.
   - Phase 3.1.3 must avoid any wording that implies full Postgres runtime support today.

5. There is no root `SUPPORT_MATRIX.md`.
   - The existing versioned equivalent is `tests/SUPPORT_MATRIX.md`.
   - Any Phase 3.1 documentation work must account for that actual path.

6. There is no ADR index file under `docs/adr/`.
   - No `README*.md` or `index*.md` exists there.
   - ADR discoverability currently depends on directory listing only.

7. `documentation_files.txt` includes non-contract text artifacts because it is a raw inventory.
   - Examples: `.tmp_job_search_*.txt`, `coverage_baseline_sprint0.txt`, `requirements.txt`.
   - These are evidence or utility artifacts, not the documentation contract set for synchronization.

8. `README.md` quick-start references `.env.example`, and that file does exist.
   - This is not currently a missing-file issue.
   - `.env.example` also independently supports the 27-model routing contract.

## Phase 3.1.3 Synchronization Target

The minimum synchronized documentation set should be:

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- `tests/SUPPORT_MATRIX.md`
- `docs/adr/ADR-001-model-registry.md`
- `docs/adr/ADR-002-enterprise-module-deduplication.md`
- `docs/adr/ADR-003-db-strategy.md`

Supporting policy validation should be checked against:

- `.github/workflows/ci.yml`
- `agent/model_registry.py`
- `bandit.yaml`
- `pytest.ini`
- `ruff.toml`

## Inventory Decision

Gate 3.1.1 should treat the documents above as the versioned documentation contract set for Phase 3.1.
Raw inventory files remain useful as evidence, but synchronization decisions should be driven by the contract documents and their supporting source-of-truth files listed here.
