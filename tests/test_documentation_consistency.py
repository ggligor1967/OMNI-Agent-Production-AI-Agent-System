from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
      - name: Bandit active-path gate
        run: |
          bandit -ll -ii -c bandit.yaml \\
            main.py \\
            agent/core.py
      - run: coverage erase && coverage run -m pytest tests/ && coverage report

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
```

Current result:

- **{pass_count} passed**
- **0 failed**
- **0 errors**
"""


def build_valid_repo(root: Path) -> None:
    write(root / "agent/model_registry.py", make_model_registry(27))
    write(root / ".github/workflows/ci.yml", make_ci_workflow())
    write(root / "bandit.yaml", "exclude_dirs:\n  - agent/_legacy\n  - tests\nskips: []\n")
    write(root / "pytest.ini", "[pytest]\nasyncio_mode = auto\ntestpaths = tests\n")
    write(root / "ruff.toml", "[lint]\nselect = [\"E9\", \"F63\"]\n")
    write(root / "snapshot-phase-3-1/pytest_start.log", "============================= 410 passed in 8.42s =============================\n")

    baseline = "Documentation baseline: phase-2-complete"
    storage_line = "Storage strategy: SQLite for local development and tests; Postgres is the production target."
    common_doc = (
        f"{baseline}\n\n"
        "OMNI Agent routes across 27 cloud models.\n"
        f"{storage_line}\n"
    )
    write(root / "README.md", common_doc)
    write(root / "AGENTS.md", common_doc + "Use agent/_legacy for archived code.\n")
    write(root / "CLAUDE.md", common_doc + "Use agent/_legacy for archived code.\n")
    write(root / ".env.example", "# AVAILABLE MODELS (all 27 cloud models):\n")
    write(root / "tests/SUPPORT_MATRIX.md", make_support_matrix(410))

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


def failing_codes(results: list[object]) -> set[str]:
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
    assert facts.pytest_pass_count == 410
    assert failing_codes(results) == set()


def test_run_checks_reports_expected_drift(tmp_path, monkeypatch) -> None:
    build_valid_repo(tmp_path)
    write(tmp_path / "README.md", "OMNI Agent routes across 24 models.\n")
    write(tmp_path / "AGENTS.md", "Documentation baseline: phase-2-complete\nUse agent/legacy/ for archived code.\n")
    write(tmp_path / "CLAUDE.md", "OMNI Agent routes across 27 cloud LLM models.\n")
    write(tmp_path / "tests/SUPPORT_MATRIX.md", "# Support Matrix\nActive Suite Status: **PASSING** (325/325 tests)\n")
    (tmp_path / "docs/adr/README.md").unlink()

    monkeypatch.setattr(DOC_CHECK, "get_phase_tags", lambda root: ("phase-0-complete",))

    _, results = DOC_CHECK.run_checks(tmp_path)
    failures = failing_codes(results)

    assert {"DOC001", "DOC002", "DOC004", "DOC005", "DOC006", "DOC007", "DOC008", "DOC010"} <= failures


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
