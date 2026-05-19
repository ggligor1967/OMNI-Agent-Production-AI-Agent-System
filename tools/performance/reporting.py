from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REQUIRED_SUMMARY_FIELDS = (
    "timestamp",
    "mode",
    "target",
    "duration_seconds",
    "request_count",
    "failure_count",
    "error_rate",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "max_ms",
)
SENSITIVE_MARKERS = (
    "authorization",
    "bearer ",
    "x-api-key",
    "secret_key",
    "password",
    "token",
    "raw prompt",
    "raw response body",
)


@dataclass(frozen=True)
class RequestObservation:
    route: str
    method: str
    status_code: int
    latency_ms: float
    ok: bool
    error: str = ""


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_metrics(values: Iterable[float]) -> dict[str, float]:
    latencies = [float(value) for value in values]
    if not latencies:
        return {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
        }
    return {
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "p99_ms": round(percentile(latencies, 0.99), 3),
        "max_ms": round(max(latencies), 3),
    }


def build_summary(
    observations: list[RequestObservation],
    *,
    mode: str,
    target: str,
    duration_seconds: float,
    timestamp: str | None = None,
) -> dict[str, Any]:
    safe_timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    request_count = len(observations)
    failure_count = sum(1 for item in observations if not item.ok)
    grouped: dict[str, list[RequestObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.route, []).append(observation)

    summary: dict[str, Any] = {
        "timestamp": safe_timestamp,
        "mode": mode,
        "target": target,
        "duration_seconds": round(float(duration_seconds), 3),
        "request_count": request_count,
        "failure_count": failure_count,
        "error_rate": round((failure_count / request_count) if request_count else 0.0, 6),
        **_latency_metrics([item.latency_ms for item in observations]),
        "routes": [],
    }

    for route_name in sorted(grouped):
        route_items = grouped[route_name]
        route_failures = sum(1 for item in route_items if not item.ok)
        summary["routes"].append(
            {
                "route": route_name,
                "request_count": len(route_items),
                "failure_count": route_failures,
                "error_rate": round((route_failures / len(route_items)) if route_items else 0.0, 6),
                **_latency_metrics([item.latency_ms for item in route_items]),
            }
        )

    return summary


def validate_summary_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_SUMMARY_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"Missing required summary fields: {', '.join(missing)}")

    numeric_fields = (
        "duration_seconds",
        "request_count",
        "failure_count",
        "error_rate",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
    )
    validated = dict(payload)
    for field in numeric_fields:
        value = validated[field]
        if not isinstance(value, (int, float)):
            raise ValueError(f"Summary field '{field}' must be numeric")

    if validated["request_count"] < 0 or validated["failure_count"] < 0:
        raise ValueError("Summary counts must be non-negative")
    if validated["failure_count"] > validated["request_count"]:
        raise ValueError("failure_count cannot exceed request_count")
    if not isinstance(validated["target"], str) or not validated["target"]:
        raise ValueError("Summary target must be a non-empty string")
    if not isinstance(validated["mode"], str) or not validated["mode"]:
        raise ValueError("Summary mode must be a non-empty string")
    if not isinstance(validated["timestamp"], str) or not validated["timestamp"]:
        raise ValueError("Summary timestamp must be a non-empty string")
    return validated


def load_summary(path: Path) -> dict[str, Any]:
    return validate_summary_payload(json.loads(path.read_text(encoding="utf-8")))


def summary_to_markdown(summary: Mapping[str, Any]) -> str:
    validated = validate_summary_payload(summary)
    lines = [
        "# Local Performance Summary",
        "",
        f"- Timestamp: `{validated['timestamp']}`",
        f"- Mode: `{validated['mode']}`",
        f"- Target: `{validated['target']}`",
        f"- Duration: `{validated['duration_seconds']}` seconds",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Value |",
        "| ------ | -----:|",
        f"| Request count | {validated['request_count']} |",
        f"| Failure count | {validated['failure_count']} |",
        f"| Error rate | {validated['error_rate']} |",
        f"| p50 ms | {validated['p50_ms']} |",
        f"| p95 ms | {validated['p95_ms']} |",
        f"| p99 ms | {validated['p99_ms']} |",
        f"| Max ms | {validated['max_ms']} |",
    ]

    route_rows = validated.get("routes", [])
    if route_rows:
        lines.extend(
            [
                "",
                "## Route Breakdown",
                "",
                "| Route | Request count | Failure count | Error rate | p50 ms | p95 ms | p99 ms | Max ms |",
                "| ----- | ------------: | ------------: | ---------: | -----: | -----: | -----: | -----: |",
            ]
        )
        for route_summary in route_rows:
            lines.append(
                "| {route} | {request_count} | {failure_count} | {error_rate} | {p50_ms} | {p95_ms} | {p99_ms} | {max_ms} |".format(
                    **route_summary,
                )
            )
    return "\n".join(lines) + "\n"


def contains_sensitive_content(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def write_summary_outputs(
    summary: Mapping[str, Any],
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    validated = validate_summary_payload(summary)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(validated, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(summary_to_markdown(validated), encoding="utf-8")


def observations_to_dicts(observations: Iterable[RequestObservation]) -> list[dict[str, Any]]:
    return [asdict(observation) for observation in observations]
