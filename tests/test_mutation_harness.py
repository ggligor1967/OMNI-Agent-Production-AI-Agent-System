from __future__ import annotations

from pathlib import Path

import pytest

from tools.mutation.mutation_targets import get_targets
from tools.mutation.parse_mutation_results import parse_raw_results_text, write_summary_outputs
from tools.mutation.run_mutation_baseline import build_command, ensure_clean_worktree, isolated_overlay


def test_target_list_loads_and_points_to_existing_files() -> None:
    targets = get_targets()

    assert targets
    assert all(target.source_path.exists() for target in targets)


def test_smoke_command_can_be_generated() -> None:
    command = build_command("smoke", targets=["model_router", "sandbox"])

    assert command[:2] == ["python", "tools/mutation/run_mutation_baseline.py"]
    assert "--smoke" in command
    assert "--targets" in command
    assert "model_router" in command
    assert "sandbox" in command


def test_parser_handles_sample_mutation_results() -> None:
    raw_text = """# mode=smoke
{"mutant_id": "m1", "outcome": "killed"}
{"mutant_id": "m2", "outcome": "survived"}
"""

    records = parse_raw_results_text(raw_text)

    assert len(records) == 2
    assert records[0]["mutant_id"] == "m1"
    assert records[1]["outcome"] == "survived"


def test_harness_refuses_dirty_tree_by_default(monkeypatch) -> None:
    class _Completed:
        returncode = 0
        stdout = " M README.md\n"

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Completed())

    with pytest.raises(RuntimeError, match="dirty working tree"):
        ensure_clean_worktree(allow_dirty=False)


def test_harness_writes_no_secrets_to_reports(tmp_path: Path) -> None:
    summary = {
        "timestamp": "2026-05-19T00:00:00+00:00",
        "mode": "smoke",
        "tool": "local-ast-harness",
        "target_modules": ["agent/sandbox.py"],
        "total_mutants": 2,
        "killed": 1,
        "survived": 1,
        "timeout": 0,
        "incompetent": 0,
        "mutation_score": 50.0,
        "runtime_seconds": 1.25,
        "test_command": "python -m pytest tests/test_sandbox_policy.py -q",
    }

    json_path = tmp_path / "summary.json"
    md_path = tmp_path / "summary.md"
    write_summary_outputs(summary, json_path=json_path, markdown_path=md_path)

    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = md_path.read_text(encoding="utf-8")

    assert "authorization" not in json_text.lower()
    assert "x-api-key" not in json_text.lower()
    assert "secret_key" not in json_text.lower()
    assert "authorization" not in markdown_text.lower()


def test_temporary_mutation_worktree_is_cleaned_up_or_isolated() -> None:
    overlay_root: Path | None = None
    overlay_file: Path | None = None

    with isolated_overlay("agent/demo_module.py", "VALUE = 1\n") as temp_root:
        overlay_root = temp_root
        overlay_file = temp_root / "agent" / "demo_module.py"
        assert overlay_file.exists()
        assert overlay_root != Path.cwd()
        assert "omni-mutation-" in overlay_root.name

    assert overlay_root is not None
    assert overlay_file is not None
    assert not overlay_root.exists()
    assert not overlay_file.exists()
