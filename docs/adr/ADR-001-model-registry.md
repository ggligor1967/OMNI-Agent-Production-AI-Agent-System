# ADR-001 — Model Registry Contract

- Status: Accepted
- Date: 2026-05-18

## Context

The runtime `MODELS` registry contains 27 entries, while several tests and documentation sections still described the catalog as 24 models. Repository audit artifacts in `scripts/_model_audit.py` also identify three additional registry entries beyond the original 24:

- `deepseek-v3.2:cloud`
- `minimax-m2.7:cloud`
- `nemotron-3-super:cloud`

This mismatch caused the active suite to fail and left the code, README, and support documentation out of sync.

## Decision

Keep the 27-model registry and update the surrounding contract to match it.

The three additional models remain part of the supported catalog:

- `deepseek-v3.2:cloud`
- `minimax-m2.7:cloud`
- `nemotron-3-super:cloud`

Provider ownership is normalized for the Mistral family models:

- `ministral-3:8b-cloud` → `Mistral AI`
- `devstral-2:123b-cloud` → `Mistral AI`
- `devstral-small-2:24b-cloud` → `Mistral AI`

## Consequences

- Tests and docs must assert 27 models, not 24.
- Runtime metadata, README, and agent guidance files must use the same catalog size.
- Provider consistency checks should treat the Mistral family as `Mistral AI`.
- The active suite should verify both the expanded catalog and provider normalization.

## Notes

This ADR resolves the Sprint 0 Gate 0.4 model-registry contract mismatch by choosing the existing runtime registry as the source of truth and synchronizing tests and documentation to it.
