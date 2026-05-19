from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.performance.reporting import contains_sensitive_content, summary_to_markdown, validate_summary_payload
from tools.performance.run_local_baseline import build_command


def _sample_summary() -> dict[str, object]:
    return {
        "timestamp": "2026-05-19T00:00:00+00:00",
        "mode": "smoke",
        "target": "http://127.0.0.1:8000",
        "duration_seconds": 6.0,
        "request_count": 20,
        "failure_count": 0,
        "error_rate": 0.0,
        "p50_ms": 2.1,
        "p95_ms": 3.2,
        "p99_ms": 3.8,
        "max_ms": 4.0,
        "routes": [
            {
                "route": "/chat",
                "request_count": 10,
                "failure_count": 0,
                "error_rate": 0.0,
                "p50_ms": 2.2,
                "p95_ms": 3.3,
                "p99_ms": 3.7,
                "max_ms": 4.0,
            },
            {
                "route": "/status",
                "request_count": 10,
                "failure_count": 0,
                "error_rate": 0.0,
                "p50_ms": 2.0,
                "p95_ms": 3.1,
                "p99_ms": 3.8,
                "max_ms": 3.9,
            },
        ],
    }


def test_summary_json_schema_is_valid() -> None:
    parsed = validate_summary_payload(_sample_summary())

    assert parsed["request_count"] == 20
    assert parsed["target"] == "http://127.0.0.1:8000"


def test_summary_parser_rejects_missing_metrics() -> None:
    broken = _sample_summary()
    broken.pop("p99_ms")

    try:
        validate_summary_payload(broken)
    except ValueError as exc:
        assert "p99_ms" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("Expected validate_summary_payload to reject missing metrics")


def test_smoke_report_omits_sensitive_terms() -> None:
    summary = _sample_summary()
    json_text = json.dumps(summary, indent=2, sort_keys=True)
    markdown_text = summary_to_markdown(summary)

    assert contains_sensitive_content(json_text) is False
    assert contains_sensitive_content(markdown_text) is False


def test_smoke_command_uses_loopback_target_by_default() -> None:
    command = build_command("smoke")

    assert "http://127.0.0.1:8000" in command
    assert "--smoke" in command
