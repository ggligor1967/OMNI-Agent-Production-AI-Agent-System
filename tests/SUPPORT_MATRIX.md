# OMNI Agent Test Support Matrix

Documentation baseline: phase-2-complete
Generated: 2026-05-19
Active Suite Status: **PASSING** (474/474 tests)
Model Catalog Contract: **27 cloud models**

---

## CI Contract

- Python 3.12
- Python 3.13
- Blocking lane: `release-gate`
- Audit lanes: `full-agent-bandit-audit`, `legacy-audit`

### Blocking release-gate commands

```bash
python -m compileall agent main.py
pytest tests/ -q
ruff check .
python tools/check_documentation_consistency.py --root .
pip-audit
coverage erase && coverage run -m pytest tests/ && coverage report
```

Bandit remains a blocking active-path gate in `.github/workflows/ci.yml`, with policy sourced from `bandit.yaml`.

---

## Latest Verified Local Evidence

- **474 passed** — `snapshot-phase-3-6/gate_3_6_4_pytest.log`
- Coverage threshold gate — `snapshot-phase-3-6/gate_3_6_4_coverage.log` (`TOTAL 10416 / 4174 / 59.93%`, `fail_under = 58`)
- Coverage policy document — `docs/testing/coverage.md`
- Threshold-enforcement reference evidence — `snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log` (`TOTAL 10338 / 4171 / 59.65%`)
- Mutation smoke summary — `snapshot-phase-3-5/mutation_smoke_summary.json`
- Mutation focused baseline summary — `snapshot-phase-3-5/mutation_baseline_summary.json`
- Mutation focused baseline raw log — `snapshot-phase-3-5/mutation_baseline_raw.log`
- Ruff passed — `snapshot-phase-3-5/gate_3_5_4_ruff.log`
- Documentation consistency report — `snapshot-phase-3-5/gate_3_5_4_doc_consistency.log`
- Sandbox isolation proofs — `snapshot-phase-3-4/gate_3_4_4_isolation_tests.log`
- Bandit active-path report — `snapshot-phase-3-4/gate_3_4_4_bandit_active_path.log`
- Sandbox evaluation summary — `snapshot-phase-3-4/SANDBOX_V2_EVALUATION.md`
- Performance smoke summary — `snapshot-phase-3-3/performance_smoke_summary.json`
- Performance baseline summary — `snapshot-phase-3-3/performance_baseline_summary.json`

---

## Active Release-Gate Suite Inventory

The blocking suite is `pytest tests/ -q` with discovery controlled by `pytest.ini`.

- `test_advanced_modules.py` — runtime modules
- `test_auth_bootstrap_cli.py` — auth bootstrap
- `test_auth_ownership_binding.py` — auth ownership binding
- `test_chat_tracing.py` — chat-path tracing regression coverage
- `test_core_init_sanity.py` — core init AST audit
- `test_coverage_config.py` — coverage floor and baseline contract
- `test_dashboard.py` — dashboard UI
- `test_documentation_consistency.py` — documentation contract checker
- `test_export_api_contracts.py` — export API compatibility
- `test_http_tracing.py` — HTTP entry tracing regression coverage
- `test_job_search_tank_adr_improved.py` — job search improvements
- `test_md5_sweep.py` — active-path MD5 sweep
- `test_models.py` — model registry and routing
- `test_model_routing_tracing.py` — router and fallback tracing regression coverage
- `test_new_modules.py` — RAG, cache, templates, pipeline
- `test_phase2_import_compatibility.py` — Phase 2 import compatibility
- `test_phase2_refactor_equivalence.py` — Phase 2 behavior equivalence
- `test_performance_harness.py` — local performance harness contract
- `test_performance_smoke_summary.py` — smoke summary schema and redaction
- `test_mutation_harness.py` — local mutation harness contract
- `test_mutation_smoke_summary.py` — mutation smoke summary schema and redaction
- `test_redis_asyncio_cache.py` — Redis asyncio alignment
- `test_security_auth_tools.py` — auth and tool enforcement
- `test_security_event_audit.py` — security audit logging
- `test_sandbox_isolation_proofs.py` — safe sandbox proof-of-isolation coverage
- `test_sandbox_policy.py` — sandbox policy interface coverage
- `test_silent_exception_sweep.py` — silent exception sweep
- `test_sql_injection_sweep.py` — SQL parameterization checks
- `test_ssrf_validator.py` — SSRF validation
- `test_startup_security.py` — startup security
- `test_suite.py` — core modules
- `test_tool_registry_enforcement.py` — tool registry enforcement

---

## Archived / Non-Blocking Paths

- `tests/_archive/` — non-blocking legacy audit path
- `agent/_legacy/` — excluded from release-gate tooling and Bandit active-path enforcement

---

## Contract Notes

- The runtime model registry source of truth is `agent/model_registry.py` with **27 cloud models**.
- Use `docs/adr/ADR-001-model-registry.md` for the model-catalog decision history.
- Use `docs/adr/ADR-002-enterprise-module-deduplication.md` for Phase 2 canonical-module decisions.
- Use `docs/adr/ADR-003-db-strategy.md` for the SQLite-local / Postgres-production storage policy.
- Use `docs/performance.md` for the Phase 3.3 local workload contract and baseline metrics.
- Use `docs/testing/coverage.md` for the Phase 3.6 baseline floor and Quality Ratchet Policy.
- Use `docs/testing/mutation_testing.md` for the Phase 3.5 local mutation-testing scope, safety rules, and recorded baseline metrics.
- The support matrix lives at `tests/SUPPORT_MATRIX.md`; there is no separate root-level `SUPPORT_MATRIX.md`.
