from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.mutation.mutation_targets import (
    DEFAULT_BASELINE_TARGETS,
    DEFAULT_SMOKE_TARGETS,
    MutationTarget,
    get_targets,
    unique_test_commands,
)
from tools.mutation.parse_mutation_results import (
    MutationRecord,
    OUTCOME_INCOMPETENT,
    OUTCOME_KILLED,
    OUTCOME_SURVIVED,
    OUTCOME_TIMEOUT,
    build_summary,
    ensure_text_is_safe,
    write_summary_outputs,
)

SNAPSHOT_DIR = PROJECT_ROOT / "snapshot-phase-3-5"
DEFAULT_TOOL = "local-ast-harness"
DEFAULT_TIMEOUT_SECONDS = 45.0
BANNED_MUTATION_PREFIXES = (
    ".git",
    "docs",
    "tests",
)
BANNED_MUTATION_CONTAINS = (
    "snapshot-",
    ".venv",
    "__pycache__",
)
COMPARE_MUTATIONS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
COMPARE_SYMBOLS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
}
SAFE_ENV = {
    "SECRET_KEY": "test-secret-key-with-minimum-32-characters",
    "AUTH_ENFORCE": "true",
    "API_HOST": "127.0.0.1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@dataclass(frozen=True)
class MutationCandidate:
    mutant_id: str
    target_name: str
    module_path: str
    line: int
    col: int
    mutation_type: str
    original: str
    mutated: str


class MutationCandidateCollector(ast.NodeVisitor):
    def __init__(self, *, target_name: str, module_path: str):
        self._target_name = target_name
        self._module_path = module_path
        self._candidates: list[MutationCandidate] = []

    @property
    def candidates(self) -> list[MutationCandidate]:
        return list(self._candidates)

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1:
            op = node.ops[0]
            replacement = COMPARE_MUTATIONS.get(type(op))
            if replacement is not None:
                original_symbol = COMPARE_SYMBOLS[type(op)]
                mutated_symbol = COMPARE_SYMBOLS[replacement]
                mutant_id = (
                    f"{self._target_name}:{node.lineno}:{node.col_offset}:compare:{original_symbol}:{mutated_symbol}"
                )
                self._candidates.append(
                    MutationCandidate(
                        mutant_id=mutant_id,
                        target_name=self._target_name,
                        module_path=self._module_path,
                        line=node.lineno,
                        col=node.col_offset,
                        mutation_type="compare",
                        original=original_symbol,
                        mutated=mutated_symbol,
                    )
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            original = "True" if node.value else "False"
            mutated = "False" if node.value else "True"
            mutant_id = f"{self._target_name}:{node.lineno}:{node.col_offset}:bool:{original}:{mutated}"
            self._candidates.append(
                MutationCandidate(
                    mutant_id=mutant_id,
                    target_name=self._target_name,
                    module_path=self._module_path,
                    line=node.lineno,
                    col=node.col_offset,
                    mutation_type="boolean",
                    original=original,
                    mutated=mutated,
                )
            )
        self.generic_visit(node)


class MutationApplier(ast.NodeTransformer):
    def __init__(self, candidate: MutationCandidate):
        self.candidate = candidate
        self.applied = False

    def _matches(self, node: ast.AST, *, mutation_type: str) -> bool:
        return (
            getattr(node, "lineno", None) == self.candidate.line
            and getattr(node, "col_offset", None) == self.candidate.col
            and self.candidate.mutation_type == mutation_type
        )

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self._matches(node, mutation_type="compare") and len(node.ops) == 1:
            op = node.ops[0]
            replacement = COMPARE_MUTATIONS.get(type(op))
            if replacement is not None:
                node.ops[0] = replacement()
                self.applied = True
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if self._matches(node, mutation_type="boolean") and isinstance(node.value, bool):
            self.applied = True
            return ast.copy_location(ast.Constant(value=not node.value), node)
        return node


def build_command(mode: str = "smoke", *, targets: Sequence[str] | None = None) -> list[str]:
    command = ["python", "tools/mutation/run_mutation_baseline.py"]
    if mode == "baseline":
        command.append("--baseline")
    else:
        command.append("--smoke")
    if targets:
        command.append("--targets")
        command.extend(targets)
    return command


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 3.5 local mutation testing baseline.")
    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument("--smoke", action="store_true", help="Run the short smoke mutation baseline.")
    mode_group.add_argument("--baseline", action="store_true", help="Run the focused mutation baseline.")
    parser.add_argument("--targets", nargs="+", help="Optional target names override.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow running with a dirty working tree.")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-mutant test timeout.")
    parser.add_argument("--max-mutants-per-target", type=int, help="Optional override for the per-target mutation cap.")
    parser.add_argument("--raw-log", type=Path, help="Optional raw log output path.")
    parser.add_argument("--summary-json", type=Path, help="Optional summary JSON output path.")
    parser.add_argument("--summary-md", type=Path, help="Optional summary Markdown output path.")
    return parser.parse_args(argv)


def resolve_mode(args: argparse.Namespace) -> str:
    return "baseline" if args.baseline else "smoke"


def ensure_clean_worktree(*, allow_dirty: bool = False) -> None:
    completed = subprocess.run(
        ["git", "status", "--short"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to determine working tree state")
    if completed.stdout.strip() and not allow_dirty:
        raise RuntimeError("Mutation harness refuses to run on a dirty working tree unless --allow-dirty is set")


def ensure_safe_target(target: MutationTarget) -> None:
    relative = Path(target.module_path)
    parts = relative.parts
    if any(part in BANNED_MUTATION_PREFIXES for part in parts[:1]):
        raise ValueError(f"Mutation target '{target.module_path}' is outside the allowed source scope")
    if any(part in BANNED_MUTATION_PREFIXES for part in parts):
        raise ValueError(f"Mutation target '{target.module_path}' is inside a banned tree")
    if any(token in target.module_path for token in BANNED_MUTATION_CONTAINS):
        raise ValueError(f"Mutation target '{target.module_path}' matches a banned path pattern")
    if not target.source_path.exists():
        raise FileNotFoundError(f"Mutation target source file does not exist: {target.source_path}")


def collect_candidates(target: MutationTarget) -> list[MutationCandidate]:
    ensure_safe_target(target)
    source = target.source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(target.source_path))
    collector = MutationCandidateCollector(target_name=target.name, module_path=target.module_path)
    collector.visit(tree)
    return sorted(
        collector.candidates,
        key=lambda item: (item.line, item.col, item.mutation_type, item.original, item.mutated),
    )


def mutate_source(source_text: str, *, candidate: MutationCandidate, filename: str) -> str:
    tree = ast.parse(source_text, filename=filename)
    applier = MutationApplier(candidate)
    mutated_tree = applier.visit(tree)
    ast.fix_missing_locations(mutated_tree)
    if not applier.applied:
        raise ValueError(f"Mutation candidate was not applied: {candidate.mutant_id}")
    mutated_source = ast.unparse(mutated_tree) + "\n"
    compile(mutated_source, filename, "exec")
    return mutated_source


@contextmanager
def isolated_overlay(relative_path: str, content: str) -> Iterator[Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="omni-mutation-"))
    try:
        overlay_file = temp_root / relative_path
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_text(content, encoding="utf-8")
        yield temp_root
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _build_env(overlay_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(SAFE_ENV)
    python_path_parts = [str(overlay_root), str(PROJECT_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        python_path_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
    return env


def _run_target_tests(target: MutationTarget, *, overlay_root: Path, timeout_seconds: float) -> tuple[str, float, str]:
    started = time.perf_counter()
    command = target.pytest_command(sys.executable)
    safe_command = " ".join(target.pytest_command("python"))
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_build_env(overlay_root),
        )
    except subprocess.TimeoutExpired:
        return OUTCOME_TIMEOUT, round(time.perf_counter() - started, 3), safe_command

    duration = round(time.perf_counter() - started, 3)
    if completed.returncode == 0:
        return OUTCOME_SURVIVED, duration, safe_command
    return OUTCOME_KILLED, duration, safe_command


def execute_mutation(target: MutationTarget, candidate: MutationCandidate, *, timeout_seconds: float) -> MutationRecord:
    source_text = target.source_path.read_text(encoding="utf-8")
    detail = ""
    try:
        mutated_source = mutate_source(source_text, candidate=candidate, filename=str(target.source_path))
    except Exception as exc:
        return MutationRecord(
            mutant_id=candidate.mutant_id,
            target=target.name,
            module_path=target.module_path,
            outcome=OUTCOME_INCOMPETENT,
            line=candidate.line,
            mutation_type=candidate.mutation_type,
            original=candidate.original,
            mutated=candidate.mutated,
            duration_seconds=0.0,
            test_command=" ".join(target.pytest_command("python")),
            detail=f"mutation build failed: {type(exc).__name__}",
        )

    with isolated_overlay(target.module_path, mutated_source) as overlay_root:
        outcome, duration, safe_command = _run_target_tests(
            target,
            overlay_root=overlay_root,
            timeout_seconds=timeout_seconds,
        )
        if outcome == OUTCOME_TIMEOUT:
            detail = f"timed out after {timeout_seconds} seconds"
        elif outcome == OUTCOME_SURVIVED:
            detail = "tests passed against the mutant"
        else:
            detail = "tests failed against the mutant"

    return MutationRecord(
        mutant_id=candidate.mutant_id,
        target=target.name,
        module_path=target.module_path,
        outcome=outcome,
        line=candidate.line,
        mutation_type=candidate.mutation_type,
        original=candidate.original,
        mutated=candidate.mutated,
        duration_seconds=duration,
        test_command=safe_command,
        detail=detail,
    )


def write_raw_log(*, records: Iterable[MutationRecord], raw_log_path: Path, mode: str, tool: str) -> None:
    raw_log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# mode={mode}",
        f"# tool={tool}",
    ]
    for record in records:
        payload = record.to_dict()
        text = json.dumps(payload, sort_keys=True)
        ensure_text_is_safe(text, label="Mutation raw log entry")
        lines.append(text)
    raw_text = "\n".join(lines) + "\n"
    ensure_text_is_safe(raw_text, label="Mutation raw log")
    raw_log_path.write_text(raw_text, encoding="utf-8")


def selected_targets_for_mode(mode: str, override_names: Sequence[str] | None = None) -> list[MutationTarget]:
    if override_names:
        return get_targets(override_names)
    if mode == "baseline":
        return get_targets(DEFAULT_BASELINE_TARGETS)
    return get_targets(DEFAULT_SMOKE_TARGETS)


def select_candidates_for_target(
    target: MutationTarget,
    *,
    mode: str,
    max_mutants_per_target: int | None,
) -> list[MutationCandidate]:
    candidates = collect_candidates(target)
    limit = max_mutants_per_target
    if limit is None:
        limit = target.baseline_mutant_limit if mode == "baseline" else target.smoke_mutant_limit
    return candidates[: max(0, limit)]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    mode = resolve_mode(args)
    ensure_clean_worktree(allow_dirty=args.allow_dirty)

    targets = selected_targets_for_mode(mode, args.targets)
    if not targets:
        raise RuntimeError("No mutation targets selected")

    raw_log_path = args.raw_log or SNAPSHOT_DIR / f"mutation_{mode}_raw.log"
    summary_json_path = args.summary_json or SNAPSHOT_DIR / f"mutation_{mode}_summary.json"
    summary_md_path = args.summary_md or SNAPSHOT_DIR / f"mutation_{mode}_summary.md"

    limits_note = (
        "Focused baseline is intentionally capped per target to keep Phase 3.5 local and reproducible."
        if mode == "baseline"
        else "Smoke mode uses only a small subset of targets and mutants."
    )

    all_records: list[MutationRecord] = []
    started = time.perf_counter()
    for target in targets:
        selected = select_candidates_for_target(
            target,
            mode=mode,
            max_mutants_per_target=args.max_mutants_per_target,
        )
        for candidate in selected:
            all_records.append(
                execute_mutation(target, candidate, timeout_seconds=args.timeout_seconds)
            )

    if not all_records:
        raise RuntimeError("No mutation candidates were generated for the selected targets")

    runtime_seconds = round(time.perf_counter() - started, 3)
    test_command = " ; ".join(unique_test_commands(targets, python_executable="python"))
    summary = build_summary(
        all_records,
        mode=mode,
        tool=DEFAULT_TOOL,
        target_modules=[target.module_path for target in targets],
        test_command=test_command,
        runtime_seconds=runtime_seconds,
        limits_note=limits_note,
    )

    write_raw_log(records=all_records, raw_log_path=raw_log_path, mode=mode, tool=DEFAULT_TOOL)
    write_summary_outputs(summary, json_path=summary_json_path, markdown_path=summary_md_path)

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote raw log to {raw_log_path}")
    print(f"Wrote summary JSON to {summary_json_path}")
    print(f"Wrote summary Markdown to {summary_md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
