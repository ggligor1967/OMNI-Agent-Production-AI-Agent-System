from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "check_documentation_consistency.py"
SPEC = importlib.util.spec_from_file_location("documentation_consistency", MODULE_PATH)
assert SPEC and SPEC.loader
DOC_CHECK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DOC_CHECK
SPEC.loader.exec_module(DOC_CHECK)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_model_registry(count: int, doc_count: int | None = None) -> str:
    if doc_count is None:
        doc_count = count
    entries = ",\n".join(f'    "model-{index}": {index}' for index in range(count))
    return (
        f'"""OMNI AGENT - Model Registry\nExports:\n- MODELS dict ({doc_count} models)\n"""\n\n'
        "MODELS = {\n"
        f"{entries}\n"
        "}\n"
    )


def make_ci_workflow() -> str:
        return """name: CI

on: [push, pull_request]

jobs:
    release-gate:
        runs-on: ubuntu-latest
        strategy:
            matrix:
                python-version: [\"3.12\", \"3.13\"]
        steps:
            - run: python -m compileall agent main.py
            - run: pytest tests/ -q
            - run: ruff check .
            - run: python tools/check_documentation_consistency.py --root .
            - name: Bandit active-path gate
                run: |
                    bandit -ll -ii -c bandit.yaml \\
                        main.py \\
                        agent/core.py
            - run: pip-audit
            - name: Coverage threshold gate
                run: coverage erase && coverage run -m pytest tests/ && coverage report

    full-agent-bandit-audit:
        runs-on: ubuntu-latest
        steps:
            - run: echo audit

    legacy-audit:
        runs-on: ubuntu-latest
        steps:
            - run: pytest tests/_archive/ -q || true
"""


def make_support_matrix(pass_count: int) -> str:
    return f"""# OMNI Agent Test Support Matrix

Documentation baseline: phase-2-complete
Generated: 2026-05-18
Active Suite Status: **PASSING** ({pass_count}/{pass_count} tests)
Model Catalog Contract: **27 cloud models**

## CI Matrix

- Python 3.12
- Python 3.13
- CI lanes: `release-gate`, `full-agent-bandit-audit`, `legacy-audit`

## Release-Gate Summary

```bash
pytest tests/ -q
ruff check .
python tools/check_documentation_consistency.py --root .
pip-audit
coverage erase && coverage run -m pytest tests/ && coverage report
```

Current result:

- **{pass_count} passed**
- coverage floor: `fail_under = 58`
- threshold reference coverage total: `59.65%`
- latest coverage total: `65.26%`
- coverage policy: `docs/testing/coverage.md`
- **0 failed**
- **0 errors**
"""


def make_coveragerc() -> str:
    return """[run]
include =
    agent/*
    main.py
    config.py
    job_search_*.py
    tools/check_documentation_consistency.py

[report]
fail_under = 58
"""


def make_coverage_log() -> str:
    return """============================= 489 passed in 10.41s =============================
Name                                       Stmts   Miss   Cover   Missing
-------------------------------------------------------------------------
agent\\crypto_utils.py                        233      1  99.57%   ...
agent\\knowledge_graph.py                     299    170  43.14%   ...
agent\\ollama_client.py                        83     55  33.73%   ...
agent\\streaming.py                           210    123  41.43%   ...
main.py                                      566     74  86.93%   ...
-------------------------------------------------------------------------
TOTAL                                      10416   3619  65.26%
"""


def make_reference_coverage_log() -> str:
    return """============================= 474 passed in 10.41s =============================
Name                                       Stmts   Miss   Cover   Missing
-------------------------------------------------------------------------
agent\\crypto_utils.py                        233    165  29.18%   ...
agent\\knowledge_graph.py                     299    170  43.14%   ...
agent\\ollama_client.py                        83     55  33.73%   ...
agent\\streaming.py                           210    123  41.43%   ...
main.py                                      566    460  18.73%   ...
-------------------------------------------------------------------------
TOTAL                                      10338   4171  59.65%
"""


def make_coverage_doc() -> str:
    return """# Coverage Policy

## Baseline Guard

- `fail_under = 58`
- baseline guard for the active runtime surface
- not a quality target

## Current Measured State

- Phase 3.6 threshold reference coverage: `59.65%`
- Phase 3.6 threshold reference statements: `10338`
- Phase 3.6 threshold reference missed statements: `4171`
- latest Phase 3.7 coverage: `65.26%`
- latest Phase 3.7 statements: `10416`
- latest Phase 3.7 missed statements: `3619`

## Quality Ratchet Policy

- module-level ratchet is the quality mechanism
- global average alone does not define quality

### Priority 1

- `main.py`: start `18.73%`, target `>= 30%`, current `86.93%`
- `agent/crypto_utils.py`: start `29.18%`, target `>= 50%`, current `99.57%`

### Priority 2

- `agent/ollama_client.py`
- `agent/streaming.py`
- `agent/knowledge_graph.py`

## Interpretation Rules

- no artificial tests
- do not exclude active runtime code to improve the percentage
- no per-file hard threshold yet
"""


def build_valid_repo(root: Path) -> None:
    write(root / "agent/model_registry.py", make_model_registry(27))
    write(root / ".github/workflows/ci.yml", make_ci_workflow())
    write(root / ".coveragerc", make_coveragerc())
    write(root / "bandit.yaml", "exclude_dirs:\n  - agent/_legacy\n  - tests\nskips: []\n")
    write(root / "pytest.ini", "[pytest]\nasyncio_mode = auto\ntestpaths = tests\n")
    write(root / "ruff.toml", "[lint]\nselect = [\"E9\", \"F63\"]\n")
    write(root / "snapshot-phase-3-6/gate_3_6_3_pytest.log", "============================= 474 passed in 8.42s =============================\n")
    write(root / "snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log", make_reference_coverage_log())
    write(root / "snapshot-phase-3-7/gate_3_7_2_pytest.log", "============================= 489 passed in 8.42s =============================\n")
    write(root / "snapshot-phase-3-7/gate_3_7_2_coverage.log", make_coverage_log())

    baseline = "Documentation baseline: phase-2-complete"
    storage_line = "Storage strategy: SQLite for local development and tests; Postgres is the production target."
    coverage_line = "Coverage policy: fail_under = 58 is the baseline guard; see docs/testing/coverage.md.\n"
    common_doc = (
        f"{baseline}\n\n"
        "OMNI Agent routes across 27 cloud models.\n"
        f"{storage_line}\n"
    )
    write(root / "README.md", common_doc + coverage_line)
    write(root / "AGENTS.md", common_doc + "Use agent/_legacy for archived code.\n")
    write(root / "CLAUDE.md", common_doc + "Use agent/_legacy for archived code.\n")
    write(root / ".env.example", "# AVAILABLE MODELS (all 27 cloud models):\n")
    write(root / "tests/SUPPORT_MATRIX.md", make_support_matrix(489))
    write(root / "docs/testing/coverage.md", make_coverage_doc())

    write(root / "docs/adr/ADR-001-model-registry.md", "# ADR-001\nThe runtime MODELS registry contains 27 entries.\nTests and docs must assert 27 models, not 24.\n")
    write(root / "docs/adr/ADR-002-enterprise-module-deduplication.md", "# ADR-002\nPhase 2 canonical modules are documented here.\n")
    write(root / "docs/adr/ADR-003-db-strategy.md", "# ADR-003\nSQLite is supported for local development and test runs.\nPostgres is the production target.\n")
    write(
        root / "docs/adr/README.md",
        "Documentation baseline: phase-2-complete\n\n"
        "- ADR-001-model-registry.md\n"
        "- ADR-002-enterprise-module-deduplication.md\n"
        "- ADR-003-db-strategy.md\n",
    )


def failing_codes(results: list[Any]) -> set[str]:
    return {result.code for result in results if not result.ok}


def test_run_checks_passes_for_synchronized_fixture(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    monkeypatch.setattr(
        DOC_CHECK,
        "get_phase_tags",
        lambda root: ("phase-0-complete", "phase-1-complete", "phase-2-complete"),
    )

    facts, results = DOC_CHECK.run_checks(tmp_path)

    assert facts.model_count == 27
    assert facts.pytest_pass_count == 489
    assert failing_codes(results) == set()


def test_run_checks_reports_expected_drift(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "README.md", "OMNI Agent routes across 24 models.\n")
    write(tmp_path / "AGENTS.md", "Documentation baseline: phase-2-complete\nUse agent/legacy/ for archived code.\n")
    write(tmp_path / "CLAUDE.md", "OMNI Agent routes across 27 cloud LLM models.\n")
    write(tmp_path / "tests/SUPPORT_MATRIX.md", "# Support Matrix\nActive Suite Status: **PASSING** (325/325 tests)\n")
    write(tmp_path / "docs/testing/coverage.md", "# Coverage Policy\n\n- `fail_under = 58`\n")
    (tmp_path / "docs/adr/README.md").unlink()

    monkeypatch.setattr(DOC_CHECK, "get_phase_tags", lambda root: ("phase-0-complete",))

    _, results = DOC_CHECK.run_checks(tmp_path)
    failures = failing_codes(results)

    assert {"DOC001", "DOC002", "DOC004", "DOC005", "DOC006", "DOC007", "DOC008", "DOC010", "DOC011"} <= failures


def test_run_checks_flags_critical_bandit_skips(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "bandit.yaml", "exclude_dirs:\n  - tests\nskips: [B602, B307]\n")
    monkeypatch.setattr(
        DOC_CHECK,
        "get_phase_tags",
        lambda root: ("phase-0-complete", "phase-1-complete", "phase-2-complete"),
    )

    _, results = DOC_CHECK.run_checks(tmp_path)

    assert "DOC009" in failing_codes(results)


def test_historical_adr_context_does_not_trigger_stale_model_failure(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(
        tmp_path / "docs/adr/ADR-001-model-registry.md",
        "# ADR-001\n"
        "The runtime MODELS registry contains 27 entries, while older docs still described the catalog as 24 models.\n"
        "Tests and docs must assert 27 models, not 24.\n",
    )
    monkeypatch.setattr(
        DOC_CHECK,
        "get_phase_tags",
        lambda root: ("phase-0-complete", "phase-1-complete", "phase-2-complete"),
    )

    _, results = DOC_CHECK.run_checks(tmp_path)

    assert "DOC004" not in failing_codes(results)


def test_extract_pytest_pass_count_reads_utf16_logs(tmp_path) -> None:
    log_path = tmp_path / "pytest.log"
    log_path.write_text("============================= 417 passed in 9.99s =============================\n", encoding="utf-16")

    assert DOC_CHECK.extract_pytest_pass_count(log_path) == 417


def test_extract_coverage_metrics_reads_cp1252_logs(tmp_path) -> None:
    log_path = tmp_path / "coverage.log"
    log_text = (
        "rootdir: C:\\Users\\gligo\\My Projects\\OMNI Agent — Production AI Agent System\n"
        "Name                                       Stmts   Miss   Cover   Missing\n"
        "-------------------------------------------------------------------------\n"
        "agent\\\\crypto_utils.py                        233    165  29.18%   ...\n"
        "main.py                                      566    460  18.73%   ...\n"
        "-------------------------------------------------------------------------\n"
        "TOTAL                                      10338   4171  59.65%\n"
    )
    log_path.write_bytes(log_text.encode("cp1252"))

    assert DOC_CHECK.extract_coverage_metrics(log_path) == (59.65, 10338, 4171, 18.73, 29.18)


def test_extract_preferred_pytest_pass_count_prefers_phase_3_8_evidence(tmp_path) -> None:
    write(tmp_path / "snapshot-phase-3-1/pytest_start.log", "============================= 417 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-3/gate_3_3_3_pytest.log", "============================= 444 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-4/gate_3_4_3_pytest.log", "============================= 452 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-5/gate_3_5_3_pytest.log", "============================= 469 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-6/gate_3_6_3_pytest.log", "============================= 473 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-7/gate_3_7_2_pytest.log", "============================= 489 passed in 9.99s =============================\n")
    write(tmp_path / "snapshot-phase-3-8/gate_3_8_3_pytest.log", "============================= 509 passed in 9.99s =============================\n")

    assert DOC_CHECK.extract_preferred_pytest_pass_count(tmp_path) == 509


def test_extract_preferred_coverage_metrics_prefers_phase_3_8_evidence(tmp_path) -> None:
    write(tmp_path / "snapshot-phase-3-7/gate_3_7_4_coverage.log", make_coverage_log())
    write(
        tmp_path / "snapshot-phase-3-8/gate_3_8_3_coverage.log",
        """============================= 509 passed in 10.41s =============================
Name                                       Stmts   Miss   Cover   Missing
-------------------------------------------------------------------------
agent\\crypto_utils.py                        233      1  99.57%   ...
agent\\knowledge_graph.py                     299      3  99.00%   ...
agent\\ollama_client.py                        83      0 100.00%   ...
agent\\streaming.py                           210     21  90.00%   ...
main.py                                      566     74  86.93%   ...
-------------------------------------------------------------------------
TOTAL                                      10462   3301  68.45%
""",
    )

    assert DOC_CHECK.extract_preferred_coverage_metrics(tmp_path) == (
        68.45,
        10462,
        3301,
        86.93,
        99.57,
        "snapshot-phase-3-8/gate_3_8_3_coverage.log",
    )


def test_run_checks_flags_missing_coverage_policy_details(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "docs/testing/coverage.md", "# Coverage Policy\n\n- `fail_under = 58`\n")
    monkeypatch.setattr(
        DOC_CHECK,
        "get_phase_tags",
        lambda root: ("phase-0-complete", "phase-1-complete", "phase-2-complete"),
    )

    _, results = DOC_CHECK.run_checks(tmp_path)

    assert "DOC011" in failing_codes(results)


def test_main_returns_zero_in_report_only_mode(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "README.md", "stale docs\n")
    monkeypatch.setattr(DOC_CHECK, "get_phase_tags", lambda root: ())

    report_path = tmp_path / "report.md"
    exit_code = DOC_CHECK.main(["--root", str(tmp_path), "--report-only", "--output", str(report_path)])

    assert exit_code == 0
    assert report_path.exists()
    assert "Status: FAIL" in report_path.read_text(encoding="utf-8")


def test_main_returns_nonzero_on_failures_without_report_only(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "README.md", "stale docs\n")
    monkeypatch.setattr(DOC_CHECK, "get_phase_tags", lambda root: ())

    exit_code = DOC_CHECK.main(["--root", str(tmp_path)])

    assert exit_code == 1
