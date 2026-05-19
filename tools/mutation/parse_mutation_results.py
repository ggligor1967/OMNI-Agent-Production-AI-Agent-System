from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

OUTCOME_KILLED = "killed"
OUTCOME_SURVIVED = "survived"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_INCOMPETENT = "incompetent"
VALID_OUTCOMES = (
    OUTCOME_KILLED,
    OUTCOME_SURVIVED,
    OUTCOME_TIMEOUT,
    OUTCOME_INCOMPETENT,
)

REQUIRED_SUMMARY_FIELDS = (
    "timestamp",
    "mode",
    "tool",
    "target_modules",
    "total_mutants",
    "killed",
    "survived",
    "timeout",
    "incompetent",
    "mutation_score",
    "runtime_seconds",
    "test_command",
)

SENSITIVE_MARKERS = (
    "authorization",
    "bearer ",
    "x-api-key",
    "secret_key",
    "api_key",
    "password",
    "token",
    "raw prompt",
    "raw response body",
)


@dataclass(frozen=True)
class MutationRecord:
    mutant_id: str
    target: str
    module_path: str
    outcome: str
    line: int
    mutation_type: str
    original: str
    mutated: str
    duration_seconds: float
    test_command: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["duration_seconds"] = round(float(self.duration_seconds), 3)
        return payload


def contains_sensitive_content(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def ensure_text_is_safe(text: str, *, label: str) -> None:
    if contains_sensitive_content(text):
        raise ValueError(f"{label} contains sensitive content markers")


def calculate_mutation_score(*, total_mutants: int, killed: int, incompetent: int) -> float:
    denominator = total_mutants - incompetent
    if denominator <= 0:
        return 0.0
    return round((killed / denominator) * 100.0, 3)


def parse_raw_results_text(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        payload = json.loads(line)
        outcome = payload.get("outcome")
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Unknown mutation outcome '{outcome}'")
        records.append(payload)
    return records


def validate_summary_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_SUMMARY_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"Missing required summary fields: {', '.join(missing)}")

    validated = dict(payload)
    if validated["mode"] not in {"smoke", "baseline"}:
        raise ValueError("Mutation summary mode must be 'smoke' or 'baseline'")
    if not isinstance(validated["tool"], str) or not validated["tool"]:
        raise ValueError("Mutation summary tool must be a non-empty string")
    if not isinstance(validated["target_modules"], list) or not validated["target_modules"]:
        raise ValueError("Mutation summary target_modules must be a non-empty list")
    if not all(isinstance(item, str) and item for item in validated["target_modules"]):
        raise ValueError("Mutation summary target_modules entries must be non-empty strings")
    if not isinstance(validated["test_command"], str) or not validated["test_command"]:
        raise ValueError("Mutation summary test_command must be a non-empty string")
    if not isinstance(validated["timestamp"], str) or not validated["timestamp"]:
        raise ValueError("Mutation summary timestamp must be a non-empty string")

    numeric_int_fields = ("total_mutants", "killed", "survived", "timeout", "incompetent")
    for field in numeric_int_fields:
        if not isinstance(validated[field], int):
            raise ValueError(f"Mutation summary field '{field}' must be an integer")
        if validated[field] < 0:
            raise ValueError(f"Mutation summary field '{field}' must be non-negative")

    if not isinstance(validated["mutation_score"], (int, float)):
        raise ValueError("Mutation summary mutation_score must be numeric")
    if not isinstance(validated["runtime_seconds"], (int, float)):
        raise ValueError("Mutation summary runtime_seconds must be numeric")
    if validated["runtime_seconds"] < 0:
        raise ValueError("Mutation summary runtime_seconds must be non-negative")

    summed = validated["killed"] + validated["survived"] + validated["timeout"] + validated["incompetent"]
    if summed != validated["total_mutants"]:
        raise ValueError("Mutation summary outcome counts must add up to total_mutants")

    expected_score = calculate_mutation_score(
        total_mutants=validated["total_mutants"],
        killed=validated["killed"],
        incompetent=validated["incompetent"],
    )
    if round(float(validated["mutation_score"]), 3) != expected_score:
        raise ValueError(
            "Mutation summary mutation_score does not match the killed/total/incompetent counts"
        )

    return validated


def build_summary(
    records: Iterable[MutationRecord],
    *,
    mode: str,
    tool: str,
    target_modules: list[str],
    test_command: str,
    runtime_seconds: float,
    limits_note: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    entries = list(records)
    killed = sum(1 for item in entries if item.outcome == OUTCOME_KILLED)
    survived = sum(1 for item in entries if item.outcome == OUTCOME_SURVIVED)
    timeout = sum(1 for item in entries if item.outcome == OUTCOME_TIMEOUT)
    incompetent = sum(1 for item in entries if item.outcome == OUTCOME_INCOMPETENT)
    total_mutants = len(entries)

    summary: dict[str, Any] = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "tool": tool,
        "target_modules": list(target_modules),
        "total_mutants": total_mutants,
        "killed": killed,
        "survived": survived,
        "timeout": timeout,
        "incompetent": incompetent,
        "mutation_score": calculate_mutation_score(
            total_mutants=total_mutants,
            killed=killed,
            incompetent=incompetent,
        ),
        "runtime_seconds": round(float(runtime_seconds), 3),
        "test_command": test_command,
    }
    if limits_note:
        summary["limits_note"] = limits_note
    return validate_summary_payload(summary)


def summary_to_markdown(summary: Mapping[str, Any]) -> str:
    validated = validate_summary_payload(summary)
    lines = [
        "# Mutation Testing Summary",
        "",
        f"- Timestamp: `{validated['timestamp']}`",
        f"- Mode: `{validated['mode']}`",
        f"- Tool: `{validated['tool']}`",
        f"- Runtime: `{validated['runtime_seconds']}` seconds",
        f"- Test command: `{validated['test_command']}`",
        "",
        "## Target Modules",
        "",
    ]
    for module_path in validated["target_modules"]:
        lines.append(f"- `{module_path}`")

    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Total mutants | {validated['total_mutants']} |",
            f"| Killed | {validated['killed']} |",
            f"| Survived | {validated['survived']} |",
            f"| Timeout | {validated['timeout']} |",
            f"| Incompetent | {validated['incompetent']} |",
            f"| Mutation score | {validated['mutation_score']} |",
        ]
    )

    if validated.get("limits_note"):
        lines.extend(["", "## Limits", "", validated["limits_note"]])

    return "\n".join(lines) + "\n"


def write_summary_outputs(
    summary: Mapping[str, Any],
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    validated = validate_summary_payload(summary)
    json_text = json.dumps(validated, indent=2, sort_keys=True)
    markdown_text = summary_to_markdown(validated)
    ensure_text_is_safe(json_text, label="Mutation summary JSON")
    ensure_text_is_safe(markdown_text, label="Mutation summary Markdown")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")
