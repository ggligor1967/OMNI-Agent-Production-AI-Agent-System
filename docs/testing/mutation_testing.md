# Mutation Testing Baseline

## Scope

Phase 3.5 records a mutation testing baseline only. It does not introduce a mandatory mutation score threshold.

## Goals

- Identify test-suite blind spots.
- Establish a reproducible baseline for selected active/hot-path modules.
- Keep runtime safe and local.
- Avoid external services.

## Non-Goals

- No repository-wide mandatory mutation gate.
- No numeric mutation score threshold in CI.
- No broad refactor to improve score.
- No production deployment impact.

## Tool Evaluation

Phase 3.5 evaluated two implementation paths:

1. `mutmut` as an off-the-shelf mutation runner.
2. A local lightweight mutation harness scoped to explicit targets.

The repository does not currently have a `pyproject.toml`, does not already vendor mutation tooling, and has strict Phase 3.5 safety constraints:

- no permanent source mutation
- no network or external services
- no repository-wide default sweep
- deterministic local execution from explicit target/test mappings

Based on those constraints, the Phase 3.5 baseline uses a local lightweight mutation harness under `tools/mutation/`. This keeps the scope explicit and avoids making mutation tooling a new packaging or CI blocker in this phase.

## Target Modules

Initial active/hot-path candidates:

- routing/model selection
- auth/security helpers
- sandbox policy
- RAG retrieval/query logic
- workflow safe expression evaluation
- documentation consistency checker if cheap and useful

Phase 3.5 baseline targets the following active/hot-path modules first:

- `agent/model_router.py`
- `agent/rag.py`
- `agent/sandbox.py`
- `agent/workflow.py`

## Metrics

The baseline records:

- total mutants
- killed mutants
- survived mutants
- timed out mutants
- incompetent mutants
- mutation score
- runtime

Mutation score is reported as:

$$
\text{mutation score} = \frac{\text{killed}}{\text{total} - \text{incompetent}} \times 100
$$

When every mutant is incompetent, the score is reported as `0.0` to avoid false inflation.

## Latest Recorded Results

Phase 3.5 smoke snapshot (`snapshot-phase-3-5/mutation_smoke_summary.json`):

- Date: `2026-05-19`
- Source commit: `1bb2636fc9e443eae55cebea17cde045c99dabaf`
- Tool: `local-ast-harness`
- Target modules: `agent/model_router.py`, `agent/sandbox.py`
- Test command: `python -m pytest tests/test_models.py tests/test_model_routing_tracing.py -q ; python -m pytest tests/test_sandbox_policy.py tests/test_sandbox_isolation_proofs.py tests/test_security_event_audit.py -q`
- Total mutants: `2`
- Killed: `0`
- Survived: `2`
- Timeout: `0`
- Incompetent: `0`
- Mutation score: `0.0`
- Runtime: `5.652` seconds

Phase 3.5 focused baseline (`snapshot-phase-3-5/mutation_baseline_summary.json`):

- Date: `2026-05-19`
- Source commit: `1bb2636fc9e443eae55cebea17cde045c99dabaf`
- Tool: `local-ast-harness`
- Target modules: `agent/model_router.py`, `agent/rag.py`, `agent/sandbox.py`, `agent/workflow.py`
- Test command: `python -m pytest tests/test_models.py tests/test_model_routing_tracing.py -q ; python -m pytest tests/test_new_modules.py tests/test_sql_injection_sweep.py -q ; python -m pytest tests/test_sandbox_policy.py tests/test_sandbox_isolation_proofs.py tests/test_security_event_audit.py -q ; python -m pytest tests/test_advanced_modules.py tests/test_tool_registry_enforcement.py -q`
- Total mutants: `8`
- Killed: `0`
- Survived: `8`
- Timeout: `0`
- Incompetent: `0`
- Mutation score: `0.0`
- Runtime: `19.601` seconds

All selected mutants survived in this first recorded baseline. That result is tracked as blind-spot evidence, not as a release failure and not as justification to change production behavior solely to improve mutation score.

## Execution Policy

- Target list must be explicit.
- Default mode is smoke, not full baseline.
- Full focused baseline requires explicit `--baseline`.
- Dirty working trees are refused by default.
- Mutation work must run in isolated temporary copies, not against live tracked files.
- Reports must be written to `snapshot-phase-3-5/`.
- Reports must not contain secrets, tokens, prompts, Authorization headers, or API keys.

## CI Policy

CI may run a smoke mutation check only. Full mutation baseline is local/manual unless later promoted.

Phase 3.5 does not make mutation score a hard CI gate.

## Known Limits

- Baseline scope is intentionally bounded to a small active/hot-path set.
- Survived mutants are recorded, not “fixed away,” in this phase.
- The harness favors deterministic operator substitutions over broad speculative mutation.
- Documentation consistency and existing release gates remain authoritative and must keep passing.

## Local Runbook

Smoke run:

- `python tools/mutation/run_mutation_baseline.py --smoke`

Focused baseline:

- `python tools/mutation/run_mutation_baseline.py --baseline`

Optional target override:

- `python tools/mutation/run_mutation_baseline.py --smoke --targets sandbox workflow`

The harness is expected to refuse dirty trees unless `--allow-dirty` is explicitly provided.
