# Documentation Consistency Report

Repository root: `C:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System`
Status: FAIL

## Repository Facts

- Model registry count: `27`
- Model registry docstring count: `27`
- CI Python matrix: `3.12, 3.13`
- Baseline tag: `phase-2-complete`
- Available phase tags: `phase-0-complete, phase-1-complete, phase-2-complete`
- Snapshot pytest pass count: `410`
- CI has pytest gate: `True`
- CI has ruff gate: `True`
- CI has coverage gate: `True`
- CI has active Bandit gate: `True`
- bandit.yaml skips: `[]`

## Checks

- [FAIL] `DOC001` Missing required versioned documentation files: docs/adr/README.md
- [PASS] `DOC002` Phase completion tags phase-0/1/2 are present.
- [PASS] `DOC003` Model registry docstring and MODELS dict both report 27 models.
- [FAIL] `DOC004` stale 24-model claim in: docs/adr/ADR-001-model-registry.md
- [FAIL] `DOC005` Missing baseline marker 'Documentation baseline: phase-2-complete' in: README.md, AGENTS.md, CLAUDE.md, tests/SUPPORT_MATRIX.md, docs/adr/README.md
- [FAIL] `DOC006` support matrix pass count does not match snapshot-phase-3-1/pytest_start.log (410); support matrix is missing CI Python version 3.12; support matrix is missing the release-gate ruff command 'ruff check .'; support matrix is missing CI lane name 'full-agent-bandit-audit'
- [FAIL] `DOC007` Stale 'agent/legacy/' path found in: AGENTS.md, CLAUDE.md
- [FAIL] `DOC008` ADR index is missing or incomplete: ADR-001-model-registry.md, ADR-002-enterprise-module-deduplication.md, ADR-003-db-strategy.md
- [PASS] `DOC009` Bandit policy keeps B602/B102/B307/B608 enabled globally.
- [FAIL] `DOC010` README storage wording is not aligned with ADR-003 (SQLite local/test, Postgres production target).

Failures: `7`
