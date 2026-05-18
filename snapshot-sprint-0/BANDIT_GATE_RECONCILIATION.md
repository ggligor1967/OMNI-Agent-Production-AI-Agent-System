# Bandit Gate Reconciliation

## Status

PASS — the blocking Bandit gate is now aligned to the active/hot-path Phase 0 policy and passes locally.

**Matrix note:** Phase 0 matrix verification is not retroactively complete yet; GitHub Actions still needs to rerun on Python 3.12 and 3.13 with the reconciled CI workflow.

## Why reconciliation was required

The original release gate used a full recursive Bandit scan across `agent/`:

- `bandit -r agent -x agent/_legacy,tests -ll -ii`

That broad scan failed in the Linux `python:3.13` matrix proof recorded in `snapshot-sprint-0/PHASE_0_MATRIX_VERIFICATION.md` with:

- 18 HIGH findings
- 43 MEDIUM findings

That result was too broad for the Sprint 0 blocking policy because the scan included orphan, standalone, or legacy-adjacent modules that are not on the active runtime path.

Supporting evidence:

- `snapshot-sprint-0/orphan_module_inventory.md` classifies modules such as `ab_router`, `gateway`, `health_monitor`, `hot_reloader`, `message_queue`, `secrets_manager`, and `session` as orphan/standalone candidates rather than active runtime dependencies.
- `snapshot-sprint-0/bandit_security_triage.md` already showed that many full-agent Bandit hits were either false positives or out-of-path findings.
- The active runtime chain remains `main.py` → `config.py` → `agent/core.py`, with direct runtime API exposure for auth, routing, workflows, RAG, sandbox, streaming, dashboard, and multimodal paths.

## Blocking active-path Bandit scope

The blocking gate now covers the evidence-based hot path only:

- `main.py`
- `config.py`
- `agent/core.py`
- `agent/auth.py`
- `agent/model_registry.py`
- `agent/model_router.py`
- `agent/multi_model_client.py`
- `agent/ollama_client.py`
- `agent/memory.py`
- `agent/rag.py`
- `agent/cache.py`
- `agent/pipeline.py`
- `agent/prompt_templates.py`
- `agent/tools_registry.py`
- `agent/sandbox.py`
- `agent/workflow.py`
- `agent/streaming.py`
- `agent/dashboard.py`
- `agent/multimodal.py`
- `agent/config_manager.py`
- `agent/structured_output.py`

Notes:

- The user-provided placeholder `agent/config.py` does not exist in this repository; the active config source is root `config.py`, and runtime config routing lives in `agent/config_manager.py`.
- This list is intentionally explicit so the blocking lane cannot silently widen back into orphan or dormant surfaces.

## Active-path findings before reconciliation

| File | Rule | Classification | Resolution |
| --- | --- | --- | --- |
| `agent/workflow.py:333` | `B307` | active-path blocker | Removed `eval()` and replaced it with a constrained AST-based transform evaluator. |
| `main.py:31` | `B104` | active-path false positive | Added precise `# nosec B104` to the fail-fast guard that explicitly rejects insecure public bind + disabled auth. |
| `main.py:65` | `B104` | active-path false positive | Added precise `# nosec B104` to display-only host normalization; no socket binding occurs on that line. |

## Policy after reconciliation

### Blocking lane

The `release-gate` job now runs Bandit only against the explicit active-path file list above. This lane remains **blocking**.

### Full-agent audit lane

A separate `full-agent-bandit-audit` job now runs the broader audit command:

- `bandit -r agent -x agent/_legacy,tests -c bandit.yaml -ll -ii`

This lane is **non-blocking** and uploads `snapshot-sprint-0/bandit_full_agent_audit.log` as a CI artifact.

### Bandit policy file

`bandit.yaml` now documents the Sprint 0 rule set:

- exclude only `agent/_legacy` and `tests`
- keep `skips: []`
- do **not** add repo-wide skips for `B602`, `B102`, `B307`, or `B608`

Any remaining active-path exceptions must be justified inline with targeted `# nosec` usage.

## Local verification after reconciliation

The following commands were rerun locally after the code and CI changes:

- `pytest tests/ -q` → **327 passed**
- `ruff check .` → **All checks passed**
- active-path Bandit command with `-ll -ii -c bandit.yaml` and the explicit file list above → **No issues identified** (exit 0)

Additional targeted regression coverage was added for workflow transforms:

- safe transform expressions execute successfully
- unsafe calls such as `__import__(...)` are rejected and fail the workflow step

## Outcome

The Sprint 0 blocking Bandit policy is now reconciled as follows:

1. **Blocking gate:** active/hot-path modules only
2. **Audit gate:** full-agent recursive scan, non-blocking, artifact preserved
3. **No global security-rule skips:** strict policy remains intact
4. **Active-path blocker removed:** `agent/workflow.py` no longer depends on `eval()`

## Next step

Rerun the GitHub Actions matrix on Python 3.12 and Python 3.13. If the matrix passes with the new active-path Bandit lane, the Phase 0 matrix verification can be updated from `BLOCKED` to `PASS`.
