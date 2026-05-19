# Next Actions

## Option A — Release / Remote Handoff

- push branch
- push tags
- open release PR
- attach handoff package
- verify CI on GitHub Actions

## Option B — Phase 3.9 Remaining Active Surface Ratchet

Continue module-level ratchet only if risk justifies it.

Candidate modules:

- `agent/config_manager.py`
- `agent/multimodal.py`
- `agent/notifications.py`
- `agent/persona.py`
- `agent/evaluation.py`

## Option C — Production Readiness Review

- secrets/config review
- deployment mode review
- database production path review
- sandbox production backend decision
