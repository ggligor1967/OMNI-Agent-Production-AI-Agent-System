from __future__ import annotations

import configparser
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_CONFIG = ROOT / ".coveragerc"
BASELINE_ANALYSIS = ROOT / "snapshot-phase-3-6" / "COVERAGE_BASELINE_ANALYSIS.md"
BASELINE_JSONS = tuple(
    ROOT / "snapshot-phase-3-6" / f"coverage_baseline_run_{index}.json"
    for index in (1, 2, 3)
)


def _read_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    loaded = parser.read(COVERAGE_CONFIG, encoding="utf-8")
    assert loaded == [str(COVERAGE_CONFIG)]
    return parser


def _split_multiline(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _analysis_threshold() -> int:
    text = BASELINE_ANALYSIS.read_text(encoding="utf-8")
    match = re.search(r"## Proposed Threshold\s+`(\d+)`", text, flags=re.MULTILINE)
    assert match is not None
    return int(match.group(1))


def _baseline_min_coverage() -> float:
    totals: list[float] = []
    for path in BASELINE_JSONS:
        payload = json.loads(path.read_text(encoding="utf-8"))
        totals.append(float(payload["totals"]["percent_covered"]))
    return min(totals)


def test_coverage_config_exists() -> None:
    assert COVERAGE_CONFIG.exists()


def test_fail_under_is_numeric_and_matches_documented_threshold() -> None:
    parser = _read_config()

    fail_under = parser["report"].get("fail_under", "")
    assert fail_under.isdigit()
    assert int(fail_under) == _analysis_threshold()


def test_legacy_and_tests_are_omitted_and_runtime_scope_is_included() -> None:
    parser = _read_config()

    include = _split_multiline(parser["run"].get("include", ""))
    omit = _split_multiline(parser["run"].get("omit", ""))

    assert "agent/*" in include
    assert "main.py" in include
    assert "config.py" in include
    assert "agent/_legacy/*" in omit
    assert "tests/*" in omit
    assert "snapshot-*" in omit
    assert "tools/mutation/*" in omit
    assert "tools/performance/*" in omit
    assert "*/site-packages/*" in omit
    assert "*/Lib/*" in omit


def test_threshold_is_not_higher_than_recorded_baseline() -> None:
    parser = _read_config()

    fail_under = int(parser["report"]["fail_under"])
    min_total = _baseline_min_coverage()
    expected = math.floor(min_total) - 1
    if expected < 50 and min_total >= 51:
        expected = 50

    assert fail_under <= min_total
    assert fail_under == expected
