from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.performance.reporting import validate_summary_payload
from tools.performance.run_local_baseline import build_command, build_config, is_loopback_target, parse_args


def test_performance_harness_modules_import() -> None:
    import tools.performance.local_fixture as local_fixture
    import tools.performance.run_local_baseline as run_local_baseline

    assert local_fixture.start_local_fixture is not None
    assert run_local_baseline.run_harness is not None


def test_workload_source_does_not_embed_sensitive_auth_material() -> None:
    source = (PROJECT_ROOT / "tools" / "performance" / "run_local_baseline.py").read_text(encoding="utf-8")

    forbidden_markers = (
        "Authorization",
        "Bearer ",
        "sk-",
        "ghp_",
        "xoxb-",
    )
    for marker in forbidden_markers:
        assert marker not in source


def test_default_target_is_loopback_local_only() -> None:
    config = build_config(parse_args(["--smoke"]))

    assert config.base_url is None
    assert is_loopback_target("http://127.0.0.1:8000") is True
    assert is_loopback_target("http://localhost:8000") is True
    assert is_loopback_target("http://example.com:8000") is False


def test_report_parser_handles_sample_output() -> None:
    sample = {
        "timestamp": "2026-05-19T00:00:00+00:00",
        "mode": "smoke",
        "target": "http://127.0.0.1:8000",
        "duration_seconds": 6.0,
        "request_count": 10,
        "failure_count": 0,
        "error_rate": 0.0,
        "p50_ms": 2.0,
        "p95_ms": 4.0,
        "p99_ms": 4.5,
        "max_ms": 5.0,
        "routes": [
            {
                "route": "/status",
                "request_count": 5,
                "failure_count": 0,
                "error_rate": 0.0,
                "p50_ms": 1.0,
                "p95_ms": 2.0,
                "p99_ms": 2.0,
                "max_ms": 2.0,
            }
        ],
    }

    parsed = validate_summary_payload(sample)

    assert parsed["request_count"] == 10
    assert parsed["p99_ms"] == 4.5


def test_harness_command_can_be_generated_without_heavy_load() -> None:
    command = build_command("smoke")

    assert command[:2] == ["python", "tools/performance/run_local_baseline.py"]
    assert "--smoke" in command
    assert "http://127.0.0.1:8000" in command
    assert "6" in command
    assert "2" in command
