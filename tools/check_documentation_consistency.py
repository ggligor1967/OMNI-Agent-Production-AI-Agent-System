from __future__ import annotations

"""Deterministic documentation consistency checks for Phase 3.1.

The checker reads repository facts from versioned files and Git tags, then
verifies that the versioned documentation contract stays synchronized with
those facts. It is intentionally local-friendly: no network access, no test
execution, and no dependence on CI-only environment variables.
"""

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PHASE_TAGS = (
    "phase-0-complete",
    "phase-1-complete",
    "phase-2-complete",
)
ADR_FILES = (
    Path("docs/adr/ADR-001-model-registry.md"),
    Path("docs/adr/ADR-002-enterprise-module-deduplication.md"),
    Path("docs/adr/ADR-003-db-strategy.md"),
)
COVERAGE_POLICY_DOC = Path("docs/testing/coverage.md")
REQUIRED_VERSIONED_DOCS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("tests/SUPPORT_MATRIX.md"),
    COVERAGE_POLICY_DOC,
    Path("docs/adr/README.md"),
    *ADR_FILES,
)
BASELINE_MARKER_DOCS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("tests/SUPPORT_MATRIX.md"),
    Path("docs/adr/README.md"),
)
MODEL_COUNT_DOCS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("tests/SUPPORT_MATRIX.md"),
    Path(".env.example"),
    Path("docs/adr/ADR-001-model-registry.md"),
)
STALE_MODEL_COUNT_DOCS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("tests/SUPPORT_MATRIX.md"),
)
LEGACY_PATH_DOCS = (
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
)
CRITICAL_BANDIT_SKIPS = {"B602", "B102", "B307", "B608"}
COVERAGE_REFERENCE_LOG = Path("snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log")
COVERAGE_LOG_CANDIDATES = (
    Path("snapshot-phase-3-7/gate_3_7_4_coverage.log"),
    Path("snapshot-phase-3-7/gate_3_7_2_coverage.log"),
    Path("snapshot-phase-3-7/gate_3_7_1_coverage.log"),
    Path("snapshot-phase-3-6/gate_3_6_4_coverage.log"),
    Path("snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log"),
    Path("snapshot-phase-3-6/gate_3_6_2_coverage_report.log"),
)
PREFERRED_PYTEST_LOG_CANDIDATES = (
    Path("snapshot-phase-3-7/gate_3_7_4_pytest.log"),
    Path("snapshot-phase-3-7/gate_3_7_3_pytest.log"),
    Path("snapshot-phase-3-7/gate_3_7_2_pytest.log"),
    Path("snapshot-phase-3-7/gate_3_7_1_pytest.log"),
    Path("snapshot-phase-3-6/gate_3_6_4_pytest.log"),
    Path("snapshot-phase-3-6/gate_3_6_3_pytest_rerun.log"),
    Path("snapshot-phase-3-6/gate_3_6_3_pytest.log"),
    Path("snapshot-phase-3-6/gate_3_6_2_pytest.log"),
    Path("snapshot-phase-3-6/gate_3_6_2_coverage_pytest.log"),
    Path("snapshot-phase-3-6/gate_3_6_1_pytest.log"),
    Path("snapshot-phase-3-5/gate_3_5_4_pytest.log"),
    Path("snapshot-phase-3-5/gate_3_5_3_pytest.log"),
    Path("snapshot-phase-3-5/gate_3_5_2_pytest.log"),
    Path("snapshot-phase-3-5/gate_3_5_1_pytest.log"),
    Path("snapshot-phase-3-5/pytest_start.log"),
    Path("snapshot-phase-3-4/gate_3_4_4_pytest.log"),
    Path("snapshot-phase-3-4/gate_3_4_3_pytest.log"),
    Path("snapshot-phase-3-4/gate_3_4_2_pytest.log"),
    Path("snapshot-phase-3-4/gate_3_4_1_pytest.log"),
    Path("snapshot-phase-3-4/pytest_start.log"),
    Path("snapshot-phase-3-3/gate_3_3_4_pytest.log"),
    Path("snapshot-phase-3-3/gate_3_3_3_pytest.log"),
    Path("snapshot-phase-3-3/gate_3_3_2_pytest.log"),
    Path("snapshot-phase-3-3/gate_3_3_1_pytest.log"),
    Path("snapshot-phase-3-3/pytest_start.log"),
    Path("snapshot-phase-3-2/gate324_pytest_full.log"),
    Path("snapshot-phase-3-1/gate_3_1_3_pytest.log"),
    Path("snapshot-phase-3-1/pytest_start.log"),
)


@dataclass(frozen=True)
class CheckResult:
    code: str
    ok: bool
    message: str


@dataclass(frozen=True)
class RepoFacts:
    model_count: int
    model_docstring_count: int | None
    ci_python_versions: tuple[str, ...]
    ci_has_pytest_gate: bool
    ci_has_ruff_gate: bool
    ci_has_coverage_gate: bool
    ci_has_active_bandit_gate: bool
    bandit_skips: frozenset[str]
    phase_tags: tuple[str, ...]
    pytest_pass_count: int | None
    baseline_tag: str
    adr_files: tuple[str, ...]
    coverage_fail_under: int | None
    coverage_reference_percent: float | None
    coverage_reference_statements: int | None
    coverage_reference_missed: int | None
    main_py_start_coverage: float | None
    crypto_utils_start_coverage: float | None
    coverage_reference_path: str | None
    coverage_total_percent: float | None
    coverage_total_statements: int | None
    coverage_total_missed: int | None
    main_py_coverage: float | None
    crypto_utils_coverage: float | None
    coverage_evidence_path: str | None


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return raw.decode(encoding)
            except UnicodeError:
                continue

    for encoding in ("utf-8", "utf-8-sig", "cp1252", "cp850", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_model_facts(path: Path) -> tuple[int, int | None]:
    source = read_text(path)
    tree = ast.parse(source, filename=str(path))
    module_docstring = ast.get_docstring(tree) or ""
    docstring_match = re.search(r"MODELS dict \((\d+) models\)", module_docstring)
    docstring_count = int(docstring_match.group(1)) if docstring_match else None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "MODELS" in targets and isinstance(node.value, ast.Dict):
                return len(node.value.keys), docstring_count
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "MODELS" and isinstance(node.value, ast.Dict):
                return len(node.value.keys), docstring_count

    raise ValueError(f"Could not locate MODELS dict in {path}")


def extract_ci_python_versions(path: Path) -> tuple[str, ...]:
    text = read_text(path)
    match = re.search(r'python-version:\s*\[([^\]]+)\]', text)
    if not match:
        return ()
    versions = []
    for raw in match.group(1).split(","):
        value = raw.strip().strip('"\'')
        if value:
            versions.append(value)
    return tuple(versions)


def extract_bandit_skips(path: Path) -> frozenset[str]:
    text = read_text(path)
    inline_match = re.search(r'^\s*skips:\s*\[(.*?)\]\s*$', text, flags=re.MULTILINE)
    if inline_match:
        values = [item.strip().strip('"\'') for item in inline_match.group(1).split(",") if item.strip()]
        return frozenset(values)

    lines = text.splitlines()
    skips: list[str] = []
    in_skips = False
    base_indent = 0
    for line in lines:
        if not in_skips:
            match = re.match(r'^(\s*)skips:\s*$', line)
            if not match:
                continue
            in_skips = True
            base_indent = len(match.group(1))
            continue

        if not line.strip():
            continue
        current_indent = len(line) - len(line.lstrip())
        if current_indent <= base_indent:
            break
        entry = line.strip()
        if entry.startswith("-"):
            skips.append(entry[1:].strip().strip('"\''))
    return frozenset(skips)


def get_phase_tags(root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "tag", "--list", "phase-*-complete"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ()
    tags = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return tuple(sorted(tags))


def extract_pytest_pass_count(path: Path) -> int | None:
    if not path.exists():
        return None
    matches = re.findall(r'(\d+)\s+passed', read_text(path))
    return int(matches[-1]) if matches else None


def extract_preferred_pytest_pass_count(root: Path) -> int | None:
    for candidate in PREFERRED_PYTEST_LOG_CANDIDATES:
        absolute_candidate = root / candidate
        count = extract_pytest_pass_count(absolute_candidate)
        if count is not None:
            return count
    return None


def extract_fail_under(path: Path) -> int | None:
    if not path.exists():
        return None
    match = re.search(r"^\s*fail_under\s*=\s*(\d+)\s*$", read_text(path), flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def extract_coverage_metrics(path: Path) -> tuple[float | None, int | None, int | None, float | None, float | None]:
    if not path.exists():
        return None, None, None, None, None

    text = read_text(path)
    total_match = re.search(r"^TOTAL\s+(\d+)\s+(\d+)\s+([0-9.]+)%", text, flags=re.MULTILINE)
    main_match = re.search(r"^main\.py\s+\d+\s+\d+\s+([0-9.]+)%", text, flags=re.MULTILINE)
    crypto_match = re.search(r"^agent[\\/]+crypto_utils\.py\s+\d+\s+\d+\s+([0-9.]+)%", text, flags=re.MULTILINE)

    total_percent = float(total_match.group(3)) if total_match else None
    total_statements = int(total_match.group(1)) if total_match else None
    total_missed = int(total_match.group(2)) if total_match else None
    main_percent = float(main_match.group(1)) if main_match else None
    crypto_percent = float(crypto_match.group(1)) if crypto_match else None
    return total_percent, total_statements, total_missed, main_percent, crypto_percent


def extract_preferred_coverage_metrics(
    root: Path,
) -> tuple[float | None, int | None, int | None, float | None, float | None, str | None]:
    for candidate in COVERAGE_LOG_CANDIDATES:
        absolute_candidate = root / candidate
        total_percent, total_statements, total_missed, main_percent, crypto_percent = extract_coverage_metrics(
            absolute_candidate
        )
        if total_percent is not None:
            return (
                total_percent,
                total_statements,
                total_missed,
                main_percent,
                crypto_percent,
                candidate.as_posix(),
            )
    return None, None, None, None, None, None


def extract_reference_coverage_metrics(
    root: Path,
) -> tuple[float | None, int | None, int | None, float | None, float | None, str | None]:
    absolute_candidate = root / COVERAGE_REFERENCE_LOG
    total_percent, total_statements, total_missed, main_percent, crypto_percent = extract_coverage_metrics(
        absolute_candidate
    )
    if total_percent is None:
        return None, None, None, None, None, None
    return (
        total_percent,
        total_statements,
        total_missed,
        main_percent,
        crypto_percent,
        COVERAGE_REFERENCE_LOG.as_posix(),
    )


def collect_repo_facts(root: Path) -> RepoFacts:
    ci_text = read_text(root / ".github/workflows/ci.yml")
    model_count, docstring_count = extract_model_facts(root / "agent/model_registry.py")
    (
        coverage_reference_percent,
        coverage_reference_statements,
        coverage_reference_missed,
        main_py_start_coverage,
        crypto_utils_start_coverage,
        coverage_reference_path,
    ) = extract_reference_coverage_metrics(root)
    (
        coverage_total_percent,
        coverage_total_statements,
        coverage_total_missed,
        main_py_coverage,
        crypto_utils_coverage,
        coverage_evidence_path,
    ) = extract_preferred_coverage_metrics(root)
    return RepoFacts(
        model_count=model_count,
        model_docstring_count=docstring_count,
        ci_python_versions=extract_ci_python_versions(root / ".github/workflows/ci.yml"),
        ci_has_pytest_gate="pytest tests/ -q" in ci_text,
        ci_has_ruff_gate="ruff check ." in ci_text,
        ci_has_coverage_gate="coverage erase && coverage run -m pytest tests/ && coverage report" in ci_text,
        ci_has_active_bandit_gate=(
            "Bandit active-path gate" in ci_text and "bandit -ll -ii -c bandit.yaml" in ci_text
        ),
        bandit_skips=extract_bandit_skips(root / "bandit.yaml"),
        phase_tags=get_phase_tags(root),
        pytest_pass_count=extract_preferred_pytest_pass_count(root),
        baseline_tag="phase-2-complete",
        adr_files=tuple(path.name for path in ADR_FILES),
        coverage_fail_under=extract_fail_under(root / ".coveragerc"),
        coverage_reference_percent=coverage_reference_percent,
        coverage_reference_statements=coverage_reference_statements,
        coverage_reference_missed=coverage_reference_missed,
        main_py_start_coverage=main_py_start_coverage,
        crypto_utils_start_coverage=crypto_utils_start_coverage,
        coverage_reference_path=coverage_reference_path,
        coverage_total_percent=coverage_total_percent,
        coverage_total_statements=coverage_total_statements,
        coverage_total_missed=coverage_total_missed,
        main_py_coverage=main_py_coverage,
        crypto_utils_coverage=crypto_utils_coverage,
        coverage_evidence_path=coverage_evidence_path,
    )


def any_missing(paths: Iterable[Path], root: Path) -> list[str]:
    missing = []
    for relative_path in paths:
        if not (root / relative_path).exists():
            missing.append(relative_path.as_posix())
    return missing


def text_mentions_model_count(text: str, count: int) -> bool:
    pattern = re.compile(
        rf'\b{count}\b\s+(?:cloud\s+(?:llm\s+)?)?models?\b|\b{count}\b\s+entries\b',
        flags=re.IGNORECASE,
    )
    return bool(pattern.search(text))


def baseline_marker(tag: str) -> str:
    return f"Documentation baseline: {tag}"


def text_mentions_pass_count(text: str, count: int | None) -> bool:
    if count is None:
        return False
    ratio_pattern = re.compile(
        rf'Active Suite Status:\s*\*\*PASSING\*\*\s*\({count}\s*/\s*{count}\s+tests\)',
        flags=re.IGNORECASE,
    )
    passed_pattern = re.compile(rf'\b{count}\b\s+passed\b', flags=re.IGNORECASE)
    return bool(ratio_pattern.search(text) or passed_pattern.search(text))


def text_mentions_storage_policy(text: str) -> bool:
    sqlite_match = re.search(r'SQLite[^\n.]*?(local|development|dev|test)', text, flags=re.IGNORECASE)
    postgres_match = re.search(r'Postgres[^\n.]*?(production|target)', text, flags=re.IGNORECASE)
    return bool(sqlite_match and postgres_match)


def read_existing(root: Path, relative_path: Path) -> str:
    path = root / relative_path
    if not path.exists():
        return ""
    return read_text(path)


def run_checks(root: Path) -> tuple[RepoFacts, list[CheckResult]]:
    facts = collect_repo_facts(root)
    results: list[CheckResult] = []

    missing_docs = any_missing(REQUIRED_VERSIONED_DOCS, root)
    results.append(
        CheckResult(
            code="DOC001",
            ok=not missing_docs,
            message=(
                "All required versioned documentation files exist."
                if not missing_docs
                else f"Missing required versioned documentation files: {', '.join(missing_docs)}"
            ),
        )
    )

    missing_tags = [tag for tag in REQUIRED_PHASE_TAGS if tag not in facts.phase_tags]
    results.append(
        CheckResult(
            code="DOC002",
            ok=not missing_tags,
            message=(
                "Phase completion tags phase-0/1/2 are present."
                if not missing_tags
                else f"Missing required phase tags: {', '.join(missing_tags)}"
            ),
        )
    )

    model_count_ok = facts.model_docstring_count == facts.model_count
    results.append(
        CheckResult(
            code="DOC003",
            ok=model_count_ok,
            message=(
                f"Model registry docstring and MODELS dict both report {facts.model_count} models."
                if model_count_ok
                else (
                    "Model registry docstring count does not match MODELS dict: "
                    f"docstring={facts.model_docstring_count}, dict={facts.model_count}"
                )
            ),
        )
    )

    bad_model_docs: list[str] = []
    stale_model_docs: list[str] = []
    for relative_path in MODEL_COUNT_DOCS:
        text = read_existing(root, relative_path)
        if not text:
            continue
        if not text_mentions_model_count(text, facts.model_count):
            bad_model_docs.append(relative_path.as_posix())
        if relative_path in STALE_MODEL_COUNT_DOCS and re.search(r'\b24\s+models\b', text, flags=re.IGNORECASE):
            stale_model_docs.append(relative_path.as_posix())
    model_docs_ok = not bad_model_docs and not stale_model_docs
    pieces: list[str] = []
    if bad_model_docs:
        pieces.append("missing 27-model claim in: " + ", ".join(bad_model_docs))
    if stale_model_docs:
        pieces.append("stale 24-model claim in: " + ", ".join(stale_model_docs))
    results.append(
        CheckResult(
            code="DOC004",
            ok=model_docs_ok,
            message=(
                "Model-count claims are synchronized across docs and config references."
                if model_docs_ok
                else "; ".join(pieces)
            ),
        )
    )

    required_baseline = baseline_marker(facts.baseline_tag)
    missing_baselines = []
    for relative_path in BASELINE_MARKER_DOCS:
        text = read_existing(root, relative_path)
        if required_baseline not in text:
            missing_baselines.append(relative_path.as_posix())
    results.append(
        CheckResult(
            code="DOC005",
            ok=not missing_baselines,
            message=(
                f"All primary docs include the baseline marker '{required_baseline}'."
                if not missing_baselines
                else (
                    f"Missing baseline marker '{required_baseline}' in: "
                    + ", ".join(missing_baselines)
                )
            ),
        )
    )

    support_matrix = read_existing(root, Path("tests/SUPPORT_MATRIX.md"))
    support_matrix_problems: list[str] = []
    if not text_mentions_pass_count(support_matrix, facts.pytest_pass_count):
        support_matrix_problems.append(
            f"support matrix pass count does not match the preferred pytest snapshot ({facts.pytest_pass_count})"
        )
    for version in facts.ci_python_versions:
        if f"Python {version}" not in support_matrix:
            support_matrix_problems.append(f"support matrix is missing CI Python version {version}")
    if not facts.ci_has_pytest_gate or "pytest tests/ -q" not in support_matrix:
        support_matrix_problems.append("support matrix is missing the release-gate pytest command 'pytest tests/ -q'")
    if not facts.ci_has_ruff_gate or "ruff check ." not in support_matrix:
        support_matrix_problems.append("support matrix is missing the release-gate ruff command 'ruff check .'" )
    if facts.ci_has_coverage_gate and facts.coverage_fail_under is not None:
        if "docs/testing/coverage.md" not in support_matrix:
            support_matrix_problems.append("support matrix is missing the Phase 3.6 coverage policy document reference")
        if f"fail_under = {facts.coverage_fail_under}" not in support_matrix:
            support_matrix_problems.append(
                f"support matrix is missing the recorded coverage floor 'fail_under = {facts.coverage_fail_under}'"
            )
        if facts.coverage_total_percent is not None and f"{facts.coverage_total_percent:.2f}%" not in support_matrix:
            support_matrix_problems.append(
                f"support matrix is missing the current coverage total '{facts.coverage_total_percent:.2f}%'"
            )
    for lane_name in ("release-gate", "full-agent-bandit-audit", "legacy-audit"):
        if lane_name not in support_matrix:
            support_matrix_problems.append(f"support matrix is missing CI lane name '{lane_name}'")
    results.append(
        CheckResult(
            code="DOC006",
            ok=not support_matrix_problems,
            message=(
                "Support matrix is synchronized with CI and baseline evidence."
                if not support_matrix_problems
                else "; ".join(support_matrix_problems)
            ),
        )
    )

    legacy_path_problems = []
    for relative_path in LEGACY_PATH_DOCS:
        text = read_existing(root, relative_path)
        if "agent/legacy/" in text:
            legacy_path_problems.append(relative_path.as_posix())
    results.append(
        CheckResult(
            code="DOC007",
            ok=not legacy_path_problems,
            message=(
                "Agent guidance docs use the current '_legacy' path conventions."
                if not legacy_path_problems
                else "Stale 'agent/legacy/' path found in: " + ", ".join(legacy_path_problems)
            ),
        )
    )

    adr_index_text = read_existing(root, Path("docs/adr/README.md"))
    adr_index_missing = []
    for relative_path in ADR_FILES:
        if relative_path.name not in adr_index_text:
            adr_index_missing.append(relative_path.name)
    results.append(
        CheckResult(
            code="DOC008",
            ok=bool(adr_index_text) and not adr_index_missing,
            message=(
                "ADR index exists and references ADR-001/002/003."
                if adr_index_text and not adr_index_missing
                else (
                    "ADR index is missing or incomplete: "
                    + (", ".join(adr_index_missing) if adr_index_missing else "docs/adr/README.md missing")
                )
            ),
        )
    )

    dangerous_skips = sorted(facts.bandit_skips & CRITICAL_BANDIT_SKIPS)
    results.append(
        CheckResult(
            code="DOC009",
            ok=not dangerous_skips,
            message=(
                "Bandit policy keeps B602/B102/B307/B608 enabled globally."
                if not dangerous_skips
                else "bandit.yaml globally skips critical rules: " + ", ".join(dangerous_skips)
            ),
        )
    )

    readme_text = read_existing(root, Path("README.md"))
    results.append(
        CheckResult(
            code="DOC010",
            ok=text_mentions_storage_policy(readme_text),
            message=(
                "README documents the SQLite-local / Postgres-production storage policy."
                if text_mentions_storage_policy(readme_text)
                else "README storage wording is not aligned with ADR-003 (SQLite local/test, Postgres production target)."
            ),
        )
    )

    coverage_policy_text = read_existing(root, COVERAGE_POLICY_DOC)
    coverage_policy_problems: list[str] = []
    coverage_policy_lower = coverage_policy_text.casefold()
    if facts.coverage_fail_under is None or f"fail_under = {facts.coverage_fail_under}" not in coverage_policy_text:
        coverage_policy_problems.append("coverage policy is missing the recorded fail_under floor")
    if "baseline guard" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy does not describe the floor as a baseline guard")
    if "not a quality target" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy does not state that the floor is not a quality target")
    if (
        facts.coverage_reference_percent is None
        or f"{facts.coverage_reference_percent:.2f}%" not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the Phase 3.6 threshold-reference coverage percentage")
    if (
        facts.coverage_reference_statements is None
        or str(facts.coverage_reference_statements) not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the Phase 3.6 threshold-reference statement count")
    if (
        facts.coverage_reference_missed is None
        or str(facts.coverage_reference_missed) not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the Phase 3.6 threshold-reference missed-statement count")
    if facts.coverage_total_percent is None or f"{facts.coverage_total_percent:.2f}%" not in coverage_policy_text:
        coverage_policy_problems.append("coverage policy is missing the latest total coverage percentage")
    if facts.coverage_total_statements is None or str(facts.coverage_total_statements) not in coverage_policy_text:
        coverage_policy_problems.append("coverage policy is missing the latest total statement count")
    if facts.coverage_total_missed is None or str(facts.coverage_total_missed) not in coverage_policy_text:
        coverage_policy_problems.append("coverage policy is missing the latest missed-statement count")
    if (
        facts.main_py_start_coverage is None
        or "main.py" not in coverage_policy_text
        or f"{facts.main_py_start_coverage:.2f}%" not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the recorded starting coverage for main.py")
    if (
        facts.main_py_coverage is None
        or "main.py" not in coverage_policy_text
        or f"{facts.main_py_coverage:.2f}%" not in coverage_policy_text
        or ">= 30%" not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the Phase 3.7 Priority 1 result for main.py")
    if (
        facts.crypto_utils_start_coverage is None
        or "agent/crypto_utils.py" not in coverage_policy_text
        or f"{facts.crypto_utils_start_coverage:.2f}%" not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the recorded starting coverage for agent/crypto_utils.py")
    if (
        facts.crypto_utils_coverage is None
        or "agent/crypto_utils.py" not in coverage_policy_text
        or f"{facts.crypto_utils_coverage:.2f}%" not in coverage_policy_text
        or ">= 50%" not in coverage_policy_text
    ):
        coverage_policy_problems.append("coverage policy is missing the Phase 3.7 Priority 1 result for agent/crypto_utils.py")
    for module_name in ("agent/ollama_client.py", "agent/streaming.py", "agent/knowledge_graph.py"):
        if module_name not in coverage_policy_text:
            coverage_policy_problems.append(f"coverage policy is missing Priority 2 module '{module_name}'")
    if "module-level ratchet" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy must describe the module-level ratchet as the quality mechanism")
    if "global average alone" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy must state that the global average alone does not define quality")
    if "no artificial tests" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy must forbid artificial tests")
    if "active runtime code" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy must forbid excluding active runtime code to raise coverage")
    if "per-file hard threshold" not in coverage_policy_lower:
        coverage_policy_problems.append("coverage policy must state that no per-file hard threshold exists yet")

    results.append(
        CheckResult(
            code="DOC011",
            ok=not coverage_policy_problems,
            message=(
                "Coverage policy is synchronized with the Phase 3.6 floor, Phase 3.7 results, and ratchet priorities."
                if not coverage_policy_problems
                else "; ".join(coverage_policy_problems)
            ),
        )
    )

    return facts, results


def render_report(root: Path, facts: RepoFacts, results: Sequence[CheckResult]) -> str:
    failures = [result for result in results if not result.ok]
    lines: list[str] = []
    lines.append("# Documentation Consistency Report")
    lines.append("")
    lines.append(f"Repository root: `{root}`")
    lines.append(f"Status: {'PASS' if not failures else 'FAIL'}")
    lines.append("")
    lines.append("## Repository Facts")
    lines.append("")
    lines.append(f"- Model registry count: `{facts.model_count}`")
    lines.append(f"- Model registry docstring count: `{facts.model_docstring_count}`")
    lines.append(f"- CI Python matrix: `{', '.join(facts.ci_python_versions) or 'missing'}`")
    lines.append(f"- Baseline tag: `{facts.baseline_tag}`")
    lines.append(f"- Available phase tags: `{', '.join(facts.phase_tags) or 'none'}`")
    lines.append(f"- Snapshot pytest pass count: `{facts.pytest_pass_count}`")
    lines.append(f"- Coverage fail_under: `{facts.coverage_fail_under}`")
    lines.append(f"- Coverage reference total: `{facts.coverage_reference_percent}`")
    lines.append(f"- Coverage reference statements: `{facts.coverage_reference_statements}`")
    lines.append(f"- Coverage reference missed: `{facts.coverage_reference_missed}`")
    lines.append(f"- main.py starting coverage: `{facts.main_py_start_coverage}`")
    lines.append(f"- agent/crypto_utils.py starting coverage: `{facts.crypto_utils_start_coverage}`")
    lines.append(f"- Coverage reference source: `{facts.coverage_reference_path}`")
    lines.append(f"- Coverage total: `{facts.coverage_total_percent}`")
    lines.append(f"- Coverage statements: `{facts.coverage_total_statements}`")
    lines.append(f"- Coverage missed: `{facts.coverage_total_missed}`")
    lines.append(f"- main.py coverage: `{facts.main_py_coverage}`")
    lines.append(f"- agent/crypto_utils.py coverage: `{facts.crypto_utils_coverage}`")
    lines.append(f"- Coverage evidence source: `{facts.coverage_evidence_path}`")
    lines.append(f"- CI has pytest gate: `{facts.ci_has_pytest_gate}`")
    lines.append(f"- CI has ruff gate: `{facts.ci_has_ruff_gate}`")
    lines.append(f"- CI has coverage gate: `{facts.ci_has_coverage_gate}`")
    lines.append(f"- CI has active Bandit gate: `{facts.ci_has_active_bandit_gate}`")
    lines.append(f"- bandit.yaml skips: `{', '.join(sorted(facts.bandit_skips)) or '[]'}`")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for result in results:
        prefix = "PASS" if result.ok else "FAIL"
        lines.append(f"- [{prefix}] `{result.code}` {result.message}")
    lines.append("")
    if failures:
        lines.append(f"Failures: `{len(failures)}`")
    else:
        lines.append("Failures: `0`")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check documentation consistency against repository facts.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root to inspect.")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit with status 0 after writing the report.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for writing the report. Defaults to stdout when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    facts, results = run_checks(root)
    report = render_report(root, facts, results)
    if args.output:
        output_path = args.output if args.output.is_absolute() else root / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    has_failures = any(not result.ok for result in results)
    return 0 if args.report_only or not has_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
