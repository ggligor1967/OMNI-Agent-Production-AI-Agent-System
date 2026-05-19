from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MutationTarget:
    name: str
    module_path: str
    test_paths: tuple[str, ...]
    description: str
    smoke_mutant_limit: int = 1
    baseline_mutant_limit: int = 2

    @property
    def source_path(self) -> Path:
        return PROJECT_ROOT / self.module_path

    def pytest_command(self, python_executable: str = "python") -> list[str]:
        return [python_executable, "-m", "pytest", *self.test_paths, "-q"]


MUTATION_TARGETS: dict[str, MutationTarget] = {
    "model_router": MutationTarget(
        name="model_router",
        module_path="agent/model_router.py",
        test_paths=("tests/test_models.py", "tests/test_model_routing_tracing.py"),
        description="Task classification and routing on the active model-selection path.",
        smoke_mutant_limit=1,
        baseline_mutant_limit=2,
    ),
    "rag": MutationTarget(
        name="rag",
        module_path="agent/rag.py",
        test_paths=("tests/test_new_modules.py", "tests/test_sql_injection_sweep.py"),
        description="RAG storage, retrieval, and keyword-search logic.",
        smoke_mutant_limit=1,
        baseline_mutant_limit=2,
    ),
    "sandbox": MutationTarget(
        name="sandbox",
        module_path="agent/sandbox.py",
        test_paths=(
            "tests/test_sandbox_policy.py",
            "tests/test_sandbox_isolation_proofs.py",
            "tests/test_security_event_audit.py",
        ),
        description="Sandbox policy, isolation, and audit enforcement on the execute-python path.",
        smoke_mutant_limit=1,
        baseline_mutant_limit=2,
    ),
    "workflow": MutationTarget(
        name="workflow",
        module_path="agent/workflow.py",
        test_paths=("tests/test_advanced_modules.py", "tests/test_tool_registry_enforcement.py"),
        description="Workflow parser, safe transform evaluation, and tool-step enforcement.",
        smoke_mutant_limit=1,
        baseline_mutant_limit=2,
    ),
}

DEFAULT_SMOKE_TARGETS: tuple[str, ...] = ("model_router", "sandbox")
DEFAULT_BASELINE_TARGETS: tuple[str, ...] = tuple(MUTATION_TARGETS)


def get_target(name: str) -> MutationTarget:
    try:
        return MUTATION_TARGETS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown mutation target '{name}'. Available: {', '.join(sorted(MUTATION_TARGETS))}") from exc


def get_targets(names: Sequence[str] | None = None) -> list[MutationTarget]:
    selected = names or DEFAULT_BASELINE_TARGETS
    return [get_target(name) for name in selected]


def unique_test_commands(targets: Iterable[MutationTarget], python_executable: str = "python") -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for target in targets:
        command = " ".join(target.pytest_command(python_executable))
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands
