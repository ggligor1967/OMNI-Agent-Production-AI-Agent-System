from __future__ import annotations

import json

import pytest

from tools.mutation.parse_mutation_results import (
    calculate_mutation_score,
    contains_sensitive_content,
    summary_to_markdown,
    validate_summary_payload,
)


def _sample_summary() -> dict[str, object]:
    return {
        "timestamp": "2026-05-19T00:00:00+00:00",
        "mode": "smoke",
        "tool": "local-ast-harness",
        "target_modules": ["agent/model_router.py", "agent/sandbox.py"],
        "total_mutants": 4,
        "killed": 3,
        "survived": 1,
        "timeout": 0,
        "incompetent": 0,
        "mutation_score": 75.0,
        "runtime_seconds": 12.5,
        "test_command": "python -m pytest tests/test_models.py tests/test_sandbox_policy.py -q",
    }


def test_summary_json_schema_is_valid() -> None:
    parsed = validate_summary_payload(_sample_summary())

    assert parsed["total_mutants"] == 4
    assert parsed["mutation_score"] == 75.0


def test_missing_required_fields_are_rejected() -> None:
    broken = _sample_summary()
    broken.pop("mutation_score")

    with pytest.raises(ValueError, match="mutation_score"):
        validate_summary_payload(broken)


def test_mutation_score_calculation_is_correct() -> None:
    assert calculate_mutation_score(total_mutants=4, killed=3, incompetent=0) == 75.0
    assert calculate_mutation_score(total_mutants=5, killed=2, incompetent=1) == 50.0
    assert calculate_mutation_score(total_mutants=1, killed=0, incompetent=1) == 0.0


def test_smoke_summary_does_not_include_sensitive_terms() -> None:
    summary = _sample_summary()
    json_text = json.dumps(summary, indent=2, sort_keys=True)
    markdown_text = summary_to_markdown(summary)

    assert contains_sensitive_content(json_text) is False
    assert contains_sensitive_content(markdown_text) is False
    assert "authorization" not in markdown_text.lower()
    assert "x-api-key" not in json_text.lower()
